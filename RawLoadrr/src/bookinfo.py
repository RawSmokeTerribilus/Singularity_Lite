"""
Local metadata extraction for e-books and audiobooks.

Deliberately dependency-free beyond what the image already ships. An .epub is
a zip with an OPF manifest inside, so zipfile + ElementTree from the standard
library read it; .m4b tags come from tinytag, which is already pinned in
requirements.txt for the music path. Adding ebooklib or mutagen for this would
be a new dependency to maintain for two functions.

Everything here is best-effort: a scanned .cbz has no metadata at all, and a
DRM-stripped .azw3 may have none either. Returning an empty dict is a normal
outcome, not a failure — the resolver falls back to the filename and, past
that, to asking the operator.
"""

import os
import re
import zipfile
import xml.etree.ElementTree as ET

EBOOK_EXTS = ('.epub', '.mobi', '.azw3', '.azw', '.pdf', '.cbz', '.cbr', '.djvu', '.fb2')

# Formatos que sólo son audiolibro. `.m4b` es el contenedor con capítulos, y
# `.aax`/`.aa` son los de Audible.
AUDIOBOOK_EXTS_AUTO = ('.m4b', '.aax', '.aa')

# Y los que también son música, que en la práctica son la MAYORÍA de los
# audiolibros que circulan: medido en la caja, el primero que se probó era un
# `.m4a` y no lo veía nadie. Aquí no vale la extensión sola.
AUDIOBOOK_EXTS_AMBIGUOUS = ('.m4a', '.mp3', '.ogg', '.opus', '.flac', '.wma')

AUDIOBOOK_EXTS = AUDIOBOOK_EXTS_AUTO + AUDIOBOOK_EXTS_AMBIGUOUS

# Un audiolibro se anuncia en el nombre mucho más a menudo que un disco.
_PISTA_AUDIOLIBRO = re.compile(
    r'audiolibro|audio[\s._-]?book|narrado|unabridged|abridged|voz[\s._-]?humana',
    re.IGNORECASE)

# Dublin Core lives here inside every OPF package document.
_DC = '{http://purl.org/dc/elements/1.1/}'


def is_ebook(path):
    return str(path).lower().endswith(EBOOK_EXTS)


def is_audiobook_file(path):
    return str(path).lower().endswith(AUDIOBOOK_EXTS)


def _clean_title(raw):
    """
    Publishers and conversion tools put remarkable things in dc:title.

    Seen in the wild on this box: a technical e-book whose OPF title was the
    shell-escaped filename, extension included --
    "Fundamentals of Electric Circuits \\(5th ed\\) \\( PDFDrive.com \\).epub".
    Handing that to a metadata provider guarantees a miss, so the obvious
    damage is undone here: backslash escapes dropped, a trailing e-book
    extension removed, whitespace collapsed.
    """
    title = str(raw or '').strip()

    if not title:
        return ''

    title = re.sub(r'\\(.)', r'\1', title)                      # \( -> (
    title = re.sub(r'(?i)\.(epub|mobi|azw3?|pdf|cbz|cbr|djvu|fb2)$', '', title).strip()

    return re.sub(r'\s+', ' ', title)


def _clean_isbn(raw):
    """Pull an ISBN out of whatever string a publisher put in the identifier."""
    digits = re.sub(r'[^0-9Xx]', '', str(raw or '')).upper()
    return digits if len(digits) in (10, 13) else ''


