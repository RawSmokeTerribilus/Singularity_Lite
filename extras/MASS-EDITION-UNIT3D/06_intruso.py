#!/usr/bin/env python3
"""06_intruso — recupera galerías en torrents de OTROS usuarios.

`05_image_regenerator` sólo alcanza lo que tú siembras: mapea el id del tracker
por el `comment` del .torrent que hay en tu cliente. El barrido de cierre de NOBS
dejó ~1200 páginas rotas de otros usuarios fuera de su alcance.

Intruso trabaja en dos pasadas, y son deliberadamente independientes:

  FASE 1 · limpiar   (minutos, sin descargas)
      Barre el tracker entero, guarda la cola CON LA DESCRIPCIÓN ORIGINAL y
      quita los enlaces muertos de todas las páginas. El spam desaparece hoy.

  FASE 2 · reponer   (horas, reanudable)   --reponer
      Trabaja la cola guardada: baja unas ventanas de piezas por libtorrent,
      saca capturas y las devuelve a su sitio.

Por qué la cola se guarda ANTES de limpiar:
  · limpiar destruye el criterio de selección ("tiene un host muerto"),
  · y destruye el punto de inserción donde iba la galería,
  · y una copia de la descripción original es la única vuelta atrás cuando
    estás editando páginas que no son tuyas.

Uso:
    python3 06_intruso.py --limpiar [--real|--dry-run] [--tracker NOBS]
    python3 06_intruso.py --barrer                 # sólo construir la cola
    python3 06_intruso.py --reponer [--real] [--limite N]
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import importlib.util
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)


# ==========================================
# ♻️  REUTILIZACIÓN DE 05
# ==========================================
# 05 es un script, no un paquete, así que se carga por ruta. Al importarlo se
# resuelve la identidad del tracker (--tracker, TRACKER_<ABBREV>_*, el doctor
# del .env…) exactamente igual que en una tirada de 05: una sola forma de
# resolver credenciales para las dos herramientas.
def _cargar_05():
    ruta = os.path.join(DIR, "05_image_regenerator.py")
    if not os.path.exists(ruta):
        print("❌ Falta 05_image_regenerator.py: Intruso reutiliza sus helpers.")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("regen05", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _cargar_05()

DEAD_HOSTS   = R.DEAD_HOSTS
SITE_BASE    = None                       # se resuelve en main()
_SUF         = R._SUF
ESTADO       = R.REGEN_STATE_DIR
DELAY_MIN    = R.DELAY_MIN
DELAY_MAX    = R.DELAY_MAX

# Las capturas se escriben ya en JPG en vez de PNG: aquí las genera ffmpeg
# directamente, así que no hace falta el paso de recodificado de 05.a_jpg().
# El motivo es el mismo (ver a_jpg): un PNG de 1-12 MiB revienta a los cuatro
# hosts de la cascada a la vez. El `-q:v` que ya se pasaba era papel mojado —
# el codificador PNG lo ignora.
_EXT_CAP = "jpg" if R.REGEN_JPG else "png"
_Q_CAP   = str(R.REGEN_JPG_Q) if R.REGEN_JPG else "2"
# Umbral de "captura aprovechable" (descarta fotogramas negros o rotos). Un JPG
# pesa ~7% de lo que pesaba el PNG, así que el listón de 20 KB dejaría fuera
# capturas buenas de material SD.
_MIN_CAP = 6000 if R.REGEN_JPG else 20000

COLA      = os.path.join(ESTADO, f"intruso_cola_{_SUF}.json")
LIMPIADOS = os.path.join(ESTADO, f"intruso_limpiados_{_SUF}.txt")
MANUAL    = os.path.join(ESTADO, f"intruso_manual_{_SUF}.txt")

_ARGS = set(sys.argv[1:])


def _dry_run():
    if _ARGS & {"--real", "--no-dry-run"}:
        return False
    if _ARGS & {"--dry-run", "--seco", "--simulacro"}:
        return True
    return True                            # por defecto NO se escribe nada


DRY_RUN = _dry_run()


def _limite():
    """--limite N: parar tras N torrents. Para probar en vivo sin comprometerse."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--limite", "--limit") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                pass
        if a.startswith("--limite="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                pass
    return 0


LIMITE = _limite()


# ==========================================
# 🧹 LIMPIEZA QUIRÚRGICA DE ENLACES MUERTOS
# ==========================================
# Un [url=…] cuyo destino es un host muerto, envuelva o no una imagen. Clicarlo
# lleva igual a la web de cámaras, así que el enlace se cae aunque el texto de
# dentro se quede.
RX_URL_MUERTA = re.compile(r"\[url=(?P<destino>[^\]]*?)\](?P<dentro>.*?)\[/url\]",
                           re.IGNORECASE | re.DOTALL)


def limpiar_descripcion(texto):
    """Quita SÓLO lo que apunta a un host muerto.

    Trailer, firma, banner, mediainfo, sinopsis y envoltorios se quedan byte a
    byte. Devuelve (texto_limpio, cuántas cosas se han quitado).
    """
    quitados = 0

    def _tag_completo(m):
        nonlocal quitados
        if R._es_url_muerta(m.group("web")) or R._es_url_muerta(m.group("raw")):
            quitados += 1
            return ""
        return m.group(0)

    def _img_suelta(m):
        nonlocal quitados
        if R._es_url_muerta(m.group("raw")):
            quitados += 1
            # `cierre` es un [/url] huérfano que colgaba de la etiqueta anterior
            return ""
        return m.group(0)

    def _url_suelta(m):
        nonlocal quitados
        if R._es_url_muerta(m.group("destino")):
            quitados += 1
            dentro = m.group("dentro")
            # Si sólo envolvía imágenes ya borradas no queda nada que salvar;
            # si llevaba texto, el texto se conserva y se va el enlace.
            return dentro if dentro.strip() else ""
        return m.group(0)

    t = R.RX_IMG_TAG.sub(_tag_completo, texto)
    t = R.RX_IMG_SUELTO.sub(_img_suelta, t)
    t = RX_URL_MUERTA.sub(_url_suelta, t)

    # Etiquetas [url=…] ABIERTAS y sin su [/url]. Existen de verdad: el id 5082
    # de NOBS trae '[url=https://imgbox.com/EZKXIs05] \r' sin cerrar, así que la
    # regex de pareja no casa y el enlace sobrevivía a todo lo anterior. Quitar
    # una etiqueta huérfana no puede perder texto visible: es sólo marcado.
    def _url_abierta_huerfana(m):
        nonlocal quitados
        if R._es_url_muerta(m.group(1)):
            quitados += 1
            return ""
        return m.group(0)

    t = re.sub(r"\[url=([^\]]*?)\]", _url_abierta_huerfana, t, flags=re.IGNORECASE)

    # Envoltorios que se han quedado vacíos por el borrado. No se toca ningún
    # otro espacio: sólo se colapsa lo que el propio borrado ha dejado suelto.
    t = re.sub(r"\[center\]\s*\[/center\]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    return t.strip(), quitados


# UNIT3D rechaza una descripción vacía ("El campo descripción es obligatorio"),
# y 23 de las 1434 se quedaban en nada al quitar los enlaces: sólo tenían la
# galería muerta. Sin esto, esas páginas conservan el spam — justo lo que no
# puede pasar. Se pone un texto mínimo para que el PATCH entre.
PLACEHOLDER = os.getenv(
    "ME_INTRUSO_PLACEHOLDER",
    "[center][i]Galería pendiente de reconstrucción.[/i][/center]",
).strip()


def quedan_muertos(texto):
    bajo = (texto or "").lower()
    return [d for d in DEAD_HOSTS if d in bajo]


RX_BBCODE = re.compile(r"\[/?[^\]]{0,80}\]")


RX_IMG_ENTERA = re.compile(r"\[img[^\]]*\][^\[]*\[/img\]", re.IGNORECASE)


def _texto_visible(s):
    """Las PALABRAS que ve el usuario, sin bbcode ni espacios de más.

    Las etiquetas de imagen se quitan ENTERAS (con su URL dentro) antes de nada:
    la URL vive ENTRE `[img]` y `[/img]`, así que quitando sólo las etiquetas
    quedaría suelta como si fuera texto, y borrar la imagen parecería una
    pérdida de contenido. Que la imagen viva no se toque lo comprueba
    `_imagenes_vivas`, que para eso está aparte.
    """
    t = RX_IMG_ENTERA.sub(" ", s or "")
    return re.sub(r"\s+", " ", RX_BBCODE.sub(" ", t)).strip()


def _imagenes_vivas(s):
    """URLs de imagen que NO son de un host muerto, en orden."""
    vivas = []
    for m in R.RX_IMG_TAG.finditer(s or ""):
        if not (R._es_url_muerta(m.group("web")) or R._es_url_muerta(m.group("raw"))):
            vivas.append(m.group("raw").strip())
    for m in R.RX_IMG_SUELTO.finditer(s or ""):
        if not R._es_url_muerta(m.group("raw")):
            vivas.append(m.group("raw").strip())
    return vivas


def comprobar_limpieza(antes, despues):
    """El invariante de ESTA fase. No vale el de 05.

    `_texto_sin_imagenes` (05) trata `[center]` y `[url]` como intocables porque
    allí la sustitución es 1 a 1. Aquí sí desaparecen a propósito: el envoltorio
    que se queda vacío, y el `[url=…]` de un enlace de texto a un host muerto
    (del que se conserva el texto). Con aquel invariante se rechazarían justo las
    limpiezas que se quieren hacer.

    Lo que de verdad no puede cambiar:
      1. ninguna palabra visible se pierde ni aparece,
      2. ninguna imagen VIVA se toca.
    """
    if _texto_visible(antes) != _texto_visible(despues):
        return False, "se perdería o alteraría texto visible"
    if _imagenes_vivas(antes) != _imagenes_vivas(despues):
        return False, "se tocaría alguna imagen que sigue viva"
    return True, ""


# ==========================================
# 🗂️ ESTADO
# ==========================================
def _leer_marcador(ruta):
    if not os.path.exists(ruta):
        return set()
    with open(ruta, "r", encoding="utf-8") as f:
        return {l.split("\t")[0].strip() for l in f if l.strip()}


def _marcar(ruta, tid, extra=""):
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(f"{tid}\t{extra}\n" if extra else f"{tid}\n")


def _anotar_manual(entrada, motivo):
    """La lista que de verdad importa: lo que hay que mirar a mano.

    Dos escenarios previstos, los dos acaban en borrar el torrent tras revisar:
    sin seeds, o el fichero no da capturas aprovechables.
    """
    try:
        nuevo = not os.path.exists(MANUAL)
        with open(MANUAL, "a", encoding="utf-8") as f:
            if nuevo:
                f.write("# id\tseeders\tuploader\tmotivo\tnombre\turl\n")
            f.write("\t".join([
                str(entrada.get("id", "?")),
                str(entrada.get("seeders", "?")),
                str(entrada.get("uploader", "?")),
                motivo,
                str(entrada.get("nombre", ""))[:80],
                f"{SITE_BASE}/torrents/{entrada.get('id')}",
            ]) + "\n")
    except OSError:
        pass


# ==========================================
# 🔭 BARRIDO DEL TRACKER
# ==========================================
def barrer(session):
    """Todas las páginas con hosts muertos, con su descripción original.

    `/api/torrents` pagina por CURSOR y no avanza (repite la misma página en
    bucle): hay que usar `/api/torrents/filter`, que va por `page`. Contesta 429
    cada ~30 páginas y pide hasta ~43 s de espera.
    """
    cola, vistos, pagina = [], set(), 1
    print("🔭 Barriendo el tracker (esto tarda unos minutos)…", flush=True)

    while pagina <= 500:
        r = session.get(
            f"{SITE_BASE}/api/torrents/filter",
            params={"perPage": 100, "page": pagina, "api_token": R.TRACKER_API_KEY},
            headers={"Accept": "application/json", "User-Agent": R.CUSTOM_USER_AGENT},
            timeout=60,
        )
        if r.status_code == 429:
            espera = int(r.headers.get("Retry-After", 30) or 30) + 2
            print(f"   ⏳ 429 en la página {pagina}: espero {espera}s", flush=True)
            time.sleep(espera)
            continue
        if r.status_code != 200:
            print(f"   ⚠️  corte en HTTP {r.status_code} (página {pagina})")
            break

        datos = r.json().get("data", [])
        if not datos:
            break

        for t in datos:
            a = t.get("attributes", {}) or {}
            tid = str(t.get("id") or a.get("id") or "")
            if not tid or tid in vistos:
                continue
            vistos.add(tid)
            desc = a.get("description") or ""
            muertos = quedan_muertos(desc)
            if not muertos:
                continue
            cola.append({
                "id": tid,
                "nombre": a.get("name", ""),
                "uploader": a.get("uploader", "(anónimo)"),
                "seeders": a.get("seeders") or 0,
                "hosts_muertos": muertos,
                # La copia de seguridad: sin esto no hay marcha atrás, ni se
                # sabe dónde iba la galería cuando toque reponerla.
                "descripcion_original": desc,
            })

        if pagina % 10 == 0:
            print(f"   página {pagina}: {len(vistos)} vistos, {len(cola)} rotos", flush=True)
        pagina += 1
        time.sleep(0.25)

    print(f"✅ Barrido: {len(vistos)} torrents, {len(cola)} con enlaces muertos")
    return cola, len(vistos)


def guardar_cola(cola, total_vistos):
    # Primero lo fácil: si se para a media tirada, queda arreglado lo más
    # rentable. `seeders` viene del barrido, así que el triaje sale gratis.
    cola.sort(key=lambda e: (-int(e.get("seeders") or 0), int(e["id"])))
    R._guardar_json(COLA, {
        "tracker": _SUF,
        "creada": datetime.now().isoformat(timespec="seconds"),
        "total_en_tracker": total_vistos,
        "entradas": cola,
    })
    print(f"💾 Cola guardada: {COLA}")


# ==========================================
# 🧼 FASE 1 — LIMPIAR
# ==========================================
def limpiar_uno(session, entrada):
    tid = entrada["id"]

    form, soup, edit_url, err = R.leer_formulario(session, SITE_BASE, tid)
    if err:
        return False, err

    desc_actual, err = R.leer_descripcion(session, SITE_BASE, tid, soup)
    if err:
        return False, err

    if not quedan_muertos(desc_actual):
        return True, "ya estaba limpia"

    nueva, quitados = limpiar_descripcion(desc_actual)
    if not quitados:
        return False, "hay hosts muertos pero ningún patrón conocido casó"

    # La descripción era SÓLO la galería muerta. Vacía no se puede publicar.
    vaciada = not nueva.strip()
    if vaciada:
        nueva = PLACEHOLDER

    restos = quedan_muertos(nueva)
    if restos:
        return False, f"tras limpiar seguirían enlaces a {', '.join(restos)}"

    # Con placeholder el texto visible cambia a propósito (antes no había nada
    # que no fuera la galería), así que ese invariante no aplica; lo que sí se
    # exige igual es que no quede ningún enlace muerto.
    if not vaciada:
        seguro, motivo = comprobar_limpieza(desc_actual, nueva)
        if not seguro:
            return False, f"abortado: {motivo}"

    if DRY_RUN:
        return True, f"SIMULACRO: se quitarían {quitados} enlace(s) muerto(s)"

    payload = R.construir_payload(form, nueva)
    ok, msg = R.enviar(session, form, payload, SITE_BASE, edit_url)
    if not ok:
        return False, msg

    publicada, err = R.leer_descripcion(session, SITE_BASE, tid,
                                        __import__("bs4").BeautifulSoup("", "html.parser"))
    if err:
        return False, f"no se pudo releer para verificar: {err}"
    restos = quedan_muertos(publicada)
    if restos:
        return False, f"tras publicar siguen enlaces a {', '.join(restos)}"

    if vaciada:
        return True, f"{quitados} enlace(s) fuera — sólo había galería, queda el aviso"
    return True, f"{quitados} enlace(s) muerto(s) fuera"


def fase_limpiar(session, cola):
    hechos = _leer_marcador(LIMPIADOS)
    pend = [e for e in cola if e["id"] not in hechos]
    if LIMITE:
        pend = pend[:LIMITE]
        print(f"   (--limite {LIMITE}: sólo los {len(pend)} primeros)")
    print(f"\n🧼 Fase 1 · limpiar — {len(pend)} pendientes de {len(cola)}"
          f"   (ya hechos: {len(hechos)})")
    print(f"   Modo: {'SIMULACRO (no se escribe nada)' if DRY_RUN else 'REAL'}\n")

    ok_n = fail_n = 0
    for i, e in enumerate(pend, 1):
        print(f"[{i}/{len(pend)}] ID {e['id']}  ({e['uploader']}, seeds={e['seeders']})",
              flush=True)
        try:
            ok, msg = limpiar_uno(session, e)
        except Exception as ex:
            ok, msg = False, f"excepción: {ex}"

        if ok:
            ok_n += 1
            print(f"    ✨ {msg}", flush=True)
            if not DRY_RUN:
                _marcar(LIMPIADOS, e["id"])
        else:
            fail_n += 1
            print(f"    ❌ {msg}", flush=True)
            _anotar_manual(e, f"limpieza: {msg}"[:120])
            if "sesión caducada" in msg:
                print("\n🛑 Sesión caducada. Paro; al relanzar continúa por donde iba.")
                break

        if i < len(pend):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    print(f"\n✅ Limpiados: {ok_n}   ❌ Fallos: {fail_n}")
    if not DRY_RUN:
        print(f"   Reanudable : {LIMPIADOS}")
    if fail_n:
        print(f"   Para revisar a mano: {MANUAL}")
    return ok_n, fail_n


# ==========================================
# 🎬 MAIN
# ==========================================
def main():
    global SITE_BASE

    rl = R._cargar_rawloadrr()
    SITE_BASE = R._resolver_site_base(rl)
    if not SITE_BASE:
        print("❌ No hay URL de tracker configurada.")
        return 1
    if not R.TRACKER_API_KEY:
        print("❌ Falta la API key del tracker: el barrido la necesita.")
        return 1

    print(f"🌐 Tracker : {SITE_BASE}  [{_SUF}]")
    print(f"☠️  Muertos : {', '.join(DEAD_HOSTS)}")
    print(f"⚙️  Modo    : {'SIMULACRO' if DRY_RUN else 'REAL — se editará el tracker'}")

    session = R._sesion()
    ok, err = R.comprobar_sesion(session, SITE_BASE)
    if not ok:
        print(f"❌ {err}")
        return 1
    print("🔓 Sesión válida.")

    # La cola se reutiliza si ya existe: rebarrer después de limpiar daría cero,
    # porque limpiar destruye justo el criterio de búsqueda.
    guardada = R._cargar_json(COLA)
    if guardada.get("entradas"):
        cola = guardada["entradas"]
        print(f"\n📋 Cola existente de {guardada.get('creada', '?')}: "
              f"{len(cola)} entradas (no se rebarre).")
    else:
        cola, total = barrer(session)
        if not cola:
            print("✨ Nada con enlaces muertos. No hay trabajo.")
            return 0
        guardar_cola(cola, total)

    sin_seeds = [e for e in cola if not int(e.get("seeders") or 0)]
    if sin_seeds:
        print(f"\n⚠️  {len(sin_seeds)} sin seeds: se limpian igual, pero no se podrán "
              f"reponer capturas. Van a {os.path.basename(MANUAL)}.")
        for e in sin_seeds:
            _anotar_manual(e, "sin seeds: no se podrán regenerar capturas")

    if "--barrer" in _ARGS:
        print("\n(--barrer: sólo se ha construido la cola, no se ha tocado nada)")
        return 0

    if _ARGS & {"--reponer", "--reparar"}:
        fase_reponer(session, cola, rl)
        return 0

    fase_limpiar(session, cola)
    return 0



# ==========================================
# 🔧 FASE 2 — REPONER GALERÍAS (libtorrent)
# ==========================================
# El selector NO son los enlaces muertos: ya no existen, los quitó la fase 1.
# Es la cola guardada, que conserva la descripción original de cada página y por
# tanto el sitio exacto donde iba la galería.
REPUESTOS = os.path.join(ESTADO, f"intruso_repuestos_{_SUF}.txt")
# Aplazados por CUOTA, no por avería. Se separan de la lista de revisión
# humana porque no hay nada que revisar: cuando los hosts respiren, se
# relanza y estos vuelven a intentarse tal cual.
APLAZADOS = os.path.join(ESTADO, f"intruso_aplazados_{_SUF}.txt")
DATOS_TMP = os.path.join(ESTADO, "intruso_torrents")

VENTANAS   = [float(x) for x in
              (os.getenv("ME_INTRUSO_VENTANAS", "30,50,70").split(","))]
POR_VENTANA = int(os.getenv("ME_INTRUSO_CAPS_POR_VENTANA", "2"))
# Tope DURO de capturas por torrent. Con ~1400 torrents, cada captura de más
# son ~1400 subidas más contra unos hosts que van justos de cuota. 4 llegan
# para una galería decente y hacen la campaña terminable.
MAX_CAPS    = int(os.getenv("ME_INTRUSO_MAX_CAPS", "4"))
# Cortafuegos por torrent, en GB. Generoso a propósito: un BDREMUX de
# temporada tiene piezas enormes y tres ventanas se comieron 384 MiB, con lo
# que un tope de 400 MiB cortaba justo lo que hacía falta. Esto no es una
# cuota de ahorro, es una red por si algo se desboca; los datos se borran al
# terminar cada torrent, así que el disco no acumula.
TOPE_GB     = float(os.getenv("ME_INTRUSO_TOPE_GB", "3"))
TOPE_MIB    = int(os.getenv("ME_INTRUSO_TOPE_MIB", str(int(TOPE_GB * 1024))))
ESPERA_MAX  = int(os.getenv("ME_INTRUSO_ESPERA_MAX", "180"))
PUERTO      = int(os.getenv("ME_INTRUSO_PUERTO", "0"))  # 0 = puerto libre
# Puerto de escucha efímero (:0). Con uno fijo, dos torrentes seguidos pueden
# solaparse mientras el anterior suelta el socket, y la sesión nueva se queda
# sin escuchar — menos peers y ventanas que no llegan.
IFACE       = os.getenv("ME_INTRUSO_IFACE", "0.0.0.0:0")


def _descargar_torrent(session, tid):
    """El .torrent desde la API. UNIT3D le inyecta TU passkey en el announce."""
    r = session.get(f"{SITE_BASE}/api/torrents/{tid}",
                    params={"api_token": R.TRACKER_API_KEY},
                    headers={"Accept": "application/json",
                             "User-Agent": R.CUSTOM_USER_AGENT}, timeout=30)
    if r.status_code != 200:
        return None, f"la API no devolvió el torrent (HTTP {r.status_code})"
    datos = r.json()
    attrs = datos.get("attributes") or datos.get("data", {}).get("attributes", {})
    enlace = attrs.get("download_link")
    if not enlace:
        return None, "la API no trae download_link"
    t = session.get(enlace, headers={"User-Agent": R.CUSTOM_USER_AGENT}, timeout=60)
    if t.status_code != 200 or not t.content.startswith(b"d"):
        return None, f"la descarga del .torrent falló (HTTP {t.status_code})"
    os.makedirs(DATOS_TMP, exist_ok=True)
    ruta = os.path.join(DATOS_TMP, f"{tid}.torrent")
    with open(ruta, "wb") as f:
        f.write(t.content)
    return ruta, None


class _ServidorPiezas:
    """libtorrent + servidor HTTP que BLOQUEA hasta que llega la pieza.

    Es como lo hacen Kodi/elementum: ffmpeg lee por HTTP con Range y nunca ve un
    agujero, porque la lectura ESPERA en vez de devolver ceros. Así no hay que
    llevar registro de qué rangos han llegado.
    """

    def __init__(self, ruta_torrent, destino):
        import libtorrent as lt
        self.lt = lt
        self.ses = lt.session({
            "listen_interfaces": IFACE,
            "enable_dht": False, "enable_lsd": False,
            "enable_upnp": False, "enable_natpmp": False,
            "alert_mask": lt.alert.category_t.error_notification,
        })
        self.ti = lt.torrent_info(ruta_torrent)
        par = lt.add_torrent_params()
        par.ti = self.ti
        par.save_path = destino
        # auto_managed + 0 piezas deseadas => libtorrent PAUSA el torrent y no
        # llama a nadie. Hay que quitar los dos flags a mano.
        par.flags &= ~lt.torrent_flags.auto_managed
        par.flags &= ~lt.torrent_flags.paused
        self.h = self.ses.add_torrent(par)
        self.n = self.ti.num_pieces()
        self.pieza = self.ti.piece_length()
        self.tam = self.ti.total_size()
        self.h.prioritize_pieces([0] * self.n)
        # Cabecera y cola YA: sin nada deseado libtorrent anuncia numwant=0 y el
        # tracker no devuelve ni un peer (bloqueo circular). Además son las que
        # ffmpeg pedirá sí o sí (EBML al principio, Cues al final).
        self.h.resume()

        # Un torrent puede ser multi-fichero (packs de temporada). Se elige el
        # vídeo más grande y se trabaja con SU rango dentro del torrent: las
        # piezas van por offset global, pero ffmpeg lee el fichero suelto.
        # En un pack de temporada se coge el PRIMER vídeo, no el más grande:
        # es lo que hace RawLoadrr al generar capturas, y así la galería
        # repuesta es coherente con las que ya existen en el tracker. Además
        # evita bajar trozos de varios ficheros.
        # `files()` y `file_at()` están deprecados en libtorrent 2.x, pero las
        # bindings de Python no exponen file_path/file_size/file_offset en
        # ningún otro sitio: el file_storage que devuelve `files()` es la única
        # vía. Se silencia el aviso a propósito, no se ignora por pereza.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            fs = self.ti.files()
        mejor = None
        for i in sorted(range(fs.num_files()), key=lambda i: fs.file_path(i)):
            if fs.file_path(i).lower().endswith(R.VIDEO_EXT):
                mejor = i
                break
        if mejor is None:
            raise RuntimeError("el torrent no contiene ningún fichero de vídeo")

        self.idx = mejor
        self.off = fs.file_offset(mejor)          # offset global de ESE fichero
        self.tam = fs.file_size(mejor)            # y su tamaño (no el del torrent)
        self.nombre = os.path.basename(fs.file_path(mejor))
        self.ruta = os.path.join(destino, fs.file_path(mejor))
        self.srv = None
        # El handler corre en otro hilo: si _asegurar() revienta (piezas que no
        # llegan, tope superado), la excepción moría ahí y ffmpeg sólo recibía
        # datos truncados. Se guarda para poder decir QUÉ pasó de verdad.
        self.ultimo_error = None

        # Cabecera y cola DEL FICHERO elegido. Sin nada deseado libtorrent
        # anuncia numwant=0 y el tracker no devuelve ni un peer (bloqueo
        # circular); y son justo las piezas que ffmpeg pedirá sí o sí (EBML al
        # principio, Cues al final en MKV).
        pc = self.pieza
        for p in (list(range(self.off // pc, self.off // pc + 4))
                  + list(range(max(0, (self.off + self.tam - 1) // pc - 3),
                               (self.off + self.tam - 1) // pc + 1))):
            if 0 <= p < self.n:
                self.h.piece_priority(p, 7)
                self.h.set_piece_deadline(p, 1000, 0)

    def esperar_peers(self, seg=60):
        t0 = time.time()
        while time.time() - t0 < seg:
            if self.h.status().num_peers > 0:
                return self.h.status().num_peers
            time.sleep(0.5)
        return 0

    def bajado_mib(self):
        return self.h.status().total_done / 2 ** 20

    def _asegurar(self, desde, hasta):
        p0 = desde // self.pieza
        p1 = min((hasta - 1) // self.pieza, self.n - 1)
        faltan = [p for p in range(p0, p1 + 1) if not self.h.have_piece(p)]
        for p in faltan:
            self.h.piece_priority(p, 7)
            self.h.set_piece_deadline(p, 1000, 0)
        t0 = time.time()
        while faltan:
            if self.bajado_mib() > TOPE_MIB:
                raise RuntimeError(f"tope de {TOPE_MIB} MiB superado")
            if time.time() - t0 > ESPERA_MAX:
                raise TimeoutError(f"las piezas {faltan[:3]} no llegaron en {ESPERA_MAX}s")
            time.sleep(0.2)
            faltan = [p for p in faltan if not self.h.have_piece(p)]

    def arrancar(self):
        import http.server, threading
        srv_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _rango(self):
                r = self.headers.get("Range")
                if not r or not r.startswith("bytes="):
                    return 0, srv_self.tam - 1
                a, _, b = r[6:].partition("-")
                return (int(a) if a else 0), (min(int(b), srv_self.tam - 1) if b else srv_self.tam - 1)

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(srv_self.tam))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                ini, fin = self._rango()
                parcial = self.headers.get("Range") is not None
                self.send_response(206 if parcial else 200)
                if parcial:
                    self.send_header("Content-Range", f"bytes {ini}-{fin}/{srv_self.tam}")
                self.send_header("Content-Length", str(fin - ini + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                pos, TROZO = ini, 1 << 20
                try:
                    while pos <= fin:
                        hasta = min(pos + TROZO, fin + 1)
                        srv_self._asegurar(srv_self.off + pos, srv_self.off + hasta)
                        with open(srv_self.ruta, "rb") as f:
                            f.seek(pos)
                            datos = f.read(hasta - pos)
                        if not datos:
                            break
                        self.wfile.write(datos)
                        pos += len(datos)
                except (BrokenPipeError, ConnectionResetError):
                    pass          # ffmpeg ya tiene lo que quería: es NORMAL
                except Exception as exc:
                    srv_self.ultimo_error = f"{type(exc).__name__}: {exc}"


        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler)
        puerto = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{puerto}/{urllib.parse.quote(self.nombre)}"

    def cerrar(self):
        # ORDEN IMPORTANTE: parar el servidor del todo ANTES de soltar el
        # handle, o un hilo vivo toca un handle muerto -> "invalid torrent
        # handle [libtorrent:20]". Con 1400 torrents eso deja hilos colgando.
        if self.srv:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:
                pass
        try:
            # event=stopped OBLIGATORIO: sin él el peer sigue vivo en la memoria
            # del announce durante ACTIVE_PEER_TTL, y con
            # MAX_PEERS_PER_TORRENT_PER_USER=3 tres tiradas agotan el cupo del
            # torrent ("You already have 3 peers on this torrent").
            self.h.pause()
            time.sleep(5)
            self.ses.remove_torrent(self.h, self.lt.session.delete_files)
            time.sleep(2)
        except Exception:
            pass


def capturar_desde_torrent(entrada, rl_config, img_host, session):
    """Baja unas ventanas del fichero, saca capturas y las sube.

    Devuelve (image_list, None) o (None, motivo). No deja datos en disco.
    """
    tid = entrada["id"]
    ruta_torrent, err = _descargar_torrent(session, tid)
    if err:
        return None, err

    destino = os.path.join(DATOS_TMP, f"d{tid}")
    os.makedirs(destino, exist_ok=True)
    carpeta = os.path.join(R.RAWLOADRR_DIR, "tmp", f"intruso_{tid}__{_SUF}")
    os.makedirs(carpeta, exist_ok=True)
    srv = None
    try:
        srv = _ServidorPiezas(ruta_torrent, destino)
        peers = srv.esperar_peers()
        if not peers:
            return None, "ningún peer respondió (¿sin seeds ahora mismo?)"

        url = srv.arrancar()

        pr = subprocess.run(["ffprobe", "-v", "error", "-seekable", "1",
                             "-show_entries", "format=duration", "-of", "csv=p=0", url],
                            capture_output=True, text=True, timeout=300)
        dur = float((pr.stdout or "0").strip() or 0)
        if dur <= 0:
            return None, "ffprobe no pudo leer la duración"

        # Aquí el tonemap va EN ORIGEN, no al recodificar: estas capturas las
        # genera nuestro propio ffmpeg, así que se convierte desde el vídeo de
        # 10 bits en vez de desde un PNG de 8 con la curva ya horneada.
        # (En 05 no se puede: las captura prep.screenshots(), que es de upstream.)
        vf = ["-vf", R._filtro_tonemap()] if R.necesita_tonemap(url) else []
        if vf:
            print(f"    🌈 HDR detectado: tonemap {R.REGEN_TONEMAP_OP} → SDR", flush=True)

        pngs = []
        for v in VENTANAS:
            base = dur * v / 100.0
            for k in range(POR_VENTANA):
                # Capturas juntas dentro de la MISMA ventana: así se aprovecha
                # lo ya descargado en vez de abrir otra ventana por captura.
                ts = base + k * 12
                if ts >= dur:
                    continue
                salida = os.path.join(carpeta, f"intruso-{int(v)}-{k}.{_EXT_CAP}")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-seekable", "1",
                                "-ss", str(int(ts)), "-i", url,
                                "-frames:v", "1", *vf, "-q:v", _Q_CAP, salida],
                               capture_output=True, text=True, timeout=600)
                if os.path.exists(salida) and os.path.getsize(salida) > _MIN_CAP:
                    pngs.append(os.path.basename(salida))
                if len(pngs) >= MAX_CAPS:
                    break
            if len(pngs) >= MAX_CAPS:
                break

        if not pngs:
            motivo = srv.ultimo_error or "ffmpeg no devolvió ningún fotograma útil"
            return None, f"sin capturas aprovechables ({motivo})"

        print(f"    📥 {srv.bajado_mib():.0f} MiB bajados · {len(pngs)} capturas", flush=True)

        from src.prep import Prep
        prep = Prep(screens=len(pngs), img_host=img_host, config=rl_config)
        meta = {"base_dir": R.RAWLOADRR_DIR, "uuid": os.path.basename(carpeta),
                "path": srv.ruta, "filename": "intruso", "image_list": [],
                "imghost": img_host, "ffdebug": False, "vapoursynth": False}
        cwd = os.getcwd()
        try:
            image_list, _ = prep.upload_screens(meta, len(pngs), 1, 0, len(pngs), pngs, {})
        except KeyError as e:
            if re.fullmatch(r"img_host_\d+", str(e).strip("'\"")):
                return None, "todos los hosts de imágenes han fallado (cascada agotada)"
            raise
        finally:
            os.chdir(cwd)

        if not image_list:
            return None, "el host de imágenes no devolvió ninguna URL"
        return image_list, None
    finally:
        if srv:
            srv.cerrar()
        for d in (destino, carpeta):
            shutil.rmtree(d, ignore_errors=True)
        try:
            os.remove(ruta_torrent)
        except OSError:
            pass


def _descripcion_con_galeria(original, actual, image_list):
    """Devuelve la descripción a publicar, con la galería en su sitio.

    `original` (de la cola) marca DÓNDE iba: se reconstruye sobre ella
    sustituyendo el bloque de imágenes muertas. Antes se comprueba que nadie
    haya editado la página por medio comparando el texto visible; si difiere, se
    respeta lo que hay ahora y la galería va al final.
    """
    # sustituir_imagenes devuelve TRES valores: (texto, n, motivo_si_falla)
    nueva_orig, n, _motivo = R.sustituir_imagenes(original, image_list)
    if n and nueva_orig and _texto_visible(original) == _texto_visible(actual):
        return nueva_orig, "en su sitio original"

    bloque = "[center]" + R._bbcode(image_list, R._size_attr()) + "[/center]"
    if actual.strip() == PLACEHOLDER:
        return bloque, "sustituyendo el aviso"
    return actual.rstrip() + "\n\n" + bloque, "añadida al final (la página había cambiado)"


_MOTIVOS_CUOTA = (
    "hosts de imágenes han fallado",
    "no devolvió ninguna URL",
    "Rate limit",
    "rate limit",
)


def _es_falta_de_cuota(mensaje):
    """¿El fallo es 'ahora no hay cuota' en vez de 'este torrent está roto'?"""
    return any(m in (mensaje or "") for m in _MOTIVOS_CUOTA)


# Errores de PASARELA: el tracker no está, pero el torrent está perfecto. Los
# 5xx de nginx salen durante el backup de la base de datos (06:00) y duran
# minutos. 502 es el que se ve en la práctica; 503/504 van por completitud.
_MOTIVOS_TRACKER = ("HTTP 502", "HTTP 503", "HTTP 504",
                    "Bad Gateway", "Service Unavailable", "Gateway Time-out")


def _es_tracker_caido(mensaje):
    """¿El fallo es del tracker y no del torrent?

    Importa distinguirlo porque antes un 502 hacía tres cosas mal a la vez:
    contaba para el tope de "8 fallos seguidos", ensuciaba la lista de revisión
    manual con torrents sanos, y abortaba la tirada. Resultado medido: el backup
    de las 06:00 mataba la tirada y no se reanudaba hasta que el operador lo veía
    por la tarde — horas de nada.
    """
    return any(m in (mensaje or "") for m in _MOTIVOS_TRACKER)


def _tracker_responde(session):
    """Sonda barata: ¿contesta el tracker algo que no sea un 5xx?"""
    try:
        r = session.get(SITE_BASE, timeout=20, allow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False


# El backup de la base de datos ronda los 30 min y CRECE cada día, así que el
# tope va con margen de sobra (90 min). No cuesta nada tenerlo alto: se sondea
# cada 2 min y se sale en cuanto el tracker contesta; el tope sólo se agota en
# un corte de verdad.
ESPERA_TRACKER_S   = int(os.getenv("ME_INTRUSO_ESPERA_TRACKER", "120") or 120)
ESPERA_TRACKER_MAX = int(os.getenv("ME_INTRUSO_ESPERA_TRACKER_MAX", "5400") or 5400)


def _esperar_tracker(session):
    """Espera a que el tracker vuelva. True si volvió, False si se rindió.

    Sondea cada ESPERA_TRACKER_S hasta ESPERA_TRACKER_MAX en total. Esperar una
    hora es infinitamente más barato que perder la mañana entera parado.
    """
    esperado = 0
    while esperado < ESPERA_TRACKER_MAX:
        time.sleep(ESPERA_TRACKER_S)
        esperado += ESPERA_TRACKER_S
        if _tracker_responde(session):
            print(f"    ✅ el tracker responde otra vez (tras {esperado//60} min). Sigo.",
                  flush=True)
            return True
        print(f"    ⏳ sigue sin responder ({esperado//60} min esperando)…", flush=True)
    return False


def reponer_uno(session, entrada, rl_config, img_host):
    tid = entrada["id"]
    if not int(entrada.get("seeders") or 0):
        return False, "sin seeds: no hay de dónde sacar las capturas"

    form, soup, edit_url, err = R.leer_formulario(session, SITE_BASE, tid)
    if err:
        return False, err
    actual, err = R.leer_descripcion(session, SITE_BASE, tid, soup)
    if err:
        return False, err

    image_list, err = capturar_desde_torrent(entrada, rl_config, img_host, session)
    if err:
        return False, err

    nueva, donde = _descripcion_con_galeria(
        entrada["descripcion_original"], actual, image_list)

    if quedan_muertos(nueva):
        return False, "la reconstrucción reintroduciría enlaces muertos — abortado"

    if DRY_RUN:
        return True, f"SIMULACRO: {len(image_list)} imagen(es) {donde}"

    payload = R.construir_payload(form, nueva)
    ok, msg = R.enviar(session, form, payload, SITE_BASE, edit_url)
    if not ok:
        return False, msg

    ok, msg = R.verificar(session, SITE_BASE, tid, image_list)
    if not ok:
        return False, msg
    return True, f"{len(image_list)} imagen(es) publicadas, {donde}"


def fase_reponer(session, cola, rl_config):
    img_host = R._elegir_img_host(rl_config)
    hechos = _leer_marcador(REPUESTOS)
    # El selector es la COLA, no los enlaces: la fase 1 ya los quitó. Primero
    # los que más seeds tienen, que son los que menos van a hacer esperar.
    pend = [e for e in cola
            if e["id"] not in hechos and int(e.get("seeders") or 0) > 0]
    pend.sort(key=lambda e: -int(e.get("seeders") or 0))
    if LIMITE:
        pend = pend[:LIMITE]

    sin_seeds = sum(1 for e in cola if not int(e.get("seeders") or 0))
    print(f"\n🔧 Fase 2 · reponer galerías — {len(pend)} por hacer "
          f"(ya repuestos: {len(hechos)} · sin seeds, no reparables: {sin_seeds})")
    print(f"   Host de imágenes: {img_host}")
    print(f"   Ventanas: {', '.join(str(int(v)) + '%' for v in VENTANAS)} "
          f"× {POR_VENTANA} capturas · tope {TOPE_MIB/1024:.1f} GB por torrent")
    print(f"   Modo: {'SIMULACRO (no se escribe nada)' if DRY_RUN else 'REAL'}\n")

    ok_n = fail_n = seguidos = cuota_n = 0
    rescate_usado = False        # la espera larga del tope de fallos, una vez por tirada
    for i, e in enumerate(pend, 1):
        print(f"[{i}/{len(pend)}] ID {e['id']}  ({e['uploader']}, seeds={e['seeders']})  "
              f"{e['nombre'][:48]}", flush=True)
        # Si el tracker se cae (backup de las 06:00), se espera y se reintenta
        # ESTE MISMO torrent: el fallo no era suyo. reponer_uno() lee el
        # formulario ANTES de descargar nada, así que un corte se paga barato.
        tracker_ko = False
        for _intento in range(3):
            try:
                ok, msg = reponer_uno(session, e, rl_config, img_host)
            except Exception as ex:
                ok, msg = False, f"excepción: {ex}"
            if ok or not _es_tracker_caido(msg):
                break
            print(f"    ⏸️  {msg} — es el tracker, no el torrent. Espero a que vuelva.",
                  flush=True)
            if not _esperar_tracker(session):
                tracker_ko = True
                break

        if tracker_ko:
            print(f"\n🛑 El tracker lleva {ESPERA_TRACKER_MAX//60} min sin responder. "
                  f"Paro; al relanzar continúa por donde iba.")
            _marcar(APLAZADOS, e["id"], "tracker caído")
            break

        if ok:
            ok_n += 1
            seguidos = 0
            print(f"    ✨ {msg}", flush=True)
            if not DRY_RUN:
                _marcar(REPUESTOS, e["id"])
        else:
            fail_n += 1
            seguidos += 1
            print(f"    ❌ {msg}", flush=True)
            # Un fallo de cuota no es un torrent roto: es un "ahora no".
            if _es_falta_de_cuota(msg):
                cuota_n += 1
                _marcar(APLAZADOS, e["id"], msg[:80])
            else:
                _anotar_manual(e, f"reposición: {msg}"[:120])
            if "sesión caducada" in msg:
                print("\n🛑 Sesión caducada. Paro; al relanzar continúa por donde iba.")
                break
            if seguidos >= 7 and not rescate_usado:
                # Red de seguridad para cortes que NO se anuncian como 5xx
                # (conexión reseteada, timeouts, la pasarela devolviendo HTML…).
                # Antes de rendirse, una espera larga: si la racha la causaba el
                # backup, al volver se recoge sola en vez de perder la mañana.
                rescate_usado = True
                print(f"\n⏸️  {seguidos} fallos seguidos. Antes de rendirme espero "
                      f"a ver si es el tracker.", flush=True)
                if _esperar_tracker(session):
                    seguidos = 0
                    continue
                print("\n🛑 Y el tracker sigue sin responder. Paro.")
                break
            if seguidos >= 8:
                print("\n🛑 8 fallos seguidos. Paro para que le eches un ojo.")
                break

        if i < len(pend):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    print(f"\n✅ Repuestos: {ok_n}   ❌ Fallos: {fail_n}"
          + (f"   ⏸️  Aplazados por cuota: {cuota_n}" if cuota_n else ""))
    if not DRY_RUN:
        print(f"   Reanudable          : {REPUESTOS}")
    if cuota_n:
        print(f"   Aplazados por cuota : {APLAZADOS}")
        print("   No están marcados como hechos: relanza cuando los hosts respiren "
              "y se reintentan solos.")
    if fail_n - cuota_n > 0:
        print(f"   Para revisar a mano : {MANUAL}")
    return ok_n, fail_n

if __name__ == "__main__":
    sys.exit(main())
