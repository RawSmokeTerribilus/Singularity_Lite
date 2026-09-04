import os
import sys
from typing import Optional
import re
import time
import random
import shutil
import subprocess
import logging
import importlib.util
import json
import difflib                                # CSI v3.2: fuzzy folder match
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.align import Align
import requests
from bs4 import BeautifulSoup, element as bs4_element

try:
    import qbittorrentapi
except ImportError:
    qbittorrentapi = None

# CSI Integration
CSI_DIR  = Path(__file__).resolve().parent
BASE_DIR = CSI_DIR.parent.parent
sys.path.append(str(BASE_DIR))

try:
    from core.status_manager import update_status
except ImportError:
    def update_status(*args, **kwargs): pass

# ─── PATHS ────────────────────────────────────────────────────────────────────
ENV_PATH = BASE_DIR / ".env"
BACKUP_LOG  = CSI_DIR / "backups.log"

# CSI v1.6.5: Environment-Aware Paths & Persistent Reporting Restructuring
DOCKER_REPORTS_PATH = "/app/work_data/reports/csi_reports"
HOST_REPORTS_DIR    = "./work_data/reports/csi_reports"

def get_reports_path():
    """Detect between Docker and Host report paths."""
    if os.path.exists(HOST_REPORTS_DIR):
        return Path(HOST_REPORTS_DIR)
    return Path(DOCKER_REPORTS_PATH)

REPORTS_DIR = get_reports_path()
LOGS_DIR    = REPORTS_DIR / "logs" # v1.6.5: Moved to mapped volume for host access

# ─── LIVE REPORTING ───────────────────────────────────────────────────────────
class LiveReport:
    """CSI v1.6.5: Handle real-time report generation to mapped volumes."""
    def __init__(self, subcat: str, config: dict):
        self.subcat = subcat
        self.config = config
        self.files  = {}
        self.seen   = {}
        self.paths  = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def add(self, category: str, codec: str, path: str):
        c_up = category.upper()
        o_up = codec.upper()
        key = (c_up, o_up)
        
        if key not in self.files:
            p = get_report_path(c_up, o_up, self.subcat)
            self.files[key] = open(p, "w", encoding="utf-8")
            self.seen[key]  = set()
            self.paths.append(p)
            
            # Log the host path immediately so the user can see it's live
            relative = p.relative_to(REPORTS_DIR)
            dbg(f"[LIVE] Report initialized: {HOST_REPORTS_DIR}/{relative}")

        if path not in self.seen[key]:
            self.seen[key].add(path)
            f = self.files[key]
            f.write(path + "\n")
            f.flush()

    def close(self):
        for f in self.files.values():
            if not f.closed:
                f.close()
        self.files.clear()
        return self.paths

DOCKER_TMP_PATH = "/app/RawLoadrr/tmp"
HOST_TMP_PATH   = "./work_data/tmp"

def get_tmp_path():
    """Auto-detect between Docker and Host paths."""
    if os.path.exists(HOST_TMP_PATH):
        return Path(HOST_TMP_PATH)
    return Path(DOCKER_TMP_PATH)

TMP_ROOT = get_tmp_path()

# CSI v2.0: Ruta de persistencia del TrackerIndex entre sesiones
# Almacenado en REPORTS_DIR (ya mapeado a volumen en Docker, accesible desde host)
TRACKER_INDEX_PATH = REPORTS_DIR / "tracker_index.json"
# CSI v3.2: sidecar cache for folder→ids resolutions (Plex/Jellyfin embedded
# ids, fuzzy/external lookups). Same volume as tracker_index.json so the
# Docker bind-mount in the Makefile prep step covers it automatically.
ID_RESOLVER_CACHE_PATH = REPORTS_DIR / "id_resolver_cache.json"

# ─── LOGGING & INIT ──────────────────────────────────────────────────────────
console = Console()
DEBUG_MODE = False

# --- TROLLING SUBSYSTEM INJECTION ---
try:
    from singularity_config import GOD_PHRASES
except ImportError:
    GOD_PHRASES = []

if GOD_PHRASES:
    _original_console_print = console.print

    def troll_print(*args, **kwargs):
        # REGLA DE ORO: El log técnico SIEMPRE se imprime primero, íntegro e intocable.
        # El troleo es un addendum con probabilidad del 7%, NUNCA un sustituto.
        # Ningún path, ID, estado de match o dato crítico puede ser ocultado por el troleo.
        _original_console_print(*args, **kwargs)
        # Solo DESPUÉS del log real, y solo a veces, se infiltra una frase de amenidad.
        if random.random() < 0.07:  # 7% de probabilidad — addendum, jamás reemplazo
            phrase = random.choice(GOD_PHRASES)
            _original_console_print(f"[dim italic magenta]« {phrase} »[/dim italic magenta]")

    console.print = troll_print  # type: ignore[method-assign]
# ------------------------------------

_env_backup_done = False
_initialized = False
_fh = None

logger = logging.getLogger("CSI")
logger.setLevel(logging.DEBUG)

def _ensure_initialized():
    """Lazy initialization of directories and logging to avoid import side-effects."""
    global _initialized, _fh
    if _initialized:
        return
    
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # v1.6.5 Clean up: Standardized folders only
    for _case in ("local_cases", "user_cases", "global_cases"):
        for _sub in ("movies", "tv_shows"):
            (REPORTS_DIR / _case / _sub).mkdir(parents=True, exist_ok=True)

    # Logging setup
    _log_file = LOGS_DIR / f"csi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    _fh = logging.FileHandler(_log_file, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
    
    _initialized = True

def dbg(msg: str):
    logger.debug(msg)
    if DEBUG_MODE:
        console.print(f"[dim cyan]DBG: {msg}[/dim cyan]")

def toggle_debug(force_state=None):
    global DEBUG_MODE, _fh
    if force_state is not None:
        DEBUG_MODE = force_state
    else:
        DEBUG_MODE = not DEBUG_MODE
    
    # Refresh handlers
    logger.handlers = [h for h in logger.handlers if not isinstance(h, logging.StreamHandler)]
    if _fh:
        logger.addHandler(_fh) # Always keep file handler if initialized
    
    if DEBUG_MODE:
        _sh = logging.StreamHandler(sys.stdout)
        _sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(_sh)
        if force_state is None:
            console.print("[bold green]Debug Mode ON[/bold green]")
    else:
        if force_state is None:
            console.print("[bold yellow]Debug Mode OFF[/bold yellow]")

# ─── ASCII ART ────────────────────────────────────────────────────────────────
DETECTIVE_ART = (
    "[bold cyan]"
    "╔============================================╗\n"
    "||                                          ||\n"
    "||                                          ||\n"
    "||       ██████╗    ███████╗    ██╗         ||\n"
    "||      ██╔════╝    ██╔════╝    ██║         ||\n"
    "||      ██║         ███████╗    ██║         ||\n"
    "||      ██║         ╚════██║    ██║         ||\n"
    "||      ╚██████╗    ███████║    ██║         ||\n"
    "||       ╚═════╝    ╚══════╝    ╚═╝         ||\n"
    "||                                          ||\n"
    "||   ████████╗   ████████╗█████████████╗    ||\n"
    "||  ██╔════╚██╗ ██╔██╔══████╔════██╔══██╗   ||\n"
    "||  ██║     ╚████╔╝██████╔█████╗ ██████╔╝   ||\n"
    "||  ██║      ╚██╔╝ ██╔══████╔══╝ ██╔══██╗   ||\n"
    "||  ╚██████╗  ██║  ██████╔█████████║  ██║   ||\n"
    "||   ╚═════╝  ╚═╝  ╚═════╝╚══════╚═╝  ╚═╝   ||\n"
    "||                                          ||\n"
    "||                                          ||\n"
    "╚============================================╝\n"
    "[/bold cyan]"
)

# ─── STATE MACHINE — MÁQUINA DE ESTADOS CSI v2.0 ─────────────────────────────
# Cinco estados posibles por cada ítem de la biblioteca.
# La máquina de estados es la fuente de verdad del diagnóstico.
# PROHIBIDO usar True/False para representar el resultado de una triangulación.
#
# Jerarquía de Autoridades (orden estricto):
#   1. Tracker API / Scraper  → fuente de verdad principal
#   2. qBittorrent (Client)   → confirma si se está seeding
#   3. Disco local            → confirma existencia física
#   4. Cache/tmp/JSON         → historial suplementario, NUNCA fuente única

ESTADO_OK             = "ESTADO_OK"             # Verde   — En Tracker + en Cliente
ESTADO_DUPE_POTENCIAL = "ESTADO_DUPE_POTENCIAL"  # Amarillo— Mismo TMDB, diferente ICU
ESTADO_FALTA_CLIENTE  = "ESTADO_FALTA_CLIENTE"   # Naranja — En Tracker, sin seedear
ESTADO_NO_SUBIDO      = "ESTADO_NO_SUBIDO"       # Rojo    — No encontrado en Tracker
ESTADO_INCIDENCIA     = "ESTADO_INCIDENCIA"      # Gris    — Error de sonda / fallo conexión

# Colores Rich asociados a cada estado
STATE_COLORS = {
    ESTADO_OK:             "green",
    ESTADO_DUPE_POTENCIAL: "yellow",
    ESTADO_FALTA_CLIENTE:  "dark_orange",
    ESTADO_NO_SUBIDO:      "red",
    ESTADO_INCIDENCIA:     "dim white",
}

# Etiquetas descriptivas para output al usuario
STATE_LABELS = {
    ESTADO_OK:             "✓  OK              — Subido y seedeando",
    ESTADO_DUPE_POTENCIAL: "⚠  DUPE POTENCIAL  — Misma película, diferente versión",
    ESTADO_FALTA_CLIENTE:  "●  FALTA CLIENTE   — En tracker, no en qBittorrent",
    ESTADO_NO_SUBIDO:      "✗  NO SUBIDO       — Candidato a upload",
    ESTADO_INCIDENCIA:     "?  INCIDENCIA      — Error de sonda, no se pudo verificar",
}

# ─── SECURITY & RATE LIMITER ──────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
]