def from_epub(path):
    """
    Title / authors / publisher / year / ISBN / language out of an .epub.

    The OPF path is declared in META-INF/container.xml rather than being fixed,
    so it has to be read rather than guessed.
    """
    out = {}

    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read('META-INF/container.xml'))
            rootfile = container.find('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')

            if rootfile is None:
                return out

            opf = ET.fromstring(z.read(rootfile.attrib['full-path']))
    except Exception:                                           # noqa: BLE001
        return out

    def _text(tag):
        el = opf.find('.//' + _DC + tag)
        return (el.text or '').strip() if el is not None and el.text else ''

    out['title'] = _clean_title(_text('title'))
    out['publisher'] = _text('publisher')
    out['language'] = _text('language')

    authors = [
        (el.text or '').strip()
        for el in opf.findall('.//' + _DC + 'creator')
        if el.text and el.text.strip()
    ]

    if authors:
        out['authors'] = authors

    date = _text('date')

    if re.search(r'(\d{4})', date):
        out['year'] = int(re.search(r'(\d{4})', date).group(1))

    for el in opf.findall('.//' + _DC + 'identifier'):
        isbn = _clean_isbn(el.text)

        if isbn:
            out['isbn'] = isbn
            break

    return {k: v for k, v in out.items() if v}


def from_audiobook(path):
    """
    Title / author / narrator / ASIN out of an .m4b, via tinytag.

    Audible files carry the ASIN in a tag often enough to be worth trying: it
    turns a fuzzy title search into an exact lookup.
    """
    out = {}

    try:
        from tinytag import TinyTag
        tag = TinyTag.get(path)
    except Exception:                                           # noqa: BLE001
        return out

    if getattr(tag, 'album', None):
        out['title'] = _clean_title(tag.album)       # audiobooks put the book in album
    elif getattr(tag, 'title', None):
        out['title'] = _clean_title(tag.title)

    if getattr(tag, 'artist', None):
        out['authors'] = [str(tag.artist).strip()]

    if getattr(tag, 'composer', None):
        out['narrators'] = [str(tag.composer).strip()]   # the usual home for the narrator

    if getattr(tag, 'year', None) and re.search(r'(\d{4})', str(tag.year)):
        out['year'] = int(re.search(r'(\d{4})', str(tag.year)).group(1))

    if getattr(tag, 'duration', None):
        out['runtime_min'] = int(tag.duration // 60)

    # tinytag exposes anything it does not model under `extra`; Audible's ASIN
    # shows up there under a handful of different names.
    extra = getattr(tag, 'extra', None) or {}

    for key in ('asin', 'ASIN', 'audible_asin', 'cdek'):
        value = extra.get(key)

        if value and re.fullmatch(r'[A-Za-z0-9]{10}', str(value).strip()):
            out['asin'] = str(value).strip().upper()
            break

    return {k: v for k, v in out.items() if v}


def from_calibre_sidecar(path):
    """
    Read the metadata.opf Calibre drops next to every book it manages.

    Not a personal convenience: Calibre is close to universal among people who
    keep an e-book library, so this helps any user of the suite, not one.

    Measured over 300 sidecars from a real library: title and author 100%,
    publisher and tags 94%, series 38% -- but ISBN only 1%, exactly like the
    embedded OPF. So this is not an identification shortcut; it is where the
    series name and the subject tags come from, and those go in the upload
    description.
    """
    sidecar = os.path.join(os.path.dirname(str(path)), 'metadata.opf')

    if not os.path.isfile(sidecar):
        return {}

    try:
        root = ET.parse(sidecar).getroot()
    except Exception:                                           # noqa: BLE001
        return {}

    out = {}

    title = root.find('.//' + _DC + 'title')

    if title is not None and title.text:
        out['title'] = _clean_title(title.text)

    authors = [
        (el.text or '').strip()
        for el in root.findall('.//' + _DC + 'creator')
        if el.text and el.text.strip()
    ]

    if authors:
        out['authors'] = authors

    publisher = root.find('.//' + _DC + 'publisher')

    if publisher is not None and publisher.text:
        out['publisher'] = publisher.text.strip()

    tags = [
        (el.text or '').strip()
        for el in root.findall('.//' + _DC + 'subject')
        if el.text and el.text.strip()
    ]

    if tags:
        out['tags'] = tags[:25]

    for el in root.findall('.//' + _DC + 'identifier'):
        blob = (el.text or '') + ' ' + ' '.join(el.attrib.values())

        if 'isbn' in blob.lower():
            isbn = _clean_isbn(el.text)

            if isbn:
                out['isbn'] = isbn
                break

    # Series lives in an opf:meta element, not in Dublin Core.
    for meta_el in root.findall('.//{http://www.idpf.org/2007/opf}meta'):
        name = meta_el.attrib.get('name')

        if name == 'calibre:series' and meta_el.attrib.get('content'):
            out['series'] = meta_el.attrib['content'].strip()
        elif name == 'calibre:series_index' and meta_el.attrib.get('content'):
            out['series_index'] = meta_el.attrib['content'].strip()

    return {k: v for k, v in out.items() if v}


def from_filename(path):
    """
    Last resort: "Author - Title (Year)" is the convention this suite writes,
    so it is also the one most likely to come back in.
    """
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    out = {}

    year = re.search(r'\((\d{4})\)', stem)

    if year:
        out['year'] = int(year.group(1))
        stem = stem[:year.start()].strip()

    if ' - ' in stem:
        author, _, title = stem.partition(' - ')

        if author.strip():
            out['authors'] = [author.strip()]

        out['title'] = title.strip()
    else:
        out['title'] = stem.strip()

    return {k: v for k, v in out.items() if v}


def gather(path):
    """
    Local metadata for one file, best source first, filename as the floor.

    Cada campo se anota con su ORIGEN en `_origen`, y eso no es adorno: un
    título sacado del nombre del fichero -- "el-arte-de-la-guerra" -- vale
    mucho menos que el que declara un EPUB en su Dublin Core, y quien decida
    después si el proveedor puede pisarlo necesita saber cuál de los dos tiene
    delante. Sin esta pista, un PDF sin metadatos acababa subido como
    "- el-arte-de-la-guerra (2020) [PDF]".
    """
    path = str(path)

    if is_audiobook_file(path):
        found = from_audiobook(path)
        origen = 'etiquetas'
    elif path.lower().endswith('.epub'):
        found = from_epub(path)
        origen = 'epub'
    else:
        found = {}
        origen = None

    procedencia = {k: origen for k in found} if origen else {}

    # A Calibre sidecar is hand-curated often enough to outrank what the file
    # itself claims, so it fills gaps before the filename does.
    for fuente, etiqueta in ((from_calibre_sidecar(path), 'calibre'),
                             (from_filename(path), 'nombre')):
        for key, value in fuente.items():
            if key not in found:
                found[key] = value
                procedencia[key] = etiqueta

    if procedencia:
        found['_origen'] = procedencia

    return found

# ─── analisis del fichero: el equivalente de mediainfo ───────────────────────
def analyze(path):
    """
    Qué es este fichero, técnicamente. No qué obra es -- eso lo dicen los
    proveedores -- sino qué estás descargando.

    El equivalente exacto de mediainfo: en vídeo miras códec y bitrate para
    saber si te dan gato por liebre; en un libro la pregunta es la misma y la
    respuesta está en la proporción de texto. Un .epub con 2% de texto es un
    volcado de imágenes con extensión bonita: no se busca dentro, no se copia
    una línea y no se reajusta a la pantalla.

    Sin dependencias: zipfile y expresiones regulares de la stdlib.
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()

    try:
        size = os.path.getsize(path)
    except OSError:
        return {}

    out = {'formato': ext.lstrip('.').upper(), 'bytes': size}

    if ext == '.epub':
        out.update(_analyze_epub(path))
    elif ext == '.pdf':
        out.update(_analyze_pdf(path))
    elif ext in ('.cbz', '.cbr'):
        out.update(_analyze_comic(path))
    elif ext in AUDIOBOOK_EXTS:
        out.update(_analyze_audio(path))

    return out


def _analyze_epub(path):
    try:
        z = zipfile.ZipFile(path)
        infos = z.infolist()
    except Exception:                                           # noqa: BLE001
        return {}

    img_re  = re.compile(r'\.(jpe?g|png|gif|svg|webp)$', re.I)
    txt_re  = re.compile(r'\.(x?html?|xml)$', re.I)
    font_re = re.compile(r'\.(ttf|otf|woff2?)$', re.I)

    imagenes = [i for i in infos if img_re.search(i.filename)]
    textos   = [i for i in infos if txt_re.search(i.filename) and 'container' not in i.filename]
    fuentes  = [i for i in infos if font_re.search(i.filename)]

    total = sum(i.file_size for i in infos) or 1
    texto = sum(i.file_size for i in textos)

    nombres = [i.filename.lower() for i in infos]
    drm = any('encryption.xml' in n for n in nombres)

    # El maquetado fijo lo declara el OPF. Es peor para leer: no se reajusta.
    fijo = False
    version = ''
    for i in infos:
        if i.filename.endswith('.opf'):
            try:
                opf = z.read(i.filename)
            except Exception:                                   # noqa: BLE001
                break
            fijo = b'pre-paginated' in opf
            m = re.search(rb'version="(\d)', opf)
            if m:
                version = m.group(1).decode()
            break

    return {
        'version': version,
        'maquetado': 'fijo' if fijo else 'reflowable',
        'drm': drm,
        'capitulos': len(textos),
        'imagenes': len(imagenes),
        'fuentes': len(fuentes),
        'pct_texto': round(100 * texto / total),
    }


def _analyze_pdf(path):
    try:
        with open(path, 'rb') as fh:
            d = fh.read()
    except OSError:
        return {}

    paginas  = len(re.findall(rb'/Type\s*/Page[^s]', d))
    imagenes = len(re.findall(rb'/Subtype\s*/Image', d))

    # `/Type` es OPCIONAL dentro de un diccionario de fuente, así que buscar
    # `/Type /Font` se deja fuera a un PDF con texto perfectamente normal.
    # Medido: el de Omegalfa trae 57 referencias `/Font` y ninguna `/Type
    # /Font`, y se anunciaba como escaneo en la descripción pública.
    fuentes = max(
        len(re.findall(rb'/Type\s*/Font', d)),
        len(re.findall(rb'/BaseFont', d)),
        len(re.findall(rb'/Font\b', d)),
    )

    out = {'paginas': paginas, 'imagenes': imagenes, 'fuentes': fuentes}

    if fuentes:
        out['capa_texto'] = True
        return out

    # Sin rastro de fuentes hay dos explicaciones, y no son lo mismo: que no
    # las haya, o que estén dentro de un stream de objetos comprimido y desde
    # aquí no se vean. Si el PDF usa /ObjStm estamos ciegos, y afirmar
    # "escaneo" sería inventarse un defecto. Mejor no decir nada.
    if re.search(rb'/ObjStm', d):
        return out

    out['capa_texto'] = False
    out['escaneo'] = imagenes > 0

    return out


def _analyze_comic(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception:                                           # noqa: BLE001
        return {}                                               # .cbr es RAR, no zip

    img_re = re.compile(r'\.(jpe?g|png|webp|gif)$', re.I)
    paginas = [i for i in z.infolist() if img_re.search(i.filename)]

    if not paginas:
        return {}

    tam = sorted(i.file_size for i in paginas)

    return {
        'paginas': len(paginas),
        'kib_por_pagina': round(sum(tam) / len(tam) / 1024),
        'formato_imagen': os.path.splitext(paginas[0].filename)[1].lstrip('.').upper(),
    }


def looks_like_audiobook(path, declared=False):
    """
    ¿Este fichero de audio es un audiolibro y no música?

    La extensión no lo dice: un `.m4a` de tres horas y una canción de tres
    minutos son el mismo formato. Se mira, por orden de fiabilidad:

      1. El contenedor, si es de los que sólo se usan para esto.
      2. Que quien sube lo haya declarado.
      3. El nombre o la carpeta -- "(Audiolibro en Castellano)" es lo que
         traía el primero que se probó de verdad.
      4. Un ASIN de Audible en las etiquetas, que es prueba directa.
      5. La duración, y sólo como respaldo: 45 minutos en UN fichero no es
         una canción. No basta sola -- una sesión de DJ también dura eso --
         así que sólo cuenta si además hay un libro al lado.
    """
    lower = str(path).lower()

    if lower.endswith(AUDIOBOOK_EXTS_AUTO):
        return True

    if not lower.endswith(AUDIOBOOK_EXTS_AMBIGUOUS):
        return False

    if declared:
        return True

    if _PISTA_AUDIOLIBRO.search(str(path)):
        return True

    tags = from_audiobook(path)

    if tags.get('asin'):
        return True

    # El respaldo: largo Y con un libro de la misma obra al lado.
    if (tags.get('runtime_min') or 0) >= 45:
        try:
            carpeta = os.path.dirname(str(path))
            hermanos = os.listdir(carpeta)
        except OSError:
            return False

        return any(h.lower().endswith(EBOOK_EXTS) for h in hermanos)

    return False


# ─── el título original, que el propio fichero declara ───────────────────────
#
# Medido: el 52% de los epubs de la biblioteca traen "Título original" en su
# página de créditos. Cuando el libro es una traducción, ése es el título que
# Google Books indexa y el español no existe -- las traducciones de aficionados
# no llegan a tener ISBN propio, pero el original sí.
#
# Y a diferencia del puente de Wikipedia que hubo que montar para los juegos,
# aquí NO se infiere nada ni se encadena confianza: es el fichero el que
# afirma la equivalencia. No hay salto que demostrar.
_TITULO_ORIGINAL = re.compile(
    r'T[íi]tulo\s+original\s*:?\s*(?:</[^>]+>\s*)?(?:<[^>]+>\s*)*([^<\n\r]{3,90})',
    re.IGNORECASE)


def original_title(path):
    """El título original declarado dentro del epub, o ''."""
    if not str(path).lower().endswith('.epub'):
        return ''

    try:
        z = zipfile.ZipFile(path)
    except Exception:                                           # noqa: BLE001
        return ''

    # La página de créditos suele ir de las primeras; no hace falta leer el
    # libro entero para encontrarla.
    candidatos = [n for n in z.namelist()
                  if n.lower().endswith(('.opf', '.xhtml', '.html', '.htm'))]

    for name in candidatos[:40]:
        try:
            texto = z.read(name).decode('utf-8', 'ignore')
        except Exception:                                       # noqa: BLE001
            continue

        m = _TITULO_ORIGINAL.search(texto)
        if not m:
            continue

        titulo = _clean_title(re.sub(r'&[a-z]+;|&#\d+;', ' ', m.group(1)))
        titulo = re.sub(r'\s+', ' ', titulo).strip(' .,;:-')

        if len(titulo) >= 3:
            return titulo

    return ''


def agrupar_audiolibros(carpeta, nombres):
    """
    Reparte los ficheros de audio de una carpeta en obras.

    -> lista de listas de nombres; cada lista es un audiolibro.

    Varios ficheros juntos son dos cosas opuestas: los capítulos de UNA obra,
    o varias obras sueltas. Los capítulos comparten el título en las etiquetas
    -- ahí va el nombre del libro, no el del capítulo -- y las obras distintas
    no. Medido con tres .m4b de Laura Gallego en una carpeta: tres títulos
    distintos, tres torrents.

    Sin etiquetas legibles se agrupa por nombre de fichero: es peor, pero un
    fichero suelto por obra falla menos que meterlo todo en el mismo saco.
    """
    import os as _os

    por_titulo = {}

    for nombre in nombres:
        etiquetas = from_audiobook(_os.path.join(carpeta, nombre))
        clave = (etiquetas.get('title') or '').strip().lower()

        if not clave:
            clave = _os.path.splitext(nombre)[0].lower()

        por_titulo.setdefault(clave, []).append(nombre)

    return [sorted(v) for v in por_titulo.values()]


# Códec por contenedor. No se deduce del bitrate ni se adivina: si el
# contenedor no lo dice de forma inequívoca, no se afirma.
_CODEC_POR_EXT = {
    '.m4b': 'AAC', '.m4a': 'AAC', '.aax': 'AAC', '.aa': 'AAC (Audible)',
    '.mp3': 'MP3', '.ogg': 'Vorbis', '.opus': 'Opus', '.flac': 'FLAC',
    '.wma': 'WMA',
}


def _duracion_legible(segundos):
    """22952 -> '6 h 22 min'. Un audiolibro se mide en horas, no en segundos."""
    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos = resto // 60

    if horas:
        return f"{horas} h {minutos:02d} min"

    return f"{minutos} min {segundos % 60:02d} s"


def _capitulos_m4b(path):
    """
    Cuántos capítulos declara un MP4, o None si no se puede saber.

    Los .m4b los guardan en un átomo `chpl` (el de Nero) o en una pista de
    capítulos. Se cuenta el `chpl` porque es el que se lee sin descomprimir
    nada; si no está, se calla en vez de decir cero, que no es lo mismo que
    "no lo sé".
    """
    try:
        with open(path, 'rb') as fh:
            cabeza = fh.read(4 * 1024 * 1024)
            fh.seek(max(0, os.path.getsize(path) - 4 * 1024 * 1024))
            cola = fh.read(4 * 1024 * 1024)
    except OSError:
        return None

    for trozo in (cabeza, cola):
        i = trozo.find(b'chpl')
        if i == -1:
            continue

        # El `chpl` de Nero no tiene una sola forma: según la versión, la
        # cuenta va en un byte o en cuatro, y el hueco reservado de delante
        # cambia. Leer un desplazamiento fijo daba 301.989.888 capítulos.
        #
        # Así que se prueban los sitios donde puede estar y se acepta el
        # primero que sea PLAUSIBLE. Un audiolibro tiene capítulos, no
        # millones: fuera de 1..2000 no es la cuenta, es basura leída de
        # cualquier otro sitio del fichero, y entonces se calla.
        for desplazamiento, ancho in ((8, 1), (12, 1), (13, 1), (8, 4), (12, 4)):
            try:
                n = int.from_bytes(trozo[i + desplazamiento:i + desplazamiento + ancho], 'big')
            except Exception:                                   # noqa: BLE001
                continue

            if 1 <= n <= 2000:
                return n

        return None

    return None


def _analyze_audio(path):
    """
    Lo que se está descargando, técnicamente.

    El equivalente del mediainfo de una peli: cuánto dura, a qué bitrate, con
    qué frecuencia y cuántos canales. Antes esta rama no existía y un
    audiolibro se anunciaba sólo con su formato y su tamaño, que no dice nada
    de la calidad de la lectura.
    """
    out = {}

    try:
        from tinytag import TinyTag
        tag = TinyTag.get(path)
    except Exception:                                           # noqa: BLE001
        return out

    ext = os.path.splitext(path)[1].lower()

    if _CODEC_POR_EXT.get(ext):
        out['codec'] = _CODEC_POR_EXT[ext]

    if getattr(tag, 'duration', None):
        out['duracion'] = _duracion_legible(tag.duration)
        out['duracion_min'] = int(tag.duration // 60)

    if getattr(tag, 'bitrate', None):
        out['bitrate'] = f"{int(round(tag.bitrate))} kbps"

    if getattr(tag, 'samplerate', None):
        out['frecuencia'] = f"{int(tag.samplerate)} Hz"

    if getattr(tag, 'channels', None):
        canales = int(tag.channels)
        out['canales'] = {1: 'mono', 2: 'estéreo'}.get(canales, f"{canales} canales")

    capitulos = _capitulos_m4b(path) if ext in ('.m4b', '.m4a', '.aax') else None
    if capitulos:
        out['capitulos'] = capitulos

    # Cuántos MB por hora: es la cifra que de verdad compara dos lecturas del
    # mismo libro, porque el tamaño suelto sólo dice lo larga que es.
    if out.get('duracion_min') and out['duracion_min'] > 0:
        mb_hora = (os.path.getsize(path) / (1024 * 1024)) / (out['duracion_min'] / 60)
        out['mb_por_hora'] = f"{mb_hora:.1f}"

    return out
