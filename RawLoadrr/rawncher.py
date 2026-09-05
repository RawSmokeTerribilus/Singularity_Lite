#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rawncher — Lanzador interactivo para RawLoadrr (en español)
"""

import sys
import os
import json
import re
import logging
import importlib
import random
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime
from pprint import pformat

try:
    import qbittorrentapi as qbit_api
except ImportError:
    print("Error: qbittorrent-api no está instalado.")
    print("Por favor, ejecútalo con: pip install qbittorrent-api")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.status_manager import update_status
from src.console import console
from src.placeholders import es_placeholder, falta_por_preguntar
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule

# --- TROLLING SUBSYSTEM INJECTION ---
try:
    from singularity_config import GOD_PHRASES
except ImportError:
    GOD_PHRASES = []

if GOD_PHRASES:
    # Monkey Patch: Secuestramos console.print para inyectar caos con 1% de probabilidad
    original_print = console.print

    def troll_print(*args, **kwargs):
        if random.random() < 0.01: # 1% de probabilidad
            phrase = random.choice(GOD_PHRASES)
            original_print(f"[dim italic magenta]« {phrase} »[/dim italic magenta]")
        original_print(*args, **kwargs)

    console.print = troll_print  # type: ignore[method-assign]
# ------------------------------------

class Rawncher:
    """Lanzador principal de RawLoadrr"""

    def __init__(self):
        # 1. RECON DE ENTORNO Y CARGA INICIAL
        self.base_dir = Path(__file__).resolve().parent
        self._logger = self._setup_logger()
        self._reload_config()  # FIX: Cargar config al inicio para evitar AttributeError
        self.client = None

        # ── FASE ADUANA: Verificación del Sistema ─────────────────────────── #
        console.print(Rule("[bold yellow]⚙  ADUANA — Verificación del Sistema[/bold yellow]", style="yellow"))

        # --- ADUANA DE SOBERANÍA (Check de Fresh Install para APIs globales) ---
        if not self.config:
            console.print("\n[bold red]❌ No se pudo leer el archivo de configuración.[/bold red]")
            console.print("[bold yellow]⚠️  Asegúrate de que 'data/config.py' existe y es válido.[/bold yellow]")
        else:
            # Comprobamos APIs globales (TMDB/IMGBB) que son críticas para upload.py
            # Cada entrada: campo en config.py -> (nombre visible, pista de dónde sale).
            # Las tres últimas son las que necesitan libros y juegos, y faltaban:
            # una instalación anterior a ellas no tiene el campo en su config.py
            # —que es un fichero del usuario y NO se regenera— así que nunca se
            # le preguntaba y el resolver le respondía "faltan las claves" sin
            # decirle dónde ponerlas.
            master_keys = {
                "tmdb_api":           ("TMDB_API_KEY", "themoviedb.org/settings/api"),
                "imdb_api":           ("IMDB_API_KEY", ""),
                "imgbb_api":          ("IMGBB_API_KEY", ""),
                "ptscreens_api":      ("PTSCREENS_API_KEY", ""),
                "ptpimg_api":         ("PTPIMG_API_KEY", ""),
                "lensdump_api":       ("LENSDUMP_API_KEY", ""),
                "oeimg_api":          ("OEIMG_API_KEY", ""),
                "google_books_api":   ("GOOGLE_BOOKS_API", "console.cloud.google.com — identifica e-books y audiolibros"),
                "igdb_client_id":     ("TWITCH_CLIENT_ID", "dev.twitch.tv/console — IGDB va detrás de una app de Twitch"),
                "igdb_client_secret": ("TWITCH_CLIENT_SECRET", "la misma app de Twitch que el id"),
            }
            needs_save = False
            default_config = self.config.get("DEFAULT", {})
            for python_key, (env_key, pista) in master_keys.items():
                val = default_config.get(python_key, "")

                # El hueco se reconoce en src/placeholders.py, no aquí: había
                # tres formatos distintos y la comprobación de antes sólo veía
                # el prefijo YOUR_.
                obligatoria = python_key == "tmdb_api"

                # Una clave dejada en blanco A PROPOSITO es una respuesta, no un
                # hueco: `es_placeholder("")` es True --y hace bien, "" no
                # autentica nada-- pero preguntar por ella otra vez es no haber
                # escuchado. Reportado desde una instalacion real: "dice que no
                # se volvera a preguntar pero cada vez que abro rawloadrr me las
                # pide".
                #
                # La obligatoria es la excepcion: sin tmdb_api no se sube nada,
                # asi que ahi se sigue insistiendo aunque la dejen vacia.
                if obligatoria:
                    hay_hueco = es_placeholder(val, env_key, python_key)
                else:
                    hay_hueco = falta_por_preguntar(default_config, python_key,
                                                    env_key, python_key)

                if hay_hueco:

                    console.print(f"\n[bold red]✖ {env_key} no configurada o tiene valor por defecto.[/bold red]")

                    if pista:
                        console.print(f"[dim]  {pista}[/dim]")

                    msg = f"[bold cyan]▶ Introduce el valor para {env_key}[/bold cyan]"

                    if not obligatoria:
                        msg += "[dim] (Enter para dejarla vacía; no se vuelve a preguntar)[/dim]"

                    new_val = Prompt.ask(msg)

                    if "DEFAULT" not in self.config:
                        self.config["DEFAULT"] = {}

                    # Se escribe clave a clave y en sitio. `_guardar_config()`
                    # regeneraría config.py entero desde pformat() y se llevaría
                    # por delante los comentarios del usuario --en el config de
                    # producción hay 48 líneas de notas sobre la cascada de
                    # hosts de imágenes que no están en ningún otro sitio.
                    if new_val.strip():
                        if self._persistir_clave_default(python_key, new_val.strip()):
                            needs_save = True
                        else:
                            # No se pudo escribir con garantías: el valor vale
                            # para esta sesión, pero hay que decir que no queda.
                            self.config["DEFAULT"][python_key] = new_val.strip()
                            console.print(
                                f"[bold yellow]  ⚠ No se pudo escribir en data/config.py. "
                                f"Vale para esta sesión; ponla a mano en DEFAULT -> {python_key}.[/bold yellow]"
                            )
                    elif not obligatoria:
                        # Saltar dejaba el campo AUSENTE, así que la aduana lo
                        # volvía a preguntar en CADA arranque. Se persiste vacío:
                        # el campo pasa a existir en config.py --visible y
                        # editable a mano, que es lo que le faltaba a quien viene
                        # de una versión anterior-- y deja de molestar. Es la
                        # misma idea que ensure_env_keys() en Mass Edition.
                        if self._persistir_clave_default(python_key, ""):
                            needs_save = True
                            console.print(
                                f"[dim]  Se deja vacía. Para ponerla luego: "
                                f"data/config.py -> DEFAULT -> {python_key}[/dim]"
                            )
            if needs_save:
                console.print("[bold green]✅ Claves de API globales guardadas.[/]")
                self._reload_config()  # Recargamos para que todo esté fresco

        # ── ADUANA: Enlace con Cliente Torrent ────────────────────────────── #
        console.print(Rule("[bold yellow]⚙  ADUANA — Enlace qBittorrent[/bold yellow]", style="yellow"))

        # --- 2. CARGA Y RECON DE CONFIG (qBit) ---
        qbit_data = self.config.get("qbit", {})
        
        # Si las credenciales son las de serie o falta la URL, lanzamos la interfaz Rich
        if not qbit_data or qbit_data.get("qbit_user") == "YOUR_USERNAME" or not qbit_data.get("qbit_url"):
            self._logger.warning("ACR: Credenciales qBit por defecto o ausentes. Iniciando interceptación.")
            
            console.print(Panel("[bold yellow]🛠️ CONFIGURACIÓN DE ADUANA: qBittorrent[/]", expand=False))
            console.print("[dim]Si qBit está en un contenedor Docker, usa la IP del host (ej: [bold]172.17.0.1[/bold]), no 'localhost'.[/dim]")
            
            host = Prompt.ask("URL del Cliente", default=qbit_data.get('qbit_url', 'http://172.17.0.1'))
            port = Prompt.ask("Puerto", default=str(qbit_data.get('qbit_port', '8888')))
            user = Prompt.ask("Usuario qBit", default=qbit_data.get('qbit_user', '').replace('YOUR_USERNAME', ''))
            pwd  = Prompt.ask("Contraseña qBit", password=True)

            if "qbit" not in self.config: self.config["qbit"] = {}
            self.config["qbit"].update({
                "qbit_url": host.strip(),
                "qbit_port": str(port).strip(),
                "qbit_user": user.strip(),
                "qbit_pass": pwd.strip(),
                "enable_search": True
            })

            self._guardar_config()
            console.print("[bold green]✅ Configuración de qBittorrent persistida con éxito.[/]")
            self._reload_config()

        # --- 3. VALIDACIÓN DE ENLACE (Handshake) ---
        try:
            cfg = self.config["qbit"]
            base_url = cfg["qbit_url"].rstrip('/')
            
            self._logger.info(f"Aduana: Intentando handshake con {base_url}:{cfg['qbit_port']}")
            
            self.client = qbit_api.Client(
                host=base_url,
                port=int(cfg["qbit_port"]),
                username=cfg["qbit_user"],
                password=cfg["qbit_pass"],
                VERIFY_WEBUI_CERTIFICATE=cfg.get("VERIFY_WEBUI_CERTIFICATE", False),
                REQUESTS_ARGS={'timeout': 10}
            )
            self.client.auth_log_in()
            self._logger.info(f"Aduana: Online (v{self.client.app.version}). Conectado a qBittorrent.")
            
        except Exception as e:
            self._logger.error(f"Aduana: Error de enlace qBit ({e}). Modo 'Solo Local' activado.")
            self.client = None
            console.print(Panel(f"[yellow]No se pudo conectar a qBittorrent. El script funcionará en [bold]modo solo local[/bold], guardando los torrents en una carpeta 'watch'.\nCausa: [dim]{e}[/dim]", 
                                title="[bold yellow]⚠️ Conexión qBit fallida[/]"))

        # ── ADUANA: BT_backup / torrent_storage_dir ───────────────────────── #
        # CSI v3.4 (cosmetic fix 2026-05-23): the upload-side `clients.py`
        # reads from `config['TORRENT_CLIENTS'][default_torrent_client]
        # ['torrent_storage_dir']`, not the top-level `config['qbit']` key
        # that this aduana used to write to. Result: user typed the path in
        # the aduana prompt, it landed only in `config['qbit']`, and the
        # upload pipeline kept warning about a stale snap path from the
        # `TORRENT_CLIENTS.qbit` slot. We now write to BOTH keys so the
        # warning never returns regardless of which side is reading.
        default_client = (self.config.get('DEFAULT', {}) or {}).get(
            'default_torrent_client', 'qbit')
        clients_cell = (
            self.config.get('TORRENT_CLIENTS', {}) or {}
        ).get(default_client, {}) or {}

        legacy_path  = (self.config.get('qbit', {}) or {}).get('torrent_storage_dir', '')
        upload_path  = clients_cell.get('torrent_storage_dir', '')
        # Treat the configured path as "stored" when either key has it AND it
        # actually exists on disk. Mismatch between the two also forces the
        # prompt so we resync them.
        candidate = upload_path or legacy_path
        needs_prompt = (
            not candidate
            or not Path(candidate).exists()
            or (legacy_path and upload_path and legacy_path != upload_path)
        )
        if needs_prompt:
            console.print(Rule("[bold yellow]⚙  ADUANA — Ruta de sesión qBittorrent[/bold yellow]", style="yellow"))
            if candidate and not Path(candidate).exists():
                console.print(f"[bold red]✖ Ruta configurada no existe:[/bold red] [dim]{candidate}[/dim]")
            elif legacy_path and upload_path and legacy_path != upload_path:
                console.print(
                    f"[bold yellow]⚠ Rutas desincronizadas:[/bold yellow]\n"
                    f"  legacy (qbit):           [dim]{legacy_path}[/dim]\n"
                    f"  upload (TORRENT_CLIENTS): [dim]{upload_path}[/dim]"
                )
            else:
                console.print("[bold yellow]⚠️  torrent_storage_dir no configurado.[/bold yellow]")
            console.print("[dim]Ruta a la carpeta BT_backup de qBittorrent (permite reutilizar hashes ya descargados).[/dim]")
            new_path = Prompt.ask("Ruta BT_backup", default=str(self.base_dir / 'qbit_backup'))
            new_path = new_path.strip()

            # Write to BOTH keys to keep CSI (aduana legacy) and clients.py
            # (upload pipeline) in lockstep. Future readers see the same path
            # no matter which slot they consult.
            if "qbit" not in self.config:
                self.config["qbit"] = {}
            self.config["qbit"]["torrent_storage_dir"] = new_path
            if "TORRENT_CLIENTS" not in self.config:
                self.config["TORRENT_CLIENTS"] = {}
            if default_client not in self.config["TORRENT_CLIENTS"]:
                self.config["TORRENT_CLIENTS"][default_client] = {}
            self.config["TORRENT_CLIENTS"][default_client]["torrent_storage_dir"] = new_path

            self._guardar_config()
            console.print("[bold green]✅ Ruta de sesión persistida en ambas claves.[/]")
            self._reload_config()
            stored_path = new_path
        else:
            stored_path = candidate

        # Definimos ruta de salvaguarda
        self.watch_folder = Path(stored_path)
        self.watch_folder.mkdir(parents=True, exist_ok=True)

    # <--- AQUÍ TERMINA EL __INIT__ (Alineado con el def de arriba)
    def disparar(self, torrent_path):
        """Lógica de ejecución: API o Local"""
        path_obj = Path(torrent_path)
        
        if self.client:
            try:
                self._logger.info(f"Disparo: Enviando {path_obj.name} por API...")
                with open(path_obj, 'rb') as f:
                    self.client.torrents_add(files={'torrents': f})
                return True
            except Exception as e:
                self._logger.error(f"Fallo API: {e}. Pasando a manual...")
        
        try:
            import shutil
            destino = self.watch_folder / path_obj.name
            shutil.copy(path_obj, destino)
            self._logger.warning(f"Disparo: Guardado en local -> {destino}")
            return True
        except Exception as e:
            self._logger.critical(f"ACR: Fallo total en {path_obj.name}: {e}")
            return False

    def _setup_logger(self) -> logging.Logger:
        logs_dir = self.base_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_path = logs_dir / f"rawncher_{datetime.now().strftime('%Y-%m-%d')}.log"
        logger = logging.getLogger("rawncher")
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "[%(asctime)s] %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            ))
            logger.addHandler(fh)
        return logger

    # ------------------------------------------------------------------ #
    #  Bucle principal                                                     #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Bucle principal del lanzador"""
        self._logger.info("Rawncher session started")

        # ── ASCII ART BANNER ───────────────────────────────────────────────── #
        ascii_art = (
            "\n"
            " ██████╗  █████╗ ██╗    ██╗███╗   ██╗ ██████╗██╗  ██╗███████╗██████╗ \n"
            " ██╔══██╗██╔══██╗██║    ██║████╗  ██║██╔════╝██║  ██║██╔════╝██╔══██╗\n"
            " ██████╔╝███████║██║ █╗ ██║██╔██╗ ██║██║     ███████║█████╗  ██████╔╝\n"
            " ██╔══██╗██╔══██║██║███╗██║██║╚██╗██║██║     ██╔══██║██╔══╝  ██╔══██╗\n"
            " ██║  ██║██║  ██║╚███╔███╔╝██║ ╚████║╚██████╗██║  ██║███████╗██║  ██║\n"
            " ╚═╝  ╚═╝╚═╝  ╚═╝ ╚══╝╚══╝ ╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
            "            RawLoadrr Launcher  ·  Industrial Grade  ·  CvT\n"
        )
        console.print(f"[bold cyan]{ascii_art}[/]")
        console.print(Rule("[bold magenta]◈  RAWNCHER SESIÓN INICIADA[/bold magenta]", style="magenta"))
        while True:
            try:
                choice = self._menu_principal()
                self._logger.info(f"Menu selection: {choice}")
                if choice == "0":
                    console.print("\n[bold cyan]◈ ¡Hasta luego![/bold cyan]")
                    console.print(Rule(style="dim"))
                    self._logger.info("Rawncher session ended by user")
                    break
                elif choice == "1":
                    self.opcion_1_usar_tracker()
                elif choice == "2":
                    self.opcion_2_configurar()
                elif choice == "3":
                    self.opcion_3_crear_tracker()
                elif choice == "4":
                    self.opcion_4_debug()
                elif choice == "5":
                    self.opcion_5_listar_sesiones()
                elif choice == "6":
                    self.opcion_6_cargar_sesion()
            except KeyboardInterrupt:
                console.print("\n[bold yellow]⚠️  Operación cancelada.[/bold yellow]")
                self._logger.info("Operation cancelled by user (KeyboardInterrupt)")

    def _menu_principal(self) -> str:
        """Muestra el menú principal y devuelve la elección del usuario"""
        console.print()
        menu_text = (
            "\n"
            "  [bold cyan][1][/bold cyan]  Usar tracker existente\n"
            "  [bold cyan][2][/bold cyan]  Configurar tracker existente\n"
            "  [bold cyan][3][/bold cyan]  Crear nuevo tracker\n"
            "  [bold cyan][4][/bold cyan]  Modo debug [dim](sin subida real)[/dim]\n"
            "  [bold cyan][5][/bold cyan]  Listar sesiones guardadas\n"
            "  [bold cyan][6][/bold cyan]  Cargar sesión guardada\n"
            "  [bold cyan][0][/bold cyan]  Salir\n"
        )
        qbit_status = "[bold green]qBit: Online ✅[/bold green]" if self.client else "[bold red]qBit: Offline ❌[/bold red]"
        console.print(
            Panel(
                menu_text,
                title="[bold magenta]◈  RAWNCHER[/bold magenta]",
                subtitle=qbit_status,
                border_style="magenta",
            )
        )
        choice = Prompt.ask(
            "[bold]Elige una opción[/bold]",
            choices=["0", "1", "2", "3", "4", "5", "6"],
            default="1",
        )
        return choice

    # ------------------------------------------------------------------ #
    #  Helpers compartidos                                                 #
    # ------------------------------------------------------------------ #

    def _get_tracker_names_from_upload(self) -> list:
        """Lee upload.py como texto y extrae las abreviaciones de tracker_data['api']"""
        upload_path = self.base_dir / "upload.py"
        try:
            text = upload_path.read_text(encoding="utf-8")
            match = re.search(r"'api'\s*:\s*\[([^\]]+)\]", text)
            if not match:
                return []
            raw = match.group(1)
            return re.findall(r"'([A-Z0-9]+)'", raw)
        except Exception:
            return []

    def _sync_global_secret(self, key, value):
        """Propaga un secreto por .env, config.py y singularity_config.py"""
        import re
        # Diccionario de archivos y sus patrones de búsqueda específicos
        # .env usa MAYÚSCULAS, .py usa minúsculas
        targets = {
            self.base_dir / "data" / ".env": rf'^({key.upper()}=).*$',
            self.base_dir / "data" / "config.py": rf'("{key.lower()}":\s*")[^"]*(")',
            self.base_dir / "data" / "singularity_config.py": rf'("{key.lower()}":\s*")[^"]*(")'
        }

        for path, pattern in targets.items():
            if not path.exists():
                continue
            
            try:
                content = path.read_text(encoding="utf-8")
                
                if path.suffix == ".env":
                    # Forzamos formato KEY="value" para evitar líos de Bash
                    new_content = re.sub(pattern, f'{key.upper()}="{value}"', content, flags=re.MULTILINE)
                else:
                    # Formato Python: reemplazamos solo el contenido entre las comillas del grupo 2
                    new_content = re.sub(pattern, rf'\1{value}\2', content)
                
                if new_content != content:
                    path.write_text(new_content, encoding="utf-8")
                    console.print(f"[dim cyan]    ↳ {path.name} sincronizado.[/dim cyan]")
            except Exception as e:
                console.print(f"[dim red]    ! Fallo al escribir en {path.name}: {e}[/dim red]")

    def _build_tracker_table(self) -> tuple:
        """Construye tabla Rich con los trackers disponibles.

        Devuelve: (table, trackers_list)
        """
        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("Tracker", style="bold")
        table.add_column("API ✓/✗", justify="center")

        trackers = []
        if not self.config:
            console.print("[bold red]❌ No se encontró o no se pudo leer data/config.py[/bold red]")
            return table, trackers

        trackers_dict = self.config.get("TRACKERS", {})
        trackers = sorted(
            [t for t in trackers_dict if isinstance(trackers_dict[t], dict)]
        )
        for i, name in enumerate(trackers, 1):
            api_key = trackers_dict[name].get("api_key", "")
            configured = (
                bool(api_key)
                and "API_KEY" not in api_key
                and len(api_key) >= 32
            )
            icon = "[green]✓[/green]" if configured else "[red]✗[/red]"
            table.add_row(str(i), name, icon)

        return table, trackers

    def _seleccionar_tracker(self, preselected: Optional[str] = None) -> Optional[str]:
        """Muestra tabla de trackers y devuelve la abreviación elegida"""
        if preselected:
            return preselected

        table, trackers = self._build_tracker_table()
        if not trackers:
            console.print("[bold red]❌ No hay trackers disponibles en la configuración.[/bold red]")
            return None

        console.print(table)
        raw = Prompt.ask("[bold]Número del tracker[/bold]", default="1")
        try:
            idx = int(raw)
        except ValueError:
            idx = 0

        if 1 <= idx <= len(trackers):
            return trackers[idx - 1]

        console.print("[bold red]❌ Número no válido.[/bold red]")
        return None

    def _ejecutar_comando(self, cmd: list, cwd: Optional[Path] = None) -> int:
        """Ejecuta un comando con salida en vivo desde base_dir por defecto"""
        cwd_path = str(cwd or self.base_dir)
        self._logger.info(f"CMD: {' '.join(str(c) for c in cmd)} (cwd={cwd_path})")
        result = subprocess.run(cmd, cwd=cwd_path)
        self._logger.info(f"EXIT: {result.returncode}")
        return result.returncode

    def _get_sessions(self) -> list:
        """Devuelve lista de rutas Path de sesiones guardadas (más recientes primero)"""
        sessions_dir = self.base_dir / "tmp" / "tracker_sessions"
        if not sessions_dir.exists():
            return []
        return sorted(sessions_dir.glob("session_*.json"), reverse=True)

    # ------------------------------------------------------------------ #
    #  Opción 1 — Usar tracker existente                                  #
    # ------------------------------------------------------------------ #

    def opcion_1_usar_tracker(self, preselected_tracker: Optional[str] = None) -> None:
        """Menú principal de la opción 1"""
        tracker = self._seleccionar_tracker(preselected=preselected_tracker)
        if tracker is None:
            return
        
        update_status("RAWLOADRR", "Configuración", "PROCESSING", details=f"Tracker: {tracker}")
        console.print()

        # ── FASE RECON ────────────────────────────────────────────────────── #
        console.print(Rule(f"[bold cyan]▸  RECON — Tracker: {tracker}[/bold cyan]", style="cyan"))
        console.print()
        console.print(
            Panel(
                "\n"
                "  [bold cyan][1][/bold cyan]  Subir desde una ruta (archivo o carpeta)\n"
                "  [bold cyan][2][/bold cyan]  Usar lista existente (.txt con rutas)\n"
                "  [bold cyan][3][/bold cyan]  Triaje primero (escanear directorio y elegir listas)\n",
                title=f"[bold green]◈  Tracker: {tracker}[/bold green]",
                border_style="green",
            )
        )
        sub = Prompt.ask(
            "[bold]Elige sub-opción[/bold]",
            choices=["1", "2", "3"],
            default="1",
        )
        if sub == "1":
            self._flujo_ruta(tracker)
        elif sub == "2":
            self._flujo_lista_existente(tracker)
        elif sub == "3":
            self._flujo_triage(tracker)

    def _declarar_categoria(self) -> list:
        """
        Cuando ni abriendo el envase se sabe qué es, se pregunta AQUÍ.

        Vale la pena preguntarlo en el lanzador y no dejárselo a upload.py: se
        contesta antes de que arranque nada, y la respuesta viaja en el comando,
        que es lo que el operador ve y puede repetir.

        Devolver `[]` es legítimo: significa "sigue y ya me preguntará él".
        """
        opciones = [
            ("1", "game",      "juego"),
            ("2", "book",      "e-book"),
            ("3", "audiobook", "audiolibro"),
            ("4", "movie",     "película"),
            ("5", "tv",        "serie"),
        ]
        console.print("   [dim]Puedes declararlo ahora, o dejar que lo pregunte "
                      "la subida.[/dim]")
        for num, _valor, etiqueta in opciones:
            console.print(f"   [bold]{num}[/bold]  {etiqueta}")
        console.print("   [bold]0[/bold]  [dim]no declarar nada[/dim]")

        elegido = Prompt.ask("[bold]Categoría[/bold]",
                             choices=[n for n, _v, _e in opciones] + ["0"],
                             default="0")
        if elegido == "0":
            return []

        valor = next(v for n, v, _e in opciones if n == elegido)
        return ["--category", valor]

    def _elegir_tipos(self, ruta, hallado) -> list:
        """
        Acota QUÉ se busca en el árbol, y devuelve los flags `--only`.

        Sólo pregunta cuando hay MEZCLA: un directorio de un solo tipo no es
        ambiguo y hacer clicar por gusto sobra. La pregunta existe por un
        accidente concreto -- apuntar esto a la carpeta de descargas -- donde
        encolar todo junto es como se sube una factura a un tracker público.
        """
        if len(hallado) <= 1:
            return []

        console.print()
        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            title="[bold yellow]Aquí hay de todo — ¿qué quieres subir?[/bold yellow]",
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("Tipo")
        table.add_column("Encontrados", justify="right")

        from src.library import LABELS

        for i, (kind, n) in enumerate(hallado, 1):
            table.add_row(str(i), LABELS[kind], str(n))

        table.add_row(str(len(hallado) + 1), "[dim]todo junto[/dim]",
                      str(sum(n for _k, n in hallado)))
        console.print(table)
        console.print("[dim]Subir tipos distintos en la misma tirada casi nunca es lo "
                      "que se quiere.[/dim]")

        opciones = [str(i) for i in range(1, len(hallado) + 2)]
        elegido = int(Prompt.ask("[bold]Opción[/bold]", choices=opciones, default="1"))

        if elegido > len(hallado):
            return ["--only", "all"]

        return ["--only", hallado[elegido - 1][0]]

    def _args_opcionales(self, debug: bool = False) -> list:
        """Muestra lista de flags opcionales para upload.py y devuelve los seleccionados"""
        flags = [
            ("--anon",            "Subida anónima",                    "bool"),
            ("--skip-dupe-check", "Saltar comprobación de duplicados",  "bool"),
            ("--stream",          "Stream Optimized Upload",            "bool"),
            ("--personalrelease", "Personal Release",                   "bool"),
            ("--no-seed",         "No añadir torrent al cliente",       "bool"),
            ("--debug",           "Modo debug (sin subida real)",       "bool"),
            # Los ids a mano cortocircuitan el resolver, que es la salida honesta
            # cuando la identificación automática se equivoca. Hasta ahora había
            # que salirse del lanzador y tirar upload.py a pelo para dar uno.
            ("--tmdb",            "Id de TMDB (peli o serie)",          "valor"),
            ("--imdb",            "Id de IMDB (ttNNNNNNN)",             "valor"),
            ("--tvdb",            "Id de TVDB (sólo se envía)",         "valor"),
            ("--mal",             "Id de MyAnimeList (anime)",          "valor"),
            ("--isbn",            "ISBN del e-book (10 ó 13 dígitos)",  "valor"),
            ("--asin",            "ASIN de Audible (audiolibro)",       "valor"),
            ("--igdb",            "Id de IGDB (juego)",                 "valor"),
        ]

        selected = set()
        if debug:
            selected.add("--debug")

        def _valor_de(flag):
            """El valor ya elegido para un flag de valor, o cadena vacía."""
            for entry in selected:
                if entry.split(" ", 1)[0] == flag and " " in entry:
                    return entry.split(" ", 1)[1]
            return ""

        while True:
            console.print()
            table = Table(
                show_header=True,
                header_style="bold cyan",
                border_style="dim",
                title="[bold]Opciones adicionales[/bold]",
            )
            table.add_column("#", style="dim", justify="right")
            table.add_column("Flag")
            table.add_column("Descripción")
            table.add_column("Estado", justify="center")

            for i, (flag, desc, kind) in enumerate(flags, 1):
                is_debug_flag = flag == "--debug"
                if kind == "valor":
                    valor = _valor_de(flag)
                    estado = f"[green]{valor}[/green]" if valor else "[dim]✗[/dim]"
                elif is_debug_flag and debug:
                    estado = "[green]✓ (bloqueado)[/green]"
                elif flag in selected:
                    estado = "[green]✓[/green]"
                else:
                    estado = "[dim]✗[/dim]"
                table.add_row(str(i), f"[yellow]{flag}[/yellow]", desc, estado)

            console.print(table)
            console.print(
                "[dim]Escribe el número para activar/desactivar. "
                "[bold]c[/bold] para añadir --category, "
                "[bold]t[/bold] para añadir --type, "
                "[bold]ok[/bold] para continuar.[/dim]"
            )
            raw = Prompt.ask("[bold]Opción[/bold]", default="ok").strip().lower()

            if raw == "ok":
                break
            elif raw == "c":
                cat = Prompt.ask(
                    "Categoría",
                    choices=["movie", "tv", "fanres", "book", "audiobook", "game"],
                    default="tv",
                )
                selected_cats = {f for f in selected if f.startswith("--category")}
                selected -= selected_cats
                selected.add(f"--category {cat}")
            elif raw == "t":
                tipo = Prompt.ask(
                    "Tipo",
                    choices=["disc", "remux", "encode", "webdl", "webrip", "hdtv"],
                    default="encode",
                )
                selected_types = {f for f in selected if f.startswith("--type")}
                selected -= selected_types
                selected.add(f"--type {tipo}")
            else:
                try:
                    idx = int(raw)
                except ValueError:
                    console.print("[bold red]❌ Entrada no válida.[/bold red]")
                    continue

                if not (1 <= idx <= len(flags)):
                    console.print("[bold red]❌ Número fuera de rango.[/bold red]")
                    continue

                flag, _, kind = flags[idx - 1]
                if flag == "--debug" and debug:
                    console.print("[bold yellow]⚠️  El flag --debug está bloqueado en modo debug.[/bold yellow]")
                    continue

                if kind == "valor":
                    # Dejarlo en blanco lo quita, que es la única forma de
                    # deshacer un id tecleado mal sin salirse del menú.
                    valor = Prompt.ask(f"Valor para {flag}",
                                       default=_valor_de(flag)).strip()
                    selected -= {e for e in selected
                                 if e.split(" ", 1)[0] == flag}
                    if valor:
                        selected.add(f"{flag} {valor}")
                    continue

                if flag in selected:
                    selected.discard(flag)
                else:
                    selected.add(flag)

        result = []
        for flag in selected:
            parts = flag.split()
            result.extend(parts)
        return result

    def _flujo_ruta(self, tracker: str, debug: bool = False) -> None:
        """Sub-flujo: subir desde una ruta de archivo o carpeta"""
        while True:
            ruta_raw = Prompt.ask("[bold]Ruta al archivo o carpeta[/bold]").strip()
            ruta = Path(ruta_raw)
            if ruta.exists():
                break
            console.print(f"[bold red]❌ No existe: {ruta_raw}[/bold red]")

        # Este bloque sólo sabía de MKV y avisaba de "directorio sin archivos
        # MKV" ante 77 juegos de ScummVM perfectamente subibles.
        #
        # El conteo lo hace `library`, no este fichero: es el mismo que usa la
        # cola de subida, así que lo que aquí se anuncia y lo que allí se
        # encola no pueden discrepar. Contar por nuestra cuenta ya dio un
        # falso positivo -- una carpeta de películas con un caratulas.zip
        # salía como "1 de vídeo, 1 de juego".
        from src import library as _lib

        declarado = []

        if ruta.is_file():
            # `classify_path` ABRE los envases; `classify` sólo miraba el
            # nombre. Con el nombre a secas, un `.iso` o un `.zip` salían como
            # "extensión no reconocida" -- que es lo que vio quien intentó subir
            # "Command and Conquer Red Alert 3.iso" -- y el aviso amarillo no
            # ofrecía nada: la pregunta de más abajo sólo salta con MEZCLA, y un
            # fichero sin clasificar no es una mezcla, es cero tipos.
            kind, motivo = _lib.classify_path(str(ruta))
            hallado = [(kind, 1)] if kind else []
            if kind:
                console.print(f"[bold green]✅ {_lib.LABELS[kind].capitalize()} detectado:"
                              f"[/bold green] {ruta.name}  [dim]({motivo})[/dim]")
            else:
                console.print(f"[bold yellow]⚠️  No sé qué es[/bold yellow] {ruta.name}")
                console.print(f"   [dim]{motivo}[/dim]")
                declarado = self._declarar_categoria()
        else:
            encontrado = _lib.scan(str(ruta))
            hallado = _lib.counts(encontrado)
            if hallado:
                console.print(f"[bold green]✅ Directorio con "
                              f"{_lib.describe(encontrado)}.[/bold green]")
            else:
                console.print(
                    "[bold yellow]⚠️  Directorio sin nada que RawLoadrr sepa subir "
                    "(vídeo, e-books, audiolibros ni juegos) — se intentará de "
                    "todas formas.[/bold yellow]"
                )

        only = self._elegir_tipos(ruta, hallado)

        flags = self._args_opcionales(debug=debug)

        cmd = ["python3", "upload.py", "--tracker", tracker, "--input", str(ruta)] + only + declarado + flags

        # LÓGICA DE CONTINGENCIA: Si el cliente está caído, no intentes añadir el torrent.
        # El .torrent generado se quedará en su carpeta tmp/<uuid>/
        if self.client is None and '--no-seed' not in cmd:
            cmd.append('--no-seed')
            console.print("[bold yellow]⚠️  INFO: Cliente torrent no disponible. Se activará --no-seed automáticamente.[/bold yellow]")

        cmd_str = " ".join(cmd)
        console.print()

        # ── FASE DISPARO ─────────────────────────────────────────────────── #
        console.print(Rule("[bold yellow]▸  DISPARO — Lanzando Payload[/bold yellow]", style="yellow"))
        console.print()
        console.print(
            Panel(
                f"[bold yellow]{cmd_str}[/bold yellow]",
                title="[bold]Comando a ejecutar[/bold]",
                border_style="yellow",
            )
        )

        if Confirm.ask("[bold]¿Ejecutar?[/bold]", default=True):
            update_status("RAWLOADRR", "Subida", "PROCESSING", details=f"Subiendo: {os.path.basename(ruta)}")
            self._ejecutar_comando(cmd)
            update_status("RAWLOADRR", "Subida", "COMPLETED")
            console.print()
            console.print(Rule(style="dim"))
            console.print(Panel("[bold green]🚀 Payload lanzado. Proceso completado.[/bold green]", border_style="green"))

    def _flujo_lista_existente(self, tracker: str) -> None:
        """Sub-flujo: usar lista .txt con rutas ya preparada"""
        while True:
            ruta_raw = Prompt.ask("[bold]Ruta al archivo .txt con la lista[/bold]").strip()
            ruta = Path(ruta_raw)
            if not ruta.exists():
                console.print(f"[bold red]❌ No existe: {ruta_raw}[/bold red]")
                continue
            if not ruta.is_file():
                console.print(f"[bold red]❌ No es un archivo: {ruta_raw}[/bold red]")
                continue
            lines = [l.strip() for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                console.print("[bold red]❌ El archivo está vacío.[/bold red]")
                continue
            break

        console.print(f"[bold green]✅ Lista con {len(lines)} entrada(s).[/bold green]")

        cmd = ["python3", "auto-upload.py", "--list", str(ruta.resolve()), "--tracker", tracker]

        # LÓGICA DE CONTINGENCIA
        if self.client is None and '--no-seed' not in cmd:
            cmd.append('--no-seed')
            console.print("[bold yellow]⚠️  INFO: Cliente torrent no disponible. Se activará --no-seed automáticamente.[/bold yellow]")

        cmd_str = " ".join(cmd)
        console.print()

        # ── FASE DISPARO ─────────────────────────────────────────────────── #
        console.print(Rule("[bold yellow]▸  DISPARO — Lanzando Payload[/bold yellow]", style="yellow"))
        console.print()
        console.print(
            Panel(
                f"[bold yellow]{cmd_str}[/bold yellow]",
                title="[bold]Comando a ejecutar[/bold]",
                border_style="yellow",
            )
        )

        if Confirm.ask("[bold]¿Ejecutar?[/bold]", default=True):
            self._pending_mod().clear_pending()   # fresh pending report
            update_status("RAWLOADRR", "Subida", "PROCESSING", details=f"Subiendo: {os.path.basename(ruta)}")
            self._ejecutar_comando(cmd)
            update_status("RAWLOADRR", "Subida", "COMPLETED")
            console.print()
            console.print(Rule(style="dim"))
            console.print(Panel("[bold green]🚀 Payload lanzado. Proceso completado.[/bold green]", border_style="green"))
            self._triage_pending(tracker)

    def _pending_mod(self):
        """Lazy import of id_resolver (the pending-IDs store lives in src/)."""
        import sys
        if str(self.base_dir) not in sys.path:
            sys.path.insert(0, str(self.base_dir))
        from src import id_resolver
        return id_resolver

    def _triage_pending(self, tracker: str) -> None:
        """
        End-of-list-run triage. The bulk run logs every release whose ID it
        could not confirm to the pending-IDs report; here the user resolves
        them in one batched pass. Each answer is saved to the override store
        (so the same release is never asked about again) and the resolved
        releases are re-fed for upload.
        """
        try:
            idr = self._pending_mod()
            pend = idr.load_pending()
        except Exception as e:
            console.print(f"[yellow]Triaje no disponible: {e}[/yellow]")
            return
        if not pend:
            return
        console.print()
        console.print(Rule(f"[bold yellow]▸  TRIAJE — {len(pend)} ID(s) sin "
                            f"confirmar[/bold yellow]", style="yellow"))
        if not Confirm.ask(f"[bold]¿Resolver {len(pend)} ID(s) "
                           f"conflictivo(s) ahora?[/bold]", default=True):
            console.print(f"[dim]Pendientes en: {idr.pending_path()}[/dim]")
            return

        resolved = []
        for i, item in enumerate(pend, 1):
            console.print()
            console.print(f"[bold cyan][{i}/{len(pend)}][/bold cyan] "
                          f"{item.get('title', '?')} ({item.get('year', '?')})")
            console.print(f"[dim]   {item.get('path', '')}[/dim]")
            bg = item.get('best_guess', {}) or {}
            if bg.get('tmdb') or bg.get('imdb'):
                console.print(f"   [yellow]mejor estimación:[/yellow] "
                              f"{bg.get('title', '')} tmdb={bg.get('tmdb')} "
                              f"imdb={bg.get('imdb')}")
            for c in (item.get('candidates') or [])[:8]:
                console.print(f"   [dim][{c.get('score')}] "
                              f"{str(c.get('provider', '')):8} {c.get('title')} "
                              f"({c.get('year')}) imdb={c.get('imdb')} "
                              f"tmdb={c.get('tmdb')} mal={c.get('mal')}[/dim]")
            default = ""
            if bg.get('tmdb'):
                default = (f"{(item.get('category', 'tv') or 'tv').lower()}"
                           f"/{bg['tmdb']}")
            ans = Prompt.ask("   [bold]tmdb id[/bold] (tv/123, movie/123, "
                             "vacío para saltar)", default=default).strip()
            if not ans:
                console.print("   [dim]saltado[/dim]")
                continue
            low = ans.lower()
            if low.startswith('tv/'):
                cat, tmdb_id = 'TV', ans.split('/', 1)[1].strip()
            elif low.startswith('movie/'):
                cat, tmdb_id = 'MOVIE', ans.split('/', 1)[1].strip()
            else:
                cat, tmdb_id = (item.get('category') or 'TV'), ans
            idr.save_override(item.get('override_key', ''), {
                "tmdb": tmdb_id, "imdb": bg.get('imdb', ''),
                "category": cat, "title": item.get('title', ''),
                "year": item.get('year')})
            if item.get('path'):
                resolved.append(item['path'])
            console.print("   [green]✓ guardado[/green]")

        idr.clear_pending()
        if not resolved:
            return
        tmp_list = self.base_dir / "tmp" / "triage_resolved.txt"
        tmp_list.parent.mkdir(parents=True, exist_ok=True)
        tmp_list.write_text("\n".join(resolved) + "\n", encoding="utf-8")
        console.print()
        console.print(Rule(f"[bold yellow]▸  Resubiendo {len(resolved)} "
                            f"release(s) resuelto(s)[/bold yellow]",
                            style="yellow"))
        cmd = ["python3", "auto-upload.py", "--list", str(tmp_list),
               "--tracker", tracker]
        if self.client is None:
            cmd.append('--no-seed')
        if Confirm.ask("[bold]¿Subir los releases resueltos ahora?[/bold]",
                       default=True):
            self._ejecutar_comando(cmd)
            console.print(Panel("[bold green]🚀 Releases resueltos "
                                "procesados.[/bold green]",
                                border_style="green"))

    def _flujo_triage(self, tracker: str) -> None:
        """Sub-flujo: ejecutar triage_mkv.py y luego subir las listas generadas"""
        while True:
            ruta_raw = Prompt.ask("[bold]Directorio a analizar con el triaje[/bold]").strip()
            ruta = Path(ruta_raw)
            if ruta.is_dir():
                break
            console.print(f"[bold red]❌ No es un directorio válido: {ruta_raw}[/bold red]")

        console.print()
        console.print(Rule("[bold cyan]▸  RECON — Ejecutando Triaje[/bold cyan]", style="cyan"))
        console.print(f"\n[bold cyan]▶ Ejecutando el triaje en:[/bold cyan] {ruta}")
        self._ejecutar_comando(["python3", "../extras/Triaje-mkv/triage_mkv.py", str(ruta)])

        hevc_files = sorted(self.base_dir.glob("todo-hevc-*.txt"))
        h264_files = sorted(self.base_dir.glob("sigue-h264-*.txt"))

        if not hevc_files and not h264_files:
            console.print("[bold yellow]⚠️  No se encontraron listas generadas por el triaje.[/bold yellow]")
            return

        console.print()
        if hevc_files:
            console.print(f"[bold green]✅ HEVC:[/bold green] {hevc_files[-1].name} ({sum(1 for l in hevc_files[-1].read_text().splitlines() if l.strip())} entradas)")
        if h264_files:
            console.print(f"[bold cyan]✅ H264:[/bold cyan] {h264_files[-1].name} ({sum(1 for l in h264_files[-1].read_text().splitlines() if l.strip())} entradas)")

        opciones_disp = []
        opciones_text = ""
        if hevc_files:
            opciones_disp.append("1")
            opciones_text += "  [bold cyan][1][/bold cyan]  Subir lista HEVC\n"
        if h264_files:
            opciones_disp.append("2")
            opciones_text += "  [bold cyan][2][/bold cyan]  Subir lista H264\n"
        if hevc_files and h264_files:
            opciones_disp.append("3")
            opciones_text += "  [bold cyan][3][/bold cyan]  Subir ambas listas\n"

        console.print(
            Panel(
                opciones_text,
                title="[bold]¿Qué listas subir?[/bold]",
                border_style="cyan",
            )
        )

        eleccion = Prompt.ask(
            "[bold]Elige[/bold]",
            choices=opciones_disp,
            default=opciones_disp[0],
        )

        listas_a_subir = []
        if eleccion == "1" and hevc_files:
            listas_a_subir = [hevc_files[-1]]
        elif eleccion == "2" and h264_files:
            listas_a_subir = [h264_files[-1]]
        elif eleccion == "3":
            if hevc_files:
                listas_a_subir.append(hevc_files[-1])
            if h264_files:
                listas_a_subir.append(h264_files[-1])

        for lista in listas_a_subir:
            cmd = ["python3", "auto-upload.py", "--list", str(lista), "--tracker", tracker]

            # LÓGICA DE CONTINGENCIA
            if self.client is None and '--no-seed' not in cmd:
                cmd.append('--no-seed')

            cmd_str = " ".join(cmd)
            console.print()

            # ── FASE DISPARO ─────────────────────────────────────────────── #
            console.print(Rule(f"[bold yellow]▸  DISPARO — Lista: {lista.name}[/bold yellow]", style="yellow"))
            console.print()
            console.print(
                Panel(
                    f"[bold yellow]{cmd_str}[/bold yellow]",
                    title=f"[bold]Lista: {lista.name}[/bold]",
                    border_style="yellow",
                )
            )
            if Confirm.ask("[bold]¿Ejecutar?[/bold]", default=True):
                if self.client is None:
                    console.print("[bold yellow]⚠️  INFO: Cliente torrent no disponible. Se activará --no-seed automáticamente.[/bold yellow]")
                self._ejecutar_comando(cmd)
                console.print()
                console.print(Rule(style="dim"))
                console.print(Panel(f"[bold green]🚀 Lista {lista.name} lanzada.[/bold green]", border_style="green"))

    # ------------------------------------------------------------------ #
    #  Helpers de configuración (Options 2 & 3)                          #
    # ------------------------------------------------------------------ #

    def _leer_config(self) -> Optional[dict]:
        """Carga data/config.py y devuelve el dict config, o None si no existe"""
        config_path = self.base_dir / "data" / "config.py"
        if not config_path.exists():
            return None
        try:
            import data.config as cfg_mod
            importlib.reload(cfg_mod)
            return cfg_mod.config
        except Exception as e:
            console.print(f"[bold red]❌ Error al leer config.py: {e}[/bold red]")
            return None

    def _persistir_clave_default(self, campo: str, valor: str) -> bool:
        """Escribe UNA clave de DEFAULT en config.py sin reconstruir el fichero.

        Por qué no vale `_guardar_config()` aquí: ese método regenera config.py
        entero desde `pformat(self.config)`, y eso **borra todos los
        comentarios**. En el config.py de producción hay 48 líneas de notas
        escritas a mano sobre la cascada de hosts de imágenes —por qué imgbox y
        pixhost están fuera, qué devolvió cada host en la última prueba en
        vivo— que no viven en ningún otro sitio. Perderlas por rellenar una
        clave no compensa.

        Es la misma idea que `ensure_env_keys()` en Mass Edition: añadir sólo
        lo que falta y no tocar nada más.

        Devuelve True si lo escribió. Si no puede hacerlo con garantías
        devuelve False sin tocar el fichero, y el llamante decide qué hacer;
        nunca lo deja a medias.

        Limitación conocida: un `'DEFAULT': {}` literalmente vacío y todo en
        una línea no se puede ampliar por aquí. Falla en seguro --devuelve
        False y no toca nada-- y no se ha cubierto porque config.py.example
        trae cuarenta claves, así que esa forma no se da en la práctica.
        """
        ruta = self.base_dir / "data" / "config.py"

        try:
            original = ruta.read_text(encoding="utf-8")
        except OSError as e:
            self._logger.error(f"No se pudo leer config.py: {e}")
            return False

        literal = repr(str(valor))
        lineas = original.splitlines(keepends=True)
        nuevo = None

        # 1) El campo ya existe: se sustituye SÓLO su línea, conservando sangría
        #    y la coma final. Se exige que la línea termine en coma para no
        #    comerse un cierre de llave que compartiera línea.
        patron_campo = re.compile(r"^(\s*)['\"]" + re.escape(campo) + r"['\"]\s*:\s*.*,\s*$")

        for i, linea in enumerate(lineas):
            m = patron_campo.match(linea)

            if m:
                copia = list(lineas)
                copia[i] = f"{m.group(1)}'{campo}': {literal},\n"
                nuevo = "".join(copia)
                break

        # 2) No existe: se inserta justo después de la apertura de DEFAULT. La
        #    sangría dentro de un literal es libre, así que basta con una línea
        #    propia; la verificación de abajo confirma que quedó bien.
        if nuevo is None:
            # `search`, no `match`: pformat deja la apertura pegada a lo
            # anterior ("config = {'AUTO': {...}, 'DEFAULT': {'add_logo': ...")
            # y anclar al principio de línea no la encontraba.
            patron_default = re.compile(r"['\"]DEFAULT['\"]\s*:\s*\{")
            patron_hermano = re.compile(r"^(\s*)['\"][A-Za-z_][\w]*['\"]\s*:")

            for i, linea in enumerate(lineas):
                if not patron_default.search(linea):
                    continue

                # La sangría se copia de la clave hermana de debajo, no se
                # calcula: pformat alinea con la llave y una sangría inventada
                # queda torcida aunque sea válida.
                sangria = None

                if i + 1 < len(lineas):
                    h = patron_hermano.match(lineas[i + 1])

                    if h:
                        sangria = h.group(1)

                if sangria is None:
                    sangria = " " * (len(linea) - len(linea.lstrip()) + 4)

                copia = list(lineas)
                copia.insert(i + 1, f"{sangria}'{campo}': {literal},\n")
                nuevo = "".join(copia)
                break

        if nuevo is None:
            self._logger.warning(f"No se encontró dónde escribir '{campo}' en config.py")
            return False

        # 3) Antes de escribir, se comprueba que el resultado sigue siendo un
        #    config válido Y que la clave quedó con el valor pedido. Si algo no
        #    cuadra, el fichero se queda como estaba.
        try:
            ns: dict = {}
            exec(compile(nuevo, str(ruta), "exec"), ns)  # noqa: S102

            if ns.get("config", {}).get("DEFAULT", {}).get(campo) != str(valor):
                raise ValueError("la clave no quedó con el valor esperado")
        except Exception as e:                                    # noqa: BLE001
            self._logger.error(f"Edición de config.py descartada ({campo}): {e}")
            return False

        try:
            # write_text trunca el fichero EN SITIO y conserva el inodo. Es
            # obligatorio: config.py se monta como fichero suelto en el
            # contenedor y sustituirlo (os.replace) rompería el bind-mount.
            ruta.write_text(nuevo, encoding="utf-8")
        except OSError as e:
            self._logger.error(f"No se pudo escribir config.py: {e}")
            return False

        self.config.setdefault("DEFAULT", {})[campo] = str(valor)
        importlib.invalidate_caches()

        return True

    def _reload_config(self) -> None:
        """Recarga la configuración desde el archivo."""
        self.config = self._leer_config() or {}

    def _guardar_config(self) -> None:
        """Guarda el diccionario de configuración actual en data/config.py."""
        config_path = self.base_dir / "data" / "config.py"
        try:
            # Reconstruimos el archivo de config a partir del estado actual de self.config
            content = f"config = {pformat(self.config)}\n"
            
            config_path.write_text(content, encoding="utf-8")
            self._logger.info("Configuración guardada en data/config.py")
            # Forzamos la recarga de módulos para que el próximo import vea los cambios
            importlib.invalidate_caches()
        except Exception as e:
            self._logger.error(f"No se pudo escribir en config.py: {e}")
            console.print(f"[bold red]❌ Error al guardar la configuración: {e}[/bold red]")

    def _escribir_config_tracker(self, tracker: str, field: str, value) -> bool:
        """
        Actualiza un campo de un tracker en self.config["TRACKERS"] y persiste
        el config completo vía _guardar_config(). Devuelve True si tuvo éxito.
        """
        trackers = self.config.get("TRACKERS")
        if not isinstance(trackers, dict) or tracker not in trackers:
            console.print(f"[bold yellow]⚠️  Tracker '{tracker}' no existe en TRACKERS.[/bold yellow]")
            return False

        block = trackers[tracker]
        if not isinstance(block, dict) or field not in block:
            console.print(f"[bold yellow]⚠️  No se encontró el campo '{field}' en el bloque '{tracker}'. Nada cambió.[/bold yellow]")
            return False

        block[field] = value
        self._guardar_config()
        return True

    # ------------------------------------------------------------------ #
    #  Opción 2 — Configurar tracker existente                            #
    # ------------------------------------------------------------------ #

    def opcion_2_configurar(self, preselected_tracker: Optional[str] = None) -> None:
        """Configura api_key, announce_url y anon de un tracker en data/config.py"""
        config_path = self.base_dir / "data" / "config.py"
        if not config_path.exists():
            console.print(
                Panel(
                    "[red]No se encontró [bold]data/config.py[/bold].\n"
                    "Crea el archivo copiando [bold]data/backup/example_config.py[/bold] "
                    "a [bold]data/config.py[/bold] y vuelve a intentarlo.[/red]",
                    title="[bold red]Sin configuración[/bold red]",
                    border_style="red",
                )
            )
            return

        tracker = self._seleccionar_tracker(preselected=preselected_tracker)
        if tracker is None:
            return

        if not self.config:
            return

        tracker_cfg = self.config.get("TRACKERS", {}).get(tracker)
        if tracker_cfg is None:
            console.print(f"[bold red]❌ Tracker '{tracker}' no encontrado en config.py[/bold red]")
            return

        def _show_status(tcfg: dict) -> tuple[bool, bool]:
            api_key = tcfg.get("api_key", "")
            announce_url = tcfg.get("announce_url", "")
            anon = tcfg.get("anon", False)

            api_ok = bool(api_key) and "API_KEY" not in api_key and len(api_key) >= 32
            announce_ok = (
                bool(announce_url)
                and "Custom_Announce_URL" not in announce_url
                and "YOUR_PASSKEY" not in announce_url
                and announce_url.startswith("https://")
            )

            tbl = Table(
                show_header=True,
                header_style="bold cyan",
                border_style="dim",
                title=f"[bold]Estado de configuración: {tracker}[/bold]",
            )
            tbl.add_column("Campo", style="bold")
            tbl.add_column("Valor actual")
            tbl.add_column("Estado", justify="center")

            api_display = (
                f"{api_key[:8]}...{api_key[-4:]}"
                if api_key and len(api_key) > 12
                else api_key or "[dim]vacío[/dim]"
            )
            tbl.add_row(
                "api_key",
                api_display,
                "[green]✓[/green]" if api_ok else "[red]✗[/red]",
            )

            announce_display = announce_url if announce_url else "[dim]vacío[/dim]"
            tbl.add_row(
                "announce_url",
                announce_display,
                "[green]✓[/green]" if announce_ok else "[red]✗[/red]",
            )

            tbl.add_row(
                "anon",
                "[green]True[/green]" if anon else "[red]False[/red]",
                "[dim]—[/dim]",
            )
            console.print(tbl)
            return api_ok, announce_ok

        api_ok, announce_ok = _show_status(tracker_cfg)

        if not api_ok:
            console.print()
            console.print("[bold yellow]La API key no está configurada o es inválida.[/bold yellow]")
            while True:
                nueva_key = Prompt.ask(
                    f"[bold]API key para {tracker}[/bold] [dim](mín. 32 caracteres)[/dim]"
                ).strip()
                if len(nueva_key) < 32:
                    console.print(f"[bold red]❌ Demasiado corta ({len(nueva_key)} caracteres). Mínimo 32.[/bold red]")
                    continue
                if "API_KEY" in nueva_key:
                    console.print("[bold red]❌ El valor contiene 'API_KEY', introduce la clave real.[/bold red]")
                    continue
                break
            if self._escribir_config_tracker(tracker, "api_key", nueva_key):
                console.print("[bold green]✅ API key guardada.[/bold green]")
                self._reload_config()

        if not announce_ok:
            console.print()
            console.print("[bold yellow]La announce URL no está configurada o es inválida.[/bold yellow]")
            while True:
                nueva_url = Prompt.ask(
                    f"[bold]Announce URL para {tracker}[/bold] [dim](https://...)[/dim]"
                ).strip()
                if not nueva_url.startswith("https://"):
                    console.print("[bold red]❌ Debe empezar por 'https://'.[/bold red]")
                    continue
                if "Custom_Announce_URL" in nueva_url or "YOUR_PASSKEY" in nueva_url:
                    console.print("[bold red]❌ El valor sigue siendo el de ejemplo, introduce la URL real.[/bold red]")
                    continue
                break
            if self._escribir_config_tracker(tracker, "announce_url", nueva_url):
                console.print("[bold green]✅ Announce URL guardada.[/bold green]")
                self._reload_config()

        console.print()
        anon_actual = tracker_cfg.get("anon", False)
        cambiar_anon = Confirm.ask(
            f"[bold]¿Cambiar el modo anónimo?[/bold] [dim](actualmente: {'activado' if anon_actual else 'desactivado'})[/dim]",
            default=False,
        )
        if cambiar_anon:
            nuevo_anon = Confirm.ask("[bold]¿Activar modo anónimo?[/bold]", default=anon_actual)
            if self._escribir_config_tracker(tracker, "anon", nuevo_anon):
                console.print(f"[bold green]✅ Anon → {'True' if nuevo_anon else 'False'}[/bold green]")
                self._reload_config()

        # ── CONFIGURACIÓN DE FIRMAS ───────────────────────────────────────── #
        console.print()
        if Confirm.ask("[bold]¿Configurar firmas (signatures)?[/bold]", default=False):
            sig_fields = [
                ("signature", "Firma Normal"),
                ("anon_signature", "Firma Anónima"),
                ("pr_signature", "Firma Personal Release"),
                ("anon_pr_signature", "Firma Anon PR")
            ]
            
            for field, label in sig_fields:
                # Recargamos cfg por si hubo cambios en la iteración anterior
                t_cfg = self.config.get("TRACKERS", {}).get(tracker, {})
                current_val = t_cfg.get(field, "")
                
                console.print(f"\n[cyan]{label} ({field}):[/cyan]")
                console.print(Panel(current_val or "[dim]Vacío[/dim]", border_style="dim", expand=False))
                
                if Confirm.ask(f"¿Editar {label}?", default=False):
                    console.print("[dim]Introduce la nueva firma. Usa [bold]\\n[/bold] para saltos de línea.[/dim]")
                    new_val = Prompt.ask(f"Nueva {field}", default=current_val)
                    if self._escribir_config_tracker(tracker, field, new_val):
                        console.print(f"[bold green]✅ {field} actualizado.[/bold green]")
                        self._reload_config()

        # ── CONFIGURACIÓN DE IMÁGENES (GLOBAL) ────────────────────────────── #
        console.print()
        if Confirm.ask("[bold]¿Configurar opciones de imágenes (Global)?[/bold]", default=False):
            default_cfg = self.config.get("DEFAULT", {})
            curr_screens = default_cfg.get("screens", "4")
            curr_size = default_cfg.get("img_size", "500")

            console.print(f"\n[cyan]Capturas (global):[/cyan] {curr_screens}")
            console.print(f"[cyan]Tamaño de imagen (global):[/cyan] {curr_size}")

            if Confirm.ask("¿Editar valores?", default=False):
                new_screens = Prompt.ask("Número de capturas", default=str(curr_screens))
                new_size = Prompt.ask("Tamaño de capturas (px)", default=str(curr_size))

                if "DEFAULT" not in self.config:
                    self.config["DEFAULT"] = {}
                self.config["DEFAULT"]["screens"] = new_screens
                self.config["DEFAULT"]["img_size"] = new_size
                self._guardar_config()
                console.print("[bold green]✅ Configuración global de imágenes actualizada.[/bold green]")
                self._reload_config()

        if self.config:
            tracker_cfg_final = self.config.get("TRACKERS", {}).get(tracker, {})
            console.print()
            _show_status(tracker_cfg_final)

        console.print()
        if Confirm.ask(
            f"[bold]¿Continuar a la opción 1 con el tracker {tracker}?[/bold]",
            default=True,
        ):
            self.opcion_1_usar_tracker(preselected_tracker=tracker)

    # ------------------------------------------------------------------ #
    #  Opción 3 — Crear nuevo tracker                                     #
    # ------------------------------------------------------------------ #

    def opcion_3_crear_tracker(self) -> None:
        """Crea un nuevo tracker basado en la plantilla MILNU"""
        console.print()
        console.print(
            Panel(
                "\n"
                "  Crea un nuevo tracker usando [bold cyan]MILNU[/bold cyan] como plantilla.\n"
                "  Se crearán los archivos necesarios y se configurará el sistema.\n",
                title="[bold cyan]Crear nuevo tracker[/bold cyan]",
                border_style="cyan",
            )
        )

# --- ADUANA DE SOBERANÍA (Check de Fresh Install) ---
        if not self.config:
            console.print("\n[bold red]❌ No se pudo leer el archivo de configuración.[/bold red]")
            console.print("[bold yellow]⚠️  Asegúrate de que 'data/config.py' existe y es válido.[/bold yellow]")
        else:
            # Evitamos el hostión en upload.py asegurando las APIs globales
            master_keys = {
                "tmdb_api": "TMDB_API_KEY",
                "imdb_api": "IMDB_API_KEY",
                "imgbb_api": "IMGBB_API_KEY",
                "ptscreens_api": "PTSCREENS_API_KEY",
                "ptpimg_api": "PTPIMG_API_KEY",
                "lensdump_api": "LENSDUMP_API_KEY",
                "oeimg_api": "OEIMG_API_KEY",
            }
            default_config = self.config.get("DEFAULT", {})
            for python_key, env_key in master_keys.items():
                val = default_config.get(python_key, "")
                
                # Heurística mejorada para detectar cualquier placeholder
                if not val or val.startswith("YOUR_") or val in ["tu_clave_tmdb_aqui", "CAMBIAME"]:
                    console.print(f"\n[bold red]✖ {env_key} no configurada.[/bold red]")
                    msg = f"[bold cyan]▶ Introduce el valor para {env_key}[/bold cyan]" + ("[dim] (Enter para saltar)[/dim]" if "tmdb" not in python_key else "")
                    new_val = Prompt.ask(msg)
                    
                    if new_val.strip():
                        # Sincronización transversal usando el multiplexor
                        self._sync_global_secret(python_key, new_val.strip())
                        # Actualización en caliente de la config en memoria
                        if "DEFAULT" not in self.config:
                            self.config["DEFAULT"] = {}
                        self.config["DEFAULT"][python_key] = new_val.strip()
        # ----------------------------------------------------
        nombre_completo = Prompt.ask(
            "[bold]Nombre completo del tracker[/bold] [dim](ej: MilNueve)[/dim]"
        ).strip()
        if not nombre_completo:
            console.print("[bold red]❌ El nombre no puede estar vacío.[/bold red]")
            return

        while True:
            abrev_raw = Prompt.ask(
                "[bold]Abreviación del tracker[/bold] [dim](ej: MILNU — se fuerza a mayúsculas)[/dim]"
            ).strip()
            abrev = abrev_raw.upper()
            if not abrev:
                console.print("[bold red]❌ La abreviación no puede estar vacía.[/bold red]")
                continue
            if not re.match(r'^[A-Z0-9]+$', abrev):
                console.print("[bold red]❌ Solo letras mayúsculas y números.[/bold red]")
                continue
            existentes = self._get_tracker_names_from_upload()
            if abrev in existentes:
                console.print(f"[bold red]❌ '{abrev}' ya existe en upload.py. Elige otra.[/bold red]")
                continue
            break

        while True:
            base_url = Prompt.ask(
                "[bold]URL base del tracker[/bold] [dim](ej: https://mitracker.ejemplo.es)[/dim]"
            ).strip()
            if not base_url.startswith("https://") and not base_url.startswith("http://"):
                console.print("[bold red]❌ La URL debe empezar por https:// o http://[/bold red]")
                continue
            base_url = base_url.rstrip("/")
            break

        console.print()
        console.print(Rule("[bold cyan]▸  RECON — Configuración Inicial[/bold cyan]", style="cyan"))
        console.print()

        api_key_inicial = ""
        while True:
            val = Prompt.ask(
                "  [bold]API Key[/bold] [dim](deja en blanco para configurar después)[/dim]",
                default="",
            ).strip()
            if val and (len(val) < 32 or "API_KEY" in val.upper()):
                console.print("[bold red]❌ La API Key parece inválida (mínimo 32 caracteres).[/bold red]")
                continue
            api_key_inicial = val
            break

        announce_url_inicial = ""
        while True:
            val = Prompt.ask(
                "  [bold]Announce URL[/bold] [dim](deja en blanco para configurar después)[/dim]",
                default="",
            ).strip()
            if val and (not val.startswith("https://") and not val.startswith("http://")):
                console.print("[bold red]❌ La announce URL debe empezar por https:// o http://[/bold red]")
                continue
            announce_url_inicial = val
            break

        tracker_py = self.base_dir / "src" / "trackers" / f"{abrev}.py"
        milnu_py = self.base_dir / "src" / "trackers" / "MILNU.py"
        upload_py = self.base_dir / "upload.py"
        config_py = self.base_dir / "data" / "config.py"

        errores = []

        if not milnu_py.exists():
            console.print("[bold red]❌ No se encontró la plantilla MILNU.py[/bold red]")
            return

        api_display = api_key_inicial[:8] + "..." if api_key_inicial else "[dim]pendiente[/dim]"
        ann_display = announce_url_inicial if announce_url_inicial else "[dim]pendiente[/dim]"
        console.print()
        console.print(
            Panel(
                f"  Tracker:      [bold]{nombre_completo}[/bold]\n"
                f"  Abrev:        [bold cyan]{abrev}[/bold cyan]\n"
                f"  URL base:     [bold]{base_url}[/bold]\n"
                f"  API Key:      {api_display}\n"
                f"  Announce URL: {ann_display}\n"
                f"  Archivo:      [dim]src/trackers/{abrev}.py[/dim]",
                title="[bold]Resumen de creación[/bold]",
                border_style="blue",
            )
        )

        if not Confirm.ask("[bold]¿Confirmar creación?[/bold]", default=True):
            console.print("[bold yellow]⚠️  Operación cancelada.[/bold yellow]")
            return

        milnu_text = milnu_py.read_text(encoding="utf-8")
        nuevo_text = milnu_text
        nuevo_text = nuevo_text.replace(f"class MILNU(", f"class {abrev}(")
        nuevo_text = nuevo_text.replace(f"self.tracker = 'MILNU'", f"self.tracker = '{abrev}'")
        nuevo_text = nuevo_text.replace(
            f"self.source_flag = 'Milnueve'",
            f"self.source_flag = '{nombre_completo}'"
        )
        nuevo_text = nuevo_text.replace(
            "https://milnueve.neklair.es/api/torrents/upload",
            f"{base_url}/api/torrents/upload",
        )
        nuevo_text = nuevo_text.replace(
            "https://milnueve.neklair.es/api/torrents/filter",
            f"{base_url}/api/torrents/filter",
        )
        nuevo_text = re.sub(r'\bMILNU\b', abrev, nuevo_text)
        nuevo_text = re.sub(r'\bMilnueve\b', nombre_completo, nuevo_text)
        nuevo_text = re.sub(r'\bmilnu_name\b', f'{abrev.lower()}_name', nuevo_text)

        try:
            tracker_py.write_text(nuevo_text, encoding="utf-8")
            console.print(f"[bold green]✅ Creado:[/bold green] src/trackers/{abrev}.py")
        except Exception as e:
            console.print(f"[bold red]❌ No se pudo crear el archivo del tracker: {e}[/bold red]")
            errores.append(f"tracker .py: {e}")

        cfg_api = api_key_inicial if api_key_inicial else f"{abrev}_API_KEY"
        cfg_announce = announce_url_inicial if announce_url_inicial else f"{base_url}/announce/Custom_Announce_URL"

        if config_py.exists():
            try:
                cfg_text = config_py.read_text(encoding="utf-8")
                sig = f"\\n[center][b]PLEASE SEED {nombre_completo.upper()} FAMILY[/b][/center]\\n[center][url=https://codeberg.org/CvT/Uploadrr][img=400]https://i.ibb.co/2NVWb0c/uploadrr.webp[/img][/url][/center]"
                anon_sig = "\\n[center][url=https://codeberg.org/CvT/Uploadrr][img=40]https://i.ibb.co/n0jF73x/hacker.png[/img][/url][/center]"
                pr_sig = f"\\n [center][b][size=6]PERSONAL RELEASE[/size][/b][/center] \\n[center][b]PLEASE SEED {nombre_completo.upper()} FAMILY[/b][/center]\\n[center][url=https://codeberg.org/CvT/Uploadrr][img=400]https://i.ibb.co/2NVWb0c/uploadrr.webp[/img][/url][/center]"
                nuevo_bloque = (
                    f',\n\n    "{abrev}" : {{\n'
                    f'            "api_key" : "{cfg_api}",\n'
                    f'            "announce_url" : "{cfg_announce}",\n'
                    f'            "anon" : False,\n'
                    f'            "signature": "{sig}",\n'
                    f'            "anon_signature": "{anon_sig}",\n'
                    f'            "pr_signature": "{pr_sig}",\n'
                    f'            "anon_pr_signature": "{anon_sig}"\n'
                    f'}}'
                )
                trackers_match = re.search(r'(?:"|\')TRACKERS(?:"|\')\s*:\s*\{', cfg_text)
                if trackers_match:
                    # Find closing } of the TRACKERS dict using brace counting
                    depth = 1
                    pos = trackers_match.end()
                    insert_pos = -1
                    while pos < len(cfg_text) and depth > 0:
                        ch = cfg_text[pos]
                        if ch == '{':
                            depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                insert_pos = pos
                        pos += 1
                    if insert_pos != -1:
                        cfg_text = cfg_text[:insert_pos] + nuevo_bloque + "\n" + cfg_text[insert_pos:]
                        config_py.write_text(cfg_text, encoding="utf-8")
                        # ... después de config_py.write_text(cfg_text, encoding="utf-8")
                        self.config = self._leer_config() or {}  # <--- INYECCIÓN DE REALIDAD: Recarga el dict en RAM
                        console.print(f"[bold green]✅ Configuración recargada en memoria.[/bold green]")
                        console.print(f"[bold green]✅ Bloque añadido a data/config.py[/bold green]")
                        importlib.invalidate_caches()
                    else:
                        console.print("[bold yellow]⚠️  No se encontró el cierre del dict TRACKERS en config.py[/bold yellow]")
                        errores.append("config.py: no se encontró cierre de TRACKERS")
                else:
                    console.print("[bold yellow]⚠️  No se encontró 'TRACKERS' en config.py[/bold yellow]")
                    errores.append("config.py: no se encontró TRACKERS")
            except Exception as e:
                console.print(f"[bold red]❌ Error al modificar config.py: {e}[/bold red]")
                errores.append(f"config.py: {e}")
        else:
            console.print(
                "[bold yellow]⚠️  data/config.py no existe — añade el bloque manualmente:[/bold yellow]\n"
                f'    "{abrev}" : {{\n'
                f'            "api_key" : "{cfg_api}",\n'
                f'            "announce_url" : "{cfg_announce}",\n'
                f'            "anon" : False\n'
                f'}}'
            )

# --- NUEVA LÓGICA DINÁMICA (Adiós al Regex de 2024) ---
        registry_path = self.base_dir / "src" / "trackers" / "trackers_registry.json"
        
        if registry_path.exists():
            try:
                import json
                with open(registry_path, 'r', encoding='utf-8') as f:
                    registry = json.load(f)
                
                # Registramos el nuevo tracker como 'api' (o el tipo que elijas)
                registry[abrev] = "api"
                
                with open(registry_path, 'w', encoding='utf-8') as f:
                    json.dump(registry, f, indent=4)
                
                console.print(f"[bold green]✅ '{abrev}' registrado en trackers_registry.json[/bold green]")
            except Exception as e:
                console.print(f"[bold red]❌ Error al actualizar trackers_registry.json: {e}[/bold red]")
                errores.append(f"registry.json: {e}")
        else:
            console.print("[bold yellow]⚠️  trackers_registry.json no encontrado[/bold yellow]")
            errores.append("registry.json no encontrado")

        # ── PANEL FINAL DE RESULTADOS ────────────────────────────────────── #
        console.print()
        console.print(Rule(style="dim"))
        if errores:
            console.print(
                Panel(
                    "\n".join(f"  [bold red]❌[/bold red] {e}" for e in errores),
                    title="[bold red]Errores durante la creación[/bold red]",
                    border_style="red",
                )
            )
        else:
            console.print(
                Panel(
                    f"  [bold green]✅[/bold green] src/trackers/{abrev}.py creado\n"
                    f"  [bold green]✅[/bold green] data/config.py actualizado\n"
                    f"  [bold green]✅[/bold green] trackers_registry.json actualizado\n",
                    title=f"[bold green]🚀 Tracker '{abrev}' creado con éxito[/bold green]",
                    border_style="green",
                )
            )

        console.print()
        console.print(
            Panel(
                "\n"
                "  [bold yellow]INFORMACIÓN SOBRE TOR:[/bold yellow]\n\n"
                "  Algunos ISP (especialmente en España) bloquean conexiones a ciertos trackers.\n"
                "  Cloudflare puede estar bloqueado durante eventos deportivos.\n"
                "  [bold]Se recomienda configurar Tor[/bold] para mayor estabilidad.\n"
                "  El script [cyan]setup_milnueve.sh[/cyan] instala y configura Tor automáticamente.\n",
                title="[bold yellow]Tor — Estabilidad de conexión[/bold yellow]",
                border_style="yellow",
            )
        )

        setup_sh = self.base_dir / "setup_milnueve.sh"
        if setup_sh.exists():
            if Confirm.ask("[bold]¿Configurar Tor ahora?[/bold]", default=False):
                subprocess.run(["bash", str(setup_sh)])
        else:
            console.print("[dim]setup_milnueve.sh no encontrado — configura Tor manualmente si es necesario.[/dim]")

        console.print()
        if Confirm.ask(
            f"[bold]¿Continuar a configurar el tracker '{abrev}'?[/bold]",
            default=True,
        ):
            self.opcion_2_configurar(preselected_tracker=abrev)

    # ------------------------------------------------------------------ #
    #  Opción 4 — Modo debug (stub)                                       #
    # ------------------------------------------------------------------ #

    def opcion_4_debug(self) -> None:
        """Modo debug: lanza upload.py con --debug (sin subida real), log completo"""
        console.print()
        console.print(
            Panel(
                "\n"
                "  [bold red]ATENCIÓN:[/bold red] En este modo los torrents "
                "[bold]NO se subirán[/bold] al tracker.\n"
                "  Se activará el log completo en pantalla y en archivo.\n",
                title="[bold red]Modo Debug Activo[/bold red]",
                border_style="red",
            )
        )

        tracker = self._seleccionar_tracker()
        if tracker is None:
            return

        self._logger.info(f"Debug mode started for tracker: {tracker}")
        self._flujo_ruta(tracker, debug=True)

        session_log = self.base_dir / "logs" / f"rawncher_{datetime.now().strftime('%Y-%m-%d')}.log"
        console.print()
        console.print(
            Panel(
                f"[bold]Session log:[/bold] [cyan]{session_log}[/cyan]",
                title="[bold green]Sesión de debug completada[/bold green]",
                border_style="green",
            )
        )
        self._logger.info(f"Debug mode completed for tracker: {tracker}")

    # ------------------------------------------------------------------ #
    #  Opción 5 — Listar sesiones guardadas                               #
    # ------------------------------------------------------------------ #

    def opcion_5_listar_sesiones(self) -> None:
        """Lista todas las sesiones guardadas en una tabla Rich"""
        sessions = self._get_sessions()

        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            title="[bold]Sesiones guardadas[/bold]",
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("Archivo")
        table.add_column("Tracker", style="bold")
        table.add_column("Tamaño", justify="right")

        if not sessions:
            table.add_row("-", "[dim]Sin sesiones[/dim]", "", "")
        else:
            for i, path in enumerate(sessions, 1):
                tracker_name = "-"
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    tracker_name = data.get("name", "-")
                except Exception:
                    pass
                size = f"{path.stat().st_size} B"
                table.add_row(str(i), path.name, tracker_name, size)

        console.print(table)

    # ------------------------------------------------------------------ #
    #  Opción 6 — Cargar sesión guardada                                  #
    # ------------------------------------------------------------------ #

    def opcion_6_cargar_sesion(self) -> None:
        """Carga una sesión guardada y lanza la opción 1 con ese tracker"""
        sessions = self._get_sessions()

        if not sessions:
            console.print("[bold yellow]⚠️  No hay sesiones guardadas.[/bold yellow]")
            return

        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            title="[bold]Sesiones guardadas[/bold]",
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("Archivo")
        table.add_column("Tracker", style="bold")
        table.add_column("Tamaño", justify="right")

        sessions_data = []
        for i, path in enumerate(sessions, 1):
            tracker_name = "-"
            session_dict = None
            try:
                session_dict = json.loads(path.read_text(encoding="utf-8"))
                tracker_name = session_dict.get("name", "-")
            except Exception:
                pass
            size = f"{path.stat().st_size} B"
            table.add_row(str(i), path.name, tracker_name, size)
            sessions_data.append(session_dict)

        console.print(table)

        raw = Prompt.ask("[bold]Número de sesión a cargar[/bold]", default="1")
        try:
            idx = int(raw)
        except ValueError:
            idx = 0

        if not (1 <= idx <= len(sessions)):
            console.print("[bold red]❌ Número no válido.[/bold red]")
            return

        session = sessions_data[idx - 1]
        if session is None:
            console.print("[bold red]❌ No se pudo leer la sesión seleccionada.[/bold red]")
            return

        tracker_name = session.get("name", "?")
        url = session.get("url", "?")
        api_key = session.get("api_key", "")
        api_display = (
            f"{api_key[:8]}..." if api_key and len(api_key) > 8 else api_key or "?"
        )
        saved_at = session.get("saved_at", "?")

        summary = (
            f"[bold]Tracker:[/bold]   {tracker_name}\n"
            f"[bold]URL:[/bold]       {url}\n"
            f"[bold]API Key:[/bold]   {api_display}\n"
            f"[bold]Origen:[/bold]    {session.get('source', '?')}\n"
            f"[bold]Guardada:[/bold]  {saved_at}"
        )
        console.print(
            Panel(
                summary,
                title="[bold green]Sesión cargada[/bold green]",
                border_style="green",
            )
        )

        self.opcion_1_usar_tracker(preselected_tracker=tracker_name)


if __name__ == "__main__":
    try:
        Rawncher().run()
    except Exception:
        console.print_exception(show_locals=True)
        sys.exit(1)
