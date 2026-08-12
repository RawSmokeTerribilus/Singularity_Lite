"""05_image_regenerator — regenera las capturas desde el medio original.

Diferencia clave con 04_image_resurrector: 04 es un RE-SUBIDOR (lee PNGs que ya
existen en la carpeta local de tmp y muere con "No hay imágenes locales" si la
carpeta está vacía). Este módulo GENERA las capturas de cero a partir del
fichero que sigue sembrándose en el cliente torrent, así que no depende de tmp.

El mapeo id-del-tracker → fichero local sale del propio cliente: UNIT3D escribe
el comentario "…downloaded from X. https://host/torrents/<id>" dentro del
.torrent (TorrentDownloadController), y qBittorrent lo expone junto a
content_path. Eso da id → ruta absoluta, exacto y sin fuzzy matching.

La edición en el tracker es QUIRÚRGICA: sólo se sustituyen las etiquetas de
imagen que apuntan a un host muerto. El resto de la descripción (mediainfo,
sinopsis, tráiler, banner, firma) se reenvía byte a byte tal cual estaba.
"""

import sys, os, re, json, time, glob, shutil, random
import urllib.parse
from datetime import datetime

_AQUI = os.path.dirname(os.path.abspath(__file__))

# La raíz de la suite tiene un paquete "config/" que tapa el config.py de este
# directorio si el script no se lanza desde aquí. Nuestro directorio va primero.
if sys.path[:1] != [_AQUI]:
    sys.path.insert(0, _AQUI)

sys.path.append(os.path.abspath(os.path.join(_AQUI, '../..')))

from core.status_manager import update_status
from config import (BASE_URL, COOKIE_NAME, COOKIE_VALUE, CUSTOM_USER_AGENT,
                    TRACKER_ABBREV, TRACKER_API_KEY, DELAY_MIN, DELAY_MAX,
                    DEAD_HOSTS, REGEN_IMG_HOST, REGEN_SCREENS, REGEN_KEEP_PNG,
                    REGEN_DRY_RUN, REGEN_IMG_SIZE, REGEN_STATE_DIR, REGEN_ALL, REGEN_LIMIT,
                    filter_ids_by_range, ID_INICIO, ID_FIN)

import requests
from bs4 import BeautifulSoup

SUITE_ROOT   = os.path.abspath(os.path.join(_AQUI, "../.."))
RAWLOADRR_DIR = os.path.join(SUITE_ROOT, "RawLoadrr")

# RawLoadrr importa como "from src.x import y" / "from data.config import config",
# así que su raíz tiene que ir delante en el sys.path.
if RAWLOADRR_DIR not in sys.path:
    sys.path.insert(0, RAWLOADRR_DIR)

# El estado va por tracker: los ids son de un tracker concreto, así que un
# completados_regen.txt compartido hacía que al cambiar de tracker se saltaran
# torrents distintos que casualmente tienen el mismo id.
_SUF = (TRACKER_ABBREV or "TRACKER").strip().upper()
MAPA_QBIT   = os.path.join(REGEN_STATE_DIR, f"mapeo_qbit_{_SUF}.json")
MAPA_MAESTRO = os.path.join(REGEN_STATE_DIR, "mapeo_maestro.json")
COMPLETADOS = os.path.join(REGEN_STATE_DIR, f"completados_regen_{_SUF}.txt")

# Ficheros de antes de la separación por tracker.
COMPLETADOS_LEGADO = os.path.join(REGEN_STATE_DIR, "completados_regen.txt")


def _migrar_estado_legado():
    """Adopta el completados_regen.txt sin sufijo, si lo hay.

    Sin esto, la primera tirada tras el cambio de nombre empezaría con el
    marcador vacío y volvería a recorrer todo lo ya hecho.
    """
    if os.path.exists(COMPLETADOS) or not os.path.exists(COMPLETADOS_LEGADO):
        return
    shutil.copy(COMPLETADOS_LEGADO, COMPLETADOS)
    n = sum(1 for l in open(COMPLETADOS, encoding="utf-8") if l.strip())
    print(f"♻️  Adoptado el marcador antiguo como {os.path.basename(COMPLETADOS)} "
          f"({n} ids). Si esos ids NO son de {_SUF}, borra ese fichero y relanza.")

# ─── Modo de ejecución: los flags de la línea de órdenes MANDAN ──────────────
# El modo viajaba sólo en ME_REGEN_DRY_RUN, y un valor viejo en el .env podía
# imponerse sobre la respuesta del usuario (load_dotenv(override=True) pisa el
# entorno del proceso). Resultado real: alguien confirmó "sí, edita el tracker"
# y la tirada entera se ejecutó en simulacro sin tocar nada. Un argumento no lo
# puede pisar ningún fichero de configuración, así que el modo va por argumento
# y el entorno queda sólo como respaldo para uso suelto.
_ARGS = set(sys.argv[1:])


