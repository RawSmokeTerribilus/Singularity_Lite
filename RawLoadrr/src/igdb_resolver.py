# -*- coding: utf-8 -*-
"""
igdb_resolver -- identificar un juego a partir del nombre de un fichero.

Escrito contra medidas reales, no contra suposiciones. Las tres que mandan:

1. El `search` de IGDB NO es tolerante. Si un token de la consulta no está en
   el título devuelve CERO resultados, no resultados peores. "Code-Name,
   ICEMAN" da 0; "Code Name ICEMAN" da 1 y acierta. Por eso hay una cascada de
   variantes en vez de una limpieza única: no se sabe de antemano qué token
   sobra. Medido sobre 78 juegos: 59,0% a pelo, 70,5% con cascada.

2. Recortar el número de secuencia final fabrica mentiras seguras de sí mismas.
   "Discworld 2" -> "Discworld" puntúa 1.00 y es OTRO JUEGO; igual
   "Phantasmagoria 2". Después del recorte consulta y resultado coinciden al
   100% porque se ha borrado justo lo que distinguía las dos obras. Aquí eso
   no se hace nunca: si con el número da cero, es un `none` honesto.

3. Puntuar el último salto de una resolución en cadena LAVA el error. El puente
   de idioma daba 1.00 a "Paula y los Mayas" -> "Sonic CD" porque comparaba el
   título inglés que dio Wikipedia contra el resultado de IGDB, y claro que
   coinciden: el error se metió en el salto anterior. La confianza de un
   puente es el producto de la de cada salto, y se mide contra el nombre
   ORIGINAL del fichero.
"""

import os
import re
import time
import unicodedata
from difflib import SequenceMatcher

import requests

TIMEOUT = 15
TRUST_SCORE = 0.90           # a partir de aquí se aplica sin preguntar
MIN_CANDIDATE_SCORE = 0.50
BRIDGE_HOP1_MIN = 0.85       # el puente tiene que demostrar CADA salto
BRIDGE_HOP2_MIN = 0.90

from src.placeholders import es_placeholder

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_API = "https://api.igdb.com/v4"
WIKIPEDIA_API = "https://es.wikipedia.org/w/api.php"

IGDB_COVER = "https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{iid}.jpg"
IGDB_SHOT = "https://images.igdb.com/igdb/image/upload/t_screenshot_huge/{iid}.jpg"

# Wikimedia devuelve 403 al User-Agent por defecto de las librerías HTTP desde
# 2025 (phabricator.wikimedia.org/T400119). Sin cabecera propia salen cero
# resultados que PARECEN "no está en Wikipedia", que es el peor fallo posible:
# silencioso y con pinta de dato.
USER_AGENT = ("RawLoadrr/1.0 (+https://codeberg.org/RawSmoke/Singularity; "
              "uploader tooling)")

_ROMAN = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7,
    'viii': 8, 'ix': 9, 'x': 10, 'xi': 11, 'xii': 12, 'xiii': 13,
}


