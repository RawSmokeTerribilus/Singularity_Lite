#!/usr/bin/env python3
import os
import re
import sys
import time
import random
import logging
import importlib.util
import subprocess
import threading
import socket
import shutil
from core.status_manager import update_status, clear_all_statuses
from pathlib import Path
from datetime import datetime
from rich.console import Console

from core.status_manager import update_status
from core.dashboard import run_dashboard

# CSI Integration
sys.path.append(str(Path(__file__).parent / "extras" / "CSI"))
try:
    import csi
except ImportError:
    csi = None

from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.align import Align
from rich.rule import Rule

from singularity_config import GOD_PHRASES, MSG_NUEVO, ID_INICIO, ID_FIN, LOGS_DIR, BASE_URL, COOKIE_VALUE, IMGBB_API, PTSCREENS_API  # noqa: F401 – BASE_URL/COOKIE_VALUE/IMGBB_API/PTSCREENS_API usados en _ensure_credentials (SING_* namespace)

console = Console()

# --- TROLLING SUBSYSTEM INJECTION ---
if GOD_PHRASES:
    if not hasattr(console, 'original_print'):
        console.original_print = console.print

    def troll_print(*args, **kwargs):
        if random.random() < 0.01: # 1% de probabilidad
            phrase = random.choice(GOD_PHRASES)
            console.original_print(f"[dim italic magenta]« {phrase} »[/dim italic magenta]")
        console.original_print(*args, **kwargs)

    console.print = troll_print
# ------------------------------------
BASE_DIR = Path(__file__).parent


# --- Path setup for MKVerything ---
# This ensures that linters and IDEs can find the 'modules' package,
# which is located inside the 'MKVerything' directory.
MKVE_ROOT = BASE_DIR / "MKVerything"
if str(MKVE_ROOT) not in sys.path:
    sys.path.insert(0, str(MKVE_ROOT))

bin_dir = MKVE_ROOT / "bin" / "linux"
if bin_dir.exists():
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["LD_LIBRARY_PATH"] = str(bin_dir) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
# ------------------------------------



#redimensionar pantalla

def setup_terminal():
    """Configura el entorno visual de la terminal."""
    try:
        # 1. Limpiar pantalla para resetear el buffer
        os.system('clear')
        
        # 2. Redimensionar: 40 filas, 120 columnas
        # \x1b[8;{rows};{cols}t -> Redimensionar
        # \x1b[3;0;0t         -> Mover a la posición 0,0 (opcional)
        sys.stdout.write("\x1b[8;40;120t")
        sys.stdout.write("\x1b[3;0;0t") 
        sys.stdout.flush()
        
        # 3. Poner título a la ventana (aunque estés en Docker, el TTY lo propaga)
        sys.stdout.write("\033]0;S I N G U L A R I T Y   C O R E\007")
        sys.stdout.flush()
        
    except Exception:
        # Si falla (por ejemplo en un log sin TTY), que no rompa el programa
        pass

# Ejecutar la configuración visual
setup_terminal()

# ------------------------------------------------------------------ #
#  Logging (logs/singularity_YYYY-MM-DD.log)                         #
# ------------------------------------------------------------------ #

def _setup_logger() -> logging.Logger:
    log_path = Path(LOGS_DIR) / f"singularity_{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger("singularity")
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


log = _setup_logger()


# ------------------------------------------------------------------ #
#  Subprocess helper                                                  #
# ------------------------------------------------------------------ #

def _run(cmd: list, cwd: Path = None) -> int:
    cwd_path = str(cwd or BASE_DIR)
    log.info(f"RUN: {' '.join(str(c) for c in cmd)} (cwd={cwd_path})")
    result = subprocess.run(cmd, cwd=cwd_path)
    log.info(f"EXIT: {result.returncode}")
    return result.returncode


# ------------------------------------------------------------------ #
#  Boot sequence                                                      #
# ------------------------------------------------------------------ #

def boot_sequence():
    clear_screen()
    intro_art = """
    [bold cyan]
    ░██████╗██╗███╗   ██╗ ██████╗ ██╗   ██╗██╗      █████╗ ██████╗ ██╗████████╗██╗   ██╗
    ██╔════╝██║████╗  ██║██╔════╝ ██║   ██║██║     ██╔══██╗██╔══██╗██║╚══██╔══╝╚██╗ ██╔╝
    ╚█████╗ ██║██╔██╗ ██║██║  ███╗██║   ██║██║     ███████║██████╔╝██║   ██║    ╚████╔╝ 
     ╚═══██╗██║██║╚██╗██║██║   ██║██║   ██║██║     ██╔══██║██╔══██╗██║   ██║     ╚██╔╝  
    ██████╔╝██║██║ ╚████║╚██████╔╝╚██████╔╝███████╗██║  ██║██║  ██║██║   ██║      ██║   
    ╚═════╝ ╚═╝╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝   ╚═╝      ╚═╝   
    [/bold cyan]
    """
    with Live(Align.center(intro_art), refresh_per_second=4):
        time.sleep(1)

    with Progress(SpinnerColumn("dots12"), TextColumn("[bold yellow]{task.description}"), console=console) as progress:
        task = progress.add_task("Mapeando sectores de memoria...", total=100)
        while not progress.finished:
            time.sleep(0.01)
            progress.update(task, advance=2)

    log.info("Singularity boot sequence completed")
    time.sleep(0.8)


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


# ------------------------------------------------------------------ #
#  Sub-menú 1 — MKVerything                                          #
# ------------------------------------------------------------------ #

def _submenu_mkverything():
    while True:
        console.print()
        console.print(Panel(
            "\n"
            "  [bold cyan][1.1][/bold cyan]  Auditoría de campo\n"
            "  [bold cyan][1.2][/bold cyan]  Ajustes / Dependencias\n"
            "  [bold cyan][1.3][/bold cyan]  Testeo rápido de herramientas\n"
            "  [bold cyan][0][/bold cyan]    Atrás\n",
            title="[bold green]MKVerything[/bold green]",
            border_style="green",
        ))
        sub = Prompt.ask("root@singularidad:mkve", choices=["1.1", "1.2", "1.3", "0"], default="1.1")
        log.info(f"MKVerything submenu: {sub}")

        if sub == "1.1":
            _run(["python3", "MKVerything/launcher.py"])
        elif sub == "1.2":
            console.print(Panel(
                "  Dependencias binarias en Lite:\n"
                "  [cyan]ffmpeg[/cyan]  [cyan]ffprobe[/cyan]  "
                "[cyan]mkvmerge[/cyan]  [cyan]mediainfo[/cyan]\n\n"
                "  Vienen ya en la imagen (apt). No hace falta tocar nada.\n"
                "  [dim]makemkvcon no está: solo hay binarios x86_64 y Lite no ripea ISOs.[/dim]",
                title="[bold yellow]Ajustes de MKVerything[/bold yellow]",
                border_style="yellow",
            ))
            if Prompt.ask("¿Abro el lanzador ahora?", choices=["s", "n"], default="s") == "s":
                _run(["python3", "MKVerything/launcher.py"])
        elif sub == "1.3":
            console.print()
            console.print(Rule("[bold]Testeo de herramientas[/bold]"))
            all_ok = True
            for tool in ["ffmpeg", "ffprobe", "mkvmerge", "mediainfo"]:
                rc = subprocess.run(["which", tool], capture_output=True).returncode
                status = "[green]✓ Fetén[/green]" if rc == 0 else "[red]✗ Ni rastro[/red]"
                if rc != 0:
                    all_ok = False
                console.print(f"  {tool:14s} {status}")
            console.print()
            if all_ok:
                console.print("[green]✓ Todas las herramientas están a punto.[/green]")
            else:
                console.print("[yellow]⚠ Faltan herramientas. La imagen debería traerlas: reconstruye con 'make build'.[/yellow]")
            log.info(f"Tool test completed, all_ok={all_ok}")
            Prompt.ask("\nPulsa Enter para volver", default="")
        elif sub == "0":
            break