def _resolver_dry_run():
    if _ARGS & {"--real", "--no-dry-run"}:
        return False
    if _ARGS & {"--dry-run", "--seco", "--simulacro"}:
        return True
    return REGEN_DRY_RUN


def _resolver_all():
    if "--all" in _ARGS or "--todos" in _ARGS:
        return True
    if "--rango" in _ARGS or "--range" in _ARGS:
        return False
    return REGEN_ALL


DRY_RUN = _resolver_dry_run()
TODOS   = _resolver_all()

# Fallos seguidos tras los que se aborta una tirada larga.
MAX_FALLOS_SEGUIDOS = int(os.getenv("ME_REGEN_MAX_FALLOS", "15"))

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mpg", ".mpeg", ".wmv", ".mov")

# Una etiqueta de imagen BBCode completa: [url=…][img=…]…[/img][/url]
RX_IMG_TAG = re.compile(
    r"\[url=(?P<web>[^\]]*?)\]\s*\[img(?P<size>[^\]]*)\](?P<raw>[^\[]*?)\[/img\]\s*\[/url\]",
    re.IGNORECASE,
)

# …y el [img] suelto, sin el [url=…] que lo envuelve. Hay descripciones reales
# con BBCode roto (p.ej. NOBS id 7 empieza por "[img=500]…[/img][/url]", con el
# [url=…] de apertura perdido). Si sólo se busca la forma completa, esa imagen
# huérfana sobrevive y el host muerto sigue viéndose en la página.
# Se traga también el [/url] huérfano que suele venir detrás: si la etiqueta
# estuviera bien formada la habría cogido RX_IMG_TAG, así que un [/url] pegado a
# un [img] suelto es un cierre sin apertura. Mientras el host estuvo muerto no se
# notaba (la imagen no cargaba); en cuanto las imágenes vuelven a verse, ese
# [/url] se dibuja como texto literal en medio de la galería.
RX_IMG_SUELTO = re.compile(
    r"\[img(?P<size>[^\]]*)\](?P<raw>[^\[]*?)\[/img\](?P<cierre>\s*\[/url\])?",
    re.IGNORECASE,
)


# ==========================================
# 🧱 ESTADO (mismo patrón que 01_scraper: los ficheros de estado son
#     bind-mounts de fichero único y se rompen con os.replace)
# ==========================================
def _guardar_json(ruta, data):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    shutil.copy(tmp, ruta)
    os.remove(tmp)