# ─── normalización y puntuación ──────────────────────────────────────────────
def _norm(s):
    """Minúsculas, sin diacríticos, sin puntuación. Igual que en id_resolver."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s):
    """
    Tokens con los romanos pasados a arábigos.

    Sin esto "Myst 3 Exile" contra "Myst III: Exile" puntúa 0.85 y "EcoQuest 2"
    contra "EcoQuest II" se hunde a 0.35, ESTANDO EN LO CIERTO. El umbral
    calibrado para TMDB no vale aquí: en cine la numeración romana es rara y en
    juegos es la norma.
    """
    out = []
    for tok in _norm(s).split():
        out.append(str(_ROMAN[tok]) if tok in _ROMAN else tok)
    return out


def title_score(query, candidate):
    """
    Similitud entre dos títulos de juego, en [0, 1].

    Además del parecido literal se puntúa por SUBCONJUNTO de tokens, porque el
    nombre comercial suele llevar un prefijo que la copia no trae: "Woodruff
    and the Schnibble of Azimuth" contra "The Bizarre Adventures of Woodruff
    and the Schnibble" es un acierto y la similitud cruda lo deja en 0.58.
    """
    q, c = _tokens(query), _tokens(candidate)
    if not q or not c:
        return 0.0

    ratio = SequenceMatcher(None, " ".join(q), " ".join(c)).ratio()

    qs, cs = set(q), set(c)
    common = qs & cs
    # Contenido: cuánto de la consulta aparece en el candidato, y al revés. El
    # mínimo de los dos evita que "Myst" gane contra todo lo que empiece igual.
    contained = len(common) / max(1, min(len(qs), len(cs)))
    coverage = len(common) / max(1, max(len(qs), len(cs)))

    # Los números discriminan secuelas y no se promedian con lo demás: si la
    # consulta dice 2 y el candidato dice 1, no son el mismo juego por mucho
    # que compartan el resto del nombre.
    q_nums = {t for t in q if t.isdigit()}
    c_nums = {t for t in c if t.isdigit()}
    if q_nums and c_nums and not (q_nums & c_nums):
        return min(ratio, 0.45)
    if q_nums and not c_nums:
        return min(ratio, 0.55)

    return max(ratio, (contained + coverage) / 2)


# ─── cascada de variantes ────────────────────────────────────────────────────
_TRAILING_SEQ = re.compile(r'\b(\d{1,2}|[ivx]{1,4})\s*$', re.IGNORECASE)


def _has_trailing_sequence(text):
    tail = _TRAILING_SEQ.search(_norm(text))
    if not tail:
        return False
    tok = tail.group(1).lower()
    return tok.isdigit() or tok in _ROMAN


def title_variants(raw):
    """
    Del candidato más fiel al más laxo, y se para en el primero que devuelva
    algo. Cada variante es un intento distinto de quitar el token que IGDB no
    reconoce -- una coma, un prefijo de editora, un paréntesis.

    NUNCA se recorta el número de secuencia final. Es la única regla dura de
    esta función y la razón de que exista _has_trailing_sequence().
    """
    base = os.path.splitext(str(raw).strip())[0]
    seq = _has_trailing_sequence(base)

    seen = []

    def add(text):
        text = re.sub(r'\s+', ' ', (text or '')).strip(' -_,:;')
        if not text or len(text) < 3:
            return
        # Si el original llevaba número de secuencia, una variante que lo haya
        # perdido está prohibida: es exactamente la que fabrica el 1.00 falso.
        if seq and not _has_trailing_sequence(text):
            return
        if text.lower() not in [t.lower() for t in seen]:
            seen.append(text)

    add(base)
    add(re.sub(r'\s*\([^)]*\)\s*$', '', base))              # sin paréntesis finales
    add(re.sub(r'\s*\[[^\]]*\]\s*$', '', base))             # sin corchetes finales
    add(re.sub(r'[,:;\-_]+', ' ', base))                    # puntuación a espacios

    # Sólo el subtítulo: "Laura Bow 2, The Dagger of Amon Ra" no da nada en
    # IGDB, y "The Dagger of Amon Ra" acierta.
    for sep in (' - ', ', ', ': '):
        if sep in base:
            add(base.split(sep, 1)[1])

    # El título base hasta el primer separador, que es lo que rescata
    # "Discworld" de "Discworld - A Dead Man's Diary"... salvo que hubiera
    # secuencia, en cuyo caso add() lo rechaza solo.
    for sep in (' - ', ', ', ': '):
        if sep in base:
            add(base.split(sep, 1)[0])

    return seen


# ─── cliente IGDB ────────────────────────────────────────────────────────────
class IgdbClient:
    """Token de Twitch + Apicalypse. El token dura semanas; se cachea en memoria."""

    _FIELDS = ("fields name,slug,summary,first_release_date,"
               "cover.image_id,screenshots.image_id,videos.video_id,"
               "genres.name,platforms.name,"
               "involved_companies.company.name,involved_companies.developer;")

    def __init__(self, client_id, client_secret, log=None):
        self.client_id = (client_id or '').strip()
        self.client_secret = (client_secret or '').strip()
        self.log = log or (lambda m: None)
        self._token = None
        self._token_expires = 0

    @property
    def configured(self):
        """Con un hueco sin rellenar NO esta configurado.

        Antes bastaba con que las dos cadenas fueran no vacias, asi que los
        placeholders "TWITCH_CLIENT_ID"/"TWITCH_CLIENT_SECRET" contaban como
        credenciales: se intentaba el OAuth y el usuario recibia un error de
        Twitch en vez del honesto "faltan las claves".
        """
        return not es_placeholder(self.client_id, "TWITCH_CLIENT_ID", "igdb_client_id") \
            and not es_placeholder(self.client_secret, "TWITCH_CLIENT_SECRET", "igdb_client_secret")

    def _auth(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        r = requests.post(TWITCH_TOKEN_URL, params={
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'client_credentials',
        }, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        self._token = payload['access_token']
        self._token_expires = time.time() + int(payload.get('expires_in', 3600))
        return self._token

    def _post(self, endpoint, body):
        try:
            token = self._auth()
        except Exception as e:                                  # noqa: BLE001
            self.log(f"igdb auth failed: {e}")
            return []

        try:
            r = requests.post(f"{IGDB_API}/{endpoint}", data=body, timeout=TIMEOUT,
                              headers={'Client-ID': self.client_id,
                                       'Authorization': f'Bearer {token}',
                                       'Accept': 'application/json'})
            if r.status_code == 429:
                time.sleep(1.0)
                r = requests.post(f"{IGDB_API}/{endpoint}", data=body, timeout=TIMEOUT,
                                  headers={'Client-ID': self.client_id,
                                           'Authorization': f'Bearer {token}',
                                           'Accept': 'application/json'})
            r.raise_for_status()
            return r.json() or []
        except Exception as e:                                  # noqa: BLE001
            self.log(f"igdb {endpoint} failed: {e}")
            return []

    def search(self, title, limit=10):
        safe = str(title).replace('"', '')
        return self._post('games', f'search "{safe}"; {self._FIELDS} limit {limit};')

    def by_id(self, igdb_id):
        rows = self._post('games', f'where id = {int(igdb_id)}; {self._FIELDS} limit 1;')
        return rows[0] if rows else None


def _record(row):
    """Aplana la respuesta de IGDB a lo que la descripción y el payload usan."""
    if not row:
        return None

    year = None
    if row.get('first_release_date'):
        # IGDB entrega epoch Unix. Guardarlo tal cual ya dejó una tabla entera
        # de juegos con first_release_date = 0000-00-00 en el tracker.
        year = time.gmtime(int(row['first_release_date'])).tm_year

    companies = [c['company']['name']
                 for c in (row.get('involved_companies') or [])
                 if c.get('developer') and (c.get('company') or {}).get('name')]

    cover = (row.get('cover') or {}).get('image_id')
    videos = [v['video_id'] for v in (row.get('videos') or []) if v.get('video_id')]

    return {
        'igdb': row.get('id'),
        'igdb_slug': row.get('slug', ''),
        'title': row.get('name', ''),
        'year': year,
        'description': (row.get('summary') or '').strip(),
        'cover_url': IGDB_COVER.format(iid=cover) if cover else '',
        'screenshots': [IGDB_SHOT.format(iid=s['image_id'])
                        for s in (row.get('screenshots') or []) if s.get('image_id')],
        'trailer': videos[0] if videos else '',
        'genres': [g['name'] for g in (row.get('genres') or []) if g.get('name')],
        'platforms': [p['name'] for p in (row.get('platforms') or []) if p.get('name')],
        'companies': companies,
    }


# ─── puente de idioma por Wikipedia ──────────────────────────────────────────
def english_title_via_wikipedia(spanish_title, log=None):
    """
    -> (titulo_ingles, confianza_del_salto) o (None, 0.0)

    IGDB tiene el catálogo indexado sólo en inglés, así que un título en
    castellano no da cero por estar mal escrito sino por estar en otro idioma.
    Los enlaces interlingües de Wikipedia lo resuelven sin clave y con licencia
    libre.

    La confianza que devuelve NO es la del resultado final: es cómo de seguro
    está este salto, o sea cuánto se parece la página que ha encontrado al
    título que se buscaba. Quien llame multiplica.
    """
    log = log or (lambda m: None)

    try:
        r = requests.get(WIKIPEDIA_API, timeout=TIMEOUT,
                         headers={'User-Agent': USER_AGENT},
                         params={
                             'action': 'query',
                             'format': 'json',
                             'generator': 'search',
                             'gsrsearch': spanish_title,
                             'gsrlimit': 5,
                             'prop': 'langlinks',
                             'lllang': 'en',
                             'lllimit': 5,
                         })
        r.raise_for_status()
        pages = ((r.json() or {}).get('query') or {}).get('pages') or {}
    except Exception as e:                                      # noqa: BLE001
        log(f"wikipedia bridge failed: {e}")
        return None, 0.0

    if not pages:
        return None, 0.0

    # `query.pages` viene indexado por pageid, NO por relevancia: coger el
    # primero del map da la página equivocada. En "Indiana Jones y la última
    # cruzada" el index 1 es la PELÍCULA y el index 2 la aventura gráfica.
    ordered = sorted(pages.values(), key=lambda p: p.get('index', 9999))

    for page in ordered:
        links = page.get('langlinks') or []
        if not links:
            continue
        english = (links[0].get('*') or '').strip()
        if not english:
            continue

        # El salto se puntúa contra lo que se pidió, no contra lo que se
        # encontró. Una página que no se parece al título buscado no ha
        # demostrado nada aunque tenga enlace inglés.
        hop = title_score(spanish_title, page.get('title', ''))
        log(f"wikipedia: '{spanish_title}' -> '{page.get('title')}' "
            f"-> en:'{english}' (salto {hop:.2f})")
        return english, hop

    return None, 0.0


# ─── resolución ──────────────────────────────────────────────────────────────
def _verdict(confidence, score, record, reason):
    return {
        'confidence': confidence,
        'igdb': (record or {}).get('igdb') or 0,
        'score': round(float(score), 3),
        'record': record,
        'reason': reason,
    }


def resolve_game(raw_title, igdb_hint=None, config=None, log=None):
    """
    -> {'confidence': high|low|none, 'igdb': int, 'score': float,
        'record': dict|None, 'reason': str}

    `raw_title` es el nombre del fichero. Todo lo que se puntúe se puntúa
    contra ÉL, nunca contra una variante ni contra una traducción.
    """
    log = log or (lambda m: None)
    defaults = ((config or {}).get('DEFAULT', {}) or {})

    client = IgdbClient(defaults.get('igdb_client_id'),
                        defaults.get('igdb_client_secret'), log=log)

    if igdb_hint:
        try:
            row = client.by_id(int(str(igdb_hint).strip()))
        except (TypeError, ValueError):
            row = None
        if row:
            log(f"igdb id {igdb_hint} confirmado")
            return _verdict('high', 1.0, _record(row), 'id de IGDB dado por quien sube')
        log(f"igdb id {igdb_hint} desconocido para IGDB")

    if not client.configured:
        return _verdict('none', 0.0, None, 'faltan igdb_client_id / igdb_client_secret')

    original = os.path.splitext(os.path.basename(str(raw_title)))[0]

    # ── cascada ──
    for variant in title_variants(original):
        rows = client.search(variant)
        if not rows:
            continue

        scored = []
        for row in rows:
            rec = _record(row)
            if not rec or not rec['title']:
                continue
            # Contra el ORIGINAL. Puntuar contra `variant` sería premiar a la
            # variante por haberse parecido a sí misma.
            rec['score'] = title_score(original, rec['title'])
            scored.append(rec)

        if not scored:
            continue

        scored.sort(key=lambda r: -r['score'])
        best = scored[0]

        if best['score'] >= TRUST_SCORE:
            log(f"igdb '{original}' -> {best['title']} ({best['score']:.2f}) "
                f"por la variante '{variant}'")
            return _verdict('high', best['score'], best,
                            f"coincidencia clara con la variante '{variant}'")

        if best['score'] >= MIN_CANDIDATE_SCORE:
            log(f"igdb '{original}' -> {best['title']} ({best['score']:.2f}) DUDOSO")
            return _verdict('low', best['score'], best,
                            'el mejor candidato no llega al umbral de confianza')

    # ── puente de idioma ──
    english, hop1 = english_title_via_wikipedia(original, log=log)
    if not english:
        return _verdict('none', 0.0, None, 'ningún candidato en IGDB, ni por Wikipedia')

    rows = client.search(english)
    if not rows:
        return _verdict('none', 0.0, None,
                        f"Wikipedia da '{english}' pero IGDB no lo tiene")

    bridged = []
    for row in rows:
        rec = _record(row)
        if not rec or not rec['title']:
            continue
        hop2 = title_score(english, rec['title'])
        # Producto, no el último salto. Puntuar sólo hop2 daba 1.00 a "Paula y
        # los Mayas" -> "Sonic CD": el título inglés y el resultado coincidían
        # entre ellos porque el error estaba en el salto anterior.
        rec['score'] = hop1 * hop2
        rec['_hop1'], rec['_hop2'] = hop1, hop2
        bridged.append(rec)

    if not bridged:
        return _verdict('none', 0.0, None, 'el puente no dio candidatos utilizables')

    bridged.sort(key=lambda r: -r['score'])
    best = bridged[0]

    if best['_hop1'] >= BRIDGE_HOP1_MIN and best['_hop2'] >= BRIDGE_HOP2_MIN:
        log(f"puente '{original}' -> en:'{english}' -> {best['title']} "
            f"({best['_hop1']:.2f} x {best['_hop2']:.2f} = {best['score']:.2f})")
        return _verdict('high', best['score'], best,
                        f"puente de Wikipedia, ambos saltos demostrados ('{english}')")

    # Un puente que no puede demostrar sus dos saltos degrada a low, y low
    # pregunta. Es la salida honesta: el 70% automático viene con un 5% de
    # mentiras a 1.00 si se deja pasar esto.
    return _verdict('low', best['score'], best,
                    f"puente de Wikipedia sin demostrar "
                    f"(salto1 {best['_hop1']:.2f}, salto2 {best['_hop2']:.2f})")