# ------------------------------------------------------------------ #
#  Sub-menú 4 — Extras                                               #
# ------------------------------------------------------------------ #

def _submenu_extras():
    while True:
        console.print()
        console.print(Panel(
            "\n"
            # Lite: sin Chaos Maker (corrompe MKVs a propósito, solo sirve para
            # probar el rescatador, que aquí no existe). CSI sube al 4.4.
            "  [bold cyan][4.1][/bold cyan]  Ingestor de Tags\n"
            "  [bold cyan][4.2][/bold cyan]  Comparador de Torrents\n"
            "  [bold cyan][4.3][/bold cyan]  Triaje MKV (HEVC vs H264)\n"
            "  [bold cyan][4.4][/bold cyan]  CSI: Check, Search, Identify\n"
            "  [bold cyan][0][/bold cyan]    Atrás\n",
            title="[bold blue]Extras[/bold blue]",
            border_style="blue",
        ))
        sub = Prompt.ask("root@singularidad:extras", choices=["4.1", "4.2", "4.3", "4.4", "0"], default="0")
        log.info(f"Extras submenu: {sub}")

        if sub == "4.1":
            script = BASE_DIR / "core" / "tag_ingestor.py"
            if script.exists():
                _run(["python3", str(script)])
            else:
                console.print("[yellow]⚠ No encuentro el script core/tag_ingestor.py.[/yellow]")
                Prompt.ask("Pulsa Enter para seguir", default="")

        elif sub == "4.2":
            script = BASE_DIR / "extras" / "torrents comparison" / "checkit.py"
            if script.exists():
                _run(["python3", str(script)], cwd=script.parent)
            else:
                console.print("[yellow]⚠ No encuentro el script extras/torrents comparison/checkit.py.[/yellow]")
                Prompt.ask("Pulsa Enter para seguir", default="")

        elif sub == "4.3":
            while True:
                path_raw = Prompt.ask("[bold]Dime qué directorio analizar[/bold]").strip()
                path = Path(path_raw)
                if path.is_dir():
                    break
                console.print(f"[red]✗ Esto no es un directorio válido: {path_raw}[/red]")
            
            # Correct path for Triage MKV script
            triage_script = "extras/Triaje-mkv/triage_mkv.py"
            _run(["python3", triage_script, str(path)])

        elif sub == "4.4":
            _run(["python3", "extras/CSI/csi.py"])

        elif sub == "0":
            break


# ------------------------------------------------------------------ #
#  Opción 3 — UNIT3D Orchestrator                                    #
# ------------------------------------------------------------------ #

