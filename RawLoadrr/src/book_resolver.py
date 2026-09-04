"""
Book and audiobook identification for RawLoadrr.

The Python twin of the tracker's app/Services/Books + app/Services/Audiobooks.
Same providers, same thresholds, same verdict vocabulary (high / low / none),
so an upload identified here and the same upload identified by the tracker do
not disagree about what a book is.

Why only Google Books identifies e-books: OpenLibrary was measured against the
live API on 2026-08-20 and does not cover a Spanish catalogue -- 404 by ISBN,
and a title search that returns a different book altogether. Letting it vote
would inject false positives, so it is not consulted here at all.

Audiobooks need two hops because Audnexus is addressable by ASIN and nothing
else: Audible's catalogue turns a title into ASINs, Audnexus turns an ASIN
into a record. Audible's own relevance ordering is not trusted -- measured, it
put the correct recording third -- so every hit is scored.
"""

import re
import unicodedata
from difflib import SequenceMatcher

import requests

TIMEOUT = 12
# Google Books devuelve 503 de forma aleatoria. Medido con la clave puesta:
# 3 de cada 15 peticiones, un 20%. Con tres intentos y esperas de 0,4 y 0,8
# segundos se colaba igual -- las dos consultas de un mismo libro pueden caer
# las dos -- y el libro se quedaba sin portada ni sinopsis.
ATTEMPTS = 5
MIN_CANDIDATE_SCORE = 0.50
TRUST_SCORE = 0.90
LEAD_MARGIN = 0.05           # two editions of one book tie exactly; that is a question, not a win

from src.placeholders import limpiar

GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"
AUDNEXUS = "https://api.audnex.us"

# The `imageLinks` block of the API only ever hands back a 128px thumbnail, which
# looks miserable on a torrent page. The content endpoint takes an undocumented
# `zoom`, and measured against the real API it goes up to 2177x2771 for the same
# volume that `imageLinks` renders at 128x170.
#
# Always ask for 6 and never compute anything: the parameter self-limits to
# whatever resolution the publisher actually uploaded, so asking for more than
# exists simply returns the best available. And it is NOT a linear scale --
# zoom=5 gives back the same 128px as zoom=1 -- so there is nothing to interpolate.
GOOGLE_COVER = ("https://books.google.com/books/content"
                "?id={vid}&printsec=frontcover&img=1&zoom=6")

# Fallback only. Measured on the same book, OpenLibrary's large cover is
# 128x164 / 6.4 KiB against Google's 2177x2771 / 539 KiB, so it never goes first.
# `default=false` is not optional: without it OpenLibrary answers 200 with a
# placeholder image instead of 404, and you end up caching filler.
OPENLIBRARY_COVER = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
AUDIBLE_DOMAINS = {
    'es': 'api.audible.es', 'us': 'api.audible.com', 'uk': 'api.audible.co.uk',
    'fr': 'api.audible.fr', 'de': 'api.audible.de', 'it': 'api.audible.it',
    'ca': 'api.audible.ca', 'au': 'api.audible.com.au', 'br': 'api.audible.com.br',
    'jp': 'api.audible.co.jp', 'in': 'api.audible.in',
}


# ─── scoring (kept identical to id_resolver so "same title" means one thing) ──
def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_score(query, candidate):
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = SequenceMatcher(None, q, c).ratio()
    qt, ct = set(q.split()), set(c.split())
    if qt and ct and (qt <= ct or ct <= qt):
        ratio = max(ratio, 0.88)
    return ratio


# ─── ISBN ────────────────────────────────────────────────────────────────────
def _isbn_check13(body12):
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body12))
    return str((10 - total % 10) % 10)


def to_isbn13(raw):
    """Normalise any ISBN to a valid ISBN-13, or '' when it is neither."""
    s = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()

    if len(s) == 13:
        return s if s.isdigit() and _isbn_check13(s[:12]) == s[12] else ""

    if len(s) == 10 and re.fullmatch(r"\d{9}[0-9X]", s):
        total = sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(s))
        if total % 11 == 0:
            body = "978" + s[:9]
            return body + _isbn_check13(body)

    return ""


