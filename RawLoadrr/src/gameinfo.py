# -*- coding: utf-8 -*-
"""
gameinfo -- qué es este juego, técnicamente.

El equivalente de mediainfo para un juego, y el hermano de bookinfo. No dice
qué obra es -- eso lo dice IGDB -- sino qué estás descargando: en qué sistema
corre, cuántos ficheros trae y con qué CRC32.

El CRC32 no es decoración. Es la forma en que se identifica una ROM: los
catálogos de MAME y de No-Intro se indexan por él, y dos volcados del mismo
juego que difieren en un byte tienen CRC distinto. Es lo único de esta ficha
que un tercero puede verificar sin bajarse el fichero.

Sin dependencias: zipfile, zlib y la stdlib. Los .zip se leen del directorio
central, así que el CRC de un archivo de 4 GB sale sin descomprimir nada.
"""

import os
import re
import zipfile
import zlib

ARCHIVE_EXTS = ('.zip',)
DISC_EXTS = ('.chd', '.cue', '.bin', '.gdi', '.iso', '.img', '.ccd')

# Extensiones que sólo pueden ser un juego. Se detectan SOLAS, sin que nadie
# tenga que decir `--category game`: tirarle un directorio y que funcione es la
# premisa de la suite, y los libros ya se comportan así.
#
# Ninguna de éstas se la quita a nadie: el barrido de vídeo sólo mira
# mkv/mp4/avi/ts/m2ts/m4v, y el de música ficheros de audio.
#
# LOS ENVASES NO ESTÁN AQUÍ, Y ES DELIBERADO. `.zip` y `.7z` estuvieron en esta
# lista, y mientras estuvieron **todo zip era un juego**: un pack de biblioteca
# de 300.000 libros se resolvía contra IGDB sin que nadie hubiera declarado
# nada. Un envase no tiene clase, tiene contenido; lo abre `container` y lo
# clasifica `library.classify_container()`. Lo mismo vale para las de disco de
# GAME_EXTS_EXPLICIT, que siguen abajo sólo para `--category game` a mano.
GAME_EXTS_AUTO = (
    '.chd', '.gdi', '.scummvm',
    '.nes', '.fds', '.sfc', '.smc', '.gba', '.gb', '.gbc',
    '.n64', '.z64', '.v64', '.gen', '.smd', '.32x', '.sms', '.gg',
    '.pce', '.sgx', '.a26', '.a78', '.lnx', '.ws', '.wsc', '.ngp', '.ngc',
    '.d64', '.t64', '.adf', '.nds', '.3ds', '.cia',
)

# Éstas exigen `--category game` explícito, porque colisionan:
#   .iso/.img/.ccd  -> son envases: dentro puede haber un DVD o un instalador.
#                      MKVerything ripea .iso como disco de vídeo, pero es un
#                      programa aparte que se lanza a mano con su propio
#                      --input/--output: RawLoadrr no lo llama nunca, así que
#                      esa colisión no ocurre en este camino. La razón real de
#                      que sigan aquí es que ES un envase y lo decide su
#                      contenido, no que alguien se lo vaya a llevar.
#   .cue/.bin       -> son también el sidecar de un rip de CD de audio
#   .rom/.dsk/.st/.tap/.z80/.tzx/.col/.int/.vec -> chocan con ficheros de datos
#                      corrientes; medido, un juego DOS traía 50 ".col" que eran
#                      paletas, no ROMs de ColecoVision
GAME_EXTS_EXPLICIT = DISC_EXTS + (
    '.col', '.int', '.vec', '.tap', '.dsk', '.st', '.z80', '.tzx', '.rom',
    # .md es Mega Drive, sí, pero antes que eso es Markdown: en la prueba se
    # tragó Home.md, Services.md y todos los README de la caja. Las ROMs de
    # Mega Drive vienen casi siempre en .gen, .smd o dentro de un zip.
    '.md',
)

GAME_EXTS = tuple(dict.fromkeys(GAME_EXTS_AUTO + GAME_EXTS_EXPLICIT))