def _leer_completados():
    if not os.path.exists(COMPLETADOS):
        return set()
    with open(COMPLETADOS, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _marcar_completado(tid):
    with open(COMPLETADOS, "a", encoding="utf-8") as f:
        f.write(f"{tid}\n")


def _cargar_json(ruta):
    if not os.path.exists(ruta):
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (ValueError, OSError):
        return {}


def _anotar_maestro(tid, carpeta):
    """Mantiene mapeo_maestro.json al día: {id: carpeta_local}.

    Es el índice que 02_indexer construye y del que viven 03 y 04. Como 05
    reconstruye la carpeta de tmp de todas formas, apuntarla aquí devuelve la
    vida al resto de la cadena sin tener que volver a escanear nada.
    """
    maestro = _cargar_json(MAPA_MAESTRO)
    if maestro.get(tid) == carpeta:
        return
    maestro[tid] = carpeta
    _guardar_json(MAPA_MAESTRO, maestro)


# ==========================================
# ⚙️ CONFIG DE RAWLOADRR (sin credenciales nuevas)
# ==========================================
def _cargar_rawloadrr():
    from data.config import config
    return config


def _resolver_site_base(rl_config):
    """Devuelve 'https://host' del SITIO (no del announce).

    Prioridad: ME_TRACKER_URL (ya configurado para 03/04) → upload_url del
    módulo de tracker de RawLoadrr. Ojo: el announce puede vivir en otro host
    (MILNU announcea en tracker.milnueve.cc pero el sitio es milnueve.cc), así
    que nunca se deduce del announce_url.
    """
    if BASE_URL and "tu-tracker-unit3d.com" not in BASE_URL:
        p = urllib.parse.urlparse(BASE_URL)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"

    abbrev = TRACKER_ABBREV or rl_config.get("TRACKERS", {}).get("default_trackers", "")
    try:
        mod = __import__(f"src.trackers.{abbrev}", fromlist=[abbrev])
        tracker = getattr(mod, abbrev)(config=rl_config)
        p = urllib.parse.urlparse(getattr(tracker, "upload_url", ""))
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception as e:
        print(f"⚠️  No se pudo deducir la URL del tracker desde src/trackers/{abbrev}.py: {e}")
    return ""


def _elegir_img_host(rl_config):
    """Primer img_host_N de RawLoadrr que no esté en DEAD_HOSTS.

    ME_REGEN_IMG_HOST manda si está puesto. Las API keys siguen saliendo de
    RawLoadrr/data/config.py: aquí no se introduce ninguna credencial nueva.
    """
    if REGEN_IMG_HOST:
        return REGEN_IMG_HOST

    defaults = rl_config.get("DEFAULT", {})
    for i in range(1, 8):
        host = str(defaults.get(f"img_host_{i}", "") or "").strip().lower()
        if not host:
            continue
        if any(host in dead or dead.startswith(host) for dead in DEAD_HOSTS):
            continue
        return host
    return "imgbb"


def _es_url_muerta(url):
    u = (url or "").lower()
    return any(dead in u for dead in DEAD_HOSTS)


# ==========================================
# 🧭 MAPEO id → fichero, vía cliente torrent
# ==========================================
def construir_mapa(site_base, rl_config):
    import qbittorrentapi

    clientes = rl_config.get("TORRENT_CLIENTS", {})
    nombre   = rl_config.get("DEFAULT", {}).get("default_torrent_client", "qbit")
    cli      = clientes.get(nombre, {})

    host = str(cli.get("qbit_url", "http://localhost")).rstrip("/")
    port = str(cli.get("qbit_port", "8080"))
    if "host.docker.internal" in host:
        host = host.replace("host.docker.internal", "localhost")
    if not re.search(r":\d+$", host):
        host = f"{host}:{port}"

    qbt = qbittorrentapi.Client(
        host=host,
        username=cli.get("qbit_user"),
        password=cli.get("qbit_pass"),
        REQUESTS_ARGS={"timeout": (5, 30), "headers": {"User-Agent": "undici"}},
    )
    qbt.auth_log_in()
    if not qbt.is_logged_in:
        raise RuntimeError(f"Autenticación fallida contra {host}")

    site_host = urllib.parse.urlparse(site_base).netloc

    # Un tracker puede haber cambiado de dominio a lo largo de los años y los
    # comentarios viejos conservan el host antiguo (MILNU: 1818 apuntan a
    # milnueve.cc y 414 a tracker.milnueve.cc). Aceptamos cualquier host bajo el
    # mismo dominio registrable y luego enseñamos el desglose, para que un host
    # inesperado se vea en vez de perderse en silencio.
    partes = site_host.split(".")
    sufijo = ".".join(partes[-2:]) if len(partes) >= 2 else site_host
    rx = re.compile(
        rf"https?://([A-Za-z0-9.-]*{re.escape(sufijo)})/torrents/(\d+)", re.IGNORECASE
    )

    mapa, hosts, sin_comentario, sin_fichero = {}, {}, 0, []
    for t in qbt.torrents_info():
        m = rx.search(str(getattr(t, "comment", "") or ""))
        if not m:
            sin_comentario += 1
            continue
        comment_host, tid = m.group(1).lower(), m.group(2)
        ruta = str(t.content_path)
        if not os.path.exists(ruta):
            sin_fichero.append((tid, ruta))
            continue
        if tid in mapa and mapa[tid] != ruta:
            print(f"   ⚠️  id {tid} apunta a dos ficheros distintos; me quedo con el primero")
            continue
        mapa[tid] = ruta
        hosts[comment_host] = hosts.get(comment_host, 0) + 1

    print(f"🧭 Mapeo desde el cliente (*.{sufijo}): {len(mapa)} torrents")
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        marca = "" if host == site_host else "   [dominio antiguo]"
        print(f"   · {host}: {n}{marca}")
    print(f"   sin comentario de este tracker : {sin_comentario} (otros trackers)")
    if sin_fichero:
        print(f"   ⚠️  con fichero ausente en disco: {len(sin_fichero)}")
        for tid, ruta in sin_fichero[:5]:
            print(f"      • {tid} → {ruta}")
    return mapa


def elegir_fichero(content_path):
    """content_path puede ser un fichero suelto o un pack. Devuelve el vídeo
    más grande, que es de donde prep.py sacaría las capturas."""
    if os.path.isfile(content_path):
        return content_path

    mayor, tam = None, -1
    for root, _dirs, files in os.walk(content_path):
        for f in files:
            if not f.lower().endswith(VIDEO_EXT):
                continue
            ruta = os.path.join(root, f)
            try:
                s = os.path.getsize(ruta)
            except OSError:
                continue
            if s > tam:
                mayor, tam = ruta, s
    return mayor


# ==========================================
# 📸 GENERACIÓN + SUBIDA (reutiliza prep.py tal cual, sin tocarlo)
# ==========================================
def regenerar_imagenes(media_path, uuid, screens, img_host, rl_config, reanudar):
    from src.prep import Prep

    base_dir = RAWLOADRR_DIR
    carpeta  = os.path.join(base_dir, "tmp", uuid)
    os.makedirs(carpeta, exist_ok=True)

    # prep.screenshots() reutiliza los PNG existentes si ya hay suficientes
    # (mensaje "Reusing screenshots"). Eso es lo que queremos al reanudar, pero
    # una carpeta a medias del pasado haría que no se regenerase nada.
    if not reanudar:
        for viejo in glob.glob(os.path.join(carpeta, "*.png")):
            os.remove(viejo)

    # Prefijo de los PNG: prep los nombra "{filename}-{i}.png" y luego los
    # busca con glob, así que no puede llevar metacaracteres.
    stem = os.path.splitext(os.path.basename(media_path))[0]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:60] or "screen"

    prep = Prep(screens=screens, img_host=img_host, config=rl_config)

    meta = {
        "base_dir":   base_dir,
        "uuid":       uuid,
        "path":       media_path,
        "filename":   filename,
        "image_list": [],
        "imghost":    img_host,
        "ffdebug":    False,
        "vapoursynth": False,
    }

    cwd = os.getcwd()
    try:
        # exportInfo escribe MediaInfo.json, que screenshots() necesita sí o sí.
        prep.exportInfo(media_path, os.path.isdir(media_path), uuid, base_dir, export_text=True)

        # Se piden 2 de más: prep descarta capturas negras o diminutas y además
        # borra la más pequeña del lote, así que pedir justas devuelve de menos
        # (visto: 10 pedidas → 9 subidas) y la galería encogía en silencio.
        prep.screenshots(media_path, filename, uuid, base_dir, meta, num_screens=screens + 2)

        disponibles = sorted(glob.glob(os.path.join(carpeta, f"{filename}-*.png")))
        if not disponibles:
            return None, "ffmpeg no generó ninguna captura"

        # Sólo se suben las que se van a usar: subir el sobrante gastaría cuota
        # del host de imágenes para nada.
        elegidas = [os.path.basename(x) for x in disponibles[:screens]]
        image_list, _i = prep.upload_screens(
            meta, len(elegidas), 1, 0, len(elegidas), elegidas, {}
        )
    finally:
        os.chdir(cwd)

    # prep puede devolver alguna de más (reutiliza PNGs previos, descarta la más
    # pequeña, …). Recortamos para que la sustitución siga siendo 1 a 1.
    if image_list and len(image_list) > screens:
        image_list = image_list[:screens]

    if not image_list:
        return None, "el host de imágenes no devolvió ninguna URL"

    # Red de seguridad: aunque la cascada de img_host_N de un usuario todavía
    # liste un host muerto, no dejamos que las URLs nuevas acaben ahí.
    for img in image_list:
        for clave in ("web_url", "raw_url", "img_url"):
            if _es_url_muerta(img.get(clave, "")):
                return None, f"la subida acabó en un host muerto ({img.get(clave)})"

    return image_list, None