class RateLimiter:
    def __init__(self, calls_per_min: int = 28):
        self.interval = 60.0 / calls_per_min
        self.last_call = 0.0

    def wait(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.interval:
            sleep_for = self.interval - elapsed + random.uniform(0.1, 0.6)
            dbg(f"Rate limit: sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)
        self.last_call = time.time()

api_limiter = RateLimiter(28)

def _headers(config: Optional[dict] = None) -> dict:
    ua = "undici"
    if config and config.get("CUSTOM_USER_AGENT"):
        ua = config["CUSTOM_USER_AGENT"]
    
    return {
        "User-Agent":      ua,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
        "DNT":             "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

def _session(config: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update(_headers(config))
    
    # Add tracker cookie if available
    c_name = config.get("TRACKER_COOKIE_NAME")
    c_val  = config.get("TRACKER_COOKIE") or config.get("TRACKER_COOKIE_VALUE")
    
    if c_name and c_val:
        s.cookies.set(c_name, c_val)
    elif c_val:
        # Fallback if name is not explicitly set, try common names
        s.cookies.set("nuclear_order_bit_syndicate_session", c_val)
        
    if config.get("USE_TOR"):
        s.proxies = {
            "http":  "socks5h://127.0.0.1:9050",
            "https": "socks5h://127.0.0.1:9050",
        }
    return s

# ─── RAWLOADRR CONFIG READER ──────────────────────────────────────────────────
def _load_rl_config() -> dict:
    path = BASE_DIR / "RawLoadrr" / "data" / "config.py"
    try:
        spec = importlib.util.spec_from_file_location("rl_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec from {path}")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.config
    except Exception as e:
        dbg(f"Could not load RawLoadrr config: {e}")
        return {}

def _update_python_var(file_path: Path, var_name: str, new_value: str):
    """Actualiza una variable 'VAR = "VAL"' en un archivo Python preservando estructura."""
    if not file_path.exists(): return
    try:
        content = file_path.read_text(encoding="utf-8")
        # Regex que busca VAR = "..." o VAR = '...' al inicio de línea o tras espacio
        pattern = r'^(\s*' + re.escape(var_name) + r'\s*=\s*)(["\'])(?:(?=(\\?))\3.)*?\2'
        if re.search(pattern, content, re.MULTILINE):
            # Reemplazar manteniendo la indentación y la variable, cambiando solo el valor entre comillas
            # Se usa re.sub con una función o string formateado con cuidado
            new_content = re.sub(
                pattern,
                lambda m: f'{m.group(1)}{m.group(2)}{new_value}{m.group(2)}',
                content,
                flags=re.MULTILINE
            )
            if new_content != content:
                file_path.write_text(new_content, encoding="utf-8")
                dbg(f"Updated {var_name} in {file_path.name}")
    except Exception as e:
        dbg(f"Error updating {var_name} in {file_path}: {e}")

def _propagate_config(updates: dict):
    """Distribuye la configuración a RawLoadrr, Mass-Edition y Singularity."""
    # Rutas probables
    rl_conf = BASE_DIR / "RawLoadrr" / "data" / "config.py"
    mass_conf = BASE_DIR / "config" / "mass_config.py"
    sing_conf = BASE_DIR / "config" / "singularity_config.py"
    
    # 1. RawLoadrr Config (Dict structure)
    if rl_conf.exists() and "TMDB_API_KEY" in updates:
        try:
            c = rl_conf.read_text(encoding="utf-8")
            # Actualizar tmdb_api en bloque DEFAULT
            c = re.sub(r'("tmdb_api"\s*:\s*")([^"]*)(")', r'\1' + updates["TMDB_API_KEY"] + r'\3', c)
            rl_conf.write_text(c, encoding="utf-8")
            dbg("Propagated TMDB to RawLoadrr config")
        except Exception as e:
            dbg(f"Failed to update RawLoadrr TMDB: {e}")

    if rl_conf.exists() and "ANNOUNCE_URL" in updates and "TRACKER_DEFAULT" in updates:
        # Intento de actualizar announce_url para el tracker específico (búsqueda simple)
        # Esto es heurístico, asume estructura estándar de RawLoadrr
        t_name = updates["TRACKER_DEFAULT"]
        announce = updates["ANNOUNCE_URL"]
        try:
            c = rl_conf.read_text(encoding="utf-8")
            # Busca bloque del tracker y su announce_url
            # Patrón: "TRACKER": { ... "announce_url": "OLD", ... }
            # Usamos un patrón simplificado que busca la clave del tracker y luego el primer announce_url que le sigue
            pat = r'("' + re.escape(t_name) + r'"\s*:\s*\{[^}]*?"announce_url"\s*:\s*")([^"]*)(")'
            if re.search(pat, c, re.DOTALL):
                c = re.sub(pat, r'\1' + announce + r'\3', c, flags=re.DOTALL)
                rl_conf.write_text(c, encoding="utf-8")
                dbg(f"Propagated Announce URL for {t_name} to RawLoadrr")
        except Exception as e:
            dbg(f"Failed to update RawLoadrr Announce: {e}")

    # 2. Mass Config (Variables globales)
    if mass_conf.exists():
        if "TRACKER_URL" in updates: _update_python_var(mass_conf, "BASE_URL", updates["TRACKER_URL"])
        if "TRACKER_USERNAME" in updates: _update_python_var(mass_conf, "USERNAME", updates["TRACKER_USERNAME"])
        if "TRACKER_COOKIE_VALUE" in updates: _update_python_var(mass_conf, "COOKIE_VALUE", updates["TRACKER_COOKIE_VALUE"])

    # 3. Singularity Config (Variables globales)
    if sing_conf.exists():
        if "TRACKER_URL" in updates: _update_python_var(sing_conf, "BASE_URL", updates["TRACKER_URL"])
        if "TRACKER_COOKIE_VALUE" in updates: _update_python_var(sing_conf, "COOKIE_VALUE", updates["TRACKER_COOKIE_VALUE"])
        if "SONARR_URL" in updates: _update_python_var(sing_conf, "SONARR_URL", updates["SONARR_URL"])
        if "SONARR_API_KEY" in updates: _update_python_var(sing_conf, "SONARR_API_KEY", updates["SONARR_API_KEY"])
        if "RADARR_URL" in updates: _update_python_var(sing_conf, "RADARR_URL", updates["RADARR_URL"])
        if "RADARR_API_KEY" in updates: _update_python_var(sing_conf, "RADARR_API_KEY", updates["RADARR_API_KEY"])

def _rl_tracker_info(rl_cfg: dict, tracker_name: str):
    """Return (api_key, base_url) for tracker_name from RawLoadrr config."""
    if not tracker_name:
        return None, None
    t = rl_cfg.get("TRACKERS", {}).get(tracker_name.upper(), {})
    api_key = t.get("api_key", "")
    if not api_key or api_key.endswith("_API_KEY") or api_key.endswith("_key"):
        api_key = None
    announce = t.get("announce_url", "")
    base_url = None
    if announce:
        m = re.match(r"(https?://[^/]+)", announce)
        if m:
            base_url = m.group(1)
    return api_key, base_url

# ─── CONFIG ───────────────────────────────────────────────────────────────────
def load_config() -> dict:
    load_dotenv(ENV_PATH, override=True)
    rl_cfg = _load_rl_config()

    tracker_name = os.getenv("TRACKER_DEFAULT", "").strip().upper()

    def _env(*keys):
        for k in keys:
            if k:
                v = os.getenv(k, "").strip()
                if v:
                    return v
        return None

    tracker_url = _env("TRACKER_BASE_URL", f"TRACKER_{tracker_name}_URL" if tracker_name else None)
    api_key     = _env("TRACKER_API_KEY",  f"TRACKER_{tracker_name}_API_KEY" if tracker_name else None)
    # CSI v1.6.2: Autonomous Config
    custom_ua = _env("CUSTOM_USER_AGENT") or "undici"
    # Portability: Use TMP_PATH as primary, default to Docker path
    tmp_path  = _env("TMP_PATH") or _env("TMP_ROOT") or str(TMP_ROOT)
    
    # Cookie logic
    cookie_name = _env("TRACKER_COOKIE_NAME", f"TRACKER_{tracker_name}_COOKIE_NAME" if tracker_name else None)
    cookie_val  = _env("TRACKER_COOKIE_VALUE", f"TRACKER_{tracker_name}_COOKIE_VALUE" if tracker_name else None)
    tracker_cookie = _env("TRACKER_COOKIE") # Combined format if exists

    # TMDB: .env first, RawLoadrr fallback
    tmdb_key = _env("TMDB_API_KEY")
    if not tmdb_key or tmdb_key == "tu_clave_tmdb_aqui":
        tmdb_key = rl_cfg.get("DEFAULT", {}).get("tmdb_api") or None

    # qBit: Default to localhost:8888 for Network Agnosticism
    rl_qbit  = rl_cfg.get("TORRENT_CLIENTS", {}).get("qbit", {})
    qbit_url  = _env("QBIT_URL") or f"http://{rl_qbit.get('qbit_url','localhost')}:{rl_qbit.get('qbit_port','8888')}"
    
    # CSI v1.6.4: Strict No-host.docker.internal policy
    if "host.docker.internal" in qbit_url:
        qbit_url = qbit_url.replace("host.docker.internal", "localhost")
        
    qbit_user = _env("QBIT_USER") or rl_qbit.get("qbit_user")
    qbit_pass = _env("QBIT_PASS") or rl_qbit.get("qbit_pass")

    return {
        "TRACKER_URL":          tracker_url,
        "TRACKER_API_KEY":      api_key or "",
        "TRACKER_COOKIE_NAME":  cookie_name,
        "TRACKER_COOKIE_VALUE": cookie_val,
        "TRACKER_COOKIE":       tracker_cookie,
        "TRACKER_USERNAME":     _env("TRACKER_USERNAME") or "",
        "TRACKER_DEFAULT":      tracker_name,
        "LIBRARY_PATH":         _env("CSI_LIBRARY_PATH"),
        "SONARR_URL":           _env("SONARR_URL"),
        "SONARR_API_KEY":       _env("SONARR_API_KEY"),
        "RADARR_URL":           _env("RADARR_URL"),
        "RADARR_API_KEY":       _env("RADARR_API_KEY"),
        "QBIT_URL":             qbit_url,
        "QBIT_USER":            qbit_user,
        "QBIT_PASS":            qbit_pass,
        "USE_TOR":              (_env("CSI_USE_TOR") or "").lower() == "true",
        "TMDB_API_KEY":         tmdb_key,
        "RAWLOADRR_CFG":        rl_cfg,
        "CUSTOM_USER_AGENT":    custom_ua,
        "TMP_PATH":             tmp_path,
        "DEBUG_MODE":           (_env("CSI_DEBUG") or "").lower() == "true",
    }

def _save_config_inplace(env_path: Path, key: str, value: str):
    """
    Safely updates a key in an .env file by reading and rewriting it in-place using r+ mode.
    This bypasses Docker inode locks by avoiding file replacement operations.
    """
    env_path.touch(exist_ok=True)

    # Quote value if it contains spaces or special characters, and isn't already quoted
    if any(c in value for c in (' ', '#', "'", '"')) and not (value.startswith(('"', "'")) and value.endswith(('"', "'"))):
        value = f'"{value}"'

    key_pattern = re.compile(r"^\s*(?:export\s+)?(" + re.escape(key) + r")\s*=")

    with open(env_path, "r+", encoding="utf-8") as f:
        lines = f.readlines()
        key_found = False
        for i, line in enumerate(lines):
            if key_pattern.match(line):
                comment = ""
                if " #" in line:
                    comment_part = line.split(" #", 1)[1]
                    comment = f" #{comment_part.strip()}"
                lines[i] = f"{key}={value}{comment}\n"
                key_found = True
                break

        if not key_found:
            if lines and not lines[-1].endswith('\n'):
                lines.append('\n')
            lines.append(f"{key}={value}\n")

        f.seek(0)
        f.writelines(lines)
        f.truncate()

def save_config(key: str, value: str):
    """
    Saves a key-value pair to the .env file.
    Uses a direct in-place write approach to be resilient to Docker file-system locks.
    """
    global _env_backup_done
    if not _env_backup_done and ENV_PATH.exists():
        bkp = ENV_PATH.with_suffix(".env.bkp")
        shutil.copy2(ENV_PATH, bkp)
        with open(BACKUP_LOG, "a") as f:
            f.write(f"[{datetime.now()}] {ENV_PATH} -> {bkp}\n")
        _env_backup_done = True

    dbg(f"Saving config: {key}={value}")
    _save_config_inplace(ENV_PATH, key, value)

# ─── SHARED HELPERS ───────────────────────────────────────────────────────────
def _norm_imdb(v) -> str:
    """Normalise an IMDB ID to its bare numeric form, e.g. 'tt0042564' → '42564'."""
    return str(v or "").replace("tt", "").lstrip("0") if v else ""

# ─── VIDEO CODEC & METADATA ──────────────────────────────────────────────────
HEVC_CODECS   = {"hevc", "h.265", "h265", "x265"}
LEGACY_CODECS = {"avc", "h.264", "h264", "x264", "mpeg-4", "mpeg4", "xvid", "divx", "mpeg video"}

def get_video_info(file_path: Path) -> dict:
    """Extracts Codec, Size, and Resolution using MediaInfo."""
    try:
        # P2: Combine mediainfo calls to reduce subprocess forks
        r = subprocess.run(
            ["mediainfo", "--Inform=Video;%Format%|%Width%x%Height%", str(file_path)],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split("|")
        codec = parts[0].lower() if parts else "unknown"
        res   = parts[1] if len(parts) > 1 else "unknown"

        # Size in GB
        size_bytes = file_path.stat().st_size
        size_gb = f"{size_bytes / (1024**3):.2f}GB"

        return {
            "codec": codec,
            "res": res,
            "size": size_gb
        }
    except Exception:
        return {"codec": "unknown", "res": "unknown", "size": "unknown"}

def get_video_codec(file_path) -> str:
    info = get_video_info(Path(file_path))
    return info["codec"]

# ─── ICU — IDENTIFICADOR COMPUESTO ÚNICO (CSI v2.0) ──────────────────────────
# El ICU es la unidad atómica de identidad de un torrent en CSI v2.0.
# Sustituye la comparación simple por TMDB_ID por un fingerprint multidimensional.
# Formato: "TMDB_ID|RESOLUTION|CODEC|GROUP"  (todo en mayúsculas, separador pipe)
# Ejemplo: "12345|1080P|REMUX|GROUPNAME"
#
# La comparación exacta de ICU permite distinguir:
#   - Misma película, diferente resolución      (DUPE_POTENCIAL)
#   - Misma película, diferente codec/grupo     (DUPE_POTENCIAL)
#   - Misma película, misma versión exacta      (ESTADO_OK)

# Patrones de resolución reconocidos (orden: de mayor a menor especificidad)
_RES_PATTERNS = [
    (re.compile(r"\b2160p?\b", re.I), "2160P"),
    (re.compile(r"\b4k\b",     re.I), "2160P"),
    (re.compile(r"\b1080p?\b", re.I), "1080P"),
    (re.compile(r"\b720p?\b",  re.I), "720P"),
    (re.compile(r"\b480p?\b",  re.I), "480P"),
    (re.compile(r"\b576p?\b",  re.I), "576P"),
]

# CSI v3.3 (W8): SOURCE and CODEC are now separate dimensions.
# The old single _CODEC_PATTERNS list mixed both with source-first ordering,
# so `normalize_folder_name("Show S01 1080p WEB-DL x265-GRP")` returned
# codec="WEB-DL" — pushing a source token into the ICU's codec slot.
# Local mediainfo only knows the *codec* (HEVC/AVC), never the *source*, so
# tracker ICU `{id}|1080P|WEB-DL|GRP` could never line up with local ICU
# `{id}|1080P|X265|UNKNOWN` — L1 exact match misfired on every torrent that
# carried a source token (≈ every tracker upload).
#
# Now we extract them independently. ICU's codec slot only holds CODEC
# (X265/X264/XVID). The source travels alongside in icu_index entries for
# diagnostics but does NOT participate in the equality check.
_SOURCE_PATTERNS = [
    (re.compile(r"\bREMUX\b",             re.I), "REMUX"),
    (re.compile(r"\bBluRay\b|\bBDRip\b",  re.I), "BLURAY"),
    (re.compile(r"\bWEB[-.]?DL\b",        re.I), "WEB-DL"),
    (re.compile(r"\bWEBRip\b|\bWEB\b",    re.I), "WEBRIP"),
    (re.compile(r"\bHDTV\b",              re.I), "HDTV"),
    (re.compile(r"\bDVDRip\b|\bDVD\b",    re.I), "DVD"),
    (re.compile(r"\bSDTV\b",              re.I), "SDTV"),
]

_CODEC_PATTERNS = [
    (re.compile(r"\bx265\b|\bHEVC\b|\bH\.265\b|\bh265\b", re.I), "x265"),
    (re.compile(r"\bx264\b|\bAVC\b|\bH\.264\b|\bh264\b",  re.I), "x264"),
    (re.compile(r"\bXviD\b|\bDivX\b",                     re.I), "XVID"),
]


# CSI v3.2: shared normalized-key builder used by both MetadataManager (Arr
# lookup tiers) AND TrackerIndex (`tracker_names` fallback in has_*_state).
# Keeping it module-level avoids a circular dep between the two classes and
# guarantees both sides hash a string the same way.
def _norm_folder_key(name: str) -> str:
    if not name:
        return ""
    comps = normalize_folder_name(name)
    base = comps.get("base_name", "") or name
    base = re.sub(r"\[[^\]]+\]", " ", base)
    base = re.sub(r"[^a-z0-9]+", " ", base.lower())
    return re.sub(r"\s+", " ", base).strip()


def normalize_folder_name(name: str) -> dict:
    """
    CSI v2.0: Normaliza nombres de carpeta/archivo a componentes semánticos.
    Maneja tanto formato con puntos (Pelicula.2010.1080p.REMUX-GRP)
    como formato con espacios/paréntesis (Pelicula (2010)).

    Retorna dict con:
        base_name   — Título limpio sin año/técnica
        year        — Año de producción o ""
        resolution  — Resolución normalizada ("1080P", "720P", ...) o ""
        codec       — Fuente/codec normalizado ("REMUX", "x265", ...) o ""
        group       — Grupo de release (token tras último "-") o ""
    """
    if not name:
        return {"base_name": "", "year": "", "resolution": "", "codec": "", "group": ""}

    # Normalizar separadores: reemplazar puntos y guiones bajos por espacios
    # pero preservar guiones finales de grupo (Pelicula.2010.1080p.REMUX-GRP)
    working = name.strip()

    # Extraer grupo de release: último token separado por "-" si parece release group
    group = ""
    group_match = re.search(r"-([A-Za-z0-9]{2,12})$", working)
    if group_match:
        candidate = group_match.group(1)
        # Filtrar falsos positivos: no extraer años ni resoluciones como grupo
        if not re.match(r"^\d{4}$", candidate) and not re.match(r"^\d{3,4}[pP]$", candidate):
            group = candidate.upper()
            working = working[:group_match.start()]

    # Normalizar puntos y guiones bajos como separadores a espacios
    working = re.sub(r"[._]", " ", working)
    # Eliminar corchetes y su contenido (ej: [BluRay] → gestionar aparte)
    working = re.sub(r"\[([^\]]+)\]", r" \1 ", working)

    # Extraer resolución
    resolution = ""
    for pattern, value in _RES_PATTERNS:
        if pattern.search(working):
            resolution = value
            break

    # CSI v3.3 (W8): codec y source se extraen independientemente.
    # ICU codec slot recibe SOLO codec (X265/X264/XVID), nunca el source.
    codec = ""
    for pattern, value in _CODEC_PATTERNS:
        if pattern.search(working):
            codec = value
            break
    source = ""
    for pattern, value in _SOURCE_PATTERNS:
        if pattern.search(working):
            source = value
            break

    # Extraer año: "(YYYY)" o "YYYY" como token solitario (1900-2099)
    year = ""
    year_match = re.search(r"\((\d{4})\)|(?<!\d)(\d{4})(?!\d)", working)
    if year_match:
        year = year_match.group(1) or year_match.group(2)
        if not (1900 <= int(year) <= 2099):
            year = ""

    # Construir base_name: eliminar año, resolución, codec y técnicos del nombre
    base = working
    # Eliminar año con paréntesis
    base = re.sub(r"\(\d{4}\)", "", base)
    # Eliminar año suelto como token
    if year:
        base = re.sub(r"(?<!\d)" + re.escape(year) + r"(?!\d)", "", base)
    # Eliminar tokens técnicos (resolución, codec, fuentes comunes)
    _TECH_TOKENS = re.compile(
        r"\b(2160p?|4k|1080p?|720p?|480p?|576p?|"
        r"REMUX|BluRay|BDRip|WEB[-.]?DL|WEBRip|WEB|HDTV|"
        r"x265|x264|HEVC|AVC|H\.265|H\.264|XviD|DivX|"
        r"AAC|DTS|AC3|FLAC|TrueHD|Atmos|EAC3|"
        r"HDR|HDR10|DV|DoVi|SDR|"
        r"MKV|MP4|AVI|REMUX|PROPER|REPACK)\b",
        re.I
    )
    base = _TECH_TOKENS.sub("", base)
    # Limpiar espacios sobrantes
    base = re.sub(r"\s+", " ", base).strip(" -.,")

    dbg(f"[ICU] normalize_folder_name('{name}') → base='{base}' year='{year}' "
        f"res='{resolution}' codec='{codec}' source='{source}' group='{group}'")

    return {
        "base_name":  base,
        "year":       year,
        "resolution": resolution,
        "codec":      codec,
        "source":     source,    # CSI v3.3 (W8): separate dimension, ICU-agnostic
        "group":      group,
    }


def build_icu(tmdb_id, resolution: str, codec: str, group: str) -> str:
    """
    CSI v2.0: Construye el Identificador Compuesto Único (ICU).
    Formato: "TMDB_ID|RESOLUTION|CODEC|GROUP"
    Normaliza valores vacíos a "UNKNOWN" para garantizar ICUs válidos.

    Ejemplo: build_icu("12345", "1080p", "REMUX", "GRouP") → "12345|1080P|REMUX|GROUP"
    """
    tid  = str(tmdb_id).strip() if tmdb_id else "0"
    res  = resolution.upper().strip() if resolution else "UNKNOWN"
    cod  = codec.upper().strip()      if codec      else "UNKNOWN"
    grp  = group.upper().strip()      if group      else "UNKNOWN"
    return f"{tid}|{res}|{cod}|{grp}"


def parse_icu_from_tracker_name(name: str, tmdb_id) -> str:
    """
    CSI v2.0: Extrae un ICU del nombre de un torrent obtenido del tracker/scraper.
    Usa normalize_folder_name() para parsear los componentes y luego build_icu().
    """
    components = normalize_folder_name(name)
    return build_icu(
        tmdb_id,
        components.get("resolution", ""),
        components.get("codec", ""),
        components.get("group", ""),
    )


def _size_entropy_tiebreak(local_size_bytes: int, tracker_size_bytes: int,
                           tolerance: float = 0.05) -> bool:
    """
    CSI v2.0: Factor de desempate por tamaño de archivo.
    Si dos torrents del mismo TMDB_ID tienen ICUs distintos pero tamaños similares,
    probablemente son la misma versión con metadatos mal parseados.
    Tolerancia: ±5% del tamaño (configurable).
    Retorna True si los tamaños están dentro de la tolerancia → probable mismo contenido.
    """
    if not local_size_bytes or not tracker_size_bytes:
        return False
    ratio = abs(local_size_bytes - tracker_size_bytes) / max(local_size_bytes, tracker_size_bytes)
    within = ratio <= tolerance
    dbg(f"[ICU] Size entropy tiebreak: local={local_size_bytes} tracker={tracker_size_bytes} "
        f"ratio={ratio:.3f} tolerance={tolerance:.2f} → {'MATCH' if within else 'DIFF'}")
    return within

# ─── ID RESOLVER CACHE ────────────────────────────────────────────────────────
# CSI v3.2: persistent cache for folder→ids resolutions.
#
# Lives at REPORTS_DIR / "id_resolver_cache.json". Keyed by the folder basename
# (lowercased). Values include both positive resolutions (ids found) and
# negative ones (no match anywhere) with a TTL so we retry a misfire later
# rather than re-querying TMDB on every single scan.
#
# Negative entries are *much* more numerous than positives for a healthy
# library (every legit-untracked or junk folder), so storing them is what
# makes the second scan fast. 30-day TTL means a folder that gained an entry
# upstream gets picked up eventually without manual intervention.
class IdResolverCache:
    NEG_TTL_SECONDS = 30 * 24 * 3600   # retry "not found" entries after 30 days
    SCHEMA_VERSION  = "1.0"

    def __init__(self, path: Path):
        self.path = path
        self._entries: dict = {}
        self._dirty = False

    def load(self):
        if not self.path.exists():
            dbg(f"[RESOLVER] No cache at {self.path} — starting empty")
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != self.SCHEMA_VERSION:
                dbg(f"[RESOLVER] Schema mismatch — discarding cache")
                return
            self._entries = data.get("entries", {}) or {}
            dbg(f"[RESOLVER] Loaded {len(self._entries)} entries from {self.path}")
        except Exception as e:
            dbg(f"[RESOLVER] Load error ({e}) — starting empty")

    def save(self):
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version":      self.SCHEMA_VERSION,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "entries":      self._entries,
            }
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
            self._dirty = False
            dbg(f"[RESOLVER] Saved {len(self._entries)} entries to {self.path}")
        except Exception as e:
            dbg(f"[RESOLVER] Save error: {e}")

    def get(self, folder_name: str) -> Optional[dict]:
        """Return cached entry or None. Negative entries expire after NEG_TTL."""
        key = (folder_name or "").lower()
        entry = self._entries.get(key)
        if not entry:
            return None
        # Negative-entry TTL — let the resolver retry stale "not found"s.
        if not (entry.get("tmdb_id") or entry.get("imdb_id") or entry.get("tvdb_id")):
            try:
                ts = datetime.fromisoformat(entry.get("resolved_at", "").replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > self.NEG_TTL_SECONDS:
                    dbg(f"[RESOLVER] Negative entry expired for '{key}' — re-resolve")
                    return None
            except Exception:
                return None
        return entry

    def set(self, folder_name: str, ids: dict, source: str):
        key = (folder_name or "").lower()
        self._entries[key] = {
            "tmdb_id":      str(ids.get("tmdb_id") or ""),
            "imdb_id":      str(ids.get("imdb_id") or "").replace("tt", ""),
            "tvdb_id":      str(ids.get("tvdb_id") or ""),
            "mal_id":       str(ids.get("mal_id")  or ""),
            "media_type":   ids.get("media_type") or "",       # "movie"|"tv"
            "resolved_via": source,
            "resolved_at":  datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True


# ─── METADATA MANAGER ─────────────────────────────────────────────────────────
class MetadataManager:
    """
    Loads Sonarr and Radarr catalogs and provides folder-based lookups.
    Uses the `path` field from Sonarr/Radarr so folder names match exactly,
    avoiding fuzzy title comparisons.
    Also extracts Sonarr IDs embedded in folder names (TVdbID pattern).
    """

    def __init__(self, config: dict):
        self.cfg = config

        # ── Primary lookup: folder basename (the "saved us" trick) ────────────
        # Keys are lower-cased basenames of the Arr's reported `path`. The full
        # mount prefix is stripped on purpose so we can match across very
        # different filesystem layouts (Arr at /Downloads/X, CSI walking
        # /hardlinks/NOBS/X, etc).
        self._sonarr_by_path  = {}   # basename(path).lower() → series
        self._radarr_by_path  = {}   # basename(path).lower() → movie

        # ── Normalized basename map ───────────────────────────────────────────
        # Same key, but run through normalize_folder_name() so we can match
        # when the library kept a different extra suffix than Arr (e.g. Arr
        # renamed to "Movie (2024)" but hardlink is "Movie 2024 [1080p]").
        self._sonarr_by_norm  = {}
        self._radarr_by_norm  = {}

        # ── Reverse id indexes ────────────────────────────────────────────────
        # Built so [tmdb-N]/[imdb-tt…]/[tvdb-N] suffixes in a folder name (the
        # Plex/Jellyfin convention) can jump straight to the Arr entry.
        self._sonarr_by_tvdb  = {}
        self._sonarr_by_tmdb  = {}   # NEW
        self._sonarr_by_imdb  = {}   # NEW (numeric form, no 'tt')
        self._radarr_by_tmdb  = {}   # NEW
        self._radarr_by_imdb  = {}   # NEW

        # Resolver cache — folder_name → resolved ids. Lazy-loaded.
        self._resolver_cache: Optional[IdResolverCache] = None

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers — normalize key, embedded id extraction, fuzzy ratio.
    # Kept on the class so the same routines are used at load time and lookup.
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_key(name: str) -> str:
        """Delegate to module-level `_norm_folder_key` so MetadataManager and
        TrackerIndex hash strings identically (W5 name-fallback rely on it)."""
        return _norm_folder_key(name)

    @staticmethod
    def _embedded_ids(name: str) -> dict:
        """
        Extract Plex/Jellyfin-style id suffixes from a folder name:
          [tmdb-12345]   [tmdbid-12345]   {tmdb-12345}
          [imdb-tt1234567]
          [tvdb-67890]
        Also catches the legacy CSI pattern "TVdbID-N" and "YYYY-NNNNNN".
        """
        out = {"tmdb": "", "imdb": "", "tvdb": ""}
        if not name:
            return out
        # tmdb
        m = re.search(r"[\[\{](?:tmdb(?:id)?)[- _:]+(\d+)[\]\}]", name, re.I)
        if m: out["tmdb"] = m.group(1)
        # imdb (with or without 'tt' prefix)
        m = re.search(r"[\[\{](?:imdb(?:id)?)[- _:]+(?:tt)?(\d{6,10})[\]\}]", name, re.I)
        if m: out["imdb"] = m.group(1)
        # tvdb
        m = re.search(r"[\[\{](?:tvdb(?:id)?)[- _:]+(\d+)[\]\}]", name, re.I)
        if m: out["tvdb"] = m.group(1)
        # Legacy CSI pattern
        if not out["tvdb"]:
            m = re.search(r"TVdbID[- _](\d+)", name, re.I)
            if m: out["tvdb"] = m.group(1)
        if not out["tvdb"]:
            m = re.search(r"\d{4}-(\d{5,7})(?:\b|$|-)", name)
            if m: out["tvdb"] = m.group(1)
        return out

    @staticmethod
    def _fuzzy_ratio(a: str, b: str) -> float:
        """Cheap zero-dep string similarity (0..1). Used as last-resort match."""
        if not a or not b:
            return 0.0
        return difflib.SequenceMatcher(None, a, b).ratio()

    def load_sonarr(self):
        if not (self.cfg["SONARR_URL"] and self.cfg["SONARR_API_KEY"]):
            dbg("Sonarr not configured — skipping")
            return
        try:
            api_limiter.wait()
            r = requests.get(
                f"{self.cfg['SONARR_URL']}/api/v3/series",
                headers={"X-Api-Key": self.cfg["SONARR_API_KEY"], "User-Agent": "undici"},
                timeout=15,
            )
            for s in r.json():
                if s.get("path"):
                    base = Path(s["path"]).name
                    self._sonarr_by_path[base.lower()] = s
                    nk = self._norm_key(base)
                    if nk:
                        self._sonarr_by_norm[nk] = s
                tvdb = str(s.get("tvdbId") or "")
                if tvdb:
                    self._sonarr_by_tvdb[tvdb] = s
                tmdb = str(s.get("tmdbId") or "")        # Sonarr v3 exposes tmdbId
                if tmdb and tmdb != "0":
                    self._sonarr_by_tmdb[tmdb] = s
                imdb = str(s.get("imdbId") or "").replace("tt", "")
                if imdb:
                    self._sonarr_by_imdb[imdb] = s
            dbg(f"Sonarr: {len(self._sonarr_by_path)} series loaded "
                f"(tmdb={len(self._sonarr_by_tmdb)}, imdb={len(self._sonarr_by_imdb)}, "
                f"tvdb={len(self._sonarr_by_tvdb)}, norm={len(self._sonarr_by_norm)})")
        except Exception as e:
            dbg(f"Sonarr load error: {e}")

    def load_radarr(self):
        if not (self.cfg["RADARR_URL"] and self.cfg["RADARR_API_KEY"]):
            dbg("Radarr not configured — skipping")
            return
        try:
            api_limiter.wait()
            r = requests.get(
                f"{self.cfg['RADARR_URL']}/api/v3/movie",
                headers={"X-Api-Key": self.cfg["RADARR_API_KEY"], "User-Agent": "undici"},
                timeout=15,
            )
            for m in r.json():
                if m.get("path"):
                    base = Path(m["path"]).name
                    self._radarr_by_path[base.lower()] = m
                    nk = self._norm_key(base)
                    if nk:
                        self._radarr_by_norm[nk] = m
                tmdb = str(m.get("tmdbId") or "")
                if tmdb and tmdb != "0":
                    self._radarr_by_tmdb[tmdb] = m
                imdb = str(m.get("imdbId") or "").replace("tt", "")
                if imdb:
                    self._radarr_by_imdb[imdb] = m
            dbg(f"Radarr: {len(self._radarr_by_path)} movies loaded "
                f"(tmdb={len(self._radarr_by_tmdb)}, imdb={len(self._radarr_by_imdb)}, "
                f"norm={len(self._radarr_by_norm)})")
        except Exception as e:
            dbg(f"Radarr load error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v3.2: Layered lookup chain. Each tier is cheaper than the next; we
    # short-circuit on the first hit. The same chain shape is used for Sonarr
    # (series) and Radarr (movies):
    #   1. exact basename
    #   2. normalized basename (strip year/tech/tags, lowercase, collapse ws)
    #   3. embedded ids [tmdb-N]/[imdb-tt…]/[tvdb-N]/TVdbID-N → reverse index
    #   4. fuzzy normalized match (≥0.88 ratio, single best winner)
    #   5. external resolver (TMDB /search/multi) — cached
    # Returns the matching Arr entry dict, or None when even the resolver
    # comes up empty.
    # ──────────────────────────────────────────────────────────────────────────

    FUZZY_THRESHOLD = 0.88   # SequenceMatcher.ratio() floor for tier 4

    def get_series(self, folder_name: str) -> Optional[dict]:
        # Tier 1 — exact basename
        s = self._sonarr_by_path.get(folder_name.lower())
        if s:
            return s

        # Tier 2 — normalized
        nk = self._norm_key(folder_name)
        if nk:
            s = self._sonarr_by_norm.get(nk)
            if s:
                dbg(f"Sonarr match via normalized key '{nk}' for '{folder_name}'")
                return s

        # Tier 3 — embedded ids
        ids = self._embedded_ids(folder_name)
        if ids["tvdb"]:
            s = self._sonarr_by_tvdb.get(ids["tvdb"])
            if s:
                dbg(f"Sonarr match via tvdb={ids['tvdb']} for '{folder_name}'")
                return s
        if ids["tmdb"]:
            s = self._sonarr_by_tmdb.get(ids["tmdb"])
            if s:
                dbg(f"Sonarr match via tmdb={ids['tmdb']} for '{folder_name}'")
                return s
        if ids["imdb"]:
            s = self._sonarr_by_imdb.get(ids["imdb"])
            if s:
                dbg(f"Sonarr match via imdb={ids['imdb']} for '{folder_name}'")
                return s

        # Tier 4 — fuzzy on normalized keys
        if nk and self._sonarr_by_norm:
            best_key, best_ratio = "", 0.0
            for k in self._sonarr_by_norm:
                r = self._fuzzy_ratio(nk, k)
                if r > best_ratio:
                    best_ratio, best_key = r, k
            if best_ratio >= self.FUZZY_THRESHOLD:
                dbg(f"Sonarr fuzzy match '{best_key}' (ratio={best_ratio:.2f}) for '{folder_name}'")
                return self._sonarr_by_norm[best_key]

        # Tier 5 — external resolver (TMDB → reverse map). Only when allowed.
        ext = self._external_resolve(folder_name, hint_type="tv")
        if ext and ext.get("media_type") == "tv":
            tmdb_id = ext.get("tmdb_id") or ""
            tvdb_id = ext.get("tvdb_id") or ""
            if tmdb_id and tmdb_id in self._sonarr_by_tmdb:
                dbg(f"Sonarr match via external→tmdb={tmdb_id} for '{folder_name}'")
                return self._sonarr_by_tmdb[tmdb_id]
            if tvdb_id and tvdb_id in self._sonarr_by_tvdb:
                dbg(f"Sonarr match via external→tvdb={tvdb_id} for '{folder_name}'")
                return self._sonarr_by_tvdb[tvdb_id]
            # External says "this exists on TMDB" but not in Sonarr — return a
            # synthetic entry so callers still have ids to feed the tracker.
            dbg(f"Sonarr no entry but external resolved → synthetic for '{folder_name}'")
            return {
                "title":       ext.get("title") or folder_name,
                "tmdbId":      int(tmdb_id) if tmdb_id.isdigit() else 0,
                "tvdbId":      int(tvdb_id) if tvdb_id.isdigit() else 0,
                "imdbId":      ("tt" + ext["imdb_id"]) if ext.get("imdb_id") else "",
                "_synthetic":  True,
                "_source":     "external",
            }

        dbg(f"Sonarr: no match for folder '{folder_name}' across all tiers")
        return None

    def get_movie(self, folder_name: str) -> Optional[dict]:
        # Tier 1 — exact basename
        m = self._radarr_by_path.get(folder_name.lower())
        if m:
            return m

        # Tier 2 — normalized
        nk = self._norm_key(folder_name)
        if nk:
            m = self._radarr_by_norm.get(nk)
            if m:
                dbg(f"Radarr match via normalized key '{nk}' for '{folder_name}'")
                return m

        # Tier 3 — embedded ids
        ids = self._embedded_ids(folder_name)
        if ids["tmdb"]:
            m = self._radarr_by_tmdb.get(ids["tmdb"])
            if m:
                dbg(f"Radarr match via tmdb={ids['tmdb']} for '{folder_name}'")
                return m
        if ids["imdb"]:
            m = self._radarr_by_imdb.get(ids["imdb"])
            if m:
                dbg(f"Radarr match via imdb={ids['imdb']} for '{folder_name}'")
                return m

        # Tier 4 — fuzzy
        if nk and self._radarr_by_norm:
            best_key, best_ratio = "", 0.0
            for k in self._radarr_by_norm:
                r = self._fuzzy_ratio(nk, k)
                if r > best_ratio:
                    best_ratio, best_key = r, k
            if best_ratio >= self.FUZZY_THRESHOLD:
                dbg(f"Radarr fuzzy match '{best_key}' (ratio={best_ratio:.2f}) for '{folder_name}'")
                return self._radarr_by_norm[best_key]

        # Tier 5 — external resolver
        ext = self._external_resolve(folder_name, hint_type="movie")
        if ext and ext.get("media_type") == "movie":
            tmdb_id = ext.get("tmdb_id") or ""
            imdb_id = ext.get("imdb_id") or ""
            if tmdb_id and tmdb_id in self._radarr_by_tmdb:
                dbg(f"Radarr match via external→tmdb={tmdb_id} for '{folder_name}'")
                return self._radarr_by_tmdb[tmdb_id]
            if imdb_id and imdb_id in self._radarr_by_imdb:
                dbg(f"Radarr match via external→imdb={imdb_id} for '{folder_name}'")
                return self._radarr_by_imdb[imdb_id]
            dbg(f"Radarr no entry but external resolved → synthetic for '{folder_name}'")
            return {
                "title":      ext.get("title") or folder_name,
                "tmdbId":     int(tmdb_id) if tmdb_id.isdigit() else 0,
                "imdbId":     ("tt" + imdb_id) if imdb_id else "",
                "_synthetic": True,
                "_source":    "external",
            }

        dbg(f"Radarr: no match for folder '{folder_name}' across all tiers")
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 5 implementation — external resolver. Backed by IdResolverCache so
    # repeated CSI runs don't pound TMDB. Picks the best candidate by name
    # similarity AND year; falls back to first result for short queries.
    # ──────────────────────────────────────────────────────────────────────────

    def _resolver(self) -> IdResolverCache:
        if self._resolver_cache is None:
            self._resolver_cache = IdResolverCache(ID_RESOLVER_CACHE_PATH)
            self._resolver_cache.load()
        return self._resolver_cache

    def _external_resolve(self, folder_name: str,
                          hint_type: Optional[str] = None) -> Optional[dict]:
        """
        Resolve ids for a folder by name via TMDB /search/multi.
        hint_type: "tv" / "movie" — biases tie-breaks but doesn't filter out
        the other type (TMDB sometimes mis-types anime as movie or vice-versa).
        Cached. Returns the entry dict or None on hard miss.
        """
        cache = self._resolver()
        hit = cache.get(folder_name)
        if hit is not None:
            # Treat negative cache as None to caller
            if not (hit.get("tmdb_id") or hit.get("imdb_id") or hit.get("tvdb_id")):
                return None
            return hit

        tmdb_key = self.cfg.get("TMDB_API_KEY") or ""
        if not tmdb_key:
            dbg(f"[RESOLVER] No TMDB key — cannot externally resolve '{folder_name}'")
            return None

        comps = normalize_folder_name(folder_name)
        query = (comps.get("base_name") or folder_name).strip()
        year  = (comps.get("year") or "").strip()
        if not query:
            return None

        try:
            api_limiter.wait()
            params = {"api_key": tmdb_key, "query": query, "include_adult": "false"}
            if year:
                # TMDB /search/multi doesn't accept year; we use it for tie-break
                pass
            r = requests.get(
                "https://api.themoviedb.org/3/search/multi",
                params=params,
                headers={"Accept": "application/json", "User-Agent": "csi-resolver"},
                timeout=15,
            )
            if r.status_code != 200:
                dbg(f"[RESOLVER] TMDB HTTP {r.status_code} for '{query}'")
                cache.set(folder_name, {}, source="tmdb_error")
                return None
            results = (r.json() or {}).get("results", []) or []
        except Exception as e:
            dbg(f"[RESOLVER] TMDB exception '{query}': {e}")
            return None

        # Keep only movie + tv (drop person)
        results = [x for x in results if x.get("media_type") in ("movie", "tv")]
        if not results:
            cache.set(folder_name, {}, source="tmdb_empty")
            cache.save()
            dbg(f"[RESOLVER] TMDB returned 0 for '{query}' (cached negative)")
            return None

        # Pick the best — combined title similarity + year match + hint bias
        nk = self._norm_key(query)
        def score(item):
            title = item.get("title") or item.get("name") or ""
            it_year = ""
            for f in ("release_date", "first_air_date"):
                if item.get(f):
                    it_year = item[f][:4]; break
            ratio = self._fuzzy_ratio(nk, self._norm_key(title))
            year_bonus = 0.15 if (year and it_year and year == it_year) else 0.0
            hint_bonus = 0.05 if (hint_type and item.get("media_type") == hint_type) else 0.0
            popularity = float(item.get("popularity") or 0) / 1000.0  # mild
            return ratio + year_bonus + hint_bonus + popularity

        best = max(results, key=score)
        media_type = best.get("media_type")
        tmdb_id = str(best.get("id") or "")
        title   = best.get("title") or best.get("name") or ""

        # Optionally fetch external_ids to populate imdb_id (and tvdb for tv).
        # One extra call per resolution — paced by api_limiter.
        imdb_id, tvdb_id = "", ""
        if media_type and tmdb_id:
            try:
                api_limiter.wait()
                ext_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
                r2 = requests.get(ext_url,
                                  params={"api_key": tmdb_key},
                                  headers={"Accept": "application/json"},
                                  timeout=15)
                if r2.status_code == 200:
                    ext = r2.json() or {}
                    imdb_id = str(ext.get("imdb_id") or "").replace("tt", "")
                    tvdb_id = str(ext.get("tvdb_id") or "")
            except Exception as e:
                dbg(f"[RESOLVER] external_ids exception for {tmdb_id}: {e}")

        out = {
            "tmdb_id":    tmdb_id,
            "imdb_id":    imdb_id,
            "tvdb_id":    tvdb_id,
            "media_type": media_type,
            "title":      title,
        }
        cache.set(folder_name, out, source="tmdb_search")
        cache.save()
        dbg(f"[RESOLVER] TMDB resolved '{folder_name}' → {media_type} tmdb={tmdb_id} "
            f"imdb={imdb_id} tvdb={tvdb_id} (\"{title}\")")
        return out

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v3.2: Multi-signal TV/Movie classifier.
    #
    # Returns dict:
    #   media_type:  "tv" | "movie" | "unknown"
    #   season_num:  int | None — best guess of the season being looked at
    #   confidence:  float (0..1) — for the INCIDENCIA bucket if low
    #   reason:      str — which signal won (debug)
    #
    # Signals, in order of evaluation:
    #   S6  Sonarr/Radarr knows this folder → trust Arr completely
    #   S2  Filename(s) contain S\d+E\d+ / S\d+ / Season N
    #   S3  Filename(s) contain "\d+ - " anime-episode numbering AND ≥3 files
    #   S1  Parent dir contains "Season N" / "S\d\d" / lang variants
    #   S4  File count vote: 1-2 → movie-lean, ≥3 → tv-lean (weak)
    #   S5  Mediainfo runtime: <70 min → tv-lean, ≥80 min → movie-lean (weak)
    #   S7  External TMDB lookup (only when other signals tie)
    # ──────────────────────────────────────────────────────────────────────────

    _RX_PARENT_SEASON = re.compile(
        r"(?:season|temporada|saison|staffel|stagione|seizoen|sezon|сезон)\s*\d+|\bS\d{2}\b",
        re.I,
    )
    _RX_FILE_SE       = re.compile(r"[ ._]S\d{1,2}(?:E\d{1,3})?\b|Season\s*\d+", re.I)
    _RX_ANIME_EP      = re.compile(r"\b\d{1,3}\s*-\s+", re.I)
    _RX_SEASON_NUM    = re.compile(
        r"(?:season|temporada|saison|staffel|stagione|seizoen|sezon|сезон)\s*(\d+)|S(\d{1,2})",
        re.I,
    )
    # CSI v3.4 (W9): mini-series numbering like "1 de 6", "1 of 6", "1of6",
    # "Part 1", "Capítulo 1", "Episodio 1". Used as a separate signal: when
    # ≥2 files in the same folder match → strong TV indicator. Avoids the
    # single-file movie false positive (e.g., "Lord of the Rings Part 1").
    _RX_MINISERIES    = re.compile(
        r"\b\d+\s*(?:de|of)\s*\d+\b"        # "1 de 6" / "1 of 6"
        r"|\b\d+of\d+\b"                    # "1of6"
        r"|\bPart\s*\d+\b"                  # "Part 1"
        r"|\bParte\s*\d+\b"                 # "Parte 1"
        r"|\bCap[íi]tulo\s*\d+\b"           # "Capítulo 1"
        r"|\bEpisodio\s*\d+\b"              # "Episodio 1"
        r"|\bEp(?:isode)?\.?\s*\d+\b",      # "Ep 1" / "Episode 1"
        re.I,
    )
    # CSI v3.4 (W10): filename patterns identifying extras / non-content
    # videos that should NEVER trigger an upload candidate. Drop these from
    # mkv_files before classification + ICU building.
    _RX_EXTRAS_FILE = re.compile(
        r"^(?:opening|ending|op|ed|menu|sample|trailer|teaser|preview|"
        r"intro|outro|nc[- _]?op|nc[- _]?ed|behind|making[- _]?of|extras?)"
        r"[\s._-]*\d*\.[a-z0-9]+$",
        re.I,
    )

    def classify_folder(self, root_path: Path, lib_path: Path,
                        mkv_files: list) -> dict:
        """Multi-signal classification. See block comment above."""
        out = {"media_type": "unknown", "season_num": None, "confidence": 0.0, "reason": ""}
        if not mkv_files:
            return out

        rel_parts = root_path.relative_to(lib_path).parts
        parent_name  = root_path.name
        parent_2up   = root_path.parent.name
        # CSI v3.4 (W11): never use the library root as an Arr-lookup
        # candidate. lib_path.name could be "NOBS", "Movies", "Library" —
        # generic strings that the external resolver may have cached as a
        # bogus movie hit, polluting every top-level folder's classification.
        lib_root_name = lib_path.name
        candidates = [c for c in (parent_name, parent_2up)
                      if c and c != lib_root_name]

        # ── S6 — Arr override (strongest, free) ───────────────────────────────
        # Check the immediate dir name AND the parent (covers Show/Season N).
        for candidate in candidates:
            if self.get_series(candidate):
                # Found in Sonarr — pull the season number from path.
                m = self._RX_SEASON_NUM.search(parent_name)
                season = int(m.group(1) or m.group(2)) if m else None
                # If the immediate dir IS the show (no season subdir), look
                # inside the filenames for a season hint.
                if season is None:
                    for f in mkv_files:
                        m2 = self._RX_SEASON_NUM.search(f)
                        if m2:
                            season = int(m2.group(1) or m2.group(2))
                            break
                if season is None:
                    season = 1   # default for flat per-episode anime
                out.update(media_type="tv", season_num=season, confidence=0.95,
                           reason=f"S6:sonarr({candidate!r})")
                return out
        for candidate in candidates:
            if self.get_movie(candidate):
                out.update(media_type="movie", confidence=0.95,
                           reason=f"S6:radarr({candidate!r})")
                return out

        # ── S2 — filename SxxExx / Season N ──────────────────────────────────
        se_hits = sum(1 for f in mkv_files if self._RX_FILE_SE.search(f))
        if se_hits >= 1:
            # Find season from first matching file
            season = None
            for f in mkv_files:
                m = self._RX_SEASON_NUM.search(f)
                if m:
                    season = int(m.group(1) or m.group(2))
                    break
            out.update(media_type="tv", season_num=season or 1, confidence=0.85,
                       reason=f"S2:file_se({se_hits} files)")
            return out

        # ── S3 — anime-episode pattern, only when there are several files ────
        anime_hits = sum(1 for f in mkv_files if self._RX_ANIME_EP.search(f))
        if anime_hits >= 3:
            out.update(media_type="tv", season_num=1, confidence=0.75,
                       reason=f"S3:anime_ep({anime_hits} files)")
            return out

        # ── S3b — mini-series numbering "X de Y" / "X of Y" / "Part N" ──────
        # Requires ≥2 hits in the same folder to avoid single-file movie false
        # positives like "Lord of the Rings - Part 1". NATGEO "1 de 6 La
        # Agresion" / "2 de 6 La Derrota..." catches here.
        mini_hits = sum(1 for f in mkv_files if self._RX_MINISERIES.search(f))
        if mini_hits >= 2:
            out.update(media_type="tv", season_num=1, confidence=0.80,
                       reason=f"S3b:miniseries({mini_hits} files)")
            return out

        # ── S1 — parent dir Season/S\d\d (the old logic, kept as a tier) ─────
        if any(self._RX_PARENT_SEASON.search(p) for p in rel_parts[-2:]):
            m = self._RX_SEASON_NUM.search(parent_name)
            season = int(m.group(1) or m.group(2)) if m else None
            out.update(media_type="tv", season_num=season or 1, confidence=0.80,
                       reason="S1:parent_season")
            return out

        # ── S4/S5 weak signals + S7 external — only when nothing fired ───────
        file_count = len(mkv_files)
        movie_lean = (file_count <= 2)
        tv_lean    = (file_count >= 3)
        weak_reason = f"S4:files={file_count}"

        # S7 — external lookup as tie-breaker.
        ext = self._external_resolve(parent_name, hint_type=("tv" if tv_lean else "movie"))
        if ext and ext.get("media_type") in ("tv", "movie"):
            mt = ext["media_type"]
            out.update(media_type=mt,
                       season_num=(1 if mt == "tv" else None),
                       confidence=0.70,
                       reason=f"S7:tmdb({mt}) + {weak_reason}")
            return out

        # Fall back to weak file-count vote with low confidence.
        if movie_lean:
            out.update(media_type="movie", confidence=0.40, reason=weak_reason)
        elif tv_lean:
            out.update(media_type="tv", season_num=1, confidence=0.40,
                       reason=weak_reason)
        return out

    def get_aired_episodes(self, sonarr_series_id: int, season_num: int) -> Optional[int]:
        """Count aired episodes (airDateUtc < now UTC) for a Sonarr season."""
        try:
            api_limiter.wait()
            r = requests.get(
                f"{self.cfg['SONARR_URL']}/api/v3/episode",
                params={"seriesId": sonarr_series_id, "seasonNumber": season_num},
                headers={"X-Api-Key": self.cfg["SONARR_API_KEY"], "User-Agent": "undici"},
                timeout=15,
            )
            now = datetime.now(timezone.utc)
            count = sum(
                1 for ep in r.json()
                if ep.get("airDateUtc") and
                datetime.fromisoformat(ep["airDateUtc"].replace("Z", "+00:00")) < now
            )
            dbg(f"Sonarr aired episodes series={sonarr_series_id} S{season_num}: {count}")
            return count
        except Exception as e:
            dbg(f"Sonarr episodes error: {e}")
            return None

# ─── TRACKER INDEX ────────────────────────────────────────────────────────────
class TrackerIndex:
    """
    CSI v2.0: Índice en memoria del estado del tracker.
    Construido desde la API UNIT3D y/o el scraper HTTP.
    Persistido en tracker_index.json entre sesiones (eliminando la amnesia).

    Jerarquía de fuentes (orden de construcción):
        A. Scraper HTTP (cookie session)  → fuente principal de uploads del usuario
        B. Local TMP JSONs               → historial suplementario (no crítico)
        C. qBittorrent Client            → seeding paths para cruce de estado

    Métodos de consulta:
        has_movie_state()     → retorna estado CSI (ESTADO_OK, DUPE, etc.)
        has_tv_season_state() → retorna estado CSI para temporadas
        has_movie()           → wrapper bool legacy (compatibilidad)
        has_tv_season()       → wrapper bool legacy (compatibilidad)
    """

    def __init__(self):
        self.movie_tmdb    = set()   # str tmdb IDs de películas en tracker
        self.movie_imdb    = set()   # str imdb IDs (numeric, sin 'tt')
        self.movie_tvdb    = set()   # CSI v3.0 (A1): str tvdb IDs de películas (raro pero soportado)
        self.tv_seasons    = set()   # (tmdb_str, season_str) — temporadas en tracker
        self.tv_tvdb       = set()   # (tvdb_str, season_str) — TV por TVDB
        self.tv_imdb       = set()   # CSI v3.0 (A1): (imdb_str_norm, season_str) — TV por IMDB
        self.seeding_names = set()      # paths absolutos (lower) desde qBit — compat v1.x
        self.seeding_paths = set()      # paths absolutos (lower) desde qBit — v2.0
        self.client_path_map: dict = {} # CSI v3.0: abs_path_lower → {hash, size}

        # CSI v2.0: ICU Index — fingerprint exacto por versión de torrent
        # Clave: ICU string ("TMDB|RES|CODEC|GROUP") → metadata del torrent
        self.icu_index: dict = {}    # ICU_str → {"torrent_id", "name", "size_bytes"}

        # CSI v2.0: URL del tracker registrada al construir el índice
        # Usada para invalidar el JSON cacheado si cambia el tracker
        self.tracker_url: str = ""

        # CSI v2.0: Flag de sonda — True si el último intento de consulta falló
        # Impide marcar ítems como ESTADO_NO_SUBIDO cuando no hay certeza
        self._probe_failed: bool = False

        # CSI v3.1: Flag de auth — True si la cookie del scraper está expirada
        # Impide marcar ítems como ESTADO_NO_SUBIDO con datos de scraper inválidos
        self._auth_failed: bool = False

        # CSI v3.2 (W5 — name-fallback): mapa de nombres normalizados de
        # torrent a metadatos básicos. Lo usamos como red de seguridad en
        # has_*_state cuando los id-sets fallan (carpeta sin ids tras 5-tier
        # fallback, o torrent ingestado sin tmdb/imdb/tvdb por bug del
        # template HTML). Clave: _norm_folder_key(name). Valor: lista de
        # entradas {torrent_id, name, is_tv, season_number, ids}.
        # Lista porque dos torrents pueden compartir clave normalizada
        # (re-encodes, distintas temporadas, etc).
        self.tracker_names: dict = {}

        # CSI v3.1 (categorías dinámicas): mapa cat_id (str) → {
        #   "name": str,        # nombre humano tal como llega del tracker
        #   "is_tv": bool,
        #   "is_movie": bool,
        #   "is_anime": bool,
        # }
        # Sembrado por bootstrap_categories() al inicio de build_user() con UNA
        # llamada a /api/torrents/filter?perPage=200, persistido entre sesiones.
        # Reemplaza el viejo hardcode `cat_id == "1" / "0"` que sólo funcionaba
        # contra el layout de categorías por defecto de UNIT3D y rompía cuando
        # el admin reordenaba o añadía categorías (Arcade, Anime, E-Books, etc).
        self.categories: dict = {}

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v3.1: Bootstrap dinámico de categorías
    # ──────────────────────────────────────────────────────────────────────────
    #
    # Cada UNIT3D puede definir sus propias categorías y reordenarlas a gusto.
    # El layout por defecto pone Movies=1 / TV=2 pero NOBS, por ejemplo, lleva:
    #   1 Movies | 2 TV | 3 Arcade | 4 Anime Movies | 5 Anime TV Shows
    #   7 E-Books | 8 Audiobooks
    # Y se prevé que esa lista siga creciendo. Hardcodear los ids o trustear
    # "1=TV" rompe cada vez que un admin reorganiza. Solución: pedir una
    # muestra al endpoint público de filtro (1 llamada) y derivar la tabla
    # cat_id → tipo por keywords sobre el nombre humano. Multi-idioma OK.

    _CAT_TV_KEYWORDS    = r"\btv\b|series?|shows?|programas?|temporadas?|épisode|episodes?"
    _CAT_MOVIE_KEYWORDS = r"movies?|films?|pel[ií]culas?|cin[eé]"
    _CAT_ANIME_KEYWORDS = r"anime|manga|otaku"

    @classmethod
    def _classify_cat_name(cls, name: str) -> dict:
        """Return {is_tv, is_movie, is_anime} for a free-form category name."""
        n = (name or "").lower()
        is_anime = bool(re.search(cls._CAT_ANIME_KEYWORDS, n, re.I))
        is_tv    = bool(re.search(cls._CAT_TV_KEYWORDS,    n, re.I))
        is_movie = bool(re.search(cls._CAT_MOVIE_KEYWORDS, n, re.I)) and not is_tv
        # Tie-breaker: "Anime Movies" → movie=True, tv=False, anime=True.
        # "Anime TV Shows" → tv=True, movie=False, anime=True.
        return {"is_tv": is_tv, "is_movie": is_movie, "is_anime": is_anime}

    def bootstrap_categories(self, config: dict) -> int:
        """
        Build self.categories with one API call. Fallbacks:
          1) /api/torrents/filter?perPage=200 (preferred — gives many cat_ids)
          2) parse /torrents page (cookie scraper) for the filter dropdown
          3) leave self.categories empty → _ingest falls back to name regex
        Returns the number of categories registered.
        """
        base = (config.get("TRACKER_URL") or "").rstrip("/")
        api_key = config.get("TRACKER_API_KEY") or ""
        if not base:
            dbg("[CATBOOT] No TRACKER_URL — categories bootstrap skipped")
            return 0

        # ── Source 1: API filter (sample for variety) ────────────────────────
        if api_key:
            try:
                api_limiter.wait()
                url = f"{base}/api/torrents/filter"
                r = requests.get(
                    url,
                    params={"api_token": api_key, "perPage": 200},
                    headers={"Accept": "application/json", "User-Agent": "undici"},
                    timeout=20,
                )
                if r.status_code == 200:
                    data = r.json().get("data", []) or []
                    seen: dict = {}
                    for t in data:
                        a = t.get("attributes", {}) or {}
                        cid  = str(a.get("category_id") or "").strip()
                        cname = (a.get("category") or "").strip()
                        if not cid or cid in seen:
                            continue
                        cls = self._classify_cat_name(cname)
                        seen[cid] = {"name": cname, **cls}
                    if seen:
                        self.categories.update(seen)
                        dbg(f"[CATBOOT] API: {len(seen)} categorías → {seen}")
                        return len(seen)
                    dbg("[CATBOOT] API responded but 0 unique cats in sample")
                else:
                    dbg(f"[CATBOOT] API HTTP {r.status_code} — caerá a HTML fallback")
            except Exception as e:
                dbg(f"[CATBOOT] API exception: {e} — caerá a HTML fallback")

        # ── Source 2: HTML dropdown (cookie scraper) ─────────────────────────
        # Inertia/Vue payload sits in <div ... data-page="<urlencoded json>">.
        # We don't parse the whole payload; we just look for a flat "categories"
        # array of {id, name, *_meta} via a permissive regex. Cheap and robust.
        try:
            sess = _session(config)
            r = sess.get(f"{base}/torrents", timeout=20)
            if r.status_code == 200:
                # Common UNIT3D shapes seen in the wild:
                #   "categories":[{"id":1,"name":"Movies","movie_meta":1,...}, ...]
                seen2: dict = {}
                for m in re.finditer(
                    r'\{[^{}]*"id"\s*:\s*(\d+)[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}',
                    r.text,
                ):
                    cid = m.group(1)
                    nm  = m.group(2)
                    # Only keep entries that look category-shaped (have at least
                    # one *_meta keyword nearby). Heuristic, avoids picking up
                    # unrelated `{"id":..,"name":...}` objects on the page.
                    if not re.search(r'(movie|tv|game|music|no|anime)_meta', m.group(0), re.I):
                        continue
                    if cid in seen2:
                        continue
                    seen2[cid] = {"name": nm, **self._classify_cat_name(nm)}
                if seen2:
                    self.categories.update(seen2)
                    dbg(f"[CATBOOT] HTML: {len(seen2)} categorías → {seen2}")
                    return len(seen2)
                dbg("[CATBOOT] HTML: no categorías reconocibles en /torrents")
            else:
                dbg(f"[CATBOOT] HTML /torrents HTTP {r.status_code}")
        except Exception as e:
            dbg(f"[CATBOOT] HTML exception: {e}")

        dbg("[CATBOOT] Sin categorías — _ingest caerá a regex por nombre")
        return 0

    def _ingest(self, torrent: dict):
        """
        CSI v2.0: Ingiere un torrent del tracker en el índice en memoria.
        Además de los IDs básicos, construye el ICU (Identificador Compuesto Único)
        para comparación precisa por resolución/codec/grupo.
        """
        attrs = torrent.get("attributes", {})
        # Robust ID extraction from both attributes and top-level
        tmdb = str(attrs.get("tmdb_id") or torrent.get("tmdb_id") or attrs.get("tmdb") or "").strip()
        imdb = _norm_imdb(attrs.get("imdb_id") or torrent.get("imdb_id") or attrs.get("imdb") or "")
        tvdb = str(attrs.get("tvdb_id") or torrent.get("tvdb_id") or attrs.get("tvdb") or "").strip()

        name      = attrs.get("name") or torrent.get("name") or ""
        cat_id    = str(attrs.get("category_id") or torrent.get("category_id") or "")
        season    = attrs.get("season_number") or torrent.get("season_number")
        tid       = str(attrs.get("torrent_id") or torrent.get("torrent_id") or "")
        size_raw  = attrs.get("size") or torrent.get("size") or 0
        try:
            size_bytes = int(size_raw)
        except (ValueError, TypeError):
            size_bytes = 0

        # CSI v3.1: clasificación dinámica por mapa de categorías.
        # Prioridad:
        #   1. self.categories[cat_id] sembrado por bootstrap_categories()
        #   2. Fallback regex sobre el nombre (S01E02 / Season N / E01)
        # El antiguo hardcode `cat_id == "1"` / `== "0"` ha desaparecido porque
        # rompía contra trackers que reordenan categorías (NOBS: 1=Movies,
        # 2=TV, 3=Arcade, 4=Anime Movies, 5=Anime TV Shows). El bug
        # producía falsos NO_SUBIDO en cualquier torrent con cat_id≠"0"/"1":
        # caía al else y el regex fallaba en nombres "A Brief History 2024
        # S01E01E02..." porque `\bE\d{2,}\b` exige límite de palabra antes,
        # que no existe cuando hay "S01" pegado.
        is_tv = False
        cat_info = self.categories.get(cat_id) if cat_id else None
        if cat_info is not None:
            is_tv = bool(cat_info.get("is_tv"))
        else:
            # Sin mapa de categorías cargado o cat_id desconocido.
            # Regex permisivo: cualquier "SxxEyy" o "SxxEyyEzz..." o "Season N".
            # \bE\d{2,}\b no sirve porque no hay límite de palabra antes de E
            # cuando el nombre lleva S01E01...  → usamos un patrón sin word-
            # boundary inicial detrás de la S.
            if re.search(
                r"[ .]S\d{2}(E\d{2,})?\b|Season\s+\d+|\bE\d{2,}\b",
                name, re.I,
            ):
                is_tv = True

        season_str: Optional[str] = None
        if is_tv:
            # If it's TV, we need a season number. If missing, try to extract from name.
            if season is None:
                m = re.search(r"[ .]S(\d+)", name, re.I)
                if m: season = int(m.group(1))
                else: season = 1 # Default to S01 if totally unknown but matched as TV

            try:
                season_str = str(int(season))
                if tmdb: self.tv_seasons.add((tmdb, season_str))
                if tvdb: self.tv_tvdb.add((tvdb, season_str))
                if imdb: self.tv_imdb.add((imdb, season_str))   # CSI v3.0 (A1)
            except (ValueError, TypeError):
                pass
        else:
            if tmdb: self.movie_tmdb.add(tmdb)
            if imdb: self.movie_imdb.add(imdb)
            if tvdb: self.movie_tvdb.add(tvdb)                  # CSI v3.0 (A1)

        # CSI v2.0: Construir ICU del torrent ingresado y almacenar en icu_index
        # Esto permite comparación exacta de versión (resolución/codec/grupo).
        # CSI v3.0 (A1+A2): además guardamos imdb_id, season_number y well_configured
        # para permitir OR-match cross-id y filtrado por configuración fiable.
        if tmdb or tvdb or imdb:
            components = normalize_folder_name(name)
            icu = build_icu(
                tmdb or tvdb or imdb,
                components.get("resolution", ""),
                components.get("codec",      ""),
                components.get("group",      ""),
            )
            # CSI v3.0 (A2): well-configured (interim) = tmdb AND (tvdb OR imdb)
            well_configured = bool(tmdb) and (bool(tvdb) or bool(imdb))

            if icu not in self.icu_index:
                self.icu_index[icu] = {
                    "torrent_id":      tid,
                    "name":            name,
                    "size_bytes":      size_bytes,
                    "tmdb_id":         tmdb,
                    "tvdb_id":         tvdb,
                    "imdb_id":         imdb,             # CSI v3.0 (A1)
                    "is_tv":           is_tv,
                    "season_number":   season_str,       # CSI v3.0 (A1) — TV only
                    "well_configured": well_configured,  # CSI v3.0 (A2)
                    "source":          (components.get("source") or "").upper(),  # v3.3 (W8)
                    "resolution":      (components.get("resolution") or "").upper(),
                    "codec":           (components.get("codec") or "").upper(),
                    "group":           (components.get("group") or "").upper(),
                }
                dbg(f"[ICU] Ingested: {icu} ← '{name}' (well_configured={well_configured})")

        # CSI v3.2 (W5): name-fallback index. Stored even when the torrent had
        # ids — it's the only way to match an id-less local folder against the
        # tracker side. Key = normalized form; bucket is a list because
        # different versions / seasons of the same title collapse to the same
        # key. Filtering by season/is_tv happens at lookup time.
        nk = _norm_folder_key(name)
        if nk:
            self.tracker_names.setdefault(nk, []).append({
                "torrent_id":    tid,
                "name":          name,
                "size_bytes":    size_bytes,
                "is_tv":         is_tv,
                "season_number": season_str,
                "tmdb_id":       tmdb,
                "tvdb_id":       tvdb,
                "imdb_id":       imdb,
            })

    def build_user(self, config: dict) -> bool:
        """
        CSI v2.0: Motor de Triangulación (sin API para uploads del usuario).
        Fuentes: Scraper (A), Local TMP JSONs (B), qBittorrent (C).
        La ausencia de TMP o JSON NO es un fallo bloqueante.
        """
        if not config["TRACKER_URL"]:
            console.print("[red]TRACKER_URL not set.[/red]")
            return False

        # CSI v2.0: Registrar el tracker_url para invalidación futura del JSON
        self.tracker_url = config["TRACKER_URL"].rstrip("/")

        # CSI v3.1: Bootstrap del mapa de categorías ANTES de scrappear.
        # Una sola llamada API (o fallback HTML) → todos los _ingest() siguientes
        # consultan self.categories y deciden is_tv correctamente.
        try:
            self.bootstrap_categories(config)
        except Exception as e:
            dbg(f"[CATBOOT] excepción no fatal: {e}")

        user = config.get("TRACKER_USERNAME")
        total = 0

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as prog:
            t_id = prog.add_task(f"Triangulating index for {user}...", total=None)

            # ── Source A: UNIT3D REST API (preferred when api_token set) ──────
            # The HTML scraper used to be the only path and silently dropped
            # ~64% of rows when `data-tmdb-id`/`data-imdb-id`/`data-tvdb-id`
            # were absent on the rendered list page (markup-specific; the DB
            # still had `tmdb_movie_id`/`tmdb_tv_id` populated). The API is
            # immune to that — every torrent attribute is serialised regardless
            # of how the listing template chose to expose it.
            api_matches = 0
            if config.get("TRACKER_API_KEY"):
                prog.update(t_id, description="[API] Fetching torrents via REST...")
                api_matches = self._source_api(config, prog, t_id)
                total += api_matches

            # ── Source B: Cookie Scraper ──────────────────────────────────────
            # Kept as fallback/supplement: closed-API trackers, or to backfill
            # if the API source returned nothing (auth failure, network, etc.).
            # When API succeeded we skip scraping by default — the two sources
            # describe the same upload list and double-scanning costs 4-5 min.
            if api_matches == 0:
                prog.update(t_id, description="[Scraper] Parsing tracker pages (API fallback)...")
                total += self._source_scraper(config, prog, t_id)
            else:
                dbg(f"[BUILD_USER] API source returned {api_matches} — skipping scraper")

            # ── Source C: Local Metadata ──────────────────────────────────────
            prog.update(t_id, description="[Tmp] Scanning local metadata...")
            total += self._source_local_tmp(config)

            # ── Source D: qBittorrent ─────────────────────────────────────────
            prog.update(t_id, description="[qBit] Checking seeding status...")
            total += self._source_qbit(config)

        console.print(
            f"[green]Triangulation Complete! {total} items matched · "
            f"Movies: {len(self.movie_tmdb) + len(self.movie_imdb)} | "
            f"TV Seasons: {len(self.tv_seasons)}[/green]"
        )
        return True

    def _source_api(self, config: dict, progress, task_id) -> int:
        """
        Source A (preferred): UNIT3D REST API — paginate
        `/api/torrents/filter?uploader=USER&perPage=100`.

        Why this beats the HTML scraper:
          - Returns torrent_id + tmdb_id + tvdb_id + imdb_id + category_id +
            name + size for EVERY upload, regardless of how the listing
            template chose to render data-* attributes. The scraper missed
            ~64% of NOBS's user uploads (1152/3235) because rows whose
            `data-tmdb-id` was empty got dropped — UNIT3D had the id in
            `tmdb_movie_id`/`tmdb_tv_id`, just not in that one HTML attr.
          - Handles HTTP 429 explicitly with a Retry-After-style back-off.
          - One source of truth (no row-skip fallback regex on hrefs).

        On non-200 or empty data it returns the running count so build_user
        can fall back to the scraper.
        """
        user = config.get("TRACKER_USERNAME")
        base = config["TRACKER_URL"].rstrip("/")
        api_key = config.get("TRACKER_API_KEY", "")
        if not (user and base and api_key):
            return 0

        sess = _session(config)
        url  = f"{base}/api/torrents/filter"
        page = 0
        matches = 0
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 3
        PER_PAGE = 100
        HARD_PAGE_CAP = 5000   # only a runaway-loop guard, not a torrent limit

        while True:
            page += 1
            if page > HARD_PAGE_CAP:
                dbg(f"[API] Hard page cap {HARD_PAGE_CAP} reached — stopping")
                break

            params = {
                "api_token": api_key,
                "uploader":  user,
                "perPage":   PER_PAGE,
                "page":      page,
            }
            try:
                api_limiter.wait()
                r = sess.get(url, params=params,
                             headers={"Accept": "application/json", "User-Agent": "undici"},
                             timeout=25)
            except Exception as e:
                dbg(f"[API] page {page} exception: {e}")
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    break
                continue

            # 429 — back off using server's hint if provided
            if r.status_code == 429:
                retry_after = 0
                try:
                    retry_after = int(r.headers.get("Retry-After", "0") or 0)
                except ValueError:
                    retry_after = 0
                # Bound the sleep so we don't hang forever; min 5s, max 60s
                wait_s = max(5, min(60, retry_after or 10))
                dbg(f"[API] page {page} HTTP 429 — sleeping {wait_s}s (Retry-After={retry_after})")
                time.sleep(wait_s)
                page -= 1  # retry the same page
                continue

            if r.status_code != 200:
                dbg(f"[API] page {page} HTTP {r.status_code}")
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    break
                continue

            consecutive_errors = 0
            try:
                payload = r.json()
            except Exception as e:
                dbg(f"[API] page {page} JSON parse error: {e}")
                break

            data = payload.get("data", []) or []
            if not data:
                dbg(f"[API] page {page}: empty — end of pagination")
                break

            for t in data:
                self._ingest(t)
                matches += 1

            dbg(f"[API] page {page}: +{len(data)} cum={matches}")
            if progress is not None and task_id is not None:
                try:
                    progress.update(task_id,
                                    description=f"[API] page {page} (+{matches} torrents)")
                except Exception:
                    pass

            if len(data) < PER_PAGE:
                # Last page — UNIT3D returns < perPage rows when at the tail.
                dbg(f"[API] page {page}: partial ({len(data)}<{PER_PAGE}) — last page")
                break

        dbg(f"[API] Finished: {matches} torrents over {page} page(s)")
        return matches

    def _source_scraper(self, config: dict, progress, task_id) -> int:
        """Source B (fallback): Scraping UNIT3D torrent list via cookies."""
        user = config.get("TRACKER_USERNAME")
        base = config["TRACKER_URL"].rstrip("/")
        page = 0
        matches = 0
        sess = _session(config)
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 3

        while True:
            page += 1
            if page > 500: # Safety guard
                dbg("Scraper: safety limit reached (500 pages)")
                break
                
            url = f"{base}/torrents?uploader={user}&page={page}"
            try:
                api_limiter.wait()
                r = sess.get(url, timeout=20)
                if r.status_code != 200:
                    dbg(f"Scraper page {page}: HTTP {r.status_code}")
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS: break
                    continue
                
                consecutive_errors = 0
                soup = BeautifulSoup(r.text, "html.parser")
                rows = soup.select("tr.torrent-search--list__row")
                if not rows:
                    # CSI v3.1: Detectar si la página es un login (cookie expirada)
                    if page == 1 and _detect_auth_failure(soup):
                        self._auth_failed = True
                        console.print(
                            "[bold red]⚠ AUTH FAILURE: Cookie expirada — "
                            "el scraper recibió página de login en vez de lista de torrents.[/bold red]"
                        )
                        dbg(f"Scraper: AUTH FAILURE on page 1 — login page detected, setting _auth_failed=True")
                    else:
                        dbg(f"Scraper: No rows found on page {page} — end of pagination")
                    break
                
                for row in rows:
                    if not isinstance(row, bs4_element.Tag):
                        continue
                    # CSI v1.6.4: Strict Parse (Matched Unknown Fix)
                    tmdb = row.get("data-tmdb-id") or ""
                    imdb = row.get("data-imdb-id") or ""
                    tvdb = row.get("data-tvdb-id") or ""
                    tid  = row.get("data-torrent-id") or ""
                    cat_id = row.get("data-category-id") or ""

                    name_tag = row.select_one("a.torrent-search--list__name")
                    name_text = (
                        name_tag.get_text(strip=True)
                        if isinstance(name_tag, bs4_element.Tag) else "Unknown"
                    )

                    # Fallback for IDs if attributes are empty or placeholder
                    if not tmdb or tmdb == "0":
                        tmdb_link = row.find("a", href=re.compile(r"themoviedb.org/(movie|tv)/(\d+)"))
                        if isinstance(tmdb_link, bs4_element.Tag):
                            m = re.search(r"/(movie|tv)/(\d+)", str(tmdb_link.get("href", "") or ""))
                            if m: tmdb = m.group(2)

                    if not imdb or imdb == "0":
                        imdb_link = row.find("a", href=re.compile(r"imdb.com/title/tt(\d+)"))
                        if isinstance(imdb_link, bs4_element.Tag):
                            m = re.search(r"title/tt(\d+)", str(imdb_link.get("href", "") or ""))
                            if m: imdb = m.group(1)
                    
                    # CSI v3.1: NO longer drop rows that lack data-*-id attrs.
                    # UNIT3D's listing template sometimes omits those attrs
                    # even when the DB has tmdb_movie_id/tmdb_tv_id populated
                    # (NOBS audit 2026-05-23: 1152/3235 = ~36% — scraper was
                    # silently losing ~64% of uploads). Ingest with whatever
                    # ids we managed to extract; _ingest will still register
                    # the torrent via torrent_id + name + category_id, and
                    # the has_*_state machine can later match by name when
                    # ids are absent.
                    if not any([tmdb, imdb, tvdb]):
                        dbg(f"[SCRAPER] Row has no extractable ids — ingesting "
                            f"by name only: tid={tid} '{name_text[:60]}'")

                    fake_torrent = {
                        "attributes": {
                            "tmdb_id": tmdb,
                            "imdb_id": imdb,
                            "tvdb_id": tvdb,
                            "torrent_id": tid,
                            "category_id": cat_id,
                            "name": name_text
                        }
                    }

                    self._ingest(fake_torrent)
                    matches += 1
                    dbg(f"[DEBUG] [TRIANGULATION] Matched {name_text} via Scraper")

                # Check for next page
                if not soup.select_one("a.pagination__next"):
                    break
                    
            except Exception as e:
                dbg(f"Scraper error page {page}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS: break

        # v3.1: Log scraper summary with auth status
        auth_status = "FAILED" if self._auth_failed else "OK"
        dbg(f"[SCRAPER] Finished: {matches} matches, {page} pages, auth={auth_status}")
        if matches == 0 and page > 0 and not self._auth_failed:
            console.print(
                "[yellow]⚠ El scraper encontró 0 uploads. Verifica que la cookie "
                "sigue siendo válida si esto parece incorrecto.[/yellow]"
            )
        return matches

    def _source_local_tmp(self, config: dict) -> int:
        """
        Source B: Local JSON scanning in TMP_PATH.
        CSI v2.0: Esta fuente es SUPLEMENTARIA, no crítica.
        Si la carpeta tmp no existe, se continúa sin fallar — el índice
        se construye desde las fuentes primarias (Scraper/API/qBit).
        PROHIBIDO retornar 0 como señal de fallo bloqueante.
        """
        tmp_path = Path(config.get("TMP_PATH", TMP_ROOT))
        if not tmp_path.exists():
            dbg(f"[DEBUG] TMP_PATH no encontrado: {tmp_path} — fuente local omitida (no crítica)")
            # CSI v2.0: No retornamos 0 como señal de fallo.
            # La ausencia de tmp NO implica que el contenido no esté en el tracker.
            # Continuamos: las fuentes A (Scraper) y C (qBit) tienen prioridad.
            return 0  # 0 matches locales, pero el flujo continúa normalmente
        
        matches = 0
        found_any_json = False
        try:
            # Recon of TMP folder: look for .json files
            for item in tmp_path.iterdir():
                json_files = []
                if item.is_dir():
                    json_files = list(item.glob("*.json"))
                elif item.suffix == ".json":
                    json_files = [item]
                
                if json_files:
                    found_any_json = True

                for jf in json_files:
                    try:
                        with open(jf, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            # UNIT3D Upload JSONs usually have these
                            tmdb = data.get("tmdb_id") or data.get("tmdb")
                            imdb = data.get("imdb_id") or data.get("imdb")
                            tvdb = data.get("tvdb_id") or data.get("tvdb")
                            season = data.get("season_number")
                            cat_id = data.get("category_id") or data.get("category")
                            name = data.get("name") or jf.stem
                            
                            if tmdb or imdb or tvdb:
                                fake_t = {
                                    "attributes": {
                                        "tmdb_id": tmdb,
                                        "imdb_id": imdb,
                                        "tvdb_id": tvdb,
                                        "season_number": season,
                                        "category_id": cat_id,
                                        "name": name
                                    }
                                }
                                self._ingest(fake_t)
                                matches += 1
                                dbg(f"[DEBUG] [TRIANGULATION] Matched {name} via Tmp")
                    except Exception:
                        continue
            
            if not found_any_json:
                dbg(f"[DEBUG] Local storage empty at {tmp_path}. No .json files found.")

        except Exception as e:
            dbg(f"Local TMP source error: {e}")
            
        return matches

    def _source_qbit(self, config: dict) -> int:
        """Source C: qBittorrent seeding items (Paths)."""
        path_map = get_client_torrents(config)
        if path_map is None:
            dbg("[SOURCE_C] qBittorrent no disponible — sonda fallida, activando _probe_failed")
            self._probe_failed = True
            return 0

        self.client_path_map = path_map
        matches = 0
        for p in path_map:
            self.seeding_names.add(p)
            self.seeding_paths.add(p)
            matches += 1
            dbg(f"[DEBUG] [TRIANGULATION] Matched {p} via qBit")
        return matches
            
    def has_movie(self, tmdb_id=None, imdb_id=None, name=None) -> bool:
        """Legacy bool wrapper — compatibilidad con código v1.x."""
        if tmdb_id and str(tmdb_id) in self.movie_tmdb:
            return True
        if imdb_id:
            clean = _norm_imdb(imdb_id)
            if clean and clean in self.movie_imdb:
                return True
        if name and name.lower() in self.seeding_names:
            return True
        return False

    def has_tv_season(self, tmdb_id=None, tvdb_id=None, season_num=None, name=None) -> bool:
        """Legacy bool wrapper — compatibilidad con código v1.x."""
        if season_num is not None:
            s = str(int(season_num))
            if tmdb_id and (str(tmdb_id), s) in self.tv_seasons:
                return True
            if tvdb_id and (str(tvdb_id), s) in self.tv_tvdb:
                return True
        if name and name.lower() in self.seeding_names:
            return True
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v3.0 (A1): Helpers cross-id — devuelven los ICUs del icu_index que
    # coinciden con CUALQUIERA de los id-types dados. Permite que la máquina
    # de estados detecte "este torrent está en el tracker" aunque los lados
    # local y tracker conozcan id-types distintos (tmdb-only vs imdb-only).
    # ──────────────────────────────────────────────────────────────────────────

    def _icus_matching_ids(self, tmdb=None, tvdb=None, imdb=None,
                           is_tv: Optional[bool] = None,
                           season_str: Optional[str] = None) -> list:
        """
        Return icu keys whose entry matches ANY of the given id-types.
        Optional is_tv filter (None = both). Optional season_str filter for TV.
        """
        tmdb_s = str(tmdb).strip() if tmdb else ""
        tvdb_s = str(tvdb).strip() if tvdb else ""
        imdb_s = _norm_imdb(imdb) if imdb else ""
        if not (tmdb_s or tvdb_s or imdb_s):
            return []
        out = []
        for k, e in self.icu_index.items():
            if is_tv is not None and bool(e.get("is_tv")) != is_tv:
                continue
            if season_str is not None and str(e.get("season_number") or "") != season_str:
                continue
            e_tmdb = str(e.get("tmdb_id") or "")
            e_tvdb = str(e.get("tvdb_id") or "")
            e_imdb = str(e.get("imdb_id") or "")
            if (tmdb_s and e_tmdb and e_tmdb == tmdb_s) \
               or (tvdb_s and e_tvdb and e_tvdb == tvdb_s) \
               or (imdb_s and e_imdb and e_imdb == imdb_s):
                out.append(k)
        return out

    # CSI v3.0 (A2): Exponer flag "well-configured" para callers externos
    # (RawLoadrr/prep.py decide si confiar en los ids del tracker o invocar
    # al id_resolver). Interim: tmdb AND (tvdb OR imdb) presentes en ingest.
    def torrent_well_configured(self, icu: str) -> bool:
        entry = self.icu_index.get(icu)
        return bool(entry and entry.get("well_configured"))

    # CSI v3.3 (W7): relaxed ICU match.
    # L1 ICU exact requires byte-identical {id}|{res}|{codec}|{group}. Group
    # tokens are the most brittle component — a re-encoded local copy or a
    # tracker re-pack changes the trailing -GROUP suffix even when content
    # is the same. We add a relaxed tier: ignore group when {id, res, codec}
    # all match. Refuses to fire when res or codec is UNKNOWN — too risky.
    # Returns the matched icu_index key or None.
    def _icu_match_relaxed(self, local_icu: str,
                           ignore_group: bool = True,
                           ignore_codec: bool = False) -> Optional[str]:
        if not local_icu:
            return None
        parts = local_icu.split("|")
        if len(parts) != 4:
            return None
        l_id, l_res, l_cod, l_grp = parts
        if not l_id or l_id == "0":
            return None
        if l_res == "UNKNOWN":
            return None
        if not ignore_codec and l_cod == "UNKNOWN":
            return None
        for k in self.icu_index:
            kp = k.split("|")
            if len(kp) != 4:
                continue
            k_id, k_res, k_cod, k_grp = kp
            if k_id != l_id:
                continue
            if k_res != l_res:
                continue
            if not ignore_codec and k_cod != l_cod:
                continue
            if not ignore_group and k_grp != l_grp:
                continue
            return k
        return None

    # CSI v3.2 (W5): name-fallback lookup. Used by has_*_state as last resort
    # when none of the id-sets match (id-less local content or template-bug
    # tracker rows). Optional is_tv / season_num filter narrow the bucket.
    # Tiered:
    #   1. exact normalized match
    #   2. fuzzy ≥0.90 across all keys (single best winner)
    # Returns list of matching entries (possibly empty).
    NAME_FUZZY_THRESHOLD = 0.90

    def find_by_name(self, name: str,
                     is_tv: Optional[bool] = None,
                     season_num: Optional[int] = None) -> list:
        nk = _norm_folder_key(name)
        if not nk:
            return []
        bucket = self.tracker_names.get(nk, [])
        # Fuzzy fallback when exact key misses entirely.
        if not bucket and self.tracker_names:
            best_key, best_ratio = "", 0.0
            for k in self.tracker_names:
                # cheap pre-filter: must share at least one word
                if not (set(nk.split()) & set(k.split())):
                    continue
                r = difflib.SequenceMatcher(None, nk, k).ratio()
                if r > best_ratio:
                    best_ratio, best_key = r, k
            if best_ratio >= self.NAME_FUZZY_THRESHOLD:
                bucket = self.tracker_names.get(best_key, [])
                dbg(f"[NAME_FB] fuzzy '{nk}'→'{best_key}' (ratio={best_ratio:.2f}, "
                    f"{len(bucket)} entries)")

        if not bucket:
            return []

        # Optional filters
        out = bucket
        if is_tv is not None:
            out = [e for e in out if bool(e.get("is_tv")) == is_tv]
        if season_num is not None:
            s = str(int(season_num))
            out = [e for e in out if str(e.get("season_number") or "") == s]
        return out

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v2.0: Métodos de Máquina de Estados
    # Retornan uno de los 5 estados definidos en STATE_MACHINE section.
    # Estos métodos son la evolución de has_movie() / has_tv_season().
    # ──────────────────────────────────────────────────────────────────────────

    def has_movie_state(self, tmdb_id=None, imdb_id=None, name=None,
                        local_icu: Optional[str] = None, client_paths: Optional[set] = None,
                        local_size_bytes: int = 0) -> str:
        """
        CSI v2.0: Determina el estado de una película en la triangulación.
        Prioridad de autoridades: ICU exacto > TMDB_ID > IMDB_ID > seeding_names
        Integra la comprobación del cliente (client_paths) para distinguir
        ESTADO_OK de ESTADO_FALTA_CLIENTE.

        Args:
            tmdb_id:          TMDB ID de la película (Radarr/TMDB)
            imdb_id:          IMDB ID de la película
            name:             Nombre de carpeta local (para fallback por nombre)
            local_icu:        ICU construido desde el contenido local
            client_paths:     Set de paths absolutos (lower) desde qBittorrent
            local_size_bytes: Tamaño del archivo local en bytes (para entropía)

        Returns:
            str: Uno de los 5 estados ESTADO_* definidos en la máquina de estados.
        """
        client_paths = client_paths or set()
        tmdb_str = str(tmdb_id).strip() if tmdb_id else ""

        # ── Nivel 1: Comparación exacta por ICU ──────────────────────────────
        # Si tenemos el ICU local, buscamos una coincidencia exacta en el índice.
        if local_icu and local_icu in self.icu_index:
            tracker_entry = self.icu_index[local_icu]
            dbg(f"[STATE] ICU exact match: {local_icu} → torrent_id={tracker_entry.get('torrent_id')}")
            # Confirmar si también está en el cliente (seeding).
            # Usar nombre local primero; si no hay nombre local, usar el nombre del tracker.
            check_name = name or tracker_entry.get("name", "")
            in_client = self._check_client_path(check_name, client_paths)
            if in_client:
                return ESTADO_OK
            else:
                return ESTADO_FALTA_CLIENTE

        # ── Nivel 1.5: ICU relajado (CSI v3.3 W7) ────────────────────────────
        # Same id+res+codec, any group. Catches re-encodes and repacks where
        # the trailing -GROUP token diverged but content is the same version.
        relaxed = self._icu_match_relaxed(local_icu, ignore_group=True)
        if relaxed:
            entry = self.icu_index[relaxed]
            dbg(f"[STATE] ICU relaxed match (ignored group): "
                f"{local_icu} ≈ {relaxed} torrent_id={entry.get('torrent_id')}")
            check_name = name or entry.get("name", "")
            in_client = self._check_client_path(check_name, client_paths)
            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Nivel 2: OR-match cross-id (CSI v3.0 A1) ─────────────────────────
        # "Presente en tracker" = ANY de {tmdb, imdb} coincide con cualquier
        # id-type del torrent en tracker. Antes (v2.0) sólo se chequeaba tmdb
        # → ~38% de falsos NO_SUBIDO en NOBS porque algunos torrents son
        # tmdb-only y otros imdb-only.
        clean_imdb = _norm_imdb(imdb_id) if imdb_id else ""
        present = (
            (tmdb_str and tmdb_str in self.movie_tmdb)
            or (clean_imdb and clean_imdb in self.movie_imdb)
        )
        if present:
            # ICU version-match: buscamos ICUs en el índice que pertenezcan a
            # la MISMA película (por cualquier id-type), no sólo por prefijo
            # tmdb. Esto reemplaza el viejo startswith(f"{tmdb_str}|").
            matching_icus = self._icus_matching_ids(
                tmdb=tmdb_id, imdb=imdb_id, is_tv=False,
            )
            if matching_icus and local_icu:
                # ICU diferente posible → aplicar entropía de tamaño como desempate
                for existing_icu in matching_icus:
                    if existing_icu == local_icu:
                        # ICU exact (cubierto por L1, pero defensa en profundidad)
                        in_client = self._check_client_path(name or "", client_paths)
                        return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                    tracker_size = self.icu_index[existing_icu].get("size_bytes", 0)
                    if tracker_size and local_size_bytes:
                        if _size_entropy_tiebreak(local_size_bytes, tracker_size):
                            dbg(f"[STATE] Size entropy tiebreak → FALTA_CLIENTE "
                                f"(local={local_size_bytes}, tracker={tracker_size})")
                            in_client = self._check_client_path(name or "", client_paths)
                            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                # Sin match por tamaño → dupe potencial confirmado
                dbg(f"[STATE] DUPE POTENCIAL: tmdb={tmdb_str} imdb={clean_imdb} "
                    f"local_icu={local_icu} existing_icus={matching_icus}")
                return ESTADO_DUPE_POTENCIAL
            elif matching_icus and not local_icu:
                # No tenemos ICU local para comparar → conservador
                return ESTADO_DUPE_POTENCIAL
            else:
                # Presente por id pero sin entrada ICU en índice (v1.x legacy)
                in_client = self._check_client_path(name or "", client_paths)
                return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Nivel 4: Nombre en seeding_names (qBit path match legacy) ────────
        if name and name.lower() in self.seeding_names:
            return ESTADO_OK  # Si está seeding, asumimos subido

        # ── Nivel 5 (CSI v3.2 — W5): name-fallback contra tracker_names ─────
        # Última red de seguridad cuando ningún id local coincidió. Útil para
        # carpetas que fallaron las 5 capas de get_movie (sin tmdb/imdb tras
        # external resolver) pero cuyo nombre normalizado coincide con un
        # torrent en el tracker. Filtra por is_tv=False para no chocar con
        # series.
        if name:
            name_hits = self.find_by_name(name, is_tv=False)
            if name_hits:
                dbg(f"[STATE] name-fallback hit ({len(name_hits)} entries) for '{name}'")
                in_client = self._check_client_path(name, client_paths)
                return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Sin coincidencia ──────────────────────────────────────────────────
        # CSI v3.1: Si la sonda o la auth fallaron, no afirmar NO_SUBIDO
        if self._probe_failed or self._auth_failed:
            dbg(f"[STATE] probe_failed={self._probe_failed} auth_failed={self._auth_failed} → INCIDENCIA")
            return ESTADO_INCIDENCIA
        return ESTADO_NO_SUBIDO

    def has_tv_season_state(self, tmdb_id=None, tvdb_id=None, imdb_id=None,
                            season_num=None, name=None,
                            local_icu: str | None = None,
                            client_paths: set | None = None,
                            local_size_bytes: int = 0) -> str:
        """
        CSI v2.0: Determina el estado de una temporada de TV en la triangulación.
        Análogo a has_movie_state() pero para contenido de TV.

        CSI v3.0 (A1): acepta imdb_id y aplica OR-match cross-id en Nivel 2.

        Args:
            tmdb_id / tvdb_id / imdb_id:  IDs de la serie (Sonarr expone los 3)
            season_num:       Número de temporada (int)
            name:             Nombre de la carpeta del show (para fallback)
            local_icu:        ICU construido desde el contenido local
            client_paths:     Set de paths absolutos (lower) desde qBittorrent
            local_size_bytes: Tamaño total en bytes de la temporada local

        Returns:
            str: Uno de los 5 estados ESTADO_* definidos en la máquina de estados.
        """
        client_paths = client_paths or set()
        tmdb_str  = str(tmdb_id).strip() if tmdb_id else ""
        tvdb_str  = str(tvdb_id).strip() if tvdb_id else ""
        imdb_str  = _norm_imdb(imdb_id) if imdb_id else ""

        # ── Nivel 1: ICU exacto ───────────────────────────────────────────────
        if local_icu and local_icu in self.icu_index:
            tracker_entry = self.icu_index[local_icu]
            dbg(f"[STATE] TV ICU exact match: {local_icu} → torrent_id={tracker_entry.get('torrent_id')}")
            # Usar nombre local primero; si no hay, usar nombre del tracker como fallback
            check_name = name or tracker_entry.get("name", "")
            in_client = self._check_client_path(check_name, client_paths)
            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Nivel 1.5: ICU relajado (CSI v3.3 W7) ────────────────────────────
        relaxed = self._icu_match_relaxed(local_icu, ignore_group=True)
        if relaxed:
            entry = self.icu_index[relaxed]
            # Only accept the relaxed match if this entry's season matches too
            # (a different season of the same show would also have same id).
            if season_num is not None:
                want_s = str(int(season_num))
                if str(entry.get("season_number") or "") != want_s:
                    relaxed = None
        if relaxed:
            entry = self.icu_index[relaxed]
            dbg(f"[STATE] TV ICU relaxed match (ignored group): "
                f"{local_icu} ≈ {relaxed} torrent_id={entry.get('torrent_id')}")
            check_name = name or entry.get("name", "")
            in_client = self._check_client_path(check_name, client_paths)
            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Nivel 2: OR-match cross-id con número de temporada (CSI v3.0 A1) ─
        if season_num is not None:
            s = str(int(season_num))
            found_in_tracker = (
                (tmdb_str and (tmdb_str, s) in self.tv_seasons)
                or (tvdb_str and (tvdb_str, s) in self.tv_tvdb)
                or (imdb_str and (imdb_str, s) in self.tv_imdb)
            )

            if found_in_tracker:
                # ICUs de la misma serie+temporada por cualquier id-type
                matching_icus = self._icus_matching_ids(
                    tmdb=tmdb_id, tvdb=tvdb_id, imdb=imdb_id,
                    is_tv=True, season_str=s,
                )
                if matching_icus and local_icu:
                    # Comprobar si una versión del tracker coincide en tamaño
                    for existing_icu in matching_icus:
                        if existing_icu == local_icu:
                            in_client = self._check_client_path(name or "", client_paths)
                            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                        t_size = self.icu_index[existing_icu].get("size_bytes", 0)
                        if t_size and local_size_bytes and _size_entropy_tiebreak(local_size_bytes, t_size):
                            in_client = self._check_client_path(name or "", client_paths)
                            return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                # Presente en tracker (cualquier id-type) — versión puede o no
                # coincidir; comportamiento conservador previo: tratar como
                # presente (no como DUPE) para no bloquear seeding válido.
                in_client = self._check_client_path(name or "", client_paths)
                return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # ── Nivel 3: Nombre en seeding_names ─────────────────────────────────
        if name and name.lower() in self.seeding_names:
            return ESTADO_OK

        # ── Nivel 4 (CSI v3.2 — W5): name-fallback contra tracker_names ─────
        # Series sin ids tras todas las capas de get_series — comprobamos si
        # algún torrent TV del tracker tiene nombre normalizado equivalente.
        # Filtra por is_tv=True y, si tenemos season_num, por temporada.
        if name:
            name_hits = self.find_by_name(name, is_tv=True, season_num=season_num)
            if name_hits:
                dbg(f"[STATE] TV name-fallback hit ({len(name_hits)} entries) for "
                    f"'{name}' S{season_num}")
                in_client = self._check_client_path(name, client_paths)
                return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        # CSI v3.1: Si la sonda o la auth fallaron, no afirmar NO_SUBIDO
        if self._probe_failed or self._auth_failed:
            dbg(f"[STATE] TV probe_failed={self._probe_failed} auth_failed={self._auth_failed} → INCIDENCIA")
            return ESTADO_INCIDENCIA
        return ESTADO_NO_SUBIDO

    def _check_client_path(self, name: str, client_paths: set) -> bool:
        """
        CSI v3.1: Comprueba si un ítem está presente en qBittorrent.
        Nivel 1: coincidencia directa case-insensitive (substring).
        Nivel 2: coincidencia normalizada — ambos lados normalizados.
        Nivel 3: fallback a seeding_names (v1.x compat).
        """
        if not name or not client_paths:
            return False
        name_lower = name.lower()
        # Nivel 1: Direct substring
        for cp in client_paths:
            if name_lower in cp:
                return True
        # Nivel 2: Normalized — normalizar ambos lados
        name_norm = _normalize_torrent_name(name)
        if name_norm and len(name_norm) > 5:
            for cp in client_paths:
                cp_norm = re.sub(r"[._]", " ", cp)
                if name_norm in cp_norm:
                    dbg(f"[CLIENT_MATCH] Normalized match: '{name_norm}' in '...{cp[-60:]}'")
                    return True
        # Nivel 3: seeding_names (v1.x compat)
        if name_lower in self.seeding_names:
            return True
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # CSI v2.0: Persistencia del índice (elimina la amnesia entre sesiones)
    # ──────────────────────────────────────────────────────────────────────────

    def save_to_json(self, path: Path):
        """
        CSI v2.0: Serializa el estado actual del TrackerIndex a un archivo JSON.
        Llamar después de cada escaneo o investigación para persistir el índice.
        Garantiza que la próxima sesión no tenga que reconstruir desde cero.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version":      "3.3",   # CSI v3.3: W7 relaxed ICU + W8 source/codec split
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "tracker_url":  self.tracker_url,
                # Conjuntos → listas para serialización JSON
                "movie_tmdb":   sorted(list(self.movie_tmdb)),
                "movie_imdb":   sorted(list(self.movie_imdb)),
                "movie_tvdb":   sorted(list(self.movie_tvdb)),                       # v3.0
                "tv_seasons":   sorted([list(t) for t in self.tv_seasons]),
                "tv_tvdb":      sorted([list(t) for t in self.tv_tvdb]),
                "tv_imdb":      sorted([list(t) for t in self.tv_imdb]),             # v3.0
                "seeding_names": sorted(list(self.seeding_names)),
                # Dicts ya son serializables directamente
                "icu_index":    self.icu_index,
                "categories":   self.categories,                                     # v3.1
                "tracker_names": self.tracker_names,                                 # v3.2 (W5)
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            dbg(f"[INDEX] TrackerIndex guardado: {path} "
                f"({len(self.movie_tmdb)} movies, {len(self.tv_seasons)} seasons, "
                f"{len(self.icu_index)} ICUs)")
            console.print(
                f"[dim green]▸ Índice persistido:[/dim green] [dim]{path}[/dim] "
                f"[dim]({len(self.icu_index)} ICUs)[/dim]"
            )
        except Exception as e:
            dbg(f"[INDEX] Error al guardar TrackerIndex: {e}")
            console.print(f"[yellow]Advertencia: No se pudo guardar el índice: {e}[/yellow]")

    def load_from_json(self, path: Path, tracker_url: str = "") -> bool:
        """
        CSI v2.0: Carga el TrackerIndex desde un JSON persistido previamente.
        Verifica que el tracker_url coincida para evitar usar un índice obsoleto
        de un tracker diferente.

        Returns:
            True  si el índice fue cargado exitosamente y es válido
            False si el archivo no existe, está corrupto o la URL no coincide
        """
        if not path.exists():
            dbg(f"[INDEX] No existe {path} — se construirá el índice desde cero")
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Validar versión. CSI v3.3 sólo acepta 3.3 — las versiones
            # anteriores (3.0-3.2) tienen ICUs con SOURCE en el codec slot
            # (bug W8), incompatibles con el matcher relajado nuevo. Forzamos
            # un re-scan único para reconstruir ICUs limpios.
            version = data.get("version", "1.0")
            if version not in ("3.3",):
                dbg(f"[INDEX] Versión de JSON incompatible ({version}) — reconstruyendo")
                return False

            # Validar URL del tracker (invalidación automática si cambia el tracker)
            saved_url = data.get("tracker_url", "")
            if tracker_url and saved_url and saved_url.rstrip("/") != tracker_url.rstrip("/"):
                console.print(
                    f"[yellow]▸ Índice cacheado corresponde a tracker diferente.[/yellow]\n"
                    f"  Guardado: [dim]{saved_url}[/dim]\n"
                    f"  Actual:   [dim]{tracker_url}[/dim]\n"
                    f"  [dim]Reconstruyendo desde cero...[/dim]"
                )
                dbg(f"[INDEX] URL mismatch: saved='{saved_url}' current='{tracker_url}' → rebuild")
                return False

            # Cargar datos en los conjuntos en memoria
            self.movie_tmdb    = set(data.get("movie_tmdb", []))
            self.movie_imdb    = set(data.get("movie_imdb", []))
            self.movie_tvdb    = set(data.get("movie_tvdb", []))                     # v3.0
            self.tv_seasons    = set(tuple(t) for t in data.get("tv_seasons", []))
            self.tv_tvdb       = set(tuple(t) for t in data.get("tv_tvdb",    []))
            self.tv_imdb       = set(tuple(t) for t in data.get("tv_imdb",    []))   # v3.0
            self.seeding_names = set(data.get("seeding_names", []))
            self.seeding_paths = self.seeding_names  # alias v2.0
            self.icu_index     = data.get("icu_index", {})
            self.categories    = data.get("categories", {}) or {}                    # v3.1
            self.tracker_names = data.get("tracker_names", {}) or {}                 # v3.2 (W5)
            self.tracker_url   = saved_url

            last_updated = data.get("last_updated", "desconocido")
            console.print(
                Panel(
                    f"[green]Índice cargado desde caché[/green]\n"
                    f"Tracker : [cyan]{saved_url}[/cyan]\n"
                    f"Películas: [bold]{len(self.movie_tmdb)}[/bold] TMDBs | "
                    f"TV: [bold]{len(self.tv_seasons)}[/bold] temporadas | "
                    f"ICUs: [bold]{len(self.icu_index)}[/bold]\n"
                    f"[dim]Última actualización: {last_updated}[/dim]",
                    border_style="green",
                    title="[bold]CSI — TrackerIndex Persistente[/bold]",
                )
            )
            dbg(f"[INDEX] Cargado desde {path}: {len(self.movie_tmdb)} movies, "
                f"{len(self.tv_seasons)} seasons, {len(self.icu_index)} ICUs")
            return True

        except json.JSONDecodeError as e:
            dbg(f"[INDEX] JSON corrupto en {path}: {e} — reconstruyendo")
            console.print(f"[yellow]Índice cacheado corrupto ({e}) — reconstruyendo desde cero[/yellow]")
            return False
        except Exception as e:
            dbg(f"[INDEX] Error al cargar {path}: {e}")
            return False

# ─── GLOBAL SEARCH (per-item API call) ───────────────────────────────────────
# CSI v2.0: Sistema de consultas híbridas con fallback y máquina de estados.
#
# Flujo de consulta:
#   1. API UNIT3D (api_token)  → fuente primaria
#   2. Scraper HTTP (cookie)   → fallback si API falla o no hay API key
#   3. ESTADO_INCIDENCIA       → si ambas fuentes fallan
#
# REGLA CRÍTICA: NUNCA retornar ESTADO_NO_SUBIDO si la consulta falló.
# Solo ESTADO_INCIDENCIA cuando no hay certeza de la respuesta del tracker.

def _compare_icu_results(results: list, local_icu: str | None, tmdb_id,
                         client_paths: set, local_size_bytes: int = 0,
                         query: str = "") -> str:
    """
    CSI v3.1: Analiza la lista de torrents devuelta por el tracker
    y determina el estado CSI del ítem local comparando ICUs.

    Args:
        results:          Lista de dicts de torrents (formato UNIT3D API)
        local_icu:        ICU construido desde el contenido local
        tmdb_id:          TMDB ID del ítem (para logging)
        client_paths:     Paths del cliente qBit (para ESTADO_OK vs FALTA_CLIENTE)
        local_size_bytes: Tamaño local para entropía de desempate
        query:            Nombre de carpeta local (show_folder/movie_folder) — fallback
                          para verificar presencia en cliente cuando el nombre del
                          torrent en el tracker difiere del filename real.

    Returns:
        str: Estado CSI determinado
    """
    if not results:
        return ESTADO_NO_SUBIDO

    def _in_client(t_name: str) -> bool:
        """Verifica presencia en cliente: primero por nombre del tracker,
        luego por query (folder name) como fallback."""
        if _check_path_in_client(t_name, client_paths):
            return True
        if query and _check_path_in_client(query, client_paths):
            dbg(f"[COMPARE] Client match via query fallback: '{query}'")
            return True
        return False

    # Si tenemos ICU local, intentar coincidencia exacta primero
    if local_icu:
        for t in results:
            attrs = t.get("attributes", {})
            t_name  = attrs.get("name") or t.get("name") or ""
            t_tmdb  = str(attrs.get("tmdb_id") or t.get("tmdb_id") or "").strip()
            tracker_icu = parse_icu_from_tracker_name(t_name, t_tmdb or tmdb_id)

            if tracker_icu == local_icu:
                dbg(f"[COMPARE] ICU exact match en tracker: {tracker_icu}")
                return ESTADO_OK if _in_client(t_name) else ESTADO_FALTA_CLIENTE

        # ICU no coincide → mismo TMDB pero versión diferente → DUPE POTENCIAL
        # Aplicar entropía de tamaño como posible desempate
        for t in results:
            attrs = t.get("attributes", {})
            t_size = 0
            try:
                t_size = int(attrs.get("size") or t.get("size") or 0)
            except (ValueError, TypeError):
                pass
            if t_size and local_size_bytes:
                if _size_entropy_tiebreak(local_size_bytes, t_size):
                    dbg(f"[COMPARE] Size entropy tiebreak → probablemente misma versión")
                    t_name = attrs.get("name") or t.get("name") or ""
                    return ESTADO_OK if _in_client(t_name) else ESTADO_FALTA_CLIENTE

        dbg(f"[COMPARE] DUPE POTENCIAL: tmdb={tmdb_id}, local_icu={local_icu}")
        return ESTADO_DUPE_POTENCIAL

    # Sin ICU local, solo sabemos que existe en tracker
    # Verificar si está en el cliente por nombre o por query (folder name)
    for t in results:
        attrs = t.get("attributes", {})
        t_name = attrs.get("name") or t.get("name") or ""
        if _in_client(t_name):
            return ESTADO_OK
    return ESTADO_FALTA_CLIENTE


def _normalize_torrent_name(name: str) -> str:
    """CSI v3.1: Normaliza un nombre de torrent para comparación flexible.
    Elimina extensión y grupo, luego convierte dots/underscores a espacios."""
    n = name.lower()
    n = re.sub(r"\.(mkv|mp4|avi|srt|nfo)$", "", n).strip()
    n = re.sub(r"-[a-z0-9]{2,}$", "", n).strip()
    n = re.sub(r"[._]", " ", n)
    return n


def _check_path_in_client(name: str, client_paths: set) -> bool:
    """
    CSI v3.1: Comprueba si un nombre de torrent está presente en el cliente.
    Nivel 1: coincidencia directa case-insensitive (substring).
    Nivel 2: coincidencia normalizada — ambos lados normalizados para absorber
             diferencias dots/spaces/grupo/extensión entre tracker y filesystem.
    Función auxiliar independiente para uso en búsquedas globales.
    """
    if not name or not client_paths:
        return False
    name_lower = name.lower()
    # Nivel 1: Direct substring (existing behavior)
    for cp in client_paths:
        if name_lower in cp:
            return True
    # Nivel 2: Normalized — normalizar ambos lados
    name_norm = _normalize_torrent_name(name)
    if name_norm and len(name_norm) > 5:
        for cp in client_paths:
            cp_norm = re.sub(r"[._]", " ", cp)
            if name_norm in cp_norm:
                dbg(f"[CLIENT_MATCH] Normalized match: '{name_norm}' in '...{cp[-60:]}'")
                return True
    return False


def _scraper_search_item(config: dict, tmdb_id=None, tvdb_id=None,
                         query: Optional[str] = None, tracker_index: "Optional[TrackerIndex]" = None,
                         client_paths: Optional[set] = None, local_icu: Optional[str] = None,
                         local_size_bytes: int = 0) -> str:
    """
    CSI v2.0: Fallback scraper para búsqueda per-ítem cuando la API falla.
    Consulta el índice en memoria del TrackerIndex (si está disponible) antes
    de intentar un scrape HTTP adicional — esto evita llamadas innecesarias.

    Retorna estado CSI o ESTADO_INCIDENCIA si no hay información suficiente.
    """
    client_paths = client_paths or set()

    # Si tenemos un índice en memoria, consultarlo primero (O(1))
    if tracker_index is not None and len(tracker_index.movie_tmdb) > 0:
        dbg(f"[SCRAPER_FALLBACK] Consultando índice en memoria para tmdb={tmdb_id}")
        if tmdb_id:
            tmdb_str = str(tmdb_id).strip()
            if tmdb_str in tracker_index.movie_tmdb:
                if local_icu and local_icu in tracker_index.icu_index:
                    in_client = _check_path_in_client(query or "", client_paths)
                    return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                elif tmdb_str in tracker_index.movie_tmdb:
                    return ESTADO_DUPE_POTENCIAL
            return ESTADO_NO_SUBIDO

    # Sin índice en memoria — intentar búsqueda HTTP en el tracker con cookie
    base = config.get("TRACKER_URL", "").rstrip("/")
    if not base:
        dbg("[SCRAPER_FALLBACK] No TRACKER_URL disponible para fallback scraper")
        return ESTADO_INCIDENCIA

    cookie_available = (
        config.get("TRACKER_COOKIE") or config.get("TRACKER_COOKIE_VALUE")
    )
    if not cookie_available:
        dbg("[SCRAPER_FALLBACK] Sin cookie disponible para scrape — INCIDENCIA")
        return ESTADO_INCIDENCIA

    try:
        sess = _session(config)
        search_term = query or ""
        url = f"{base}/torrents?tmdbId={tmdb_id or ''}&name={search_term}&page=1"
        dbg(f"[SCRAPER_FALLBACK] HTTP scrape: {url}")
        api_limiter.wait()
        r = sess.get(url, timeout=15)

        if r.status_code != 200:
            dbg(f"[SCRAPER_FALLBACK] HTTP {r.status_code} — INCIDENCIA")
            return ESTADO_INCIDENCIA

        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("tr.torrent-search--list__row")
        if not rows:
            dbg("[SCRAPER_FALLBACK] Sin resultados en scrape — NO_SUBIDO")
            return ESTADO_NO_SUBIDO

        for row in rows:
            if not isinstance(row, bs4_element.Tag):
                continue
            row_tmdb = row.get("data-tmdb-id") or ""
            row_name_tag = row.select_one("a.torrent-search--list__name")
            row_name = (
                row_name_tag.get_text(strip=True)
                if isinstance(row_name_tag, bs4_element.Tag) else ""
            )
            if (tmdb_id and str(row_tmdb) == str(tmdb_id)) or (query and query.lower() in row_name.lower()):
                # v3.1: Verificar cliente con fallback por query (folder name)
                in_client = _check_path_in_client(row_name, client_paths)
                if not in_client and query:
                    in_client = _check_path_in_client(query, client_paths)
                if local_icu:
                    tracker_icu = parse_icu_from_tracker_name(row_name, tmdb_id)
                    if tracker_icu == local_icu:
                        return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE
                    return ESTADO_DUPE_POTENCIAL
                return ESTADO_OK if in_client else ESTADO_FALTA_CLIENTE

        return ESTADO_NO_SUBIDO

    except Exception as e:
        dbg(f"[SCRAPER_FALLBACK] Error: {e} — INCIDENCIA")
        return ESTADO_INCIDENCIA


def search_global_state(config: dict, tmdb_id=None, imdb_id=None, tvdb_id=None,
                        season_num=None, query=None, local_icu: Optional[str] = None,
                        client_paths: Optional[set] = None, local_size_bytes: int = 0,
                        tracker_index: "Optional[TrackerIndex]" = None) -> str:
    """
    CSI v2.0: Consulta per-ítem al tracker con máquina de estados y fallback híbrido.

    Flujo de ejecución:
        1. API UNIT3D (api_token)  → resultado análizado con _compare_icu_results()
        2. Scraper HTTP (cookie)   → _scraper_search_item() si API falla
        3. ESTADO_INCIDENCIA       → si ambas fuentes fallan

    REGLA CRÍTICA:
        NUNCA retornar ESTADO_NO_SUBIDO si hay un fallo de conexión.
        Solo ESTADO_INCIDENCIA cuando no hay certeza de la respuesta del tracker.

    Args:
        config:           Config del script con TRACKER_URL, API_KEY, cookies
        tmdb_id:          TMDB ID a buscar
        imdb_id:          IMDB ID a buscar
        tvdb_id:          TVDB ID a buscar (TV)
        season_num:       Número de temporada (TV)
        query:            Texto de búsqueda por nombre (fallback)
        local_icu:        ICU construido desde el contenido local
        client_paths:     Paths del cliente qBit
        local_size_bytes: Tamaño local para entropía
        tracker_index:    Índice en memoria para consultas sin API

    Returns:
        str: Estado CSI (ESTADO_OK, ESTADO_DUPE_POTENCIAL, ESTADO_FALTA_CLIENTE,
             ESTADO_NO_SUBIDO, ESTADO_INCIDENCIA)
    """
    client_paths = client_paths or set()
    base_url = config.get("TRACKER_URL", "").rstrip("/")
    api_key  = config.get("TRACKER_API_KEY", "")

    # ── Sin URL del tracker → no podemos consultar nada ──────────────────────
    if not base_url:
        dbg("[GLOBAL_STATE] No TRACKER_URL — INCIDENCIA")
        return ESTADO_INCIDENCIA

    # ── Sin API Key → fallback directo al scraper ─────────────────────────────
    if not api_key:
        dbg("[GLOBAL_STATE] Sin API key — usando scraper fallback directamente")
        return _scraper_search_item(
            config, tmdb_id=tmdb_id, tvdb_id=tvdb_id, query=query,
            tracker_index=tracker_index, client_paths=client_paths,
            local_icu=local_icu, local_size_bytes=local_size_bytes,
        )

    # ── Intentar API UNIT3D ───────────────────────────────────────────────────
    url    = f"{base_url}/api/torrents/filter"
    params = {"api_token": api_key, "perPage": 10}

    if tmdb_id:
        params["tmdbId"] = tmdb_id
    elif imdb_id:
        params["imdbId"] = str(imdb_id).replace("tt", "")
    elif tvdb_id:
        params["tvdbId"] = tvdb_id
    elif query:
        params["name"] = query
    else:
        dbg("[GLOBAL_STATE] Sin identificador para consultar — INCIDENCIA")
        return ESTADO_INCIDENCIA

    if season_num is not None:
        params["season_number"] = int(season_num)

    try:
        api_limiter.wait()
        sess = _session(config)
        r    = sess.get(url, params=params, headers=_headers(), timeout=12)

        if r.status_code != 200:
            dbg(f"[GLOBAL_STATE] API HTTP {r.status_code} — intentando scraper fallback")
            return _scraper_search_item(
                config, tmdb_id=tmdb_id, tvdb_id=tvdb_id, query=query,
                tracker_index=tracker_index, client_paths=client_paths,
                local_icu=local_icu, local_size_bytes=local_size_bytes,
            )

        data    = r.json()
        results = data.get("data", [])
        dbg(f"[GLOBAL_STATE] API OK: tmdb={tmdb_id} tvdb={tvdb_id} "
            f"season={season_num} → {len(results)} resultados")

        return _compare_icu_results(results, local_icu, tmdb_id, client_paths, local_size_bytes, query=query or "")

    except Exception as e:
        dbg(f"[GLOBAL_STATE] API Exception: {e} — intentando scraper fallback")
        try:
            return _scraper_search_item(
                config, tmdb_id=tmdb_id, tvdb_id=tvdb_id, query=query,
                tracker_index=tracker_index, client_paths=client_paths,
                local_icu=local_icu, local_size_bytes=local_size_bytes,
            )
        except Exception as e2:
            dbg(f"[GLOBAL_STATE] Scraper fallback también falló: {e2} → INCIDENCIA")
            return ESTADO_INCIDENCIA


def search_global(config: dict, tmdb_id=None, imdb_id=None, tvdb_id=None,
                  season_num=None, query=None) -> bool:
    """
    Legacy bool wrapper de search_global_state() — compatibilidad con v1.x.
    Retorna True si el ítem está confirmado en el tracker (ESTADO_OK o FALTA_CLIENTE o DUPE).
    Retorna False solo si la consulta fue exitosa y el ítem NO está en el tracker.
    CSI v2.0: En caso de INCIDENCIA retorna False conservadoramente (no "no subido").
    """
    state = search_global_state(config, tmdb_id=tmdb_id, imdb_id=imdb_id,
                                tvdb_id=tvdb_id, season_num=season_num, query=query)
    # Interpretar como "existe en tracker" para los estados que no son NO_SUBIDO/INCIDENCIA
    return state in (ESTADO_OK, ESTADO_FALTA_CLIENTE, ESTADO_DUPE_POTENCIAL)

# ─── LIBRARY SCANNER ──────────────────────────────────────────────────────────
def forensic_scan(config: dict, meta: MetadataManager, tracker_index: TrackerIndex,
                  do_global: bool = False, client_paths: Optional[set] = None,
                  client_path_map: Optional[dict] = None,
                  qbit_available: bool = True, report: Optional[LiveReport] = None):
    """
    CSI v2.0: Motor de escaneo forense con máquina de estados completa.
    Walk the library directory tree y clasifica cada ítem en uno de los 5 estados CSI.
    v1.6.5+: Multi-codec (HEVC/H264) reporting logic con LiveReport support.
    TV: Folders only. Movies: Files.

    Cambios v2.0:
      - Eliminado bloque 'if found_in_qbit: continue' como exit temprano.
        El cruce con qBit ahora se hace DENTRO de la determinación de estado,
        para distinguir ESTADO_OK de ESTADO_FALTA_CLIENTE correctamente.
      - Nuevos conjuntos de resultado: dupe_items, falta_cliente_items, incidencia_items.
      - Usa has_movie_state() y has_tv_season_state() en lugar de has_movie() bool.
      - En modo global, usa search_global_state() con ICU y fallback scraper.
    """
    lib_path = Path(config["LIBRARY_PATH"])
    # Conjuntos para ítems ESTADO_NO_SUBIDO (candidatos a upload) — separados por codec
    hevc_movies, h264_movies = set(), set()
    hevc_tv, h264_tv = set(), set()

    # CSI v2.0: Nuevos conjuntos para los demás estados del diagnóstico
    dupe_items        = set()  # ESTADO_DUPE_POTENCIAL — misma peli, diferente versión
    falta_cliente     = set()  # ESTADO_FALTA_CLIENTE  — en tracker, no en qBit
    incidencia_items  = set()  # ESTADO_INCIDENCIA     — error de sonda, sin certeza

    # Contadores de estados para el resumen final
    state_counts = {
        ESTADO_OK:             0,
        ESTADO_DUPE_POTENCIAL: 0,
        ESTADO_FALTA_CLIENTE:  0,
        ESTADO_NO_SUBIDO:      0,
        ESTADO_INCIDENCIA:     0,
    }

    # CSI v3.0: Normalizar client_path_map para path-first matching
    if client_path_map is None:
        client_path_map = {}
    if client_paths is None:
        client_paths = set(client_path_map.keys())

    # Precalcular un set de basenames del cliente para fallback de coincidencia
    client_basenames: set = {os.path.basename(p) for p in client_path_map}

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as prog:
        task = prog.add_task("Scanning library...", total=None)

        for root, dirs, files in os.walk(lib_path):
            dirs.sort()
            root_path = Path(root)

            # v1.6.5: Update Dashboard with current folder and real-time counts
            found_msg = f"Movies: {len(hevc_movies)+len(h264_movies)} | TV: {len(hevc_tv)+len(h264_tv)}"
            update_status("CSI", "Scanning Library", "PROCESSING",
                          details=f"[{found_msg}] Scanning: {root_path}")

            # CSI v3.4 (W10): pre-filter extras. opening.mkv / ending.mkv /
            # menu / sample / trailer / behind-the-scenes / etc never go up
            # as upload candidates and pollute classification (a folder with
            # only opening+ending would otherwise be misread as a 2-file
            # "movie" pack). If filtering empties the folder, skip entirely.
            mkv_files_raw = [f for f in files if f.lower().endswith(".mkv")]
            mkv_files = [f for f in mkv_files_raw
                         if not MetadataManager._RX_EXTRAS_FILE.search(f)]
            dropped = len(mkv_files_raw) - len(mkv_files)
            if dropped:
                dbg(f"[EXTRAS] Dropped {dropped} extras file(s) from {root_path}")
            if not mkv_files:
                continue

            # CSI v2.0: ELIMINADO el bloque 'if found_in_qbit: continue'.
            # El cruce con qBit se hace dentro de has_movie_state() / has_tv_season_state()
            # para poder distinguir ESTADO_OK de ESTADO_FALTA_CLIENTE correctamente.
            # Items que están en qBit Y en tracker → ESTADO_OK (ya gestionados)
            # Items que están en tracker pero NO en qBit → ESTADO_FALTA_CLIENTE (reportar)

            # CSI v3.2: multi-signal classification replaces the old
            # single-regex parent-dir check. classify_folder() considers Arr
            # knowledge, filename patterns (incl. anime episode numbering),
            # parent dir hints, file count, and finally external TMDB lookup.
            # Returns media_type + a best-effort season_num + a confidence
            # score we can route to INCIDENCIA when truly uncertain.
            cls = meta.classify_folder(root_path, lib_path, mkv_files)
            is_tv         = cls["media_type"] == "tv"
            classified_mv = cls["media_type"] == "movie"
            cls_season    = cls.get("season_num")
            cls_conf      = cls.get("confidence", 0.0)
            dbg(f"[CLASSIFY] {root_path} → {cls['media_type']} "
                f"season={cls_season} conf={cls_conf:.2f} reason={cls['reason']}")

            if is_tv:
                # ── TV Season ──────────────────────────────────────────────
                # If classify_folder didn't pin a season, try the dir name once
                # more (covers explicit "Season N" trees that S6 didn't need).
                season_num = cls_season
                if season_num is None:
                    sm = re.search(
                        r"(?:season|temporada|saison|staffel|stagione|seizoen|sezon|сезон)\s*(\d+)|S(\d{1,2})",
                        root_path.name, re.I,
                    )
                    if sm:
                        season_num = int(sm.group(1) or sm.group(2))
                if season_num is None:
                    season_num = 1   # safe default; tracker lookups are id+season

                # CSI v3.2: Specials (S00) NO longer skipped. Tracker may have
                # an explicit S00 / Specials pack; let has_tv_season_state()
                # decide presence. Worst case it returns NO_SUBIDO same as
                # before — but the Babylon 5 / Doctor Who Specials class of
                # folders now gets a chance.

                # Show folder picker — walk up from leaf until Sonarr matches,
                # else fall back to whichever level looks more "showy".
                # Covers four shapes:
                #   /Show/Season 1/        → root.parent
                #   /Show/Specials/        → root.parent (Sonarr matches Show)
                #   /Show/flat-episodes    → root  (Sonarr matches Show)
                #   /[Tonoss] Anime/files  → root  (no Sonarr, name regex)
                # W11: never let the picker descend into lib_path.name itself.
                parent_candidate = root_path.parent.name
                if parent_candidate == lib_path.name:
                    parent_candidate = ""
                if parent_candidate and meta.get_series(parent_candidate):
                    show_folder = parent_candidate
                elif meta._RX_PARENT_SEASON.search(root_path.name):
                    show_folder = parent_candidate or root_path.name
                else:
                    show_folder = root_path.name
                prog.update(task, description=f"TV: {show_folder} S{season_num:02d}")

                series  = meta.get_series(show_folder)
                s_tmdb  = str(series.get("tmdbId") or "") if series else None
                s_tvdb  = str(series.get("tvdbId") or "") if series else None
                s_imdb  = str(series.get("imdbId") or "") if series else None  # CSI v3.0 (A1)

                # CSI v2.0: Construir ICU local para la temporada
                # Usamos el primer mkv como muestra de resolución/codec
                sample_info   = get_video_info(root_path / sorted(mkv_files)[0])
                local_res_raw = sample_info.get("res", "")
                local_cod_raw = sample_info.get("codec", "")
                # Normalizar resolución del mediainfo (ej: "1920x1080" → "1080P")
                local_res = _normalize_mediainfo_res(local_res_raw)
                local_cod = _normalize_mediainfo_codec(local_cod_raw)
                local_grp = normalize_folder_name(show_folder).get("group", "")
                # CSI v3.0 (A1): prioridad simétrica con _ingest — tmdb→tvdb→imdb
                local_icu = build_icu(
                    s_tmdb or s_tvdb or _norm_imdb(s_imdb) or "",
                    local_res, local_cod, local_grp,
                )

                # Tamaño total de la temporada
                local_size_bytes = sum(
                    (root_path / f).stat().st_size for f in mkv_files
                    if (root_path / f).exists()
                )

                # CSI v2.0: Determinación de estado con máquina de estados
                # CSI v3.0 (A1): se propaga s_imdb para OR-match cross-id
                if do_global:
                    item_state = search_global_state(
                        config,
                        tmdb_id=s_tmdb or None,
                        tvdb_id=s_tvdb or None,
                        imdb_id=s_imdb or None,
                        season_num=season_num,
                        query=show_folder,
                        local_icu=local_icu,
                        client_paths=client_paths,
                        local_size_bytes=local_size_bytes,
                        tracker_index=tracker_index,
                    )
                else:
                    item_state = tracker_index.has_tv_season_state(
                        tmdb_id=s_tmdb,
                        tvdb_id=s_tvdb,
                        imdb_id=s_imdb,
                        season_num=season_num,
                        name=show_folder,
                        local_icu=local_icu,
                        client_paths=client_paths,
                        local_size_bytes=local_size_bytes,
                    )

                # CSI v3.1: PATH-FIRST override — evita falsos FALTA_CLIENTE
                # CSI v3.2 (W6): también demote NO_SUBIDO → INCIDENCIA cuando
                # la carpeta está siendo seedeada localmente. No subimos algo
                # que el usuario ya está sembrando: o ya está en este tracker
                # y nuestro lookup falló, o está en otro tracker — ambos
                # escenarios son "no recomendar como uploadable".
                if item_state in (ESTADO_FALTA_CLIENTE, ESTADO_NO_SUBIDO) and client_path_map:
                    target = ESTADO_OK if item_state == ESTADO_FALTA_CLIENTE else ESTADO_INCIDENCIA
                    current_abs = os.path.abspath(str(root_path)).lower()
                    if current_abs in client_path_map:
                        item_state = target
                        dbg(f"[PATH-FIRST] TV {item_state} (exact path): {current_abs}")
                    else:
                        current_basename = os.path.basename(current_abs)
                        if current_basename and current_basename in client_basenames:
                            item_state = target
                            dbg(f"[PATH-FIRST] TV {item_state} (basename): {current_basename}")
                        else:
                            # v3.1: Show folder substring — cubre rutas raíz diferentes
                            sf_lower = show_folder.lower()
                            for cp in client_path_map:
                                if sf_lower in cp:
                                    item_state = target
                                    dbg(f"[PATH-FIRST] TV {item_state} (folder substr): '{sf_lower}' in '{cp[-70:]}'")
                                    break

                # CSI v3.0: QBIT DOWN — FALTA_CLIENTE → INCIDENCIA si qBit no está disponible
                if not qbit_available and item_state == ESTADO_FALTA_CLIENTE:
                    item_state = ESTADO_INCIDENCIA
                    dbg(f"[QBIT_DOWN] TV FALTA_CLIENTE→INCIDENCIA (qBit no disponible)")

                state_counts[item_state] = state_counts.get(item_state, 0) + 1
                dbg(f"[SCAN] TV {show_folder} S{season_num:02d} → [{item_state}] ICU={local_icu}")

                # Clasificar según estado — solo ESTADO_NO_SUBIDO va al report de upload
                abs_dir = str(root_path.absolute())
                if item_state == ESTADO_OK:
                    continue  # Ya está todo en orden
                elif item_state == ESTADO_DUPE_POTENCIAL:
                    dupe_items.add(abs_dir)
                    continue  # No subir — puede ser dupe
                elif item_state == ESTADO_FALTA_CLIENTE:
                    falta_cliente.add(abs_dir)
                    continue  # En tracker pero no seeding
                elif item_state == ESTADO_INCIDENCIA:
                    incidencia_items.add(abs_dir)
                    continue  # Sin certeza — no reportar como uploadable
                # else: ESTADO_NO_SUBIDO → clasificar por codec para upload report

                # Classification by dominant codec (ESTADO_NO_SUBIDO only)
                infos      = [get_video_info(root_path / f) for f in mkv_files]
                hevc_count = sum(1 for info in infos if info["codec"] in HEVC_CODECS)
                all_hevc   = hevc_count == len(mkv_files)

                # CSI v1.6.5: TV_SHOWS -> PARENT DIRECTORY ONLY
                if all_hevc:
                    hevc_tv.add(abs_dir)
                    if report: report.add("TV", "HEVC", abs_dir)
                else:
                    h264_tv.add(abs_dir)
                    if report: report.add("TV", "H264", abs_dir)

            else:
                # ── Movie ──────────────────────────────────────────────────
                movie_folder = root_path.name
                movie_data   = meta.get_movie(movie_folder)
                # CSI v3.2 (W4): Cross-Arr safety net — when the classifier
                # called this a movie but Radarr (incl. all 5 tiers + external
                # resolver) has nothing, try Sonarr too. Some folders look
                # movie-shaped (single file, no Season subdir) but are TV
                # specials, documentaries, or anime that the user dropped in
                # the wrong Arr. Sonarr-hit wins → switches the branch into
                # the TV state machine on the spot rather than burning the
                # item as NO_SUBIDO in the movie list.
                if not movie_data:
                    cross_series = meta.get_series(movie_folder)
                    if cross_series:
                        dbg(f"[CROSS_ARR] '{movie_folder}' missed Radarr but hit "
                            f"Sonarr → re-routing as TV")
                        s_tmdb = str(cross_series.get("tmdbId") or "")
                        s_tvdb = str(cross_series.get("tvdbId") or "")
                        s_imdb = str(cross_series.get("imdbId") or "")
                        # synthesize as virtual S01 — caller has no season
                        cls_season_xa = 1
                        item_state = tracker_index.has_tv_season_state(
                            tmdb_id=s_tmdb, tvdb_id=s_tvdb, imdb_id=s_imdb,
                            season_num=cls_season_xa, name=movie_folder,
                            local_icu=None,
                            client_paths=client_paths,
                            local_size_bytes=0,
                        )
                        abs_dir = str(root_path.absolute())
                        state_counts[item_state] = state_counts.get(item_state, 0) + 1
                        if item_state == ESTADO_OK:
                            continue
                        if item_state == ESTADO_DUPE_POTENCIAL:
                            dupe_items.add(abs_dir); continue
                        if item_state == ESTADO_FALTA_CLIENTE:
                            falta_cliente.add(abs_dir); continue
                        if item_state == ESTADO_INCIDENCIA:
                            incidencia_items.add(abs_dir); continue
                        # else NO_SUBIDO falls through to movie classification below
                m_tmdb = str(movie_data.get("tmdbId") or "") if movie_data else None
                m_imdb = str(movie_data.get("imdbId") or "") if movie_data else None
                prog.update(task, description=f"Movie: {movie_folder}")

                # CSI v2.0: Construir ICU local para la película
                # Usamos el primer mkv como muestra de resolución/codec
                first_mkv    = sorted(mkv_files)[0]
                sample_info  = get_video_info(root_path / first_mkv)
                local_res    = _normalize_mediainfo_res(sample_info.get("res", ""))
                local_cod    = _normalize_mediainfo_codec(sample_info.get("codec", ""))
                local_grp    = normalize_folder_name(movie_folder).get("group", "")
                # CSI v3.0 (A1): prioridad simétrica con _ingest — tmdb→imdb
                local_icu    = build_icu(
                    m_tmdb or _norm_imdb(m_imdb) or "",
                    local_res, local_cod, local_grp,
                )

                # Tamaño del archivo de muestra (película principal)
                try:
                    local_size_bytes = (root_path / first_mkv).stat().st_size
                except Exception:
                    local_size_bytes = 0

                # CSI v2.0: Determinación de estado con máquina de estados
                if do_global:
                    item_state = search_global_state(
                        config,
                        tmdb_id=m_tmdb or None,
                        imdb_id=m_imdb or None,
                        query=movie_folder,
                        local_icu=local_icu,
                        client_paths=client_paths,
                        local_size_bytes=local_size_bytes,
                        tracker_index=tracker_index,
                    )
                else:
                    item_state = tracker_index.has_movie_state(
                        tmdb_id=m_tmdb,
                        imdb_id=m_imdb,
                        name=movie_folder,
                        local_icu=local_icu,
                        client_paths=client_paths,
                        local_size_bytes=local_size_bytes,
                    )

                # CSI v3.1: PATH-FIRST override — evita falsos FALTA_CLIENTE
                # CSI v3.2 (W6): también demote NO_SUBIDO → INCIDENCIA cuando
                # la carpeta está siendo seedeada localmente (no recomendamos
                # subir algo ya en el cliente — el id-lookup pudo fallar pero
                # el archivo está claramente "en uso" desde algún tracker).
                if item_state in (ESTADO_FALTA_CLIENTE, ESTADO_NO_SUBIDO) and client_path_map:
                    target = ESTADO_OK if item_state == ESTADO_FALTA_CLIENTE else ESTADO_INCIDENCIA
                    current_abs = os.path.abspath(str(root_path)).lower()
                    if current_abs in client_path_map:
                        item_state = target
                        dbg(f"[PATH-FIRST] Movie {item_state} (exact path): {current_abs}")
                    else:
                        current_basename = os.path.basename(current_abs)
                        if current_basename and current_basename in client_basenames:
                            item_state = target
                            dbg(f"[PATH-FIRST] Movie {item_state} (basename): {current_basename}")
                        else:
                            # v3.1: Movie folder substring — rutas raíz diferentes
                            mf_lower = movie_folder.lower()
                            for cp in client_path_map:
                                if mf_lower in cp:
                                    item_state = target
                                    dbg(f"[PATH-FIRST] Movie {item_state} (folder substr): '{mf_lower}' in '{cp[-70:]}'")
                                    break

                # CSI v3.0: QBIT DOWN — FALTA_CLIENTE → INCIDENCIA si qBit no está disponible
                if not qbit_available and item_state == ESTADO_FALTA_CLIENTE:
                    item_state = ESTADO_INCIDENCIA
                    dbg(f"[QBIT_DOWN] Movie FALTA_CLIENTE→INCIDENCIA (qBit no disponible)")

                state_counts[item_state] = state_counts.get(item_state, 0) + 1
                dbg(f"[SCAN] Movie '{movie_folder}' → [{item_state}] ICU={local_icu}")

                abs_dir = str(root_path.absolute())
                if item_state == ESTADO_OK:
                    continue
                elif item_state == ESTADO_DUPE_POTENCIAL:
                    dupe_items.add(abs_dir)
                    continue
                elif item_state == ESTADO_FALTA_CLIENTE:
                    falta_cliente.add(abs_dir)
                    continue
                elif item_state == ESTADO_INCIDENCIA:
                    incidencia_items.add(abs_dir)
                    continue
                # else: ESTADO_NO_SUBIDO → clasificar por codec

                # Not on tracker - MOVIES -> INDIVIDUAL FILES
                for mkv in sorted(mkv_files):
                    info     = get_video_info(root_path / mkv)
                    abs_path = str((root_path / mkv).absolute())
                    if info["codec"] in HEVC_CODECS:
                        hevc_movies.add(abs_path)
                        if report: report.add("MOVIE", "HEVC", abs_path)
                    else:
                        h264_movies.add(abs_path)
                        if report: report.add("MOVIE", "H264", abs_path)

    # CSI v2.0: Mostrar resumen de estados al finalizar el scan
    console.print(Panel(
        f"[bold]Resumen del Escaneo Forense CSI v2.0[/bold]\n\n"
        f"  [{STATE_COLORS[ESTADO_OK]}]{STATE_LABELS[ESTADO_OK]}[/{STATE_COLORS[ESTADO_OK]}]: "
        f"[bold]{state_counts[ESTADO_OK]}[/bold]\n"
        f"  [{STATE_COLORS[ESTADO_DUPE_POTENCIAL]}]{STATE_LABELS[ESTADO_DUPE_POTENCIAL]}"
        f"[/{STATE_COLORS[ESTADO_DUPE_POTENCIAL]}]: [bold]{state_counts[ESTADO_DUPE_POTENCIAL]}[/bold]\n"
        f"  [{STATE_COLORS[ESTADO_FALTA_CLIENTE]}]{STATE_LABELS[ESTADO_FALTA_CLIENTE]}"
        f"[/{STATE_COLORS[ESTADO_FALTA_CLIENTE]}]: [bold]{state_counts[ESTADO_FALTA_CLIENTE]}[/bold]\n"
        f"  [{STATE_COLORS[ESTADO_NO_SUBIDO]}]{STATE_LABELS[ESTADO_NO_SUBIDO]}"
        f"[/{STATE_COLORS[ESTADO_NO_SUBIDO]}]: [bold]{state_counts[ESTADO_NO_SUBIDO]}[/bold]\n"
        f"  [{STATE_COLORS[ESTADO_INCIDENCIA]}]{STATE_LABELS[ESTADO_INCIDENCIA]}"
        f"[/{STATE_COLORS[ESTADO_INCIDENCIA]}]: [bold]{state_counts[ESTADO_INCIDENCIA]}[/bold]",
        border_style="cyan",
        title="[bold cyan]Estado de la Triangulación[/bold cyan]",
    ))

    # Convert sets back to sorted lists for reporting
    return (
        sorted(list(hevc_tv)),
        sorted(list(h264_tv)),
        sorted(list(hevc_movies)),
        sorted(list(h264_movies)),
        sorted(list(dupe_items)),
        sorted(list(falta_cliente)),
        sorted(list(incidencia_items)),
    )


def _normalize_mediainfo_res(res_raw: str) -> str:
    """
    CSI v2.0: Convierte resolución de MediaInfo (ej: '1920x1080') al formato ICU (ej: '1080P').
    Necesario para cruzar información local de MediaInfo con ICU del tracker.
    """
    if not res_raw or res_raw == "unknown":
        return "UNKNOWN"
    try:
        parts = res_raw.lower().replace("x", "×").split("×")
        if len(parts) >= 2:
            height = int(parts[1])
            if height >= 2000:  return "2160P"
            if height >= 1000:  return "1080P"
            if height >= 700:   return "720P"
            if height >= 500:   return "576P"
            if height >= 400:   return "480P"
    except (ValueError, IndexError):
        pass
    # Fallback: buscar patrones directamente en el string raw
    for pattern, value in _RES_PATTERNS:
        if pattern.search(res_raw):
            return value
    return "UNKNOWN"


def _normalize_mediainfo_codec(codec_raw: str) -> str:
    """
    CSI v2.0: Normaliza el codec de MediaInfo al formato ICU.
    MediaInfo retorna 'HEVC', 'AVC', 'MPEG Video', etc.
    Los normalizamos al mismo vocabulario que usa normalize_folder_name().
    """
    if not codec_raw or codec_raw == "unknown":
        return "UNKNOWN"
    c = codec_raw.lower().strip()
    if c in {"hevc", "h.265", "h265", "x265"}:
        return "x265"
    if c in {"avc", "h.264", "h264", "x264"}:
        return "x264"
    if c in {"mpeg video", "mpeg-4", "mpeg4", "xvid", "divx"}:
        return "XVID"
    # Para codecs de fuente (REMUX, etc.) que no vienen de MediaInfo
    for pattern, value in _CODEC_PATTERNS:
        if pattern.search(codec_raw):
            return value
    return codec_raw.upper()

# ─── REPORTS ──────────────────────────────────────────────────────────────────
def get_report_path(category: str, codec: str, subcat: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_map = {"local": "local_cases", "user": "user_cases", "global": "global_cases"}
    base   = REPORTS_DIR / subdir_map.get(subcat, subcat)
    folder = base / ("tv_shows" if category.upper() == "TV" else "movies")
    folder.mkdir(parents=True, exist_ok=True)
    
    # CSI v1.6.5 Naming
    return folder / f"{category.upper()}_{codec.upper()}_uploadable_{ts}.txt"

def generate_reports(hevc_tv, h264_tv, hevc_m, h264_m, subcat: str, config: dict,
                     dupe_items: Optional[list] = None, falta_cliente_items: Optional[list] = None,
                     incidencia_items: Optional[list] = None):
    """
    CSI v2.0: Genera reportes de todos los estados del diagnóstico.
    - HEVC/H264 TV y Movie: candidatos a upload (ESTADO_NO_SUBIDO)
    - DUPE_POTENCIAL: ítems con mismo TMDB pero diferente versión (revisar manualmente)
    - FALTA_CLIENTE: en tracker pero sin seedear (descargar .torrent)
    - INCIDENCIA: ítems que no se pudieron verificar (error de sonda)
    """
    dupe_items        = dupe_items        or []
    falta_cliente_items = falta_cliente_items or []
    incidencia_items  = incidencia_items  or []

    # Reportes de upload (ESTADO_NO_SUBIDO) — comportamiento v1.6.5 intacto
    upload_sets = [
        ("TV",    "HEVC", hevc_tv),
        ("TV",    "H264", h264_tv),
        ("MOVIE", "HEVC", hevc_m),
        ("MOVIE", "H264", h264_m),
    ]
    generated_upload = []
    for cat, codec, data in upload_sets:
        if data:
            p = get_report_path(cat, codec, subcat)
            with open(p, "w", encoding="utf-8") as f:
                # CSI v1.6.5: Strict Output Protocol (Absolute Paths Only)
                # No headers, no metadata, one per line.
                f.write("\n".join(data) + "\n")
            generated_upload.append(p)

    # CSI v2.0: Reportes adicionales de diagnóstico
    generated_diag = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_map = {"local": "local_cases", "user": "user_cases", "global": "global_cases"}
    base_dir = REPORTS_DIR / subdir_map.get(subcat, subcat)

    if dupe_items:
        p_dupe = base_dir / f"DUPE_POTENCIAL_{ts}.txt"
        p_dupe.parent.mkdir(parents=True, exist_ok=True)
        with open(p_dupe, "w", encoding="utf-8") as f:
            f.write("# CSI v2.0 — DUPE POTENCIAL: misma película, versión diferente en tracker\n")
            f.write("# Revisar manualmente antes de hacer upload\n")
            f.write("\n".join(dupe_items) + "\n")
        generated_diag.append(("DUPE_POTENCIAL", p_dupe))
        dbg(f"[REPORT] DUPE_POTENCIAL: {len(dupe_items)} ítems → {p_dupe}")

    if falta_cliente_items:
        p_fc = base_dir / f"FALTA_CLIENTE_{ts}.txt"
        p_fc.parent.mkdir(parents=True, exist_ok=True)
        with open(p_fc, "w", encoding="utf-8") as f:
            f.write("# CSI v2.0 — FALTA EN CLIENTE: en tracker pero no seeding en qBittorrent\n")
            f.write("# Descargar el .torrent y añadir al cliente para reanudar seeding\n")
            f.write("\n".join(falta_cliente_items) + "\n")
        generated_diag.append(("FALTA_CLIENTE", p_fc))
        dbg(f"[REPORT] FALTA_CLIENTE: {len(falta_cliente_items)} ítems → {p_fc}")

    if incidencia_items:
        p_inc = base_dir / f"INCIDENCIA_{ts}.txt"
        p_inc.parent.mkdir(parents=True, exist_ok=True)
        with open(p_inc, "w", encoding="utf-8") as f:
            f.write("# CSI v2.0 — INCIDENCIA: error de sonda, no se pudo verificar estado\n")
            f.write("# Reintentar cuando la conexión al tracker esté restaurada\n")
            f.write("\n".join(incidencia_items) + "\n")
        generated_diag.append(("INCIDENCIA", p_inc))
        dbg(f"[REPORT] INCIDENCIA: {len(incidencia_items)} ítems → {p_inc}")

    # ── Mostrar resultados ────────────────────────────────────────────────────
    if generated_upload or generated_diag:
        console.print(Panel("[bold green]CSI Reports Generated (v2.0)[/bold green]", border_style="green"))

        if generated_upload:
            console.print(f"\n  [bold cyan]Candidatos a Upload (ESTADO_NO_SUBIDO):[/bold cyan]")
            for i, g in enumerate(generated_upload, 1):
                relative_path = g.relative_to(REPORTS_DIR)
                host_path = f"{HOST_REPORTS_DIR}/{relative_path}"
                console.print(f"  {i}. [cyan]{host_path}[/cyan]")
                dbg(f"[OK] Report saved to: {host_path}")

        if generated_diag:
            console.print(f"\n  [bold yellow]Reportes de Diagnóstico:[/bold yellow]")
            for label, p in generated_diag:
                color = STATE_COLORS.get(f"ESTADO_{label}", "yellow")
                relative_path = p.relative_to(REPORTS_DIR)
                host_path = f"{HOST_REPORTS_DIR}/{relative_path}"
                console.print(f"  [{color}]{label}[/{color}]: [dim]{host_path}[/dim]")

        if generated_upload:
            if Confirm.ask("\nFeed Singularity for upload?"):
                choices = [str(i) for i in range(1, len(generated_upload) + 1)]
                sel = Prompt.ask("Select report number", choices=choices)
                _feed_singularity(generated_upload[int(sel) - 1], config)
    else:
        console.print(Panel("[yellow]No new items found for reporting.[/yellow]", border_style="yellow"))

def _feed_singularity(report_path: Path, config: dict):
    rawloadrr_dir = BASE_DIR / "RawLoadrr"
    if not rawloadrr_dir.exists():
        dbg(f"RawLoadrr directory not found: {rawloadrr_dir}")
        return

    tracker = config.get("TRACKER_DEFAULT") or "MILNU"
    cmd = [
        "python3", "auto-upload.py",
        "--list", str(Path(report_path).resolve()), "--tracker", tracker,
    ]
    console.print(f"[cyan]Launching: {' '.join(cmd)}[/cyan]")
    try:
        subprocess.run(cmd, cwd=rawloadrr_dir, check=True)
    except subprocess.CalledProcessError as e:
        dbg(f"auto-upload.py failed with exit code {e.returncode}")
    except Exception as e:
        dbg(f"Error launching singularity feed: {e}")

# ─── qBIT LOCAL ───────────────────────────────────────────────────────────────
def get_client_torrents(config: dict):
    """
    CSI v3.0: Retorna un diccionario de torrents indexados por ruta absoluta.
    Clave  : os.path.abspath(content_path).lower()
    Valor  : {"hash": str, "size": int}
    Filtrado por dominio del TRACKER_URL configurado (si está disponible).
    Retorna None si la conexión con qBittorrent falla (señal de error para el llamador).
    Retorna {} si qbittorrentapi no está instalado (no-op silencioso).
    """
    if not qbittorrentapi:
        dbg("qbittorrentapi not installed")
        return {}

    tracker_domain = ""
    tracker_url = config.get("TRACKER_URL", "") or ""
    if tracker_url:
        m = re.match(r"https?://([^/]+)", tracker_url)
        if m:
            tracker_domain = m.group(1).lower()

    try:
        qbt = qbittorrentapi.Client(
            host=config["QBIT_URL"],
            username=config["QBIT_USER"],
            password=config["QBIT_PASS"],
            REQUESTS_ARGS={'timeout': (5, 15), 'headers': {'User-Agent': 'undici'}}
        )
        qbt.auth_log_in()

        if not qbt.is_logged_in:
            dbg(f"qBit: Authentication failed for {config['QBIT_URL']}")
            return None

        result: dict = {}
        skipped = 0
        no_tracker = 0
        sample_skipped_tracker = ""
        for t in qbt.torrents_info():
            if tracker_domain:
                t_tracker = str(getattr(t, "tracker", "") or "").lower()
                if not t_tracker:
                    no_tracker += 1
                elif tracker_domain not in t_tracker:
                    if not sample_skipped_tracker:
                        sample_skipped_tracker = t_tracker
                    skipped += 1
                    continue
            abs_path = os.path.abspath(str(t.content_path)).lower()
            result[abs_path] = {
                "hash": str(getattr(t, "hash", "") or ""),
                "size": int(getattr(t, "size", 0) or 0),
            }

        total = len(result) + skipped
        dbg(
            f"qBit: {len(result)} torrent paths loaded (total={total}, "
            f"filtered by '{tracker_domain}', skipped={skipped}, no_tracker={no_tracker})"
        )
        if sample_skipped_tracker:
            dbg(f"qBit: Sample skipped tracker URL: '{sample_skipped_tracker}'")
        # v3.1: Warning si el filtro excluye todos los torrents
        if tracker_domain and len(result) == 0 and skipped > 0:
            console.print(
                f"[bold yellow]⚠ El filtro de dominio '{tracker_domain}' excluyó los {skipped} "
                f"torrents. Posible mismatch de dominio.[/bold yellow]"
            )
        return result

    except Exception as e:
        dbg(f"qBit error: {e}")
        return None

# ─── MENU HELPERS ─────────────────────────────────────────────────────────────
def welcome():
    console.clear()
    console.print(Align.center(DETECTIVE_ART))
    console.print(Align.center(
        Panel.fit("[bold cyan]Forensic Detective[/bold cyan] · Check · Search · Identify", border_style="cyan")
    ))

def _status_panel(config: dict):
    t_url  = config["TRACKER_URL"] or "[red]NOT SET[/red]"
    t_key  = "[green]✓[/green]" if config["TRACKER_API_KEY"] else "[red]✗[/red]"
    t_user = f"[cyan]{config['TRACKER_USERNAME']}[/cyan]" if config["TRACKER_USERNAME"] else "[yellow]no username[/yellow]"
    lib    = config["LIBRARY_PATH"] or "[red]NOT SET[/red]"
    tor    = "[red]ON ⚠[/red]"  if config["USE_TOR"]  else "[green]OFF[/green]"
    dbg_s  = "[green]ON[/green]" if DEBUG_MODE         else "[dim]OFF[/dim]"
    console.print(Panel(
        f"Tracker : {t_url}  API:{t_key}  user:{t_user}\n"
        f"Library : [cyan]{lib}[/cyan]\n"
        f"TOR: {tor}   Debug: {dbg_s}",
        border_style="cyan", title="[bold]CSI Status[/bold]",
    ))
    if config["USE_TOR"]:
        console.print(Panel(
            "[bold red]TOR IS ENABLED[/bold red]\n"
            "CF Rule 1 blocks ALL paths (incl. /api/) from non-ES IPs with threat_score>15.\n"
            "Disable TOR in Settings before running any investigation.",
            border_style="red", title="⚠ CF Firewall Warning",
        ))

def _confirm_scan_path(config: dict) -> dict:
    current = config.get("LIBRARY_PATH")
    if current and os.path.isdir(current):
        console.print(f"Library path: [cyan]{current}[/cyan]")
        if Confirm.ask("Use this path?", default=True):
            return config
    while True:
        lib = Prompt.ask("[bold]Enter library path to scan[/bold]")
        if os.path.isdir(lib):
            save_config("CSI_LIBRARY_PATH", lib)
            return load_config()
        console.print(f"[red]Path not found: {lib}[/red]")

# ─── CONFIGURE TRACKER ────────────────────────────────────────────────────────
def configure_tracker(config: dict) -> dict:
    rl_cfg      = config.get("RAWLOADRR_CFG", {})
    rl_trackers = [
        k for k, v in rl_cfg.get("TRACKERS", {}).items()
        if isinstance(v, dict) and not k.startswith("default")
    ]

    while True:
        console.print(Panel("CSI Settings & Tracker Management", border_style="cyan"))
        cur_url  = config["TRACKER_URL"]  or "None"
        cur_name = config["TRACKER_DEFAULT"] or "None"
        cur_user = config["TRACKER_USERNAME"] or "None"
        cur_ua   = config["CUSTOM_USER_AGENT"]
        cur_tmp  = config["TMP_PATH"]
        cur_qbit = config["QBIT_URL"]
        
        console.print(f"  Tracker: [cyan]{cur_url}[/cyan]  ID:[bold]{cur_name}[/bold]  user:[bold]{cur_user}[/bold]")
        console.print(f"  TMP Path: [green]{cur_tmp}[/green]")
        console.print(f"  qBit URL: [green]{cur_qbit}[/green]")
        console.print(f"  User-Agent: [bold yellow]{cur_ua}[/bold yellow]")
        
        console.print(
            f"\n  1. Edit Tracker (URL, API, Cookies)\n"
            f"  2. Import from RawLoadrr config\n"
            f"  3. Set TMP Path\n"
            f"  4. Set qBittorrent URL\n"
            f"  5. Set Custom User-Agent\n"
            f"  6. Toggle TOR: {'[red]ON[/red]' if config.get('USE_TOR') else '[green]OFF[/green]'}\n"
            f"  7. Toggle Debug: {'[green]ON[/green]' if DEBUG_MODE else '[dim]OFF[/dim]'}\n"
            f"  0. Back"
        )
        c = Prompt.ask("Select", choices=["1", "2", "3", "4", "5", "6", "7", "0"], default="0")

        if c == "0":
            return config

        elif c == "1":
            name = Prompt.ask("Tracker ID", default=config.get("TRACKER_DEFAULT") or "NOBS")
            url = Prompt.ask("Base URL", default=config.get("TRACKER_URL") or "")
            user = Prompt.ask("Username", default=config.get("TRACKER_USERNAME") or "")
            api_key = Prompt.ask("API Key", default=config.get("TRACKER_API_KEY") or "")
            
            # Cookie handling v1.6.2
            old_cookie = config.get("TRACKER_COOKIE") or config.get("TRACKER_COOKIE_VALUE") or ""
            cookie_val = Prompt.ask("TRACKER_COOKIE (Session Value)", default=old_cookie)
            
            n = name.strip().upper()
            save_config("TRACKER_DEFAULT", n)
            save_config("TRACKER_BASE_URL", url)
            save_config(f"TRACKER_{n}_URL", url)
            save_config("TRACKER_USERNAME", user)
            save_config("TRACKER_API_KEY", api_key)
            save_config(f"TRACKER_{n}_API_KEY", api_key)
            save_config("TRACKER_COOKIE", cookie_val)
            save_config("TRACKER_COOKIE_VALUE", cookie_val)
            
            # Propagar cambios
            updates = {
                "TRACKER_DEFAULT": n,
                "TRACKER_URL": url,
                "TRACKER_USERNAME": user,
                "TRACKER_COOKIE_VALUE": cookie_val
            }
            
            # Pedir Announce URL para RawLoadrr
            announce = Prompt.ask("Announce URL (para RawLoadrr)", default="")
            if announce:
                updates["ANNOUNCE_URL"] = announce
            
            _propagate_config(updates)
            config = load_config()

        elif c == "3":
            new_tmp = Prompt.ask("Enter TMP path", default=config["TMP_PATH"])
            save_config("TMP_PATH", new_tmp)
            config = load_config()

        elif c == "4":
            new_qbit = Prompt.ask("Enter qBittorrent URL", default=config["QBIT_URL"])
            save_config("QBIT_URL", new_qbit)
            config = load_config()

        elif c == "5":
            new_ua = Prompt.ask("Enter Custom User-Agent", default=config["CUSTOM_USER_AGENT"])
            save_config("CUSTOM_USER_AGENT", new_ua)
            config = load_config()

        elif c == "6":
            config["USE_TOR"] = not config.get("USE_TOR", False)
            save_config("CSI_USE_TOR", str(config["USE_TOR"]))
            config = load_config()

        elif c == "7":
            toggle_debug()
            save_config("CSI_DEBUG", str(DEBUG_MODE))
            config = load_config()

        elif c == "2":
            if not rl_trackers:
                console.print("[yellow]No configured trackers found in RawLoadrr/data/config.py[/yellow]")
                continue
            console.print("\n[bold]Trackers available in RawLoadrr:[/bold]")
            for i, t in enumerate(rl_trackers, 1):
                ak, bu = _rl_tracker_info(rl_cfg, t)
                status = f"[green]API ✓[/green]" if ak else "[red]no API key[/red]"
                console.print(f"  {i}. {t} — {status}  {bu}")
            sel = Prompt.ask("Select", choices=["0"] + [str(i) for i in range(1, len(rl_trackers) + 1)], default="0")
            if sel != "0":
                name = rl_trackers[int(sel) - 1]
                ak, bu = _rl_tracker_info(rl_cfg, name)
                user = Prompt.ask("Username", default=config.get("TRACKER_USERNAME") or "")
                n = name.upper()
                save_config("TRACKER_DEFAULT", n)
                save_config("TRACKER_BASE_URL", bu or "")
                save_config(f"TRACKER_{n}_URL", bu or "")
                save_config("TRACKER_USERNAME", user)
                if ak:
                    save_config("TRACKER_API_KEY", ak)
                config = load_config()

def _detect_auth_failure(soup: BeautifulSoup) -> bool:
    """CSI v3.1: Detecta si una página UNIT3D es un login page en vez de contenido.
    UNIT3D devuelve HTTP 200 con login page para cookies expiradas."""
    # Indicadores explícitos de login page
    if soup.select("form[action*=login]") or soup.select("input[name=password]"):
        return True
    # Ausencia total del contenedor de lista de torrents
    if not soup.select(".torrent-search") and not soup.select("table.table") and not soup.select("tr.torrent-search--list__row"):
        return True
    return False


def validate_cookie(config: dict) -> tuple:
    """CSI v3.1: Valida la cookie del tracker con inspección de contenido.
    Retorna (is_valid: bool, reason: str)."""
    if not config.get("TRACKER_URL") or not (config.get("TRACKER_COOKIE") or config.get("TRACKER_COOKIE_VALUE")):
        return False, "No hay cookie configurada"

    base = config["TRACKER_URL"].rstrip("/")
    user = config.get("TRACKER_USERNAME", "")
    sess = _session(config)
    try:
        url = f"{base}/torrents"
        if user:
            url = f"{base}/torrents?uploader={user}&page=1"
        r = sess.get(url, timeout=15, allow_redirects=True)

        if r.status_code != 200:
            reason = f"HTTP {r.status_code}"
            dbg(f"Cookie validation failed: {reason}")
            return False, reason

        # Inspeccionar contenido — UNIT3D devuelve 200 con login page para cookies expiradas
        soup = BeautifulSoup(r.text, "html.parser")
        if _detect_auth_failure(soup):
            reason = "Cookie expirada — el tracker devuelve página de login"
            dbg(f"Cookie validation failed: {reason}")
            return False, reason

        return True, "OK"
    except Exception as e:
        reason = f"Error de conexión: {e}"
        dbg(f"Cookie validation error: {reason}")
        return False, reason

def validate_qbit(config: dict) -> bool:
    """Test connectivity to qBittorrent."""
    if not qbittorrentapi:
        dbg("qbittorrentapi not installed")
        return False
    if not config.get("QBIT_URL"):
        return False
    try:
        # CSI v1.6.4: Robust Client Initialization
        qbt = qbittorrentapi.Client(
            host=config["QBIT_URL"],
            username=config["QBIT_USER"],
            password=config["QBIT_PASS"],
            REQUESTS_ARGS={'timeout': (5, 10), 'headers': {'User-Agent': 'undici'}}
        )
        qbt.auth_log_in()
        return qbt.is_logged_in
    except Exception as e:
        dbg(f"qBit validation error: {e}")
        return False

def check_integrations_config(config: dict) -> dict:
    """
    CSI Auto-Configuration: Recon & Setup for TMDB, Sonarr, and Radarr.
    Prioriza descubrimiento en archivos de configuración, luego pregunta.
    """
    needs_save = False
    updates_to_propagate = {}

    def _recon_val(key: str, current: Optional[str]) -> Optional[str]:
        # Si ya lo tenemos en config y es válido, usarlo
        # FIX: Endurecer filtro de valores inválidos (YOUR_, None, vacíos)
        invalid_markers = ["tu_clave", "your_", "none", "null"]
        if current and current.strip() and not any(m in current.lower() for m in invalid_markers):
            return current
        
        # Búsqueda forense en archivos conocidos de la suite
        candidates = [BASE_DIR / ".env", BASE_DIR / "RawLoadrr" / ".env"]
        for p in candidates:
            if p.exists():
                try:
                    c = p.read_text(encoding="utf-8")
                    m = re.search(fr"^\s*{key}\s*=\s*['\"]?([^'\"]+)['\"]?", c, re.MULTILINE)
                    if m:
                        v = m.group(1).strip()
                        if v and not any(mk in v.lower() for mk in invalid_markers):
                            return v
                except:
                    pass
        return None

    console.print(Panel("[bold cyan]🕵️  RECON & AUTO-CONFIGURATION[/bold cyan]", border_style="cyan"))

    # 1. TMDB_API_KEY
    tmdb = _recon_val("TMDB_API_KEY", config.get("TMDB_API_KEY"))
    if tmdb:
        if tmdb != config.get("TMDB_API_KEY"):
            save_config("TMDB_API_KEY", tmdb)
            config["TMDB_API_KEY"] = tmdb
            needs_save = True
            updates_to_propagate["TMDB_API_KEY"] = tmdb
        console.print(f"[green]✓ [OK] TMDB detectado.[/green]")
    else:
        console.print("[yellow]⚠ TMDB API Key no encontrada.[/yellow]")
        val = Prompt.ask("TMDB API Key (v3)")
        if val:
            save_config("TMDB_API_KEY", val)
            config["TMDB_API_KEY"] = val
            needs_save = True
            updates_to_propagate["TMDB_API_KEY"] = val

    # 2. SONARR
    s_url = _recon_val("SONARR_URL", config.get("SONARR_URL"))
    s_key = _recon_val("SONARR_API_KEY", config.get("SONARR_API_KEY"))

    if s_url and s_key:
        if s_url != config.get("SONARR_URL") or s_key != config.get("SONARR_API_KEY"):
            save_config("SONARR_URL", s_url)
            save_config("SONARR_API_KEY", s_key)
            config["SONARR_URL"] = s_url
            config["SONARR_API_KEY"] = s_key
            needs_save = True
            updates_to_propagate["SONARR_URL"] = s_url
            updates_to_propagate["SONARR_API_KEY"] = s_key
        console.print(f"[green]✓ [OK] Sonarr detectado ({s_url}).[/green]")
    else:
        console.print("\n[bold]Configurando acceso a Sonarr...[/bold]")
        if Confirm.ask("¿Tienes Sonarr instalado?", default=True):
            s_url = Prompt.ask("Sonarr URL", default="http://localhost:8989")
            s_key = Prompt.ask("Sonarr API Key")
            if s_url and s_key:
                save_config("SONARR_URL", s_url)
                save_config("SONARR_API_KEY", s_key)
                config["SONARR_URL"] = s_url
                config["SONARR_API_KEY"] = s_key
                needs_save = True
                updates_to_propagate["SONARR_URL"] = s_url
                updates_to_propagate["SONARR_API_KEY"] = s_key

    # 3. RADARR
    r_url = _recon_val("RADARR_URL", config.get("RADARR_URL"))
    r_key = _recon_val("RADARR_API_KEY", config.get("RADARR_API_KEY"))

    if r_url and r_key:
        if r_url != config.get("RADARR_URL") or r_key != config.get("RADARR_API_KEY"):
            save_config("RADARR_URL", r_url)
            save_config("RADARR_API_KEY", r_key)
            config["RADARR_URL"] = r_url
            config["RADARR_API_KEY"] = r_key
            needs_save = True
            updates_to_propagate["RADARR_URL"] = r_url
            updates_to_propagate["RADARR_API_KEY"] = r_key
        console.print(f"[green]✓ [OK] Radarr detectado ({r_url}).[/green]")
    else:
        console.print("\n[bold]Configurando acceso a Radarr...[/bold]")
        if Confirm.ask("¿Tienes Radarr instalado?", default=True):
            r_url = Prompt.ask("Radarr URL", default="http://localhost:7878")
            r_key = Prompt.ask("Radarr API Key")
            if r_url and r_key:
                save_config("RADARR_URL", r_url)
                save_config("RADARR_API_KEY", r_key)
                config["RADARR_URL"] = r_url
                config["RADARR_API_KEY"] = r_key
                needs_save = True
                updates_to_propagate["RADARR_URL"] = r_url
                updates_to_propagate["RADARR_API_KEY"] = r_key

    if needs_save:
        console.print("[green]✓ Configuración guardada en .env[/green]")
        if updates_to_propagate:
            _propagate_config(updates_to_propagate)
        return load_config()
    return config

def check_essential_config(config: dict) -> dict:
    """Ensure mandatory v1.6.2 settings are present and valid (TUI Bootstrap)."""
    needs_save = False
    updates = {}
    
    # 1. Tracker Cookie
    cookie = config.get("TRACKER_COOKIE") or config.get("TRACKER_COOKIE_VALUE")
    _cookie_valid, _cookie_reason = validate_cookie(config)
    if not cookie or not _cookie_valid:
        console.print("[bold red]TRACKER_COOKIE missing or expired![/bold red]")
        new_cookie = Prompt.ask("Enter your tracker session cookie", default=cookie or "")
        if new_cookie:
            save_config("TRACKER_COOKIE", new_cookie)
            save_config("TRACKER_COOKIE_VALUE", new_cookie)
            config["TRACKER_COOKIE"] = new_cookie
            needs_save = True
            updates["TRACKER_COOKIE_VALUE"] = new_cookie

    # 2. qBittorrent
    if not validate_qbit(config):
        console.print("[bold yellow]qBittorrent connection failed or not configured.[/bold yellow]")
        new_qbit = Prompt.ask("qBittorrent URL", default=config.get("QBIT_URL") or "http://localhost:8888")
        new_user = Prompt.ask("qBit Username", default=config.get("QBIT_USER") or "admin")
        
        # Security: Do not echo password, don't pre-populate default unless empty
        new_pass = Prompt.ask("qBit Password", password=True)
        if not new_pass:
            new_pass = config.get("QBIT_PASS") or "adminadmin"
        
        save_config("QBIT_URL", new_qbit)
        save_config("QBIT_USER", new_user)
        save_config("QBIT_PASS", new_pass)
        config["QBIT_URL"] = new_qbit
        config["QBIT_USER"] = new_user
        config["QBIT_PASS"] = new_pass
        needs_save = True

    # 3. TMP Path
    if not os.path.exists(config.get("TMP_PATH", "")):
        console.print(f"[bold yellow]TMP_PATH not found: {config.get('TMP_PATH')}[/bold yellow]")
        new_tmp = Prompt.ask("Enter TMP path", default="/app/RawLoadrr/tmp")
        save_config("TMP_PATH", new_tmp)
        config["TMP_PATH"] = new_tmp
        needs_save = True

    if needs_save:
        if updates:
            _propagate_config(updates)
        return load_config()
    return config

# ─── INVESTIGATIONS ───────────────────────────────────────────────────────────
def tracker_investigation(config: dict):
    """
    CSI v2.0: Investigación principal con tracker.
    Integra carga/guardado del TrackerIndex (persistencia entre sesiones),
    máquina de estados completa y reportes de diagnóstico adicionales.
    """
    update_status("CSI", "Tracker Investigation", "STARTING")
    user = config.get("TRACKER_USERNAME", "").strip()
    if not user or user.lower() == "no username":
        console.print("[yellow]TRACKER_USERNAME is missing or set to 'no username'.[/yellow]")
        user = Prompt.ask("Enter your username on this tracker")
        if not user:
            return
        save_config("TRACKER_USERNAME", user)
        config["TRACKER_USERNAME"] = user
    console.print(Panel(
        f"Target: [bold cyan]{config['TRACKER_URL']}[/bold cyan]  "
        f"user:[bold]{config['TRACKER_USERNAME']}[/bold]",
        border_style="cyan",
    ))
    console.print("  a. Your Uploads\n  b. Global Search\n  r. Return")
    sub = Prompt.ask("Mode", choices=["a", "b", "r"], default="a")
    if sub == "r":
        return

    config = _confirm_scan_path(config)

    # CSI v3.1: Pre-flight cookie validation
    cookie_ok, cookie_reason = validate_cookie(config)
    if not cookie_ok:
        console.print(Panel(
            f"[bold red]Cookie inválida[/bold red]: {cookie_reason}\n\n"
            "[yellow]Si continúas, el scraper no podrá verificar uploads del usuario.\n"
            "Los ítems no verificables se marcarán como INCIDENCIA en vez de NO_SUBIDO.\n"
            "El modo Global (API) puede funcionar si la API key es válida.[/yellow]",
            border_style="red", title="⚠ Auth Warning",
        ))
        if not Confirm.ask("¿Continuar en modo degradado?", default=False):
            return
    else:
        dbg(f"[AUTH] Cookie validation OK")

    meta = MetadataManager(config)
    console.print("[cyan]Loading Sonarr / Radarr catalogs...[/cyan]")
    meta.load_sonarr()
    meta.load_radarr()

    tracker_index = TrackerIndex()
    do_global     = False

    if sub == "a":
        # CSI v2.0: Intentar cargar el índice persistido antes de reconstruir
        index_loaded = tracker_index.load_from_json(
            TRACKER_INDEX_PATH,
            tracker_url=config.get("TRACKER_URL", ""),
        )

        if index_loaded:
            console.print(
                "[dim green]▸ Índice cargado desde caché. "
                "Úsalo para esta sesión o reconstruye desde cero.[/dim green]"
            )
            rebuild = Confirm.ask("¿Reconstruir el índice desde el tracker?", default=False)
            if rebuild:
                tracker_index = TrackerIndex()  # Reset
                if not tracker_index.build_user(config):
                    return
        else:
            # No hay índice cacheado o es inválido → construir desde cero
            if not tracker_index.build_user(config):
                return

        subcat = "user"
    else:
        do_global = True
        subcat    = "global"
        console.print(
            "[yellow]Global mode: one API call per library item. "
            "Rate limiter active.[/yellow]"
        )

    # v1.6.5 + v2.0 + v3.0: Multi-codec scan con máquina de estados y path-first matching
    console.print("[cyan]Loading torrent client index...[/cyan]")
    _qbit_raw     = get_client_torrents(config)
    qbit_ok       = _qbit_raw is not None
    _client_map   = _qbit_raw if qbit_ok else {}
    _client_paths = set(_client_map.keys())

    if not qbit_ok:
        console.print(
            "[yellow]⚠ qBittorrent no disponible. Los ítems FALTA_CLIENTE se marcarán como "
            "INCIDENCIA hasta que el cliente vuelva a estar accesible.[/yellow]"
        )

    with LiveReport(subcat, config) as live:
        hevc_tv, h264_tv, hevc_m, h264_m, dupes, falta_cli, incidencias = forensic_scan(
            config, meta, tracker_index, do_global=do_global,
            client_paths=_client_paths, client_path_map=_client_map,
            qbit_available=qbit_ok, report=live,
        )

    update_status("CSI", "Tracker Investigation", "GENERATING_REPORTS")

    # CSI v2.0: Guardar el índice actualizado antes de generar reportes
    if sub == "a" and not do_global:
        tracker_index.save_to_json(TRACKER_INDEX_PATH)

    generate_reports(
        hevc_tv, h264_tv, hevc_m, h264_m, subcat, config,
        dupe_items=dupes,
        falta_cliente_items=falta_cli,
        incidencia_items=incidencias,
    )
    update_status("CSI", "Tracker Investigation", "FINISHED")

def local_investigation(config: dict):
    """
    CSI v2.0: Investigación local (sin tracker).
    Compara la biblioteca contra qBittorrent usando IDs de Sonarr/Radarr.
    El TrackerIndex se usa vacío (sin scraping) — solo cruce con cliente local.
    """
    update_status("CSI", "Local Investigation", "STARTING")
    config = _confirm_scan_path(config)
    console.print("[cyan]Loading torrent client index...[/cyan]")

    meta = MetadataManager(config)
    console.print("[cyan]Loading Sonarr / Radarr catalogs...[/cyan]")
    meta.load_sonarr()
    meta.load_radarr()

    tracker_index = TrackerIndex()
    console.print(
        "[yellow]Local mode: comparing library against torrent client. "
        "ID matching used where Sonarr/Radarr data is available.[/yellow]"
    )
    # v3.0: Build client_path_map for path-first matching
    _qbit_raw     = get_client_torrents(config)
    qbit_ok       = _qbit_raw is not None
    _client_map   = _qbit_raw if qbit_ok else {}
    _client_paths = set(_client_map.keys())
    if not qbit_ok:
        console.print(
            "[yellow]⚠ qBittorrent no disponible. Los ítems FALTA_CLIENTE se marcarán como "
            "INCIDENCIA hasta que el cliente vuelva a estar accesible.[/yellow]"
        )
    # v1.6.5 + v2.0 + v3.0: Multi-codec scan con máquina de estados
    with LiveReport("local", config) as live:
        hevc_tv, h264_tv, hevc_m, h264_m, dupes, falta_cli, incidencias = forensic_scan(
            config, meta, tracker_index, do_global=False,
            client_paths=_client_paths, client_path_map=_client_map,
            qbit_available=qbit_ok, report=live,
        )
    update_status("CSI", "Local Investigation", "GENERATING_REPORTS")
    generate_reports(
        hevc_tv, h264_tv, hevc_m, h264_m, "local", config,
        dupe_items=dupes,
        falta_cliente_items=falta_cli,
        incidencia_items=incidencias,
    )
    update_status("CSI", "Local Investigation", "FINISHED")

# ─── MAIN MENU ────────────────────────────────────────────────────────────────
def main_menu():
    _ensure_initialized()
    config = load_config()
    toggle_debug(config.get("DEBUG_MODE", False))
    config = check_integrations_config(config)
    config = check_essential_config(config)
    while True:
        welcome()
        config = load_config()
        toggle_debug(config.get("DEBUG_MODE", False))
        _status_panel(config)

        console.print(
            "\n  [bold]1.[/bold] Tracker Investigation"
            "\n  [bold]2.[/bold] Local Investigation"
            "\n  [bold]3.[/bold] Settings"
            "\n  [bold]0.[/bold] Exit\n"
        )
        choice = Prompt.ask("Mode", choices=["1", "2", "3", "0"], default="1")

        if choice == "0":
            sys.exit(0)

        elif choice == "3":
            configure_tracker(config)
            continue

        elif choice == "1":
            if not config["TRACKER_URL"]:
                console.print("[yellow]Tracker not configured. Use Settings (3) first.[/yellow]")
                time.sleep(2)
                continue
            if not config["TRACKER_API_KEY"]:
                console.print("[yellow]API key missing. Use Settings (3) to configure.[/yellow]")
                time.sleep(2)
                continue
            tracker_investigation(config)
            Prompt.ask("\n[dim]Press Enter to return to menu[/dim]", default="")

        elif choice == "2":
            local_investigation(config)
            Prompt.ask("\n[dim]Press Enter to return to menu[/dim]", default="")

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