# Extensión -> sistema. Sólo lo que es inequívoco: .iso y .bin salen de aquí a
# propósito, porque los comparten media docena de plataformas y adivinar es
# peor que callarse.
_BY_EXT = {
    '.nes': 'Nintendo NES', '.fds': 'Nintendo FDS',
    '.sfc': 'Super Nintendo', '.smc': 'Super Nintendo',
    '.n64': 'Nintendo 64', '.z64': 'Nintendo 64', '.v64': 'Nintendo 64',
    '.gb': 'Game Boy', '.gbc': 'Game Boy Color', '.gba': 'Game Boy Advance',
    '.nds': 'Nintendo DS', '.3ds': 'Nintendo 3DS', '.cia': 'Nintendo 3DS',
    '.md': 'Sega Mega Drive', '.gen': 'Sega Mega Drive', '.smd': 'Sega Mega Drive',
    '.32x': 'Sega 32X', '.sms': 'Sega Master System', '.gg': 'Sega Game Gear',
    '.gdi': 'Sega Dreamcast',
    '.pce': 'PC Engine', '.sgx': 'SuperGrafx',
    '.a26': 'Atari 2600', '.a78': 'Atari 7800', '.lnx': 'Atari Lynx',
    '.st': 'Atari ST',
    '.ws': 'WonderSwan', '.wsc': 'WonderSwan Color',
    '.ngp': 'Neo Geo Pocket', '.ngc': 'Neo Geo Pocket Color',
    '.col': 'ColecoVision', '.int': 'Intellivision', '.vec': 'Vectrex',
    '.d64': 'Commodore 64', '.t64': 'Commodore 64', '.tap': 'Commodore 64',
    '.adf': 'Commodore Amiga',
    '.dsk': 'Amstrad CPC', '.z80': 'ZX Spectrum', '.tzx': 'ZX Spectrum',
    '.scummvm': 'ScummVM',
}

# Ficheros que delatan un directorio de juego de ScummVM. La lista corta y
# segura: son nombres propios de los motores de LucasArts y Sierra, no
# extensiones genéricas.
_SCUMMVM_MARKERS = (
    'monster.sou', 'resource.000', 'resource.map', 'sierra.exe',
    'comi.la0', 'atlantis.000', 'monkey.000', 'monkey2.000', 'tentacle.000',
    'indy3.000', 'loom.000', 'samnmax.000', 'ft.la0', 'dig.la0',
)

# Lo que cuenta como "una obra por fichero" al decidir si algo es un pack.
# Deliberadamente NO están las extensiones de tres letras ambiguas -- .col,
# .int, .st, .rom, .tap, .dsk, .bin -- porque colisionan con ficheros de datos
# corrientes: medido en la caja, "Spider-Man - The Sinister Six" trae 50 .col
# que son paletas de un juego DOS, no cincuenta ROMs de ColecoVision.
_PACK_UNITS = frozenset(ARCHIVE_EXTS) | {
    '.7z', '.chd',
    '.nes', '.fds', '.sfc', '.smc', '.n64', '.z64', '.v64',
    '.gb', '.gbc', '.gba', '.nds', '.3ds',
    '.md', '.gen', '.smd', '.32x', '.sms', '.gg',
    '.pce', '.sgx', '.a26', '.a78', '.lnx',
    '.ws', '.wsc', '.ngp', '.ngc', '.adf', '.d64',
}

# "Sony - PlayStation (A-L).zip" y demás: un pack por plataforma no es una
# obra, no tiene id de IGDB posible, y hay que decirlo en vez de inventarse uno.
_PACK_HINT = re.compile(
    r'\((?:[A-Z]\s*-\s*[A-Z]|\d+\s*of\s*\d+)\)|(?:best[\s_-]?set|full[\s_-]?set|romset|collection|colecci[oó]n)',
    re.IGNORECASE)


def is_game_file(path, explicit=False):
    """
    `explicit=True` sólo cuando quien sube ha dicho `--category game`: entonces
    entran también las extensiones ambiguas.
    """
    exts = GAME_EXTS if explicit else GAME_EXTS_AUTO
    return str(path).lower().endswith(exts)


def _crc32_of_file(path, chunk=1024 * 1024):
    """CRC32 en streaming; una ROM de PS2 no cabe en RAM y no hace falta."""
    crc = 0
    try:
        with open(path, 'rb') as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                crc = zlib.crc32(block, crc)
    except OSError:
        return ''
    return f"{crc & 0xFFFFFFFF:08X}"