def _write_env_key(key: str, value: str) -> None:
    """Persists a single key=value to .env and updates os.environ immediately."""
    env_path = BASE_DIR / ".env"
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    with open(env_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.environ[key] = value


def _me_load_config() -> dict:
    """Lee la configuración activa de Mass Edition desde las ME_* env vars."""
    return {
        "tracker_url":   os.getenv("ME_TRACKER_URL",         ""),
        "tracker_name":  os.getenv("ME_TRACKER_DEFAULT",     ""),
        "cookie_name":   os.getenv("ME_TRACKER_COOKIE_NAME", ""),
        "cookie":        os.getenv("ME_TRACKER_COOKIE",      ""),
        "username":      os.getenv("ME_TRACKER_USERNAME",    ""),
        "api_key":       os.getenv("ME_TRACKER_API_KEY",     ""),
        "imgbb_api":     os.getenv("ME_IMGBB_API",           ""),
        "ptscreens_api": os.getenv("ME_PTSCREENS_API",       ""),
        "tmp_root":      os.getenv("ME_TMP_ROOT",            str(BASE_DIR / "RawLoadrr" / "tmp")),
        # Por defecto 'undici', no un UA de navegador. Un UA de navegador aquí
        # es lo que provoca un 403 en la primera petición del scraper contra un
        # tracker cuyo WAF espera el UA del cliente. Debe cuadrar con
        # CUSTOM_USER_AGENT del .env.
        "user_agent":    os.getenv("ME_CUSTOM_USER_AGENT",   "undici"),
    }


def _me_load_rl_trackers() -> dict:
    """Carga el dict TRACKERS de RawLoadrr/data/config.py (solo lectura). Devuelve {} si falla."""
    try:
        rl_config_path = BASE_DIR / "RawLoadrr" / "data" / "config.py"
        if not rl_config_path.exists():
            return {}
        spec = importlib.util.spec_from_file_location("rl_config", rl_config_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "config", {}).get("TRACKERS", {})
    except Exception:
        return {}


def _me_check_essential_config(me_cfg: dict) -> dict:
    """Aduana de arranque: valida los campos obligatorios de Mass Edition (al estilo CSI)."""
    changed = False

    if not me_cfg.get("tracker_url"):
        console.print("\n[bold red]ME_TRACKER_URL no configurada.[/bold red]")
        val = Prompt.ask("URL base del tracker  [dim](ej: https://milnueve.cc)[/dim]").strip()
        if val:
            _write_env_key("ME_TRACKER_URL", val)
            me_cfg["tracker_url"] = val
            changed = True

    if not me_cfg.get("cookie"):
        console.print("\n[bold red]ME_TRACKER_COOKIE (sesión) no configurada.[/bold red]")
        val = Prompt.ask("Cookie de sesión", password=True).strip()
        if val:
            _write_env_key("ME_TRACKER_COOKIE", val)
            me_cfg["cookie"] = val
            changed = True

    tmp = me_cfg.get("tmp_root", "")
    if not tmp or not os.path.exists(tmp):
        default_tmp = str(BASE_DIR / "RawLoadrr" / "tmp")
        console.print(f"\n[bold yellow]ME_TMP_ROOT no existe o no está configurado: {tmp or '(vacío)'}[/bold yellow]")
        val = Prompt.ask("Ruta TMP", default=default_tmp).strip()
        _write_env_key("ME_TMP_ROOT", val)
        me_cfg["tmp_root"] = val
        changed = True

    if changed:
        me_cfg = _me_load_config()
    return me_cfg


def _me_configure_tracker(me_cfg: dict) -> dict:
    """Menú interactivo de configuración de Mass Edition (al estilo CSI's configure_tracker)."""
    rl_trackers_raw = _me_load_rl_trackers()
    rl_trackers = [
        k for k, v in rl_trackers_raw.items()
        if isinstance(v, dict) and not k.startswith("default") and v.get("announce_url")
    ]

    while True:
        cur_url  = me_cfg.get("tracker_url")  or "(no configurada)"
        cur_name = me_cfg.get("tracker_name") or "?"
        cur_user = me_cfg.get("username")     or "(no configurado)"
        cur_ua   = me_cfg.get("user_agent",   "")
        cur_tmp  = me_cfg.get("tmp_root",     "")
        cur_imgbb = "[green]✓[/green]" if me_cfg.get("imgbb_api")     else "[red]✗ vacía[/red]"
        cur_pts   = "[green]✓[/green]" if me_cfg.get("ptscreens_api") else "[red]✗ vacía[/red]"
        _c = me_cfg.get("cookie", "")
        cookie_prev = f"…{_c[-8:]}" if len(_c) > 8 else (_c or "[red]✗ vacía[/red]")

        console.print()
        console.print(Panel(
            f"  Tracker  : [cyan]{cur_url}[/cyan]  ID:[bold]{cur_name}[/bold]  user:[bold]{cur_user}[/bold]\n"
            f"  Cookie   : {cookie_prev}\n"
            f"  TMP Root : [green]{cur_tmp}[/green]\n"
            f"  UA       : [yellow]{cur_ua[:60]}{'…' if len(cur_ua) > 60 else ''}[/yellow]\n"
            f"  ImgBB    : {cur_imgbb}    PTScreens: {cur_pts}\n\n"
            "  [bold cyan][1][/bold cyan]  Editar tracker  (URL, abrev, cookie, usuario, API key)\n"
            f"  [bold cyan][2][/bold cyan]  Importar desde RawLoadrr  [dim]({len(rl_trackers)} trackers disponibles)[/dim]\n"
            "  [bold cyan][3][/bold cyan]  APIs de imágenes  (ImgBB, PTScreens)\n"
            "  [bold cyan][4][/bold cyan]  Ruta TMP\n"
            "  [bold cyan][5][/bold cyan]  User-Agent\n"
            "  [bold cyan][0][/bold cyan]  Volver",
            title="[bold green]MASS EDITION — Configuración del módulo[/bold green]",
            border_style="green",
        ))

        c = Prompt.ask("Selección", choices=["1", "2", "3", "4", "5", "0"], default="0")

        if c == "0":
            return me_cfg

        elif c == "1":
            name = Prompt.ask("Abreviatura del tracker", default=me_cfg.get("tracker_name") or "").strip().upper()
            url  = Prompt.ask("URL base", default=me_cfg.get("tracker_url") or "").strip()
            user = Prompt.ask("Usuario", default=me_cfg.get("username") or "").strip()
            api_key = Prompt.ask("API Key del tracker", default=me_cfg.get("api_key") or "").strip()
            _c_old = me_cfg.get("cookie") or ""
            console.print(f"[dim]Cookie actual: {'…' + _c_old[-8:] if len(_c_old) > 8 else '(vacía)'}[/dim]")
            cookie = Prompt.ask("Cookie de sesión [Enter para no cambiar]", password=True, default="").strip()
            cookie_name = Prompt.ask(
                "Nombre de la cookie  [dim](ej: milnueve_session)[/dim]",
                default=me_cfg.get("cookie_name") or "",
            ).strip()
            if name:        _write_env_key("ME_TRACKER_DEFAULT",      name)
            if url:         _write_env_key("ME_TRACKER_URL",          url)
            if user:        _write_env_key("ME_TRACKER_USERNAME",     user)
            if api_key:     _write_env_key("ME_TRACKER_API_KEY",      api_key)
            if cookie:      _write_env_key("ME_TRACKER_COOKIE",       cookie)
            if cookie_name: _write_env_key("ME_TRACKER_COOKIE_NAME",  cookie_name)
            me_cfg = _me_load_config()
            console.print("[green]✓ Configuración del tracker actualizada[/green]")

        elif c == "2":
            if not rl_trackers:
                console.print("[yellow]No hay trackers con announce_url en RawLoadrr/data/config.py[/yellow]")
                continue
            console.print("\n[bold]Trackers disponibles en RawLoadrr:[/bold]")
            for i, t in enumerate(rl_trackers, 1):
                t_data = rl_trackers_raw.get(t, {})
                announce = t_data.get("announce_url", "")
                m = re.match(r"(https?://[^/]+)", announce)
                base_url = m.group(1) if m else ""
                has_api = bool(t_data.get("api_key", "").strip())
                status = "[green]API ✓[/green]" if has_api else "[red]sin API key[/red]"
                console.print(f"  {i}. [bold]{t}[/bold]  {status}  [dim]{base_url}[/dim]")

            sel = Prompt.ask(
                "Selección  [dim](0 para cancelar)[/dim]",
                choices=["0"] + [str(i) for i in range(1, len(rl_trackers) + 1)],
                default="0",
            )
            if sel != "0":
                chosen = rl_trackers[int(sel) - 1]
                t_data = rl_trackers_raw.get(chosen, {})
                announce = t_data.get("announce_url", "")
                m = re.match(r"(https?://[^/]+)", announce)
                base_url = m.group(1) if m else ""
                api_key  = t_data.get("api_key", "")

                user = Prompt.ask("Usuario en el tracker", default=me_cfg.get("username") or "").strip()
                cookie_name = Prompt.ask(
                    "Nombre de la cookie  [dim](ej: milnueve_session, nuclear_order_bit_syndicate_session)[/dim]",
                    default=me_cfg.get("cookie_name") or "",
                ).strip()

                _write_env_key("ME_TRACKER_DEFAULT", chosen)
                if base_url:    _write_env_key("ME_TRACKER_URL",         base_url)
                if user:        _write_env_key("ME_TRACKER_USERNAME",    user)
                if api_key:     _write_env_key("ME_TRACKER_API_KEY",     api_key)
                if cookie_name: _write_env_key("ME_TRACKER_COOKIE_NAME", cookie_name)
                console.print(f"[green]✓ Tracker cambiado a {chosen} → {base_url or '(URL no extraída — edita manualmente con [1])'}[/green]")
                me_cfg = _me_load_config()

        elif c == "3":
            _i = me_cfg.get("imgbb_api") or ""
            _p = me_cfg.get("ptscreens_api") or ""
            console.print(f"[dim]ImgBB actual    : {'…' + _i[-8:] if len(_i) > 8 else '(vacía)'}[/dim]")
            console.print(f"[dim]PTScreens actual: {'…' + _p[-8:] if len(_p) > 8 else '(vacía)'}[/dim]")
            new_i = Prompt.ask("ImgBB API key     [Enter para no cambiar]", password=True, default="").strip()
            new_p = Prompt.ask("PTScreens API key [Enter para no cambiar]", password=True, default="").strip()
            if new_i: _write_env_key("ME_IMGBB_API",     new_i)
            if new_p: _write_env_key("ME_PTSCREENS_API", new_p)
            me_cfg = _me_load_config()

        elif c == "4":
            new_tmp = Prompt.ask(
                "Ruta TMP",
                default=me_cfg.get("tmp_root") or str(BASE_DIR / "RawLoadrr" / "tmp"),
            ).strip()
            _write_env_key("ME_TMP_ROOT", new_tmp)
            me_cfg = _me_load_config()

        elif c == "5":
            new_ua = Prompt.ask("User-Agent", default=me_cfg.get("user_agent") or "").strip()
            if new_ua:
                _write_env_key("ME_CUSTOM_USER_AGENT", new_ua)
                me_cfg = _me_load_config()


def unit3d_orchestrator():
    me_cfg = _me_check_essential_config(_me_load_config())

    while True:
        cur_url   = me_cfg.get("tracker_url")  or "(no configurada)"
        cur_name  = me_cfg.get("tracker_name") or "?"
        cur_mode  = os.getenv("EDIT_MODE", "BANNER_URL")
        _c = me_cfg.get("cookie", "")
        cookie_prev = f"…{_c[-8:]}" if len(_c) > 8 else (_c or "[red]✗ vacía[/red]")
        cur_imgbb = "✓" if me_cfg.get("imgbb_api")     else "✗"
        cur_pts   = "✓" if me_cfg.get("ptscreens_api") else "✗"

        console.print()
        console.print(Panel(
            f"[cyan]Tracker  [/cyan]: [{cur_name}] {cur_url}\n"
            f"[cyan]Cookie   [/cyan]: {cookie_prev}\n"
            f"[cyan]ImgBB    [/cyan]: {cur_imgbb}    [cyan]PTScreens[/cyan]: {cur_pts}\n"
            f"[cyan]Modo edit[/cyan]: {cur_mode}\n\n"
            "[C] Configuración del módulo  [dim](tracker, APIs, paths)[/dim]\n"
            "[E] Configurar edición y lanzar\n"
            "[0] Volver",
            title="[bold green]ORQUESTADOR UNIT3D[/bold green]",
            border_style="green",
        ))

        sel = Prompt.ask("Selección", choices=["c", "e", "0"])

        if sel == "0":
            break

        elif sel == "c":
            me_cfg = _me_configure_tracker(me_cfg)

        elif sel == "e":
            # --- SELECCIÓN DE MODO ---
            console.print()
            console.print(Panel(
                "[1] [bold]BANNER_URL[/bold]   — Cambiar el link del repo en el banner  [dim](el más seguro)[/dim]\n"
                "[2] [bold]BANNER_IMG[/bold]   — Cambiar la imagen del banner\n"
                "[3] [bold]FIRMA_TEXT[/bold]   — Cambiar el texto 🌱...🌱 de la firma\n"
                "[4] [bold]FIRMA_FULL[/bold]   — Reemplazar el bloque firma+banner completo\n"
                "[5] [bold]URL_REPLACE[/bold]  — Reemplazar cualquier URL arbitraria\n"
                "[6] [bold]BLOCK_REPLACE[/bold]— Find & Replace libre  [dim](escape hatch)[/dim]\n"
                "[0] Sin cambios  [dim](lanzar con el modo guardado en .env)[/dim]",
                title=f"[bold yellow]¿Qué editamos? (modo actual: {cur_mode})[/bold yellow]",
                border_style="yellow",
            ))

            edit_sel = Prompt.ask("Selección", choices=["1", "2", "3", "4", "5", "6", "0"])

            if edit_sel == "1":
                _write_env_key("EDIT_MODE", "BANNER_URL")
                cur = os.getenv("BANNER_URL_NUEVA", "")
                console.print(f"[dim]URL actual: {cur or '(vacía)'}[/dim]")
                val = Prompt.ask("Nueva URL del repo [Enter para no cambiar]", default="").strip()
                if val:
                    _write_env_key("BANNER_URL_NUEVA", val)
                    console.print("[green]✓ BANNER_URL_NUEVA guardado[/green]")

            elif edit_sel == "2":
                _write_env_key("EDIT_MODE", "BANNER_IMG")
                cur = os.getenv("BANNER_IMG_NUEVA", "")
                console.print(f"[dim]URL imagen actual: {cur or '(vacía)'}[/dim]")
                val = Prompt.ask("Nueva URL de la imagen del banner [Enter para no cambiar]", default="").strip()
                if val:
                    _write_env_key("BANNER_IMG_NUEVA", val)
                    console.print("[green]✓ BANNER_IMG_NUEVA guardado[/green]")

            elif edit_sel == "3":
                _write_env_key("EDIT_MODE", "FIRMA_TEXT")
                cur = os.getenv("FIRMA_TEXT_NUEVA", "")
                console.print(f"[dim]Texto actual: {cur or '(vacío)'}[/dim]")
                val = Prompt.ask("Nuevo texto de firma [Enter para no cambiar]", default="").strip()
                if val:
                    _write_env_key("FIRMA_TEXT_NUEVA", val)
                    console.print("[green]✓ FIRMA_TEXT_NUEVA guardado[/green]")

            elif edit_sel == "4":
                _write_env_key("EDIT_MODE", "FIRMA_FULL")
                cur = os.getenv("FIRMA_FULL_NUEVA", "")
                console.print(f"[dim]Bloque actual: {cur or '(vacío)'}[/dim]")
                val = Prompt.ask("Nuevo bloque firma+banner completo [Enter para no cambiar]", default="").strip()
                if val:
                    _write_env_key("FIRMA_FULL_NUEVA", val)
                    console.print("[green]✓ FIRMA_FULL_NUEVA guardado[/green]")

            elif edit_sel == "5":
                _write_env_key("EDIT_MODE", "URL_REPLACE")
                cur_find    = os.getenv("FIND_URL", "")
                cur_replace = os.getenv("REPLACE_URL", "")
                console.print(f"[dim]Buscar   : {cur_find or '(vacío)'}[/dim]")
                console.print(f"[dim]Reemplazar: {cur_replace or '(vacío)'}[/dim]")
                find_val = Prompt.ask("URL a buscar [Enter para no cambiar]", default="").strip()
                repl_val = Prompt.ask("URL de reemplazo [Enter para no cambiar]", default="").strip()
                if find_val:
                    _write_env_key("FIND_URL", find_val)
                    console.print("[green]✓ FIND_URL guardado[/green]")
                if repl_val:
                    _write_env_key("REPLACE_URL", repl_val)
                    console.print("[green]✓ REPLACE_URL guardado[/green]")

            elif edit_sel == "6":
                _write_env_key("EDIT_MODE", "BLOCK_REPLACE")
                cur_viejo = os.getenv("BLOCK_VIEJO", "")
                cur_nuevo = os.getenv("BLOCK_NUEVO", "")
                console.print(f"[dim]Buscar   : {cur_viejo or '(vacío)'}[/dim]")
                console.print(f"[dim]Reemplazar: {cur_nuevo or '(vacío)'}[/dim]")
                viejo = Prompt.ask("Texto a buscar [Enter para no cambiar]", default="").strip()
                nuevo = Prompt.ask("Texto de reemplazo [Enter para no cambiar]", default="").strip()
                if viejo:
                    _write_env_key("BLOCK_VIEJO", viejo)
                    console.print("[green]✓ BLOCK_VIEJO guardado[/green]")
                if nuevo:
                    _write_env_key("BLOCK_NUEVO", nuevo)
                    console.print("[green]✓ BLOCK_NUEVO guardado[/green]")

            # --- RANGO DE IDs ---
            start = IntPrompt.ask(
                "\nID del primer torrent\n  [dim](tracker.com/torrents/[bold]14[/bold])[/dim]",
                default=int(os.getenv("ID_START", str(ID_INICIO))),
            )
            end = IntPrompt.ask(
                "ID del último torrent",
                default=int(os.getenv("ID_END", str(ID_FIN))),
            )
            _write_env_key("ID_START", str(start))
            _write_env_key("ID_END",   str(end))

            modo_final = os.getenv("EDIT_MODE", "BANNER_URL")
            confirm = Prompt.ask(
                f"¿Lanzo la secuencia 01-04 para IDs {start}–{end} en modo [bold]{modo_final}[/bold]?",
                choices=["s", "n"],
                default="s",
            )

            if confirm == "s":
                scripts = [
                    "extras/MASS-EDITION-UNIT3D/01_scraper.py",
                    "extras/MASS-EDITION-UNIT3D/02_indexer.py",
                    "extras/MASS-EDITION-UNIT3D/03_mass_updater.py",
                    "extras/MASS-EDITION-UNIT3D/04_image_resurrector.py",
                ]
                os.environ["ID_START"] = str(start)
                os.environ["ID_END"]   = str(end)
                log.info(f"UNIT3D Orchestrator: IDs {start}-{end}, modo {modo_final}")
                for script in scripts:
                    script_path = BASE_DIR / script
                    if not script_path.exists():
                        console.print(f"[yellow]⚠ Script no encontrado, se salta: {script}[/yellow]")
                        log.warning(f"UNIT3D script not found: {script}")
                        continue
                    console.print(Panel(f"[bold yellow]DÁNDOLE CAÑA A:[/bold yellow] {script}", style="bold"))
                    _run(["python3", str(script_path)])
                console.print("[bold green]✓ Secuencia masiva finiquitada.[/bold green]")
                time.sleep(2)


# ------------------------------------------------------------------ #
#  Menú principal                                                     #
# ------------------------------------------------------------------ #

# ------------------------------------------------------------------ #

def _submenu_mantenimiento():
    base_data = Path("work_data")
    
    while True:
        console.print()
        console.print(Panel(
            "[1] Purgar Temporales (work_data/tmp/*)\n"
            "[2] Purgar Logs (*.log y vestigios)\n"
            "[3] Purgar Reportes (*.txt)\n"
            "[4] Resetear Dashboard (Estado Neutro)\n"
            "[5] DEFCON 1 (Fuego purificador total)\n"
            "[0] Volver al Menú Principal",
            title="[bold red]MANTENIMIENTO & PULICIÓN[/bold red]",
            border_style="red",
            padding=(1, 2)
        ))
        
        sel = Prompt.ask("root@mantenimiento", choices=["1", "2", "3", "4", "5", "0"])
        
        if sel == "0":
            break
            
        if sel in ["1", "5"]:
            tmp_dir = base_data / "tmp"
            count = 0
            if tmp_dir.exists():
                for item in tmp_dir.iterdir():
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                    count += 1
            console.print(f"[green]✓ TMP pulicionado. {count} focos de basura eliminados.[/green]")

        if sel in ["2", "5"]:
            count = 0
            for log_file in base_data.rglob("*.log"):
                log_file.unlink()
                count += 1
            # Destruir la fosa común fósil de CSI si existe
            csi_fosa = base_data / "logs" / "csi_log"
            if csi_fosa.exists() and csi_fosa.is_dir():
                shutil.rmtree(csi_fosa)
                count += 1
            console.print(f"[green]✓ Logs fulminados. {count} archivos al pozo.[/green]")

        if sel in ["3", "5"]:
            count = 0
            for txt_file in base_data.rglob("*.txt"):
                txt_file.unlink()
                count += 1
            console.print(f"[green]✓ Reportes aniquilados. {count} .txt destruidos.[/green]")

        if sel in ["4", "5"]:
            clear_all_statuses()
            console.print("[green]✓ Dashboard reseteado. Tabula rasa en la FastAPI.[/green]")

        if sel == "5":
            console.print(Rule("[bold red]DEFCON 1 COMPLETO — EL ENTORNO ESTÁ ESTÉRIL[/bold red]", style="red"))

def main_menu():
    # Iniciar dashboard en segundo plano si no está corriendo
    def is_dashboard_running(port=8002):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0

    if not is_dashboard_running():
        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()
        details_msg = "Radar operativo en el puerto 8002"
    else:
        details_msg = "Radar persistente detectado (Puerto 8002)"
    
    update_status("CORE", "Menú Principal", "EN LÍNEA", details=details_msg)
    boot_sequence()
    while True:
        clear_screen()
        menu = Table(
            title="[bold cyan]S I N G U L A R I T Y[/bold cyan]",
            box=None,
            show_header=True,
            header_style="bold magenta",
        )
        menu.add_column("SYS", justify="center")
        menu.add_column("MÓDULO", style="white")
        menu.add_column("ESTADO", justify="right")

        # EDICIÓN LITE. Fuera del menú, y por qué:
        #   Recordrr  -> necesita Google Chrome, que no existe para ARM64, y el
        #                Chromium de ARM64 no lleva Widevine.
        #   God/Goddess Mode -> sus fases 1 y 2 (extracción de ISOs, rescate y
        #                triaje con decodificación) transcodifican. Lo que queda
        #                del pipeline, subir y editar, es la opción [5].
        # Numeración corrida y toda numérica: sin mezclar letras y números.
        menu.add_row("1", "MKVerything (Auditoría y Spam)", "[green]EN LÍNEA[/green]")
        menu.add_row("2", "RawLoadrr (Subidas automáticas)", "[green]EN LÍNEA[/green]")
        menu.add_row("3", "UNIT3D Ed. (Edita en el Tracker)", "[yellow]LISTO[/yellow]")
        menu.add_row("4", "Extras (Ingestor, Triaje, CSI)", "[blue]ACTIVO[/blue]")
        menu.add_row("5", "SINGULARIDAD (Subida + Edición masiva)", "[cyan]EN CADENA[/cyan]")
        menu.add_row("6", "Mantenimiento & Limpieza", "[red]PELIGRO[/red]")
        menu.add_row("7", "Download more RAM", "[magenta]GRATIS[/magenta]")
        menu.add_row("0", "Cerrar Conexión", "")

        console.print(Align.center(Panel(menu, border_style="cyan", padding=(1, 5))))

        sel = Prompt.ask("root@singularidad", choices=["1", "2", "3", "4", "5", "6", "7", "0"])
        log.info(f"Main menu selection: {sel}")

        if sel == "1":
            _submenu_mkverything()
        elif sel == "2":
            _run(["python3", "RawLoadrr/rawncher.py"])
        elif sel == "3":
            unit3d_orchestrator()
        elif sel == "4":
            _submenu_extras()
        elif sel == "5":
            singularity_mode()
        elif sel == "6":
            _submenu_mantenimiento()
        elif sel == "7":
            _run(["python3", "RawLoadrr/data/reconfig.py"])
        elif sel == "0":
            log.info("User exited Singularity")
            break


def _singularity_summary(results: dict, elapsed_total: float):
    status_icons = {
        "OK":    "[green]✓ OK[/green]",
        "WARN":  "[yellow]⚠ OJO[/yellow]",
        "ERROR": "[red]✗ ERROR[/red]",
        "SKIP":  "[dim]— SALTADO[/dim]",
    }
    # Las claves internas (fase2/3/4) se conservan tal cual para no tocar el
    # resto del pipeline; solo cambia el número que se muestra. En Lite no hay
    # fase1 (MKVerything Modo Dios: transcodifica).
    labels = {
        "fase2": "Fase 1 · Triaje MKV",
        "fase3": "Fase 2 · Auto-Upload",
        "fase4": "Fase 3 · Orquestador UNIT3D",
    }
    t = Table(box=None, show_header=True, header_style="bold magenta")
    t.add_column("FASE", style="cyan")
    t.add_column("ESTADO", justify="center")
    t.add_column("DETALLES", style="white")

    for key, label in labels.items():
        r = results.get(key)
        if r is None:
            continue
        icon = status_icons.get(r.get("status", "?"), r.get("status", "?"))
        parts = []
        if "elapsed" in r:
            parts.append(f"{r['elapsed']:.1f}s")
        if "stats" in r:
            s = r["stats"]
            mb = s.get("saved_bytes", 0) // (1024 * 1024)
            parts.append(
                f"ISOs {s.get('isos_ok', 0)}ok/{s.get('isos_fail', 0)}fallos  "
                f"conv={s.get('processed', 0)}  ahorro={mb}MB"
            )
        if "count" in r:
            parts.append(f"{r['count']} carpetas con HEVC")
        if "hevc_list" in r:
            parts.append(Path(r["hevc_list"]).name)
        if r.get("rc") is not None:
            parts.append(f"salida={r['rc']}")
        if "scripts" in r:
            ok_scripts = sum(1 for s in r["scripts"] if s.get("rc") == 0)
            parts.append(f"{ok_scripts}/{len(r['scripts'])} scripts correctos")
        if "error" in r:
            parts.append(f"[red]{str(r['error'])[:60]}[/red]")
        if "msg" in r:
            parts.append(r["msg"])
        t.add_row(label, icon, "  ".join(parts))

    total_min = int(elapsed_total // 60)
    total_sec = int(elapsed_total % 60)
    console.print()
    console.print(Rule("[bold cyan]SINGULARIDAD — REPORTE DE DAÑOS[/bold cyan]"))
    console.print(Panel(
        t,
        title=f"[bold cyan]Pipeline finiquitado en {total_min}m {total_sec}s[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    ))


def _ensure_credentials(need_unit3d: bool) -> None:
    """Prompt for any credentials that are missing from the environment."""
    env_path = BASE_DIR / ".env"

    _creds: list[tuple[str, str, str, bool]] = [
        (
            "SING_TRACKER_URL",
            "La URL base del tracker para el pipeline de Singularidad (ej: https://mitracker.com)",
            BASE_URL,
            need_unit3d,
        ),
        (
            "SING_TRACKER_COOKIE",
            "La cookie de sesión del tracker para el pipeline de Singularidad\n"
            "  → F12 → Application → Cookies → copia el valor de la sesión",
            COOKIE_VALUE,
            need_unit3d,
        ),
        (
            "SING_IMGBB_API",
            "La API Key de ImgBB para el pipeline de Singularidad",
            IMGBB_API,
            need_unit3d,
        ),
        (
            "SING_PTSCREENS_API",
            "La API Key de PTScreens para el pipeline de Singularidad",
            PTSCREENS_API,
            need_unit3d,
        ),
        (
            "SONARR_API_KEY",
            "La API Key de Sonarr (opcional, para indexar)\n"
            "  → Settings -> General -> API Key",
            os.getenv("SONARR_API_KEY", ""),
            False,
        ),
        (
            "SONARR_URL",
            "La URL de Sonarr (ej: http://localhost:8989)",
            os.getenv("SONARR_URL", "http://127.0.0.1:8989"),
            False,
        ),
        (
            "RADARR_API_KEY",
            "La API Key de Radarr (opcional, para indexar)\n"
            "  → Settings -> General -> API Key",
            os.getenv("RADARR_API_KEY", ""),
            False,
        ),
        (
            "RADARR_URL",
            "La URL de Radarr (ej: http://localhost:7878)",
            os.getenv("RADARR_URL", "http://127.0.0.1:7878"),
            False,
        ),
        (
            "TMP_ROOT",
            "Carpeta temporal para el curro",
            os.getenv("TMP_ROOT", str(BASE_DIR / "tmp")),
            False,
        ),
    ]

    missing = [(env_key, desc, cur) for env_key, desc, cur, required in _creds if required and not cur]
    if not missing:
        return

    console.print()
    console.print(Panel(
        "[yellow]Ojo, que faltan credenciales para poder arrancar el pipeline.\n"
        "Te las pediré ahora. Puedes guardarlas en el archivo .env\n"
        "para no dar la brasa otra vez.[/yellow]",
        title="[bold yellow]⚠ A rellenar credenciales[/bold yellow]",
        border_style="yellow",
    ))

    new_vals: dict[str, str] = {}
    for env_key, desc, _ in missing:
        console.print(f"\n[bold cyan]{env_key}[/bold cyan]")
        console.print(f"[dim]{desc}[/dim]")
        val = Prompt.ask(f"[bold]Mete el valor[/bold]", password=("COOKIE" in env_key or "KEY" in env_key)).strip()
        os.environ[env_key] = val
        new_vals[env_key] = val

    if new_vals:
        save = Prompt.ask(
            "\n¿Te guardo estas credenciales en el .env para no dar la brasa otra vez?",
            choices=["s", "n"],
            default="s",
        )
        if save == "s":
            existing: dict[str, str] = {}
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        existing[k.strip()] = v.strip()
            existing.update(new_vals)
            with open(env_path, "w") as fh:
                for k, v in existing.items():
                    fh.write(f"{k}={v}\n")
            console.print("[green]✓ ¡Ale! Credenciales guardadas en el .env[/green]")


def singularity_mode():
    clear_screen()

    mode_label = "MODO SINGULARIDAD — PIPELINE AUTOMÁTICO"
    mode_style = "cyan"

    console.print(Rule(f"[bold {mode_style}]⚡ {mode_label}[/bold {mode_style}]", style=mode_style))

    # EDICIÓN LITE: la antigua Fase 1 (MKVerything God Mode: extraer ISOs,
    # convertir legacy, rescatar MKVs) no está. Transcodifica y necesita
    # MakeMKV y VapourSynth. Las otras tres fases sí siguen: el triaje solo
    # lee codecs con ffprobe, y subir y editar no tocan vídeo.
    # Por eso tampoco hay God vs Goddess: fast_scan solo afectaba a la Fase 1.
    console.print(Panel(
        "\n"
        "  Estas son las fases que se van a ejecutar solitas:\n\n"
        "  [bold yellow][1][/bold yellow]  Triaje MKV             — Separa HEVC de H264 (solo lee codecs)\n"
        "  [bold green][2][/bold green]  RawLoadrr Auto-Upload  — Sube a cholón la lista elegida\n"
        "  [bold blue][3][/bold blue]  Orquestador UNIT3D     — (Opcional) Lanza los scripts 01-04\n",
        title="[bold white]Qué se va a liar[/bold white]",
        border_style="white",
    ))

    while True:
        media_root_str = Prompt.ask(
            "[bold]Dime la carpeta raíz donde guardas los vídeos[/bold]\n"
            "  [dim](mete la ruta completa, ej: /media/peliculas)[/dim]"
        ).strip()
        media_root = Path(media_root_str)
        if media_root.is_dir():
            break
        console.print(f"[red]✗ No es un directorio válido: {media_root_str}[/red]")

    tracker = Prompt.ask(
        "[bold]Dime la abreviatura del tracker para subir[/bold]\n"
        "  [dim](ej: MILNU, BHD, HDB — tiene que coincidir con tu config de RawLoadrr)[/dim]",
        default="MILNU",
    )

    console.print()
    console.print(Panel(
        "\n"
        "  [bold cyan][1][/bold cyan]  Solo lo bueno (HEVC)         [dim](todo-hevc-*.txt)[/dim]   — carpetas listas para subir\n"
        "  [bold cyan][2][/bold cyan]  Lo de antes (H264/Legacy)  [dim](sigue-h264-*.txt)[/dim]  — carpetas con cosas aún por convertir\n"
        "  [bold cyan][3][/bold cyan]  Una lista tuya, a medida                    — tú metes la ruta a tu propio fichero\n"
        "  [bold cyan][4][/bold cyan]  Todo lo que pille en el directorio       — mezcla de las dos listas anteriores\n",
        title="[bold cyan]¿Qué le metemos al Auto-Upload en la Fase 3?[/bold cyan]",
        border_style="cyan",
    ))
    list_mode = Prompt.ask("[bold]Venga, elige[/bold]", choices=["1", "2", "3", "4"], default="1")
    custom_list_path: "Path | None" = None
    if list_mode == "3":
        while True:
            cl_raw = Prompt.ask(
                "[bold]Pásame la ruta completa del fichero con tu lista[/bold]"
            ).strip()
            cl_path = Path(cl_raw)
            if cl_path.is_file():
                custom_list_path = cl_path
                break
            console.print(f"[red]✗ Esto no es un fichero válido: {cl_raw}[/red]")

    run_unit3d = Prompt.ask(
        "¿Le damos caña a la [bold]Fase 3 - Orquestador UNIT3D[/bold]?\n"
        "  [dim](edita en masa los torrents del tracker: scraping, indexado, descripciones e imágenes)[/dim]",
        choices=["s", "n"],
        default="n",
    )
    unit3d_start = unit3d_end = None
    if run_unit3d == "s":
        unit3d_start = IntPrompt.ask(
            "Dime el ID del primer torrent a editar en UNIT3D\n"
            "  [dim](el número de ID que se ve en la URL, ej: tracker.com/torrents/[bold]14[/bold])[/dim]",
            default=ID_INICIO,
        )
        unit3d_end = IntPrompt.ask(
            "Y ahora el ID del último torrent a tocar\n"
            "  [dim](se procesarán todos los IDs entre el primero y este)[/dim]",
            default=ID_FIN,
        )

    if run_unit3d == "s":
        _me_check_essential_config(_me_load_config())

    cfg_table = Table(box=None, show_header=False)
    cfg_table.add_column(style="cyan", no_wrap=True)
    cfg_table.add_column(style="white")
    _list_mode_labels = {
        "1": "HEVC  (todo-hevc-*.txt)",
        "2": "H264/Legacy  (sigue-h264-*.txt)",
        "3": f"Personalizada  → {custom_list_path}",
        "4": "Todo el directorio  (HEVC + H264/Legacy)",
    }
    cfg_table.add_row("Raíz de medios", str(media_root))
    cfg_table.add_row("Tracker", tracker)
    cfg_table.add_row("Lista Fase 2", _list_mode_labels[list_mode])
    cfg_table.add_row("UNIT3D", "Sí" if run_unit3d == "s" else "No")
    if run_unit3d == "s":
        cfg_table.add_row("IDs UNIT3D", f"{unit3d_start} → {unit3d_end}")

    console.print()
    console.print(Panel(cfg_table, title="[bold yellow]Configuración[/bold yellow]", border_style="yellow"))

    if Prompt.ask("¿Arrancamos el pipeline de Singularidad?", choices=["s", "n"], default="s") != "s":
        return

    phase_results: dict = {}
    start_time_total = time.time()

    # ------------------------------------------------------------------ #
    # FASE 1 — Triaje MKV                                                  #
    # ------------------------------------------------------------------ #
    console.print()
    console.print(Rule("[bold yellow]FASE 1 — Triaje MKV (HEVC vs Jurásico)[/bold yellow]", style="yellow"))
    update_status("PIPELINE", "FASE 1: Triaje", "CURRANDO", progress=20, details="Analizando codecs")
    log.info("Singularity Phase 1 start (Triage)")
    t2 = time.time()
    upload_list_path: "Path | None" = None
    try:
        # Correct path for Triage MKV script
        triage_script = "extras/Triaje-mkv/triage_mkv.py"
        _run(["python3", triage_script, str(media_root)])
        date_str = datetime.now().strftime("%d-%m-%y")
        if list_mode == "1":
            candidate = BASE_DIR / f"todo-hevc-{date_str}.txt"
            list_label_p2 = "HEVC"
        elif list_mode == "2":
            candidate = BASE_DIR / f"sigue-h264-{date_str}.txt"
            list_label_p2 = "H264/Legacy"
        elif list_mode == "4":
            hevc_candidate = BASE_DIR / f"todo-hevc-{date_str}.txt"
            h264_candidate = BASE_DIR / f"sigue-h264-{date_str}.txt"
            combined_path = BASE_DIR / f"todo-all-{date_str}.txt"
            combined_lines: list[str] = []
            for src in (hevc_candidate, h264_candidate):
                if src.exists() and src.stat().st_size > 0:
                    combined_lines.extend(
                        ln for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()
                    )
            combined_path.write_text("\n".join(combined_lines) + ("\n" if combined_lines else ""), encoding="utf-8")
            candidate = combined_path
            list_label_p2 = "Todo el directorio (HEVC + H264/Legacy)"
        else:
            candidate = custom_list_path
            list_label_p2 = "personalizada"
        if candidate and candidate.exists() and candidate.stat().st_size > 0:
            upload_list_path = candidate
            count = sum(
                1 for ln in upload_list_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            )
            phase_results["fase2"] = {
                "status": "OK",
                "elapsed": time.time() - t2,
                "hevc_list": str(upload_list_path),
                "count": count,
            }
            console.print(
                f"[green]✓ Fase 1 finiquitada — {count} carpetas en la lista '{list_label_p2}' listas para el despegue.[/green]"
            )
        else:
            phase_results["fase2"] = {
                "status": "WARN",
                "elapsed": time.time() - t2,
                "msg": f"Lista {list_label_p2} vacía o no encontrada",
            }
            console.print(
                f"[yellow]⚠ Fase 1: La lista '{list_label_p2}' no se ha generado o está a cero.[/yellow]"
            )
        log.info(f"Singularity Phase 1 done, upload_list={upload_list_path}, mode={list_mode}")

    except Exception as exc:
        log.error(f"Singularity Phase 1 failed: {exc}")
        phase_results["fase2"] = {
            "status": "ERROR",
            "elapsed": time.time() - t2,
            "error": str(exc),
        }
        console.print(f"[red]✗ La Fase 1 ha petado: {exc}[/red]")

    # ------------------------------------------------------------------ #
    # FASE 2 — RawLoadrr Auto-Upload                                       #
    # ------------------------------------------------------------------ #
    console.print()
    console.print(Rule("[bold green]FASE 2 — RawLoadrr: Fuego a Discreción[/bold green]", style="green"))
    update_status("PIPELINE", "FASE 2: Auto-Upload", "CURRANDO", progress=60, details="Inyectando torrents al tracker")
    log.info("Singularity Phase 2 start (Auto-Upload)")
    t3 = time.time()
    if upload_list_path and upload_list_path.exists():
        try:
            rc = _run(
                ["python3", "auto-upload.py", "--list", str(upload_list_path), "--tracker", tracker],
                cwd=BASE_DIR / "RawLoadrr",
            )
            status = "OK" if rc == 0 else "WARN"
            phase_results["fase3"] = {"status": status, "elapsed": time.time() - t3, "rc": rc}
            color = "green" if rc == 0 else "yellow"
            symbol = "✓" if rc == 0 else "⚠"
            console.print(f"[{color}]{symbol} Fase 2 finiquitada (código de salida: {rc}).[/{color}]")
            log.info(f"Singularity Phase 2: rc={rc}")
        except Exception as exc:
            log.error(f"Singularity Phase 2 failed: {exc}")
            phase_results["fase3"] = {
                "status": "ERROR",
                "elapsed": time.time() - t3,
                "error": str(exc),
            }
            console.print(f"[red]✗ La Fase 2 ha petado: {exc}[/red]")
    else:
        phase_results["fase3"] = {
            "status": "SKIP",
            "elapsed": 0,
            "msg": "Sin lista de upload — fase omitida",
        }
        console.print("[yellow]⚠ Fase 2 omitida: no tengo lista para subir nada.[/yellow]")

    # ------------------------------------------------------------------ #
    # FASE 3 — UNIT3D Orchestrator (opcional)                              #
    # ------------------------------------------------------------------ #
    if run_unit3d == "s":
        console.print()
        console.print(Rule("[bold blue]FASE 3 — Orquestador UNIT3D[/bold blue]", style="blue"))
        update_status("PIPELINE", "FASE 3: Orquestador", "CURRANDO", progress=85, details="Haciendo mantenimiento masivo en el tracker")
        log.info(f"Singularity Phase 3 start (UNIT3D), IDs {unit3d_start}-{unit3d_end}")
        t4 = time.time()
        try:
            os.environ["ID_START"] = str(unit3d_start)
            os.environ["ID_END"] = str(unit3d_end)
            scripts = [
                "extras/MASS-EDITION-UNIT3D/01_scraper.py",
                "extras/MASS-EDITION-UNIT3D/02_indexer.py",
                "extras/MASS-EDITION-UNIT3D/03_mass_updater.py",
                "extras/MASS-EDITION-UNIT3D/04_image_resurrector.py",
            ]
            script_results = []
            for script in scripts:
                sp = BASE_DIR / script
                if sp.exists():
                    console.print(f"[blue]  → {Path(script).name}[/blue]")
                    rc = _run(["python3", str(sp)])
                    script_results.append({"script": Path(script).name, "rc": rc})
                else:
                    console.print(f"[yellow]  ⚠ No lo encuentro: {script}[/yellow]")
                    script_results.append({"script": Path(script).name, "rc": None, "note": "not found"})
            phase_results["fase4"] = {
                "status": "OK",
                "elapsed": time.time() - t4,
                "scripts": script_results,
            }
            console.print("[green]✓ Fase 3 finiquitada.[/green]")
            log.info("Singularity Phase 3 OK")
        except Exception as exc:
            log.error(f"Singularity Phase 3 failed: {exc}")
            phase_results["fase4"] = {
                "status": "ERROR",
                "elapsed": time.time() - t4,
                "error": str(exc),
            }
            console.print(f"[red]✗ La Fase 3 ha petado: {exc}[/red]")

    _singularity_summary(phase_results, time.time() - start_time_total)
    update_status("PIPELINE", "COMPLETADO", "FINIQUITADO", progress=100, details="Pipeline completado con éxito")
    log.info("Singularity pipeline finished")
    Prompt.ask("\nPulsa Enter para volver al menú principal", default="")


if __name__ == "__main__":
    # Interactive entrypoint: abort hard. A user sitting at the TUI can act on
    # the message immediately, and continuing would only fail later, deeper in.
    from core.preflight import enforce as _preflight_enforce
    _preflight_enforce()
    try:
        main_menu()
    except KeyboardInterrupt:
        log.info("Singularity interrupted by user (KeyboardInterrupt)")
        console.print("\n[yellow]Conexión cerrada a las bravas.[/yellow]")
        sys.exit(0)
