import os
import subprocess
import sys
import shutil
import time
from pathlib import Path
try:
    import vapoursynth as vs
    core = vs.core
    _VS_AVAILABLE = True
except ImportError:
    _VS_AVAILABLE = False

from .verifier import Verifier, GOOD_CODECS, PORTABLE_SUB_CODECS
from .hardware_agent import HardwareAgent

_MKVE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_MKVE_ROOT, "logs")
FAST_WORK_DIR = "/app/RawLoadrr/tmp/TEMP_RESCUE"
_LWI_CACHE_DIR = Path("/app/work_data/tmp")
_VSPIPE_BIN = shutil.which('vspipe')


class UniversalRescuer:
    def __init__(self):
        self.verifier = Verifier()
        self.agent = HardwareAgent()
        os.makedirs(FAST_WORK_DIR, exist_ok=True)
        _LWI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        os.makedirs(_LOGS_DIR, exist_ok=True)
        self.log_file = os.path.join(_LOGS_DIR, "rescue_process.log")
        self._log("🦾 Hardware Agent inicializado.")

    def _log(self, msg):
        print(msg)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    def _get_vs_script(self, source_path):
        """Genera un script VapourSynth con un nombre de archivo fijo para el pipe."""
        source_repr = repr(str(source_path))
        script = f"""import vapoursynth as vs
core = vs.core
clip = core.lsmas.LWLibavSource(source={source_repr})
clip = core.std.Transpose(clip) if clip.width < clip.height else clip
clip.set_output()
"""
        # Usamos un nombre de script fijo en el directorio de trabajo rápido.
        script_path = Path(FAST_WORK_DIR) / "script.vpy"
        script_path.write_text(script, encoding="utf-8")
        return str(script_path)

    def _run_command(self, cmd, level_name, fallback_trigger=None):
        """Ejecuta un comando. Si falla y `fallback_trigger` está en stderr, devuelve un código especial."""
        try:
            if "ffmpeg" in cmd and "-hide_banner" not in cmd:
                cmd = cmd.replace("ffmpeg", "ffmpeg -hide_banner", 1)

            process = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, errors='ignore'
            )
            
            if process.returncode != 0:
                self._log(f"   ⚠️  Fallo técnico en {level_name} (Código {process.returncode}). Detalles a continuación.")
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n--- ERROR DETALLADO {level_name} ---\n")
                    f.write(f"Comando: {cmd}\n")
                    f.write(process.stderr)
                    f.write("\n--------------------------------------------\n")
                
                print(f"   ⚠️  Fallo técnico en {level_name} (Código {process.returncode}). Detalles en el log.")

                if fallback_trigger and fallback_trigger in process.stderr:
                    return "FALLBACK_TRIGGERED"
                
                return False
            return True
        except Exception as e:
            self._log(f"   💥 EXCEPCIÓN CRÍTICA EN {level_name}: {e}")
            return False

    def _run_with_sub_fallback(self, cmd_with_subs, cmd_no_subs, output_file, level_name):
        """
        Intenta el comando con subtítulos. Si falla, reintenta sin ellos.
        No tiramos un buen transcode por culpa de un subtítulo legacy o corrupto.
        """
        if os.path.exists(output_file):
            os.remove(output_file)
        result = self._run_command(cmd_with_subs, level_name)
        if result is True:
            return True
        if result is False:
            self._log(f"   ↪️ Fallo con subs. Reintentando sin subtítulos...")
            if os.path.exists(output_file):
                os.remove(output_file)
            return self._run_command(cmd_no_subs, level_name + " (sin subs)")
        return result  # FALLBACK_TRIGGERED pasa hacia arriba sin cambios

    def _text_sub_maps(self, input_file, input_idx=0):
        """Devuelve los args -map para SOLO los subtítulos de texto (convertibles a
        SRT). Los de imagen (dvd_subtitle, PGS, DVB...) se omiten a propósito: no se
        pueden pasar a SRT, revientan el transcode y se rompen en reproductores raros.
        Así conservamos los subs buenos y tiramos los tóxicos sin tumbar el encode.
        `input_idx` = índice del input del que salen los subs (0 normal, 1 en Nivel 3)."""
        meta = self.verifier._run_ffprobe(input_file)
        maps = []
        for s in (meta or {}).get('streams', []):
            if (s.get('codec_type') == 'subtitle' and
                    s.get('codec_name', '').lower() in PORTABLE_SUB_CODECS):
                maps.append(f"-map {input_idx}:{s['index']}")
        return " ".join(maps)

    def execute_strategy(self, level, input_file, output_file, source_codec=None):
        """Construye y ejecuta el comando FFmpeg según el nivel y la estrategia del HardwareAgent.

        Reglas globales aplicadas en todos los niveles:
          - Mapeado explícito: solo video, audio y subtítulos (nada de carátulas/adjuntos)
          - Subtítulos: SOLO los de texto → SRT; los de imagen (PGS/VOBSUB/DVB) se
            descartan siempre. Si aun así fallan, se descartan (no se tira el transcode)
          - -map_metadata -1: sin spam en los metadatos del contenedor
          - Audio → AAC siempre
        """
        # Flags de limpieza comunes a todos los niveles
        CLEAN_FLAGS = "-map_metadata -1 -ignore_unknown"

        if level == 3:
            level_name = "Nivel 3 (VapourSynth + HW)"
            if not (Path(FAST_WORK_DIR) / "script.vpy").exists():
                self._log(f"   💥 No se encontró script.vpy para Nivel 3.")
                return False
            hw_args = self.agent.get_transcode_args(target_codec="h264")
            vpy_path = str(Path(FAST_WORK_DIR) / "script.vpy")
            # Subs del original (input 1): solo texto, imagen descartada
            sub_maps = self._text_sub_maps(input_file, input_idx=1)
            # Video del pipe VapourSynth, audio/subs del original
            cmd_subs   = (f"{_VSPIPE_BIN} -c y4m \"{vpy_path}\" - | "
                          f"ffmpeg -hide_banner -y -i - -i \"{input_file}\" "
                          f"-map 0:v:0 -map 1:a? {sub_maps} -map -1:d "
                          f"{hw_args} -c:a aac -c:s srt {CLEAN_FLAGS} \"{output_file}\"")
            cmd_no_subs = (f"{_VSPIPE_BIN} -c y4m \"{vpy_path}\" - | "
                           f"ffmpeg -hide_banner -y -i - -i \"{input_file}\" "
                           f"-map 0:v:0 -map 1:a? -sn -map -1:d "
                           f"{hw_args} -c:a aac {CLEAN_FLAGS} \"{output_file}\"")
            return self._run_with_sub_fallback(cmd_subs, cmd_no_subs, output_file, level_name)

        elif level == 2:
            if source_codec in GOOD_CODECS:
                # Remux puro: solo cambia el contenedor, audio y vídeo intactos
                level_name  = "Nivel 2 (Remux Médico)"
                video_args  = "-c:v copy"
                audio_args  = "-c:a copy"
            else:
                # Legacy/desconocido: transcode completo, preservar canal count (sin -b:a fijo)
                level_name  = "Nivel 2 (Transcode h264)"
                video_args  = "-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p"
                audio_args  = "-c:a aac -af aresample=async=1"
                self._log(f"   🔄 {level_name}: codec fuente '{source_codec}' → h264 -crf 18")
            sub_maps = self._text_sub_maps(input_file, input_idx=0)
            base = (f"ffmpeg -hide_banner -y -fflags +genpts -i \"{input_file}\" "
                    f"-map 0:v:0 -map 0:a? -map -0:d ")
            cmd_subs    = base + f"{sub_maps} {video_args} {audio_args} -c:s srt {CLEAN_FLAGS} \"{output_file}\""
            cmd_no_subs = base + f"-sn {video_args} {audio_args} {CLEAN_FLAGS} \"{output_file}\""
            return self._run_with_sub_fallback(cmd_subs, cmd_no_subs, output_file, level_name)

        elif level == 1:
            level_name_hw = "Nivel 1 (HW)"
            self._log(f"   🪜 {level_name_hw}: Intentando recodificación acelerada por hardware...")
            hw_args = self.agent.get_transcode_args(target_codec="h264")
            sub_maps = self._text_sub_maps(input_file, input_idx=0)
            base_hw = (f"ffmpeg -hide_banner -y -i \"{input_file}\" "
                       f"-map 0:v:0 -map 0:a? -map -0:d ")
            cmd_hw_subs    = base_hw + f"{sub_maps} {hw_args} -c:a aac -c:s srt {CLEAN_FLAGS} \"{output_file}\""
            cmd_hw_no_subs = base_hw + f"-sn {hw_args} -c:a aac {CLEAN_FLAGS} \"{output_file}\""

            result = self._run_with_sub_fallback(cmd_hw_subs, cmd_hw_no_subs, output_file, level_name_hw)

            if result is True:
                return True

            # HW falló (o "Could not write header") → CPU Tank
            level_name_cpu = "Nivel 1 (CPU Tank)"
            self._log(f"   ↪️ HW falló. Activando {level_name_cpu}...")
            base_cpu = (f"ffmpeg -hide_banner -y "
                        f"-fflags +genpts+igndts+discardcorrupt "
                        f"-i \"{input_file}\" "
                        f"-map 0:v:0 -map 0:a? -map -0:d ")
            cmd_cpu_subs    = (base_cpu + f"{sub_maps} "
                               f"-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p "
                               f"-c:a aac -c:s srt "
                               f"-metadata:s:v:0 sar=1/1 {CLEAN_FLAGS} \"{output_file}\"")
            cmd_cpu_no_subs = (base_cpu +
                               f"-sn "
                               f"-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p "
                               f"-c:a aac "
                               f"-metadata:s:v:0 sar=1/1 {CLEAN_FLAGS} \"{output_file}\"")
            return self._run_with_sub_fallback(cmd_cpu_subs, cmd_cpu_no_subs, output_file, level_name_cpu)

        self._log(f"   💥 Nivel de estrategia desconocido: {level}")
        return False
        
        self._log(f"   💥 Nivel de estrategia desconocido: {level}")
        return False

    def _post_recovery_check(self, recovered_path, fast_mode):
        """
        Verifica la integridad del archivo recuperado.
        God Mode (fast_mode=False): decode completo.
        Goddess Mode (fast_mode=True): structural + timestamps (sin decode).
        """
        if not fast_mode:
            return self.verifier.check_health(recovered_path, fast_mode=False)
        else:
            return (self.verifier.check_timestamps(recovered_path) and
                    self.verifier.check_health(recovered_path, fast_mode=True))

    def transcodificar_archivo(self, ruta_origen, triage=None):
        """
        Implementa la "Escalera de Resiliencia" para procesar un archivo de vídeo.
        Prueba secuencialmente desde el método más sofisticado al más robusto.
        El triage dict (de verifier.triage_file) se usa si ya está disponible.
        """
        nombre_archivo = os.path.basename(ruta_origen)
        nombre_mkv = os.path.splitext(nombre_archivo)[0] + ".mkv"
        ruta_destino_temp = os.path.join(FAST_WORK_DIR, nombre_mkv)

        if os.path.exists(ruta_destino_temp):
            os.remove(ruta_destino_temp)

        # Obtener codec y estado de timestamps (reutilizar del triage si ya se hizo)
        if triage:
            source_codec  = triage.get("codec")
            timestamps_ok = triage.get("timestamps_ok", True)
        else:
            source_codec  = self.verifier.get_video_codec(ruta_origen)
            timestamps_ok = self.verifier.check_timestamps(ruta_origen)

        self._log(f"   💉 Procesando en NVMe: {nombre_mkv}")
        self._log(f"   🔍 Codec fuente: {source_codec or 'DESCONOCIDO'} | Timestamps: {'OK' if timestamps_ok else 'ROTOS'}")

        # --- NIVEL 3: VAPOURSYNTH + HW ---
        self._log(f"   🪜 Intentando Nivel 3 (VapourSynth + HW)...")
        if _VS_AVAILABLE and _VSPIPE_BIN:
            vpy_script_path = self._get_vs_script(ruta_origen)
            if self.execute_strategy(level=3, input_file=ruta_origen, output_file=ruta_destino_temp):
                self._log(f"   ✅ ÉXITO en Nivel 3 para '{nombre_archivo}'.")
                if os.path.exists(vpy_script_path): os.remove(vpy_script_path)
                lwi_file = Path(ruta_origen).with_suffix('.lwi')
                if lwi_file.exists():
                    try: os.remove(lwi_file)
                    except OSError: pass
                return ruta_destino_temp, True

            if os.path.exists(vpy_script_path): os.remove(vpy_script_path)
            lwi_file = Path(ruta_origen).with_suffix('.lwi')
            if lwi_file.exists():
                try: os.remove(lwi_file)
                except OSError: pass
        else:
            self._log("   ⚠️ VapourSynth o vspipe no están disponibles. Saltando Nivel 3.")

        # --- NIVEL 2: REMUX MÉDICO / FORZADO h264 ---
        self._log(f"   ↪️ Fallback a Nivel 2...")
        if self.execute_strategy(level=2, input_file=ruta_origen, output_file=ruta_destino_temp,
                                 source_codec=source_codec):
            self._log(f"   ✅ ÉXITO en Nivel 2 para '{nombre_archivo}'.")
            return ruta_destino_temp, True

        # --- NIVEL 1: RECODIFICACIÓN BRUTA + HW ---
        self._log(f"   ↪️ Fallback a Nivel 1 (Recodificación Bruta + HW)...")
        if self.execute_strategy(level=1, input_file=ruta_origen, output_file=ruta_destino_temp):
            self._log(f"   ✅ ÉXITO en Nivel 1 para '{nombre_archivo}'.")
            return ruta_destino_temp, True

        # --- NIVEL 0: SALVAGUARDA ---
        self._log(f"   🪜 Nivel 0 (Salvaguarda): Todos los métodos fallaron para '{nombre_archivo}'.")
        self._log(f"   🛑 REQUIRES_MANUAL_REVIEW: {ruta_origen}")

        if os.path.exists(ruta_destino_temp):
            os.remove(ruta_destino_temp)

        return None, False

    def procesar_lista(self, entrada, modo_estricto=False, fast_scan=False):
        """
        :param modo_estricto: Si True, procesa solo archivos que necesiten intervención.
                              Si False, procesa todos los formatos de video encontrados.
        :param fast_scan: Goddess Mode (True) = triage structural + timestamps.
                          God Mode (False) = triage con decode completo.
        """
        stats = {"processed": 0, "saved_bytes": 0, "skipped": [], "failed": [], "suspicious": []}
        rutas = []
        if isinstance(entrada, list):
            rutas = entrada
        elif isinstance(entrada, str):
            entrada = entrada.strip().replace('"', '').replace("'", "")
            if not os.path.exists(entrada): return stats

            exts = ('.mkv', '.avi', '.mp4', '.m4v', '.divx', '.wmv', '.mov')
            if os.path.isdir(entrada):
                for root, _, files in os.walk(entrada):
                    for f in files:
                        if f.lower().endswith(exts):
                            rutas.append(os.path.join(root, f))
            elif os.path.isfile(entrada):
                if entrada.lower().endswith('.txt'):
                    with open(entrada, 'r', encoding='utf-8') as f:
                        rutas = [l.strip().replace('"', '').replace("'", "") for l in f if l.strip()]
                else:
                    rutas = [entrada]

        if not rutas:
            print("⚠️ Nada que procesar.")
            return stats

        total = len(rutas)
        modo_str = 'GODDESS' if fast_scan else 'GOD'
        print(f"🚀 Iniciando cola de {total} archivos... ({modo_str} MODE)")

        for i, ruta_origen in enumerate(rutas):
            print(f"\n[{i+1}/{total}] 🔎 {os.path.basename(ruta_origen)}")

            if not os.path.exists(ruta_origen): continue
            org_size = os.path.getsize(ruta_origen)

            # --- TRIAGE UNIFICADO ---
            print(f"    🩺 Triage ({modo_str})...", end=" ")
            triage = self.verifier.triage_file(ruta_origen, fast_mode=fast_scan)
            action = triage["action"]

            if action == "FLAG_SUSPICIOUS":
                print(f"⚠️ SOSPECHOSO (<1MB) — omitir, revisar manualmente.")
                stats["suspicious"].append(ruta_origen)
                continue

            if action == "SKIP":
                print("✅ SANO. Saltando.")
                stats["skipped"].append(ruta_origen)
                continue

            # TRANSCODE (no es h264/h265.mkv) o RESCUE (h264/h265.mkv roto) → misma escalera
            if action == "TRANSCODE":
                tag = f"→ h264 (era: codec={triage['codec']}, mkv={triage['path'].lower().endswith('.mkv')})"
            else:
                tag = f"ROTO (codec={triage['codec']})"
            print(f"❌ {tag}. Iniciando recuperación...")

            # --- PROCESO ---
            start_time = time.time()
            ruta_nuevo, _ = self.transcodificar_archivo(ruta_origen, triage=triage)

            if not ruta_nuevo:
                stats["failed"].append(ruta_origen)
                continue

            # --- VALIDACIÓN check_rescue ---
            print("   ⚖️  Validando resultado...", end=" ")
            veredicto = self.verifier.check_rescue(ruta_origen, ruta_nuevo)

            if veredicto["valid"]:
                # --- POST-RECOVERY HEALTH CHECK (profundidad según modo) ---
                print(f"\n   🔬 Post-check ({modo_str})...", end=" ")
                post_ok = self._post_recovery_check(ruta_nuevo, fast_mode=fast_scan)
                if not post_ok:
                    print("💀 RECHAZADO: El archivo resultante no pasa el post-check.")
                    veredicto["valid"] = False
                else:
                    print("✅ ESTABLE.")

            if veredicto["valid"]:
                try:
                    nuevo_nombre_final = os.path.splitext(ruta_origen)[0] + ".mkv"
                    if os.path.exists(ruta_origen):
                        os.remove(ruta_origen)
                    shutil.move(ruta_nuevo, nuevo_nombre_final)
                    print(f"🎉 ÉXITO: {os.path.basename(nuevo_nombre_final)}")
                    stats["processed"] += 1
                    stats["saved_bytes"] += (org_size - os.path.getsize(nuevo_nombre_final))
                    self._log(f"PROCESSED: {os.path.basename(ruta_origen)} -> {os.path.basename(nuevo_nombre_final)}")
                except Exception as e:
                    self._log(f"   🚨 ERROR CRÍTICO AL MOVER: {e}")
                    stats["failed"].append(ruta_origen)
            else:
                print(f"   💀 FALLO DE VALIDACIÓN: {veredicto.get('errors')}")
                stats["failed"].append(ruta_origen)
                if os.path.exists(ruta_nuevo): os.remove(ruta_nuevo)

            print(f"   ⏱️  {time.time() - start_time:.1f}s")

        if stats["skipped"]:
            print(f"\n✨ Se omitieron {len(stats['skipped'])} archivos sanos.")
        if stats["suspicious"]:
            print(f"\n⚠️  {len(stats['suspicious'])} archivo(s) sospechosos (<1MB) — revisar manualmente:")
            for s in stats["suspicious"]: print(f"   → {s}")
        return stats

