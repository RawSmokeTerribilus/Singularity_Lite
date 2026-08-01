import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from singularity_config import GOD_PHRASES
from core.status_manager import update_status

import random
import threading
import itertools
import platform
import subprocess
import time
from datetime import datetime

# --- TROLLING SUBSYSTEM INJECTION ---
if GOD_PHRASES:
    import builtins
    if not hasattr(builtins, 'original_print'):
        builtins.original_print = builtins.print

    def troll_print(*args, **kwargs):
        if random.random() < 0.01: # 1% de probabilidad
            phrase = random.choice(GOD_PHRASES)
            builtins.original_print(f"\033[95m« {phrase} »\033[0m")
        builtins.original_print(*args, **kwargs)

    print = troll_print
# ------------------------------------

# --- CONFIGURACIÓN DE RUTAS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE_DIR, 'bin')
MODULES_DIR = os.path.join(BASE_DIR, 'modules')

# Añadimos modules al path
sys.path.append(MODULES_DIR)

# --- COLORES ---
C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_MAGENTA = "\033[95m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

BANNER = f"""{C_CYAN}

   _____   ____  __.____   ____                     __  .__    .__                
  /     \ |    |/ _|\   \ /   /___________ ___.__._/  |_|  |__ |__| ____    ____  
 /  \ /  \|      <   \   Y   // __ \_  __ <   |  |\   __\  |  \|  |/    \  / ___\ 
/    Y    \    |  \   \     /\  ___/|  | \/\___  | |  | |   Y  \  |   |  \/ /_/  >
\____|__  /____|__ \   \___/  \___  >__|   / ____| |__| |___|  /__|___|  /\___  / 
        \/        \/              \/       \/                \/        \//_____/     

   --- GET YOUR SHIT TOGETHER, WHICH MEANS, FIX AND PASS TO MKV --- {C_RESET}

"""

def limpiar_pantalla():
    os.system('cls' if os.name == 'nt' else 'clear')

def configurar_entorno():
    """Inyecta binarios en el PATH y librerías en LD_LIBRARY_PATH."""
    sistema = platform.system()
    path_binario = ""
    if sistema == "Windows":
        path_binario = os.path.join(BIN_DIR, 'win')
    elif sistema == "Linux":
        path_binario = os.path.join(BIN_DIR, 'linux')
        if not os.path.exists(path_binario): path_binario = BIN_DIR 

    if path_binario and os.path.exists(path_binario):
        os.environ["PATH"] += os.pathsep + path_binario
        
        # En Linux, también configurar LD_LIBRARY_PATH para que los binarios
        # encuentren sus librerías .so (especialmente importantes para ffmpeg, mkvtoolnix)
        if sistema == "Linux":
            ld_lib_path = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = path_binario + (os.pathsep + ld_lib_path if ld_lib_path else "")
            subprocess.run(f"chmod +x {path_binario}/* 2>/dev/null", shell=True)
    return sistema

def scan_files(folder, extensions):
    """Escáner recursivo para encontrar archivos por extensión."""
    found = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(tuple(extensions)):
                found.append(os.path.join(root, f))
    return found

def typewriter(text, delay=0.01):
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    print()

class FakeProgress(threading.Thread):
    def __init__(self, task):
        super().__init__()
        self.task = task
        self._stop_event = threading.Event()
    def run(self):
        for c in itertools.cycle(['|', '/', '-', '\\']):
            if self._stop_event.is_set(): break
            sys.stdout.write(f"\r   ⏳ {self.task} {c}")
            sys.stdout.flush()
            time.sleep(0.1)
    def stop(self):
        self._stop_event.set()
        sys.stdout.write("\r" + " "*50 + "\r")