# ==========================================
# ✂️ SUSTITUCIÓN QUIRÚRGICA EN LA DESCRIPCIÓN
# ==========================================
def _tag(img, size_attr):
    """Mismo formato que COMMON.py:121, conservando el ancho que ya tenía la
    etiqueta original ('=600', '' , …) para no alterar la maquetación."""
    return f"[url={img['web_url']}][img{size_attr}]{img['raw_url']}[/img][/url]"


def _size_attr(match=None):
    if match is not None:
        return match.group("size") or ""
    return f"={REGEN_IMG_SIZE}" if REGEN_IMG_SIZE else ""


def _texto_sin_imagenes(s):
    """El documento sin ninguna etiqueta de imagen y con los espacios
    colapsados. Es el invariante que una edición quirúrgica debe respetar."""
    return re.sub(r"\s+", " ", RX_IMG_SUELTO.sub("", RX_IMG_TAG.sub("", s))).strip()


def _bbcode(image_list, size_attr):
    """Se unen con UN espacio: los saltos de línea sueltos rompen el render."""
    return " ".join(_tag(img, size_attr) for img in image_list)


def _etiquetas_imagen(desc):
    """Todas las imágenes del documento, en orden: primero las completas
    ([url=…][img]…) y además los [img] sueltos que no caigan dentro de una
    completa (BBCode roto, ver RX_IMG_SUELTO)."""
    completas = list(RX_IMG_TAG.finditer(desc))
    ocupado = [(m.start(), m.end()) for m in completas]
    tags = [("completa", m) for m in completas]

    for m in RX_IMG_SUELTO.finditer(desc):
        if any(ini <= m.start() and m.end() <= fin for ini, fin in ocupado):
            continue
        tags.append(("suelta", m))

    tags.sort(key=lambda par: par[1].start())
    return tags


def contar_muertas(desc):
    """Cuántas imágenes hay que reponer. Se generan EXACTAMENTE esas: si se
    suben más, la sustitución deja de ser 1 a 1 y hay que reemplazar el bloque
    entero, lo que se lleva por delante el texto suelto que hubiera entre medias
    (p.ej. el `[/url]` huérfano del id 7)."""
    return sum(
        1 for _c, m in _etiquetas_imagen(desc)
        if _es_url_muerta(m.groupdict().get("web") or "") or _es_url_muerta(m.group("raw"))
    )