def _entries_from_zip(path, limit=400):
    """
    El directorio central de un zip ya lleva el CRC32 de cada entrada, así que
    esto no descomprime ni un byte.
    """
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist()[:limit]:
                if info.is_dir():
                    continue
                out.append({
                    'nombre': info.filename,
                    'bytes': info.file_size,
                    'crc32': f"{info.CRC & 0xFFFFFFFF:08X}",
                })
    except Exception:                                           # noqa: BLE001
        return []
    return out


def _detect_system(path, entries):
    ext = os.path.splitext(str(path))[1].lower()

    if ext in _BY_EXT:
        return _BY_EXT[ext]

    names = [e['nombre'].lower() for e in entries]
    base = [n.rsplit('/', 1)[-1] for n in names]

    if any(m in base for m in _SCUMMVM_MARKERS):
        return 'ScummVM'

    # Un solo .scummvm ya identifica el juego, y no puede pasar por la regla de
    # mayoría de abajo: es UN fichero entre los cincuenta del juego.
    if any(n.endswith('.scummvm') for n in base):
        return 'ScummVM'

    # Un archivo cuyo contenido es todo del mismo sistema hereda su sistema.
    #
    # "Todo" hay que tomárselo en serio: bastaba con ser la única extensión
    # CONOCIDA para ganar, y así 50 paletas .col entre 1003 ficheros de un
    # juego DOS lo convertían en ColecoVision. Se exige mayoría.
    hits = [_BY_EXT[os.path.splitext(n)[1]] for n in names
            if os.path.splitext(n)[1] in _BY_EXT]
    inner = set(hits)
    if len(inner) == 1 and names and len(hits) >= max(1, len(names) // 2):
        return inner.pop()

    if ext == '.chd':
        return 'Disco (CHD)'
    if ext in ('.cue', '.bin', '.iso', '.img', '.ccd'):
        return 'Disco'

    return ''


def _looks_like_pack(path, entries):
    """
    Un pack por plataforma no es una obra. 27 zips llamados "Sony - PS1 (A-L)"
    no tienen id de IGDB, y forzar uno es peor que dejarlo vacío.
    """
    if _PACK_HINT.search(os.path.basename(str(path))):
        return True

    # Decenas de obras dentro de un mismo archivo tampoco son una obra. Los
    # sets por sistema medidos en la caja son zip-de-zips -- "Atari - 2600.zip"
    # trae 50 zips y el de MAME 375 -- así que contar sólo extensiones de ROM
    # los daba por juegos sueltos.
    inner = [e for e in entries
             if os.path.splitext(e['nombre'])[1].lower() in _PACK_UNITS]
    return len(inner) > 5


def analyze(path):
    """
    -> {'formato', 'bytes', 'sistema', 'es_pack', 'entradas': [...]}

    Devuelve {} si la ruta no se puede leer, igual que bookinfo.analyze().
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()

    if os.path.isdir(path):
        entries = []
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                total += size
                if len(entries) < 400:
                    entries.append({
                        'nombre': os.path.relpath(full, path),
                        'bytes': size,
                        # Un directorio de ScummVM son ficheros sueltos, así que
                        # aquí sí hay que leerlos para tener el CRC. Se limita
                        # a los pequeños: un CRC de 4 GB no vale la espera.
                        'crc32': _crc32_of_file(full) if size <= 64 * 1024 * 1024 else '',
                    })
        out = {'formato': 'CARPETA', 'bytes': total}
    else:
        try:
            total = os.path.getsize(path)
        except OSError:
            return {}

        if ext in ARCHIVE_EXTS:
            entries = _entries_from_zip(path)
        else:
            entries = [{
                'nombre': os.path.basename(path),
                'bytes': total,
                'crc32': _crc32_of_file(path) if total <= 4 * 1024 * 1024 * 1024 else '',
            }]

        out = {'formato': ext.lstrip('.').upper(), 'bytes': total}

    sistema = _detect_system(path, entries)
    if sistema:
        out['sistema'] = sistema

    if _looks_like_pack(path, entries):
        out['es_pack'] = True

    out['entradas'] = entries
    return out