def main():
    sistema = configurar_entorno()
    
    while True:
        update_status("MKVERYTHING", "Esperando órdenes", "ONLINE")
        limpiar_pantalla()
        print(BANNER)
        print(f"   🖥️  Sistema: {sistema}")
        
        print(f"\n   {C_YELLOW}--- HERRAMIENTAS INDIVIDUALES ---{C_RESET}")
        print("   [1] ⚖️  AUDITORÍA DE CAMPO (Recursivo + Informe de Bajas)")

        print("\n   [0] 🚪 SALIR")

        # EDICIÓN LITE: el rescate, la conversión legacy, la extracción de ISOs
        # y los modos God/Goddess no se incluyen. Todos ellos transcodifican o
        # dependen de MakeMKV (binarios x86_64) y VapourSynth, que esta imagen
        # no lleva. Auditar es barato: solo lee metadatos. Reconstruir no.
        # Para eso, usa la suite completa en una máquina de escritorio.

        opcion = input(f"\n   {C_GREEN}👉 Selecciona: {C_RESET}")

        try:
            if opcion == "0":
                sys.exit()

            elif opcion in ("2", "3", "4", "5", "6"):
                print(f"\n   {C_YELLOW}⚠  Esa opción no existe en Singularity Lite.{C_RESET}")
                print("   Rescate, conversión, extracción de ISOs y God/Goddess Mode")
                print("   requieren transcodificación. Usa la suite completa.")
                input("\n   Pulsa Enter para volver...")
                continue

            elif opcion == "1":
                from modules import verifier
                v = verifier.Verifier()

                path_target = input("\n📂 Carpeta o Punto de Montaje a auditar: ").strip().replace("'","").replace('"','')
                if not os.path.exists(path_target):
                    print("❌ La ruta no existe.")
                    time.sleep(2)
                    continue

                # Profundidad del escaneo. Determinante en hardware modesto:
                #   rápido -> mkvmerge -J + ffprobe. Solo lee cabeceras.
                #   profundo -> además decodifica el fichero entero con
                #               `ffmpeg -xerror -f null -`. Detecta corrupción
                #               que las cabeceras no delatan, pero en una Pi o
                #               un NAS son horas por biblioteca.
                print(f"\n   {C_YELLOW}Profundidad:{C_RESET}")
                print("   [1] Rápido   — solo estructura y metadatos (recomendado)")
                print("   [2] Profundo — decodifica cada fichero (lento; horas en hardware modesto)")
                modo = input("\n   👉 Selecciona [1]: ").strip() or "1"
                fast = (modo != "2")
                print(f"\n   Modo: {C_CYAN}{'rápido' if fast else 'profundo'}{C_RESET}")

                fecha_str = datetime.now().strftime("%d-%m-%y")
                logs_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(logs_dir, exist_ok=True)
                archivo_bajas       = os.path.join(logs_dir, f"videos-rotos-{fecha_str}.txt")
                archivo_sospechosos = os.path.join(logs_dir, f"videos-sospechosos-{fecha_str}.txt")

                print(f"\n🔍 {C_CYAN}Iniciando inventario...{C_RESET}")
                archivos = scan_files(path_target, ['.mkv', '.avi', '.mp4', '.mov', '.wmv'])
                total = len(archivos)

                print(f"📊 {total} archivos detectados. Iniciando escaneo de integridad...")
                print(f"📄 Informe de bajas: {archivo_bajas}\n")

                rotos = 0
                sanos = 0
                spam  = 0
                sospechosos = 0

                for i, f in enumerate(archivos):
                    prog = int((i / total) * 100)
                    print(f"[{i+1}/{total}] {os.path.basename(f)[:50]}...", end="\r")
                    update_status("MKVERYTHING", "Auditoría", "PROCESSING", progress=prog, details=f"Escaneando: {os.path.basename(f)}")

                    # Triage unificado (detecta legacy, corruptos y sospechosos)
                    triage    = v.triage_file(f, fast_mode=fast)
                    spam_info = v.audit_file_metadata(f)

                    if triage["action"] == "FLAG_SUSPICIOUS":
                        sospechosos += 1
                        with open(archivo_sospechosos, "a", encoding="utf-8") as out:
                            out.write(f + "\n")
                    elif triage["action"] in ("TRANSCODE", "RESCUE"):
                        rotos += 1
                        with open(archivo_bajas, "a", encoding="utf-8") as out:
                            out.write(f + "\n")
                    else:
                        sanos += 1

                    if not spam_info['clean']:
                        spam += 1

                print(f"\n\n{C_YELLOW}--- RESUMEN DE AUDITORÍA ---{C_RESET}")
                print(f"✅ Sanos:        {sanos}")
                print(f"❌ Rotos/Legacy: {C_RED}{rotos}{C_RESET}")
                print(f"⚠️  Sospechosos:  {C_YELLOW}{sospechosos}{C_RESET}  (<1MB — revisar manualmente)")
                print(f"🏷️  Spam:         {spam}")
                print(f"----------------------------")
                update_status("MKVERYTHING", "Auditoría", "COMPLETED", progress=100,
                              details=f"Sanos: {sanos}, Rotos: {rotos}, Sospechosos: {sospechosos}")

                if sospechosos > 0:
                    print(f"\n⚠️  Lista de sospechosos: {C_YELLOW}{archivo_sospechosos}{C_RESET}")

                if rotos > 0:
                    print(f"\n📢 Lista de bajas: {C_CYAN}{archivo_bajas}{C_RESET}")
                    # EDICIÓN LITE: sin hand-off al Rescatador. Reconstruir un MKV
                    # transcodifica, y esta imagen no lleva VapourSynth. La lista
                    # es portable: pásala a la suite completa en una máquina capaz.
                    print(f"   {C_YELLOW}Lite no repara.{C_RESET} Llévate esa lista a la suite")
                    print("   completa (opción [2] Rescatar) en una máquina de escritorio.")
                else:
                    print(f"\n{C_GREEN}💎 Librería impecable. No se han detectado errores.{C_RESET}")

                input("\n✅ Pulsa Enter para volver...")
                continue

        except Exception as e:
            print(f"❌ Error inesperado: {e}")
            input()

if __name__ == "__main__":
    main()