def _reemplazo(clase, match, img):
    """Un [img] suelto se repone como [img] suelto, salvo que arrastre un
    [/url] huérfano: en ese caso se reconstruye la pareja completa, que es lo
    que el BBCode quería decir y evita que el [/url] se vea como texto."""
    if clase == "suelta":
        if match.groupdict().get("cierre"):
            return _tag(img, _size_attr(match))
        return f"[img{_size_attr(match)}]{img['raw_url']}[/img]"
    return _tag(img, _size_attr(match))


def sustituir_imagenes(desc, image_list):
    """Devuelve (nueva_desc, n_sustituidas, motivo_si_falla).

    Sólo toca las etiquetas que apuntan a un host muerto. Todo lo demás
    (tráiler, banner, firma, mediainfo, sinopsis, espaciado) queda intacto.
    """
    tags = _etiquetas_imagen(desc)
    if not tags:
        return None, 0, "la descripción no tiene etiquetas de imagen"

    muertos = [i for i, (_c, m) in enumerate(tags)
               if _es_url_muerta(m.groupdict().get("web") or "") or _es_url_muerta(m.group("raw"))]
    if not muertos:
        return None, 0, "sin imágenes de hosts muertos"

    # Caso normal: tantas capturas nuevas como muertas → sustitución 1 a 1,
    # que es la que menos toca el documento.
    if len(image_list) == len(muertos):
        out, cursor = [], 0
        for idx, img in zip(muertos, image_list):
            clase, m = tags[idx]
            out.append(desc[cursor:m.start()])
            out.append(_reemplazo(clase, m, img))
            cursor = m.end()
        out.append(desc[cursor:])
        return "".join(out), len(muertos), None

    # Si el número no cuadra sólo podemos reemplazar el bloque entero, y eso
    # únicamente es seguro si las muertas van seguidas: si hubiera una imagen
    # viva en medio (un banner, por ejemplo) nos la llevaríamos por delante.
    if muertos != list(range(muertos[0], muertos[-1] + 1)):
        return None, 0, (f"{len(muertos)} imágenes muertas no contiguas y "
                         f"{len(image_list)} nuevas — no se toca")

    ini = tags[muertos[0]][1].start()
    fin = tags[muertos[-1]][1].end()
    bloque = _bbcode(image_list, _size_attr(tags[muertos[0]][1]))
    return desc[:ini] + bloque + desc[fin:], len(muertos), None


# ==========================================
# 🌐 TRACKER
# ==========================================
def _sesion():
    s = requests.Session()
    s.cookies.set(COOKIE_NAME, COOKIE_VALUE)
    return s


def _parece_login(res):
    """UNIT3D devuelve 200 con la página de login cuando la cookie ha caducado
    (y 302 hacia /login si se siguen redirecciones). Un código 2xx NO es prueba
    de que la edición se haya aplicado."""
    if "/login" in (res.url or ""):
        return True
    cuerpo = res.text or ""
    return 'name="password"' in cuerpo or "auth.login" in cuerpo


def comprobar_sesion(session, site_base):
    """Aduana: falla al arrancar en vez de dar 3000 falsos positivos."""
    try:
        r = session.get(f"{site_base}/torrents", headers={
            "User-Agent": CUSTOM_USER_AGENT}, timeout=25, allow_redirects=True)
    except Exception as e:
        return False, f"no se pudo contactar con {site_base}: {e}"

    if r.status_code == 200 and not _parece_login(r):
        return True, None
    return False, (f"la cookie de sesión no vale (HTTP {r.status_code}, "
                   f"url final {r.url}). Copia una nueva del navegador: "
                   f"menú 3 → 1 → 'Cookie de sesión'.")


def leer_descripcion(session, site_base, tid, soup):
    """La descripción NO se puede leer del <textarea>.

    UNIT3D pinta ese campo con el componente Livewire `bbcode-input`
    (resources/views/livewire/bbcode-input.blade.php:219-227): el textarea sale
    VACÍO en el HTML del servidor y lo rellena Livewire en el cliente desde
    `wire:snapshot`. Leerlo con BeautifulSoup devuelve "" y hace creer que el
    torrent ya está limpio.

    Orden: API del tracker (BBCode crudo) → wire:snapshot → textarea (legado).
    """
    if TRACKER_API_KEY:
        try:
            r = session.get(
                f"{site_base}/api/torrents/{tid}",
                params={"api_token": TRACKER_API_KEY},
                headers={"Accept": "application/json", "User-Agent": CUSTOM_USER_AGENT},
                timeout=25,
            )
            if r.status_code == 200:
                data = r.json()
                attrs = data.get("attributes") or data.get("data", {}).get("attributes", {})
                desc = attrs.get("description")
                if desc:
                    return desc, None
        except Exception as e:
            print(f"   ⚠️  la API no devolvió la descripción ({e}); pruebo con el snapshot")

    for tag in soup.find_all(attrs={"wire:snapshot": True}):
        try:
            snap = json.loads(tag["wire:snapshot"])
        except (ValueError, KeyError):
            continue
        data = snap.get("data", {})
        valor = data.get("contentBbcode")
        # Livewire envuelve algunos valores como [valor, {metadatos}]
        if isinstance(valor, list) and valor:
            valor = valor[0]
        if isinstance(valor, str) and valor.strip():
            return valor, None

    textarea = soup.find("textarea", {"name": "description"})
    if textarea is not None and textarea.text.strip():
        return textarea.text, None

    return None, ("no se pudo leer la descripción (API sin ME_TRACKER_API_KEY, "
                  "sin wire:snapshot y textarea vacío por Livewire)")