# ─── Google Books ────────────────────────────────────────────────────────────
def _google_get(params, key, log):
    # Sin clave se consulta igual, pero el parametro NO puede ir vacio: Google
    # trata `key=` como una clave invalida, no como su ausencia.
    params = dict(params, key=key) if key else dict(params)

    for attempt in range(ATTEMPTS):
        try:
            r = requests.get(GOOGLE_BOOKS, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
            if items:
                return items
        except Exception as e:                                  # noqa: BLE001
            log(f"google books failed: {e}")

        if attempt < ATTEMPTS - 1:
            import random
            import time
            # Exponencial con algo de azar: si una tirada entera reintenta a la
            # vez y siempre al mismo ritmo, vuelve a chocar toda junta.
            espera = min(8.0, 0.6 * (2 ** attempt)) * (0.7 + random.random() * 0.6)
            time.sleep(espera)

    return []


def _best_image_link(v):
    """The largest thumbnail Google admits to, as a fallback for the cover."""
    links = v.get("imageLinks") or {}
    for size in ("extraLarge", "large", "medium", "small",
                 "thumbnail", "smallThumbnail"):
        if links.get(size):
            # These come back over plain http and with edge curl painted on.
            return links[size].replace("http://", "https://").replace("&edge=curl", "")
    return ""


def _google_candidate(item):
    v = (item or {}).get("volumeInfo") or {}
    if not v.get("title"):
        return None

    # The volume id is a sibling of volumeInfo in the response we already asked
    # for to identify the book, so the cover costs no second request.
    volume_id = (item or {}).get("id") or ""

    isbn13 = isbn10 = ""
    for ident in v.get("industryIdentifiers") or []:
        if ident.get("type") == "ISBN_13":
            isbn13 = to_isbn13(ident.get("identifier"))
        elif ident.get("type") == "ISBN_10":
            isbn10 = re.sub(r"[^0-9Xx]", "", str(ident.get("identifier") or "")).upper()

    if not isbn13 and isbn10:
        isbn13 = to_isbn13(isbn10)

    if not isbn13:
        return None            # nothing to key a row on

    year = None
    m = re.search(r"(\d{4})", str(v.get("publishedDate") or ""))
    if m:
        year = int(m.group(1))

    cover = GOOGLE_COVER.format(vid=volume_id) if volume_id else ""

    return {
        "provider": "google", "title": v["title"], "subtitle": v.get("subtitle", ""),
        "authors": [str(a) for a in (v.get("authors") or [])],
        "year": year, "isbn13": isbn13, "isbn10": isbn10,
        "publisher": v.get("publisher", ""), "page_count": v.get("pageCount"),
        "language": v.get("language", ""), "score": 0.0,
        "volume_id": volume_id,
        "cover_url": cover,
        "cover_fallbacks": [u for u in (_best_image_link(v),
                                        OPENLIBRARY_COVER.format(isbn=isbn13)) if u],
        # Google's own subject headings. Worth keeping: OpenLibrary's `subjects`
        # were measured to return the same concept in five languages plus tags
        # belonging to other works entirely.
        "genres": [str(c) for c in (v.get("categories") or [])],
        "description": v.get("description", "") or "",
    }


def _score_book(cand, title, author, year):
    score = _title_score(title, cand["title"])

    if cand.get("subtitle"):
        score = max(score, _title_score(title, f"{cand['title']} {cand['subtitle']}"))

    if year and cand.get("year"):
        diff = abs(int(year) - int(cand["year"]))
        if diff == 0:
            score += 0.05
        elif diff > 1:
            score -= 0.30

    if author and cand.get("authors"):
        if max(_title_score(author, a) for a in cand["authors"]) >= 0.85:
            score += 0.05

    return max(0.0, min(1.0, score))



# ─── cascada de títulos ──────────────────────────────────────────────────────
_PARENTESIS_FINAL = re.compile(r'\s*[\(\[][^\)\]]*[\)\]]\s*$')
_SEQ_FINAL = re.compile(r'\b\d{1,2}\s*$')


def query_variants(title, author=None):
    """
    Del candidato más fiel al más laxo. Se para en el primero que dé algo.

    Nace de un caso real: el `.m4a` de *El arte de la guerra* traía en las
    etiquetas "Sun Tzu - El Arte de la Guerra (Audiolibro en Castellano)".
    Audible no conoce ningún libro que se llame así, y el nombre del formato
    y del idioma pegados al título son la norma, no la excepción.

    Misma regla dura que en juegos: **nunca se recorta un número final**. "Dune
    2" y "Dune" son libros distintos, y quitar el número fabrica un acierto de
    los que puntúan perfecto y están mal.
    """
    base = str(title or '').strip()
    if not base:
        return []

    lleva_secuencia = bool(_SEQ_FINAL.search(base))
    vistos = []

    def add(texto):
        texto = re.sub(r'\s+', ' ', (texto or '')).strip(' -_,:;.')
        if len(texto) < 3:
            return
        if lleva_secuencia and not _SEQ_FINAL.search(texto):
            return
        if texto.lower() not in [v.lower() for v in vistos]:
            vistos.append(texto)

    add(base)

    # Los paréntesis finales se van de uno en uno: "(Audiolibro) (192kbit_AAC)"
    # son dos, y quitar sólo el último no arregla nada.
    recortado = base
    while _PARENTESIS_FINAL.search(recortado):
        recortado = _PARENTESIS_FINAL.sub('', recortado)
        add(recortado)

    # "Autor - Título" es como nombra medio mundo sus ficheros. Sólo se quita
    # cuando la parte de delante ES el autor que ya conocemos: cortar por el
    # primer guion a ciegas destroza "Cien años - de soledad" y parecidos.
    if author:
        for texto in list(vistos):
            for sep in (' - ', ' – ', ': '):
                if sep in texto:
                    izquierda, derecha = texto.split(sep, 1)
                    if _title_score(author, izquierda) >= 0.80:
                        add(derecha)

    for texto in list(vistos):
        add(re.sub(r'[_\.]+', ' ', texto))

    return vistos



# ─── una obra con muchas ediciones NO es una duda ────────────────────────────
def _mismo_trabajo(a, b, umbral=0.85):
    """
    ¿Estos dos títulos son la misma obra?

    No basta la similitud cruda. Las ediciones añaden coletillas -- "completo",
    "3ª Edición", "(Spanish Edition)" -- que dejan el parecido en 0.88, justo
    por debajo de cualquier umbral razonable, y entonces diez ediciones del
    mismo libro parecen diez libros distintos.

    Lo que de verdad distingue una coletilla de otra obra es la CONTENCIÓN: si
    todas las palabras del título corto están en el largo, el largo es el
    mismo libro con algo añadido.
    """
    ta, tb = set(_norm(a).split()), set(_norm(b).split())

    if not ta or not tb:
        return False

    corto, largo = (ta, tb) if len(ta) <= len(tb) else (tb, ta)

    if corto <= largo:
        return True

    return _title_score(a, b) >= umbral


def _misma_obra(cands, minimo=0.70):
    """
    ¿La mayoría de estos candidatos son el mismo libro en ediciones distintas?

    Es la distinción que faltaba. "No sé qué obra es esto" y "sé perfectamente
    qué obra es, pero hay doce ediciones" son incertidumbres distintas y sólo
    una merece molestar a nadie. Medido con *El arte de la guerra*: los diez
    candidatos eran Sun Tzu y sólo cambiaban editorial, año y páginas.

    Mayoría y no unanimidad: en una lista de diez ediciones se cuela siempre
    una antología o un comentario sobre la obra, y un solo intruso no puede
    convertir una decisión evidente en una pregunta.

    -> lista de candidatos de la obra dominante, o `[]` si no hay tal.
    """
    if not cands:
        return []

    ref = cands[0]
    ref_a = _norm(", ".join(ref.get("authors") or []))

    grupo = []
    for c in cands:
        if not _mismo_trabajo(ref["title"], c["title"]):
            continue

        autores = _norm(", ".join(c.get("authors") or []))
        # Si alguno no declara autor no se cuenta como desacuerdo: sería
        # castigar la falta de dato.
        if ref_a and autores and _title_score(ref_a, autores) < 0.70:
            continue

        grupo.append(c)

    return grupo if len(grupo) >= max(2, int(len(cands) * minimo)) else []


def _elegir_edicion(cands, local, log):
    """
    De varias ediciones de la misma obra, la que más se parece AL FICHERO.

    Nada de preguntar: el fichero ya sabe cosas de sí mismo -- cuántas páginas
    tiene, de qué editorial es, de qué año, en qué idioma -- y eso desempata
    solo. Cuando no sabe nada, gana el registro más completo, que es el que
    mejor ficha va a producir.
    """
    local = local or {}
    paginas = local.get('paginas') or local.get('page_count')
    editorial = (local.get('publisher') or '').strip()
    anio = local.get('year')
    idioma = (local.get('language') or '').strip().lower()

    def puntos(c):
        p = 0.0

        if paginas and c.get('page_count'):
            # Un PDF nunca cuadra exacto con una edición impresa: portada y
            # cortesías bailan. Se premia la cercanía, no la igualdad.
            diff = abs(int(paginas) - int(c['page_count']))
            if diff == 0:
                p += 3.0
            elif diff <= 5:
                p += 2.0
            elif diff <= 20:
                p += 1.0

        if editorial and c.get('publisher') and _title_score(editorial, c['publisher']) >= 0.85:
            p += 2.0

        if anio and c.get('year') and abs(int(anio) - int(c['year'])) <= 1:
            p += 1.5

        if idioma and (c.get('language') or '').lower() == idioma:
            p += 0.5

        # Desempate final: el registro más completo hace mejor ficha.
        p += 0.1 * sum(1 for k in ('publisher', 'page_count', 'year') if c.get(k))

        return p

    ordenados = sorted(cands, key=lambda c: (-puntos(c), -c['score']))
    ganador = ordenados[0]

    log(f"misma obra, {len(cands)} ediciones -> elegida "
        f"{ganador.get('publisher') or 's/editorial'} {ganador.get('year') or ''} "
        f"({ganador.get('page_count') or '?'}p) por parecido con el fichero")

    return ganador


def resolve_book(title, author=None, year=None, isbn_hint=None, config=None, log=None, local=None):
    """
    -> {'confidence': high|low|none, 'isbn13': str, 'score': float,
        'record': dict|None, 'reason': str}
    """
    log = log or (lambda m: None)
    # Un hueco sin rellenar vale MENOS que ninguna clave: Google Books
    # responde sin ella --con un limite mas bajo-- pero devuelve 400 si se le
    # manda una que no vale, y el usuario veia ese 400 en vez de "te falta
    # configurar la clave".
    key = limpiar(((config or {}).get("DEFAULT", {}) or {}).get("google_books_api"),
                  "GOOGLE_BOOKS_API", "google_books_api")

    hint = to_isbn13(isbn_hint or "")
    if hint:
        for item in _google_get({"q": f"isbn:{hint}", "maxResults": 5}, key, log):
            cand = _google_candidate(item)
            if cand and cand["isbn13"] == hint:
                log(f"isbn hint {hint} confirmed by google books")
                return _verdict("high", 1.0, cand, "isbn supplied by uploader")

        log(f"isbn hint {hint} not found upstream")

    if not key:
        return _verdict("none", 0.0, None, "no google books api key configured")

    # El título original que declara el fichero va PRIMERO: es un dato, no una
    # deducción, y es lo único que encuentra a una traducción de aficionados.
    variantes = query_variants(title, author)
    declarado = ((local or {}).get('original_title') or '').strip()
    if declarado and declarado.lower() not in [v.lower() for v in variantes]:
        variantes.insert(0, declarado)

    cands = []
    for variante in variantes:
        q = f"intitle:{variante}"
        if author:
            q += f" inauthor:{author}"      # a literal space; a '+' is sent as %2B and 503s

        for item in _google_get({"q": q, "maxResults": 10,
                                 "langRestrict": "es", "country": "ES"}, key, log):
            cand = _google_candidate(item)
            if cand:
                # Se puntúa contra la variante QUE SE CONSULTÓ, no contra el
                # título original: si se limpió "(Audiolibro en Castellano)"
                # es porque sobraba, y penalizar al candidato por no traerlo
                # sería castigarle por acertar.
                cand["score"] = _score_book(cand, variante, author, year)
                if cand["score"] >= MIN_CANDIDATE_SCORE:
                    cands.append(cand)

        if cands:
            if variante != title:
                log(f"variante '{variante}' dio {len(cands)} candidatos")
            break

    if not cands:
        log(f"no book candidate above threshold for '{title}'")
        return _verdict("none", 0.0, None, "no candidate scored high enough")

    cands.sort(key=lambda c: -c["score"])
    best = cands[0]
    lead = best["score"] - cands[1]["score"] if len(cands) > 1 else 1.0

    if best["score"] >= TRUST_SCORE and lead >= LEAD_MARGIN:
        conf, reason = "high", "coincidencia única y clara"
    elif best["score"] >= TRUST_SCORE and _misma_obra(
            [c for c in cands if c["score"] >= TRUST_SCORE]):
        # Empataban porque son la MISMA OBRA en ediciones distintas. Eso no es
        # una duda sobre qué es el libro, así que no se pregunta: se elige la
        # edición que más se parece al fichero.
        #
        # Y es lo correcto para esta biblioteca: medido sobre 300 epubs, sólo
        # el 4,3% trae ISBN dentro -- el resto son ediciones digitales de
        # bibliotecas libres, reproducciones del original, que no tienen ISBN
        # propio que registrar.
        best = _elegir_edicion(
            _misma_obra([c for c in cands if c["score"] >= TRUST_SCORE]), local, log)
        conf = "high"
        reason = "misma obra en varias ediciones; elegida la más parecida al fichero"
    else:
        conf = "low"
        reason = ("el mejor candidato no llega al umbral de confianza"
                  if best["score"] < TRUST_SCORE
                  else "varias obras distintas puntúan igual; lo elige una persona")

    log(f"resolved book '{title}' -> {conf} isbn13={best['isbn13']} "
        f"score={best['score']:.3f} lead={lead:.3f} ({reason})")

    return _verdict(conf, best["score"], best, reason)


# ─── Audible + Audnexus ──────────────────────────────────────────────────────
def _audible_search(title, author, region, log):
    domain = AUDIBLE_DOMAINS.get((region or "es").lower())
    if not domain:
        return []

    params = {
        "title": title, "num_results": 10, "products_sort_by": "Relevance",
        # Without this every product comes back with a null title.
        "response_groups": "product_desc,contributors,product_attrs",
    }
    if author:
        params["author"] = author

    try:
        r = requests.get(f"https://{domain}/1.0/catalog/products", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        products = (r.json() or {}).get("products") or []
    except Exception as e:                                      # noqa: BLE001
        log(f"audible search failed: {e}")
        return []

    def names(people):
        return [p["name"] for p in (people or []) if isinstance(p, dict) and p.get("name")]

    def anio(p):
        fecha = str(p.get("release_date") or p.get("issue_date") or "")
        m = re.search(r"(\d{4})", fecha)
        return int(m.group(1)) if m else None

    # `product_attrs` ya se pedía, pero sólo se leían título y personas. La
    # duración venía en la misma respuesta y se estaba tirando: es el dato que
    # distingue una grabación de otra sin preguntarle a nadie.
    return [{
        "asin": p.get("asin", ""), "title": p.get("title") or "",
        "subtitle": p.get("subtitle") or "",
        "authors": names(p.get("authors")), "narrators": names(p.get("narrators")),
        "runtime_min": p.get("runtime_length_min"),
        "publisher": p.get("publisher_name") or "",
        "language": (p.get("language") or "").lower(),
        "year": anio(p),
    } for p in products if p.get("asin")]


def audnexus_book(asin, region="es", log=None):
    log = log or (lambda m: None)
    try:
        r = requests.get(f"{AUDNEXUS}/books/{asin}", params={"region": region}, timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json() or {}
    except Exception as e:                                      # noqa: BLE001
        log(f"audnexus failed: {e}")
        return None

    if not d.get("title"):
        return None

    def names(items):
        return [i["name"] if isinstance(i, dict) else str(i)
                for i in (items or []) if i]

    return {
        "asin": d.get("asin", asin), "title": d["title"], "subtitle": d.get("subtitle", ""),
        "authors": names(d.get("authors")), "narrators": names(d.get("narrators")),
        "series": (d.get("seriesPrimary") or {}).get("name", ""),
        "runtime_min": d.get("runtimeLengthMin"),
        "release_date": (d.get("releaseDate") or "")[:10],
        "publisher": d.get("publisherName", ""), "language": d.get("language", ""),
        "genres": names(d.get("genres")), "isbn13": to_isbn13(d.get("isbn") or ""),
        "cover_url": d.get("image", ""),
        "description": re.sub(r"<[^>]+>", "", str(d.get("summary") or d.get("description") or "")).strip(),
    }




def _prueba_del_fichero(cands, local):
    """
    La grabación que el fichero DEMUESTRA ser, o None.

    Prueba, no ranking. "La menos lejana de las que hay" no identifica nada:
    muchas lecturas libres no tienen equivalente comercial, y colocarles la
    grabación más parecida es dar gato por liebre. Sólo cuentan dos hechos:

      - el narrador de las etiquetas coincide con el de la grabación, o
      - la duración cuadra al 95%, que es lo que da un rip del original.

    Si dos grabaciones cumplen, tampoco vale: eso ya es una duda de verdad.
    """
    local = local or {}
    duracion = local.get('runtime_min')
    narrador = ', '.join(local.get('narrators') or []).strip()

    if not duracion and not narrador:
        return None

    def ratio(c):
        if not (duracion and c.get('runtime_min')):
            return 0.0
        a, b = int(duracion), int(c['runtime_min'])
        return (min(a, b) / max(a, b)) if max(a, b) else 0.0

    def mismo_narrador(c):
        if not (narrador and c.get('narrators')):
            return False
        return max(_title_score(narrador, n) for n in c['narrators']) >= 0.85

    coinciden = [c for c in cands if mismo_narrador(c) or ratio(c) >= 0.95]

    if not coinciden:
        return None

    if len(coinciden) == 1:
        return coinciden[0]

    # Varias cumplen. Suele pasar porque un mismo narrador tiene la lectura
    # suelta y además una recopilación con otros libros dentro: mismo nombre,
    # 309 minutos contra 769. La duración las separa sin dudar.
    ordenadas = sorted(coinciden, key=lambda c: (-int(mismo_narrador(c)), -ratio(c)))
    primera, segunda = ordenadas[0], ordenadas[1]

    if ratio(primera) >= 0.95 and ratio(primera) - ratio(segunda) >= 0.10:
        return primera

    # Empate de verdad: eso sí es una pregunta.
    return None


def _elegir_grabacion(cands, local, log):
    """
    De varias grabaciones del mismo libro, la que corresponde AL FICHERO.

    Un audiolibro no es como un libro: aquí el narrador sí distingue productos
    de verdad, y elegir mal es dar gato por liebre. Pero eso no obliga a
    preguntar, porque el fichero lleva la respuesta encima.

    La duración es el desempate bueno: es objetiva, viene gratis en la misma
    búsqueda de Audible y separa de verdad. Medido con *El arte de la guerra*,
    las cinco grabaciones duran 83, 120, 177, 184 y 277 minutos -- no hay
    empate posible contra un fichero de 129.
    """
    local = local or {}
    duracion = local.get('runtime_min')
    narrador = ', '.join(local.get('narrators') or []).strip()
    idioma = (local.get('language') or '').lower()

    def puntos(c):
        p = 0.0

        # El narrador declarado en las etiquetas es prueba directa.
        if narrador and c.get('narrators'):
            if max(_title_score(narrador, n) for n in c['narrators']) >= 0.85:
                p += 4.0

        if duracion and c.get('runtime_min'):
            # Por PROPORCIÓN y no por minutos: 20 minutos de diferencia son
            # ruido en una lectura de nueve horas y un abismo en una de dos.
            a, b = int(duracion), int(c['runtime_min'])
            r = min(a, b) / max(a, b) if max(a, b) else 0

            if r >= 0.95:
                p += 3.0
            elif r >= 0.85:
                p += 2.0
            elif r >= 0.70:
                p += 1.0
            elif r < 0.50:
                # Medido: contra un fichero de 129 minutos, una grabación de
                # 309 es otra lectura y una de 769 es una recopilación con
                # otros libros dentro. Ni una ni otra son esto.
                p -= 3.0

        if idioma and c.get('language') and c['language'].startswith(idioma[:2]):
            p += 0.5

        return p

    ordenados = sorted(cands, key=lambda c: (-puntos(c), -c['score']))
    g = ordenados[0]

    # "El mejor de los que hay" NO es lo mismo que "es éste". Muchas lecturas
    # libres -- de YouTube, de aficionados -- no tienen equivalente en Audible,
    # y ahí lo correcto es decir que no está, no colocar la grabación menos
    # lejana. Medido: contra un fichero de 129 minutos la más cercana dura 111,
    # que es un 14% menos: OTRA lectura, con otro narrador.
    #
    # Así que la elección automática exige una prueba, no un ranking: o el
    # narrador coincide con el que declaran las etiquetas, o la duración cuadra
    # de verdad.
    coincide_narrador = bool(
        narrador and g.get('narrators')
        and max(_title_score(narrador, n) for n in g['narrators']) >= 0.85)

    cuadra_duracion = False
    if duracion and g.get('runtime_min'):
        a, b = int(duracion), int(g['runtime_min'])
        cuadra_duracion = (min(a, b) / max(a, b)) >= 0.95 if max(a, b) else False

    if not (coincide_narrador or cuadra_duracion):
        log(f"ninguna de las {len(cands)} grabaciones cuadra con el fichero "
            f"({duracion or '?'} min): la más cercana dura {g.get('runtime_min') or '?'}. "
            f"Probablemente no esté en Audible")
        return None

    log(f"misma obra, {len(cands)} grabaciones -> elegida "
        f"{', '.join(g.get('narrators') or ['sin narrador'])} "
        f"({g.get('runtime_min') or '?'} min) contra {duracion or '?'} min del fichero")

    return g


def resolve_audiobook(title, author=None, region="es", asin_hint=None, log=None, local=None):
    log = log or (lambda m: None)

    hint = (asin_hint or "").strip().upper()
    if hint:
        rec = audnexus_book(hint, region, log)
        if rec:
            log(f"asin hint {hint} confirmed by audnexus")
            return _verdict("high", 1.0, rec, "asin supplied by uploader", asin=hint)

        log(f"asin hint {hint} unknown to audnexus in region {region}")

    # Misma cascada que en resolve_book, y por el mismo motivo: las etiquetas
    # de un audiolibro traen el idioma y el bitrate pegados al título, y
    # Audible no conoce ningún libro llamado "... (Audiolibro en Castellano)".
    scored = []
    consultado = title

    for variante in query_variants(title, author):
        products = _audible_search(variante, author, region, log)
        if not products:
            continue

        for p in products:
            score = _title_score(variante, p["title"])
            if p["subtitle"]:
                score = max(score, _title_score(variante, f"{p['title']} {p['subtitle']}"))

            # Contención, igual que en juegos: la consulta lleva el prefijo del
            # autor ("Sun Tzu - El Arte de la Guerra") y Audible no, así que la
            # similitud cruda se quedaba en 0.880 para las TRES grabaciones y
            # ninguna llegaba al umbral. Si las palabras de una están dentro de
            # la otra, es el mismo libro con adornos.
            if _mismo_trabajo(variante, p["title"]):
                score = max(score, 0.95)
            if author and p["authors"] and max(_title_score(author, a) for a in p["authors"]) >= 0.85:
                score += 0.05
            score = max(0.0, min(1.0, score))
            if score >= MIN_CANDIDATE_SCORE:
                scored.append(dict(p, score=score))

        if scored:
            consultado = variante
            if variante != title:
                log(f"variante '{variante}' dio {len(scored)} candidatos en audible")
            break

    if not scored:
        log(f"no audiobook candidate above threshold for '{title}'")
        return _verdict("none", 0.0, None, "no candidate scored high enough")

    scored.sort(key=lambda c: -c["score"])
    best = scored[0]
    lead = best["score"] - scored[1]["score"] if len(scored) > 1 else 1.0

    rec = audnexus_book(best["asin"], region, log)          # only the winner costs a second hop
    if not rec:
        return _verdict("none", best["score"], None,
                        "audnexus has no record for the best match", asin=best["asin"])

    if best["score"] >= TRUST_SCORE and lead >= LEAD_MARGIN:
        conf, reason = "high", "coincidencia única y clara"
    elif best["score"] >= TRUST_SCORE and _prueba_del_fichero(scored, local):
        # La prueba NO depende de que las demás candidatas sean la misma obra.
        # Estaba atado a eso y era un error: las tres grabaciones de *El arte
        # de la guerra* son productos distintos --dos lecturas y una
        # recopilación-- así que nunca agrupaban, y un fichero que cuadraba al
        # minuto con una de ellas seguía preguntando.
        #
        # Que la duración o el narrador del fichero coincidan con UNA es
        # prueba de por sí, sin importar qué sean las otras.
        best = _prueba_del_fichero(scored, local)
        rec = audnexus_book(best["asin"], region, log)

        if not rec:
            return _verdict("none", best["score"], None,
                            "audnexus no tiene ficha de la grabación elegida",
                            asin=best["asin"])

        log(f"'{title}' -> high asin={best['asin']} "
            f"({', '.join(best.get('narrators') or ['sin narrador'])}, "
            f"{best.get('runtime_min')} min) por coincidir con el fichero")

        return _verdict("high", best["score"], rec,
                        "la duración o el narrador del fichero identifican la grabación",
                        asin=best["asin"])
    elif False:
        # Mismo libro, varias lecturas. El narrador importa de verdad -- por
        # eso no vale coger la primera -- pero el fichero trae con qué
        # decidir. Y si NO lo trae, no se decide solo: se pregunta. Elegir
        # narrador a ciegas es dar gato por liebre.
        elegida = _elegir_grabacion(
            _misma_obra([c for c in scored if c["score"] >= TRUST_SCORE]), local, log)

        if elegida is None:
            # Ninguna grabación cuadra: no es que haya duda de cuál, es que
            # probablemente ésta no esté en el catálogo.
            return _verdict("none", best["score"], None,
                            "el libro está en Audible pero ninguna grabación "
                            "coincide con la duración de este fichero")

        best = elegida
        rec = audnexus_book(best["asin"], region, log)
        if not rec:
            return _verdict("none", best["score"], None,
                            "audnexus no tiene ficha de la grabación elegida",
                            asin=best["asin"])
        log(f"resolved audiobook '{title}' -> high asin={best['asin']}")
        return _verdict("high", best["score"], rec,
                        "misma obra en varias grabaciones; elegida por duración",
                        asin=best["asin"])
    else:
        # Antes de rendirse a un `low`: si el fichero dice cuánto dura y
        # NINGUNA grabación se le parece, no es que haya duda de cuál es --
        # es que no está. Enseñar como "mejor apuesta" una lectura de 309
        # minutos para un fichero de 129 es peor que no enseñar nada.
        duracion = (local or {}).get('runtime_min')

        if duracion:
            mejor_ratio = 0.0
            for c in scored:
                if not c.get('runtime_min'):
                    continue
                a, b = int(duracion), int(c['runtime_min'])
                if max(a, b):
                    mejor_ratio = max(mejor_ratio, min(a, b) / max(a, b))

            # 0.90 y no menos: un rip de un audiolibro comprado cuadra con
            # el original al 97-99%, así que exigir un 90% sigue siendo
            # generoso. Con 0.85 se colaba una lectura un 14% más corta, que
            # es otro narrador leyendo otra edición.
            if mejor_ratio and mejor_ratio < 0.90:
                log(f"'{title}': el libro está en Audible pero ninguna grabación "
                    f"se acerca a los {duracion} min del fichero "
                    f"(la mejor se queda en {mejor_ratio:.0%})")
                return _verdict("none", 0.0, None,
                                f"ninguna grabación de Audible dura lo que este "
                                f"fichero ({duracion} min); probablemente sea una "
                                f"lectura libre sin equivalente comercial")

        conf = "low"
        reason = ("el mejor candidato no llega al umbral de confianza"
                  if best["score"] < TRUST_SCORE
                  else "varias grabaciones puntúan igual; el narrador lo elige una persona")

    log(f"resolved audiobook '{title}' -> {conf} asin={best['asin']} "
        f"score={best['score']:.3f} lead={lead:.3f} ({reason})")

    return _verdict(conf, best["score"], rec, reason, asin=best["asin"])


def _verdict(confidence, score, record, reason, asin=None):
    return {
        "confidence": confidence,
        "score": round(float(score), 3),
        "isbn13": (record or {}).get("isbn13", "") if record else "",
        "asin": asin or (record or {}).get("asin", "") if record else "",
        "record": record,
        "reason": reason,
    }


if __name__ == "__main__":
    import os
    import sys
    import json

    # Run either as `python3 src/book_resolver.py` from RawLoadrr/ or directly
    # from inside src/; the config lives one level up from this file either way.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data.config import config as _cfg

    kind = sys.argv[1] if len(sys.argv) > 1 else "book"
    _title = sys.argv[2] if len(sys.argv) > 2 else "El nombre del viento"
    _author = sys.argv[3] if len(sys.argv) > 3 else None

    fn = resolve_book if kind == "book" else resolve_audiobook
    out = fn(_title, _author, config=_cfg, log=print) if kind == "book" \
        else fn(_title, _author, log=print)
    print(json.dumps({k: v for k, v in out.items() if k != "record"}, indent=2, ensure_ascii=False))