def leer_formulario(session, site_base, tid):
    edit_url = f"{site_base}/torrents/{tid}/edit"
    res = session.get(edit_url, headers={
        "User-Agent": CUSTOM_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    }, timeout=20)
    if res.status_code != 200:
        return None, None, None, f"HTTP {res.status_code} al abrir el formulario"

    soup = BeautifulSoup(res.text, "html.parser")
    textarea = soup.find("textarea", {"name": "description"})
    if not textarea:
        return None, None, None, "el formulario no tiene textarea 'description'"

    form = textarea.find_parent("form")
    if not form:
        return None, None, None, "textarea sin <form> padre"

    return form, soup, edit_url, None


def construir_payload(form, desc_nueva):
    """Reenvía el formulario tal cual y sólo cambia 'description'.

    A diferencia de 04, aquí NO se filtran los campos de metadatos: no tenemos
    meta.json local con el que reponerlos, así que quitarlos del PATCH los
    borraría en el tracker. Se devuelven con el valor que ya tenían.
    """
    payload = {}
    for tag in form.find_all(["input", "select", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        if tag.get("type") in ("checkbox", "radio"):
            if tag.has_attr("checked"):
                payload[name] = tag.get("value", "1")
        elif tag.name == "select":
            opt = tag.find("option", selected=True)
            payload[name] = opt["value"] if opt else ""
        else:
            payload[name] = tag.get("value", tag.text)

    payload["description"] = desc_nueva
    payload["_method"] = "PATCH"
    _reconciliar_flags_de_metadatos(payload)
    return payload


# Cada casilla "<algo>_exists_on_<proveedor>" va emparejada con el campo del id.
PAREJAS_EXISTS = {
    "title_exists_on_imdb": "imdb",
    "movie_exists_on_tmdb": "tmdb_movie_id",
    "tv_exists_on_tmdb":    "tmdb_tv_id",
    "tv_exists_on_tvdb":    "tvdb",
    "anime_exists_on_mal":  "mal",
    "game_exists_on_igdb":  "igdb",
}


def _reconciliar_flags_de_metadatos(payload):
    """El formulario llega en un estado que el navegador arregla con JS.

    UNIT3D pinta cada id como dos inputs con el mismo name: un `hidden` con "0"
    y el visible. La casilla `<x>_exists_on_<y>` controla si el visible está
    deshabilitado; cuando lo está, sólo viaja el hidden. Al raspar el HTML nos
    llevamos la casilla marcada Y el input visible vacío, y el servidor rechaza
    el PATCH con 422 ("el campo mal es obligatorio cuando anime exists on mal
    está presente"). Si el id viene vacío, la casilla se cae: es el mismo estado
    que ya tiene la base de datos, no inventamos metadatos.
    """
    for casilla, campo_id in PAREJAS_EXISTS.items():
        if casilla not in payload:
            continue
        valor = str(payload.get(campo_id, "")).strip()
        if valor in ("", "0"):
            payload.pop(casilla)


def enviar(session, form, payload, site_base, edit_url):
    target = form.get("action") or edit_url
    if target.startswith("/"):
        target = site_base + target

    headers = {
        "User-Agent": CUSTOM_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": edit_url,
        "Accept": "application/json",
    }
    xsrf = session.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)

    res = session.post(target, data=payload, headers=headers, timeout=30)

    # UNIT3D contesta 200/302 aunque la sesión haya caducado: lo que devuelve es
    # la página de login. Dar eso por bueno haría que 3000 torrents se marcaran
    # como hechos sin haber tocado nada.
    if _parece_login(res):
        return False, "sesión caducada: la respuesta es la página de login"

    if res.status_code in (200, 302):
        return True, "OK"
    if res.status_code == 422:
        try:
            return False, f"Error 422: {res.json().get('errors', '???')}"
        except Exception:
            pass
    return False, f"HTTP {res.status_code}"


def verificar(session, site_base, tid, image_list):
    """Relee la descripción publicada y comprueba que el cambio existe.

    Es la única confirmación de verdad: el POST puede devolver 200 y no haber
    cambiado nada. Sólo tras esto se marca el torrent como completado.
    """
    desc, err = leer_descripcion(session, site_base, tid, BeautifulSoup("", "html.parser"))
    if err:
        return False, f"no se pudo releer la descripción para verificar: {err}"

    restos = [d for d in DEAD_HOSTS if d in desc.lower()]
    if restos:
        return False, f"tras publicar siguen quedando enlaces a {', '.join(restos)}"

    faltan = [img["raw_url"] for img in image_list if img["raw_url"] not in desc]
    if faltan:
        return False, f"las URLs nuevas no aparecen en la descripción publicada ({len(faltan)} de {len(image_list)})"

    return True, None


# ==========================================
# 🚀 PROCESO POR TORRENT
# ==========================================
def procesar(tid, media_root, session, site_base, rl_config, screens, img_host):
    # Primero lo barato: una llamada a la API dice si hay algo que arreglar.
    # Antes se pedía el formulario de edición (HTML pesado) y se recorría el
    # disco buscando el vídeo ANTES de saberlo, así que cada re-pasada sobre
    # torrents ya sanos costaba dos peticiones y un walk del sistema de ficheros
    # para nada. En una tirada reanudada eso es casi todo el trabajo.
    desc_actual, err = leer_descripcion(session, site_base, tid, BeautifulSoup("", "html.parser"))
    if err:
        return False, err

    if not any(dead in desc_actual.lower() for dead in DEAD_HOSTS):
        return True, "ya está limpio"

    media_path = elegir_fichero(media_root)
    if not media_path:
        return False, "no hay ningún fichero de vídeo en la ruta del torrent"

    form, soup, edit_url, err = leer_formulario(session, site_base, tid)
    if err:
        return False, err

    uuid = os.path.basename(os.path.normpath(media_root))
    carpeta = os.path.join(RAWLOADRR_DIR, "tmp", uuid)
    reanudar = os.path.isdir(carpeta) and bool(glob.glob(os.path.join(carpeta, "*.png")))

    # Tantas capturas como imágenes muertas haya: así la sustitución es 1 a 1 y
    # el documento no cambia en nada más. Aquí manda la descripción, no el
    # `screens` de RawLoadrr (que es el valor por defecto para subidas nuevas).
    objetivo = contar_muertas(desc_actual) or 1

    image_list, err = regenerar_imagenes(
        media_path, uuid, objetivo, img_host, rl_config, reanudar
    )
    if err:
        return False, err

    desc_nueva, n, err = sustituir_imagenes(desc_actual, image_list)
    if err:
        return False, err

    # Cinturón de seguridad antes de tocar una página en producción: lo único
    # que puede haber cambiado son las etiquetas de imagen. Si el texto que
    # queda al quitarlas no es idéntico, algo se ha comido contenido y no se
    # envía nada.
    if not desc_nueva.strip():
        return False, "la descripción resultante está vacía — abortado"
    if _texto_sin_imagenes(desc_nueva) != _texto_sin_imagenes(desc_actual):
        return False, "el texto de fuera de las imágenes cambió — abortado"

    aviso = "" if len(image_list) == n else f"  ⚠️  {n} muertas pero sólo {len(image_list)} nuevas"

    if DRY_RUN:
        print(f"\n───── SIMULACRO id {tid} — {n} etiqueta(s) → {len(image_list)} imagen(es){aviso} ─────")
        print(f"ANTES : {desc_actual[:400]}")
        print(f"AHORA : {desc_nueva[:400]}")
        print("─" * 60)
        return True, f"SIMULACRO: {n} etiqueta(s) → {len(image_list)} imagen(es), nada escrito{aviso}"

    ok, msg = enviar(session, form, construir_payload(form, desc_nueva), site_base, edit_url)
    if not ok:
        return False, msg

    ok, msg = verificar(session, site_base, tid, image_list)
    if not ok:
        return False, msg

    # Repoblar tmp: deja la carpeta con la misma pinta que la dejaría RawLoadrr,
    # para que 02_indexer vuelva a mapearla y 03/04 tengan de dónde tirar.
    try:
        with open(os.path.join(carpeta, f"[{TRACKER_ABBREV}]DESCRIPTION.txt"), "w", encoding="utf-8") as f:
            f.write(desc_nueva)
        _guardar_json(os.path.join(carpeta, "meta.json"), {
            "name": uuid,
            "uuid": uuid,
            "path": media_root,
            "filelist": [media_path],
            "image_list": image_list,
            "imghost": img_host,
            "tracker_id": tid,
            "regenerated_at": datetime.now().isoformat(timespec="seconds"),
        })
        _anotar_maestro(tid, carpeta)
    except Exception as e:
        print(f"   ⚠️  subido y editado, pero no se pudo escribir el estado local: {e}")

    if not REGEN_KEEP_PNG:
        for png in glob.glob(os.path.join(carpeta, "*.png")):
            try:
                os.remove(png)
            except OSError:
                pass

    return True, f"{n} etiqueta(s) → {len(image_list)} imagen(es) publicadas{aviso}"


# ==========================================
# 🎬 MAIN
# ==========================================
def main():
    rl_config = _cargar_rawloadrr()

    site_base = _resolver_site_base(rl_config)
    if not site_base:
        print("❌ No hay URL de tracker configurada (ME_TRACKER_URL ni src/trackers/*.py).")
        return 1
    if not COOKIE_VALUE:
        print("❌ Falta ME_TRACKER_COOKIE: sin sesión no se puede editar el tracker.")
        return 1

    screens  = int(REGEN_SCREENS or rl_config.get("DEFAULT", {}).get("screens", 8))
    img_host = _elegir_img_host(rl_config)

    print(f"🌐 Tracker : {site_base}  [{TRACKER_ABBREV}]")
    print(f"🖼️  Host    : {img_host}   (capturas: las que haga falta reponer en cada torrent)")
    print(f"⚙️  Modo    : {'SIMULACRO (no se escribe nada)' if DRY_RUN else 'REAL — se editará el tracker'}")
    print(f"☠️  Muertos : {', '.join(DEAD_HOSTS)}")

    update_status("UNIT3D", "Regeneración de Imágenes", "PROCESSING",
                  details="Mapeando torrents desde el cliente")
    try:
        mapa = construir_mapa(site_base, rl_config)
    except Exception as e:
        print(f"❌ No se pudo hablar con el cliente torrent: {e}")
        update_status("UNIT3D", "Regeneración de Imágenes", "ERROR", details=str(e))
        return 1

    if not mapa:
        print("❌ El cliente no tiene ningún torrent de este tracker.")
        return 1
    _guardar_json(MAPA_QBIT, mapa)

    _migrar_estado_legado()
    completados = _leer_completados()
    if TODOS:
        candidatos = sorted(mapa.keys(), key=int)
        ambito = "todo el cliente"
    else:
        candidatos = filter_ids_by_range(mapa.keys())
        ambito = f"IDs {ID_INICIO}-{ID_FIN}"
    pendientes = [tid for tid in candidatos if tid not in completados]

    total_pendientes = len(pendientes)
    if REGEN_LIMIT and len(pendientes) > REGEN_LIMIT:
        pendientes = pendientes[:REGEN_LIMIT]
        print(f"🚀 Pendientes ({ambito}): {total_pendientes} — esta tirada hace {len(pendientes)} "
              f"(ME_REGEN_LIMIT), ya hechos: {len(completados)}")
    else:
        print(f"🚀 Pendientes ({ambito}): {len(pendientes)} (ya hechos: {len(completados)})")
    if not pendientes:
        update_status("UNIT3D", "Regeneración de Imágenes", "COMPLETED", progress=100)
        return 0

    session = _sesion()

    ok, err = comprobar_sesion(session, site_base)
    if not ok:
        print(f"❌ {err}")
        update_status("UNIT3D", "Regeneración de Imágenes", "ERROR", details="cookie caducada")
        return 1
    print("🔓 Sesión válida.")

    ok_n = fail_n = seguidos = 0

    for i, tid in enumerate(pendientes, 1):
        prog = int((i / len(pendientes)) * 100)
        update_status("UNIT3D", "Regeneración de Imágenes", "PROCESSING", progress=prog,
                      details=f"ID {tid} ({i}/{len(pendientes)})")
        print(f"[{i}/{len(pendientes)}] ID {tid} … ", end="", flush=True)

        try:
            exito, mensaje = procesar(tid, mapa[tid], session, site_base,
                                      rl_config, screens, img_host)
        except Exception as e:
            exito, mensaje = False, f"excepción: {e}"

        if exito:
            ok_n += 1
            seguidos = 0
            print(f"✨ {mensaje}")
            if not DRY_RUN:
                _marcar_completado(tid)
        else:
            fail_n += 1
            seguidos += 1
            print(f"❌ {mensaje}")

            # Cortafuegos para tiradas largas sin vigilancia. Lo que más duele
            # es que la cookie caduque a mitad: sin esto seguiría 3000 torrents
            # fallando uno a uno y dejando sus PNG en tmp.
            if "sesión caducada" in mensaje:
                print("\n🛑 La sesión ha caducado a mitad de la tirada. Paro aquí.")
                print("   Copia una cookie nueva (menú 3 → 1) y vuelve a lanzar: "
                      "continúa por donde iba.")
                break
            if seguidos >= MAX_FALLOS_SEGUIDOS:
                print(f"\n🛑 {seguidos} fallos seguidos. Paro para que le eches un ojo "
                      f"en vez de seguir a ciegas.")
                break

        if i < len(pendientes):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    quedan = len(pendientes) - (ok_n + fail_n)
    print(f"\n✅ Hechos: {ok_n}   ❌ Fallos: {fail_n}" + (f"   ⏸️  Sin tocar: {quedan}" if quedan else ""))
    if not DRY_RUN:
        print(f"   Reanudable: {COMPLETADOS}")
    update_status("UNIT3D", "Regeneración de Imágenes", "COMPLETED", progress=100,
                  details=f"{ok_n} ok / {fail_n} fallos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
