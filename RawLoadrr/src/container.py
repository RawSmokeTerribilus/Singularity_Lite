# -*- coding: utf-8 -*-
"""
container -- abrir un envase para ver qué lleva dentro.

Un contenedor no es una categoría: es un directorio que hay que abrir. Un
`.zip` puede ser un juego de ScummVM o una biblioteca de 300.000 libros, y una
`.iso` puede ser un DVD o el instalador de Red Alert 3. Decidir por la
extensión del envase es decidir por el color de la caja.

Este módulo NO clasifica -- eso es de `library`. Aquí sólo se abre y se
devuelve la lista de nombres de dentro.

Dos reglas que abaratan todo:

  1. **Sólo se leen NOMBRES, nunca los datos.** El directorio central de un zip
     ya trae nombres, tamaños y CRC; la tabla de un 7z va en la cabecera; el
     árbol de una ISO son unos KB de descriptores. Da igual que el envase pese
     100 GB.
  2. **`None` no es lo mismo que `[]`.** `None` es "no he podido abrirlo" y
     manda seguir bajando por la cascada hasta preguntarle al operador. `[]` es
     "lo he abierto y está vacío", que sí es un veredicto.

Backends, y por qué éstos:

  .zip            zipfile (stdlib)
  .iso .img       ISO9660 parseado aquí mismo (stdlib)
  .rar            rarfile -- Python puro, sin extensiones en C y sin binarios:
                  las cabeceras RAR3/RAR5 se leen en Python y el `unrar`
                  non-free sólo hace falta para EXTRAER, que aquí no se hace.
  .7z .cab .tar*  bsdtar si está; para .7z vale también p7zip (`7z l -slt`),
                  que está en muchas más máquinas

`py7zr` queda fuera a propósito: arrastra pyzstd, pybcj, pyppmd, inflate64,
brotli y pycryptodomex, todo extensiones en C, y requirements.txt promete por
escrito que una instalación normal de arm64 no compila nada.

Ninguna dependencia de aquí es obligatoria. Sin `rarfile` y sin `bsdtar` el
módulo sigue funcionando: abre zip e ISO, y de lo demás dice `None`, que es la
respuesta honesta y la que dispara la pregunta.
"""

import os
import shutil
import struct
import subprocess
import zipfile

# Cuántos nombres se leen como mucho. Para votar por mayoría sobra: 5.000
# entradas ya dan un veredicto estable, y un romset puede traer 200.000.
MAX_ENTRIES = 5000

# Hasta dónde se baja dentro de una ISO. Ocho niveles cubren cualquier
# instalador; más abajo sólo hay coste.
MAX_DEPTH = 8

# Envases que sabemos abrir, o que al menos sabemos que SON envases. Estar en
# esta lista no promete que `entries()` devuelva algo: promete que la extensión
# no dice nada de la clase de obra y que hay que mirar dentro.
CONTAINER_EXTS = (
    '.zip', '.7z', '.rar',
    '.iso', '.img',
    '.cab', '.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz',
)

# Envases que NO sabemos abrir de forma barata, y que por tanto van derechos a
# la pregunta. Se listan igualmente para que nadie los confunda con un fichero
# suelto de una clase concreta.
OPAQUE_EXTS = ('.cue', '.bin', '.ccd', '.nrg', '.mdf', '.mds')

# Lo que hay en la raíz de un disco de vídeo, y de nada más. Se comprueba
# aparte de las extensiones porque un DVD son cuatro directorios y un montón de
# .VOB, y `.VOB` no está en ninguna lista de extensiones de vídeo de la casa.
#
# EN ORDEN DE FUERZA, y el orden importa: `AUDIO_TS` a solas es un DVD-Audio,
# que es música y no vídeo. Un DVD de vídeo trae los dos directorios (el
# AUDIO_TS casi siempre vacío), así que hay que quedarse con el más fuerte de
# los encontrados y no con el primero que aparezca -- medido con una ISO de
# prueba, donde el recorrido devolvía "audio_ts" para un DVD de vídeo perfecto.
VIDEO_DISC_MARKERS = ('bdmv', 'video_ts', 'bdav')

# Éste NO implica vídeo por sí solo.
AUDIO_DISC_MARKER = 'audio_ts'

_SECTOR = 2048
_PVD_OFFSET = 16 * _SECTOR


def ext(path):
    """La extensión, con `.tar.gz` contando como una sola."""
    lower = str(path).lower()
    for compound in ('.tar.gz', '.tar.bz2', '.tar.xz'):
        if lower.endswith(compound):
            return compound
    return os.path.splitext(lower)[1]


# `_ext` se quedó como nombre privado en la primera versión y ya hay quien lo
# llama; el alias evita romperlo.
_ext = ext


def is_container(path):
    """¿Es un envase cuyo contenido decide la clase, y no su extensión?"""
    return ext(path) in CONTAINER_EXTS


def is_opaque(path):
    """¿Es un envase que no sabemos abrir barato? (`.cue`, `.bin`, `.nrg`...)"""
    return ext(path) in OPAQUE_EXTS


def is_wrapper(path):
    """Envase, se pueda abrir o no. Lo que NUNCA debe decidir por su extensión."""
    return is_container(path) or is_opaque(path)


# ─── backends ────────────────────────────────────────────────────────────────

def _zip_entries(path):
    """
    El directorio central de un zip ya lleva el nombre de cada entrada, así que
    esto no descomprime ni un byte ni lee el cuerpo del fichero.
    """
    with zipfile.ZipFile(path) as z:
        return [info.filename for info in z.infolist()[:MAX_ENTRIES]
                if not info.is_dir()]


def _rar_entries(path):
    """
    `rarfile` parsea las cabeceras en Python puro. El binario externo sólo
    entra a jugar al extraer, y aquí no se extrae nada.
    """
    try:
        import rarfile
    except ImportError:
        return None

    try:
        with rarfile.RarFile(path) as rf:
            return [info.filename for info in rf.infolist()[:MAX_ENTRIES]
                    if not info.is_dir()]
    except Exception:                                           # noqa: BLE001
        # Cabeceras cifradas, RAR partido sin la primera parte, fichero roto.
        return None


_BSDTAR = None


def _bsdtar():
    """`shutil.which` una vez por proceso, no una por fichero de la cola."""
    global _BSDTAR
    if _BSDTAR is None:
        _BSDTAR = shutil.which('bsdtar') or ''
    return _BSDTAR or None


def _bsdtar_entries(path):
    """
    libarchive sabe de 7z, cab y tar. Listar es leer la tabla de la cabecera:
    no descomprime.
    """
    exe = _bsdtar()
    if not exe:
        return None

    try:
        proc = subprocess.run(
            [exe, '-tf', str(path)],
            capture_output=True, text=True, errors='replace',
            stdin=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None

    nombres = [line.rstrip('/') for line in proc.stdout.splitlines() if line.strip()]
    return nombres[:MAX_ENTRIES]


_SIETEZ = None


def _7z():
    """p7zip. Está en muchas más máquinas que libarchive-tools."""
    global _SIETEZ
    if _SIETEZ is None:
        _SIETEZ = shutil.which('7z') or shutil.which('7za') or shutil.which('7zr') or ''
    return _SIETEZ or None


def _7z_entries(path):
    """
    Respaldo de `bsdtar` para `.7z`.

    Se usa `-slt`, que saca un `Path = ...` por entrada, en vez de la tabla
    bonita de `7z l`: la tabla lleva cabecera, pie y anchuras que cambian entre
    versiones, y un nombre con espacios la parte mal.
    """
    exe = _7z()
    if not exe:
        return None

    try:
        proc = subprocess.run(
            [exe, 'l', '-slt', '-ba', '-p', str(path)],
            capture_output=True, text=True, errors='replace',
            stdin=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None

    nombres = [line[7:].strip() for line in proc.stdout.splitlines()
               if line.startswith('Path = ')]
    return nombres[:MAX_ENTRIES] or None


# ─── ISO9660 ─────────────────────────────────────────────────────────────────
#
# Se parsea a mano en vez de tirar de pycdlib porque son cuatro campos y una
# dependencia menos que pinar y vigilar en tres arquitecturas.
#
# Se prefiere Joliet cuando está: el descriptor primario guarda los nombres en
# 8.3 MAYÚSCULAS con un ";1" pegado detrás, y ahí un "novela.epub" llega como
# "NOVELA.EPU;1" -- extensión truncada, justo el dato que veníamos a buscar.
# Joliet los trae enteros en UCS-2BE. Sin Joliet se usa el primario igualmente:
# aunque las extensiones lleguen mochas, "VIDEO_TS" y "BDMV" sobreviven al 8.3
# y son la señal que más pesa.

def _volume_descriptors(fh):
    """Los descriptores desde el sector 16 hasta el terminador (tipo 255)."""
    out = []
    for i in range(32):                     # de sobra; un disco real trae 2-4
        fh.seek(_PVD_OFFSET + i * _SECTOR)
        block = fh.read(_SECTOR)
        if len(block) < 8 or block[1:6] != b'CD001':
            break
        tipo = block[0]
        if tipo == 255:
            break
        out.append((tipo, block))
    return out


def _pick_descriptor(descriptors):
    """-> (bloque, codec). Joliet si lo hay; si no, el primario."""
    primario = None
    for tipo, block in descriptors:
        if tipo == 1 and primario is None:
            primario = block
        elif tipo == 2:
            # Secuencia de escape de Joliet en el offset 88: %/@, %/C o %/E.
            if block[88:91] in (b'%/@', b'%/C', b'%/E'):
                return block, 'utf-16-be'
    return primario, 'ascii'


def _rock_ridge_name(rec, n_len):
    """
    -> el nombre largo de la entrada `NM` de Rock Ridge, o `''`.

    Hace falta porque Joliet no está siempre. Una ISO masterizada en Linux --
    que es como se empaqueta un pack de biblioteca -- suele llevar Rock Ridge y
    no Joliet, y sin esto sus nombres llegan en 8.3 MAYÚSCULAS: medido, un
    "Perez-Reverte, Arturo - El Club Dumas.epub" se queda en "PEREZ_RE.EPU",
    donde se pierden el nombre Y la extensión, que son las dos señales.

    El área de System Use empieza tras el nombre, más un byte de relleno si la
    longitud del nombre es par. Dentro van entradas de 4 bytes de cabecera:
    firma, longitud, versión, flags. `NM` puede venir partida en varias con el
    bit 0 de flags a 1 ("continúa").
    """
    inicio = 33 + n_len + (1 if n_len % 2 == 0 else 0)
    su = rec[inicio:]

    trozos = []
    pos = 0
    while pos + 4 <= len(su):
        firma = su[pos:pos + 2]
        longitud = su[pos + 2]
        if longitud < 4 or pos + longitud > len(su):
            break
        if firma == b'NM':
            flags = su[pos + 4] if longitud > 4 else 0
            # Los bits 1 y 2 son "." y ".."; ésos no son un nombre.
            if not (flags & 0x06):
                trozos.append(su[pos + 5:pos + longitud])
        elif firma == b'ST':                # fin del área
            break
        pos += longitud

    if not trozos:
        return ''
    return b''.join(trozos).decode('utf-8', errors='replace')


def _dir_records(fh, lba, size, codec):
    """
    Recorre un extent de directorio y devuelve `(nombre, lba, size, es_dir)`.

    Los registros no cruzan sectores: un `length` de 0 significa "salta a lo que
    queda de sector", no "se acabó el directorio".
    """
    out = []
    try:
        fh.seek(lba * _SECTOR)
        data = fh.read(size)
    except (OSError, ValueError, OverflowError):
        return out

    pos = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos = (pos // _SECTOR + 1) * _SECTOR
            continue
        if pos + length > len(data) or length < 34:
            break

        rec = data[pos:pos + length]
        try:
            hijo_lba = struct.unpack('<I', rec[2:6])[0]
            hijo_size = struct.unpack('<I', rec[10:14])[0]
            flags = rec[25]
            n_len = rec[32]
            nombre = rec[33:33 + n_len]
        except (struct.error, IndexError):
            break

        # 0 y 1 son "." y "..".
        if not (n_len == 1 and nombre in (b'\x00', b'\x01')):
            # Rock Ridge manda sobre el 8.3 cuando está. En el árbol de Joliet
            # no hay Rock Ridge, así que esto devuelve '' y no estorba.
            texto = _rock_ridge_name(rec, n_len) or _decode(nombre, codec)
            out.append((texto, hijo_lba, hijo_size, bool(flags & 0x02)))

        pos += length

    return out


def _decode(nombre, codec):
    try:
        texto = nombre.decode(codec, errors='replace')
    except (UnicodeDecodeError, LookupError):
        texto = nombre.decode('latin-1', errors='replace')
    # El ";1" es el número de versión de ISO9660, no parte del nombre.
    return texto.split(';')[0].rstrip('.')


def _iso_entries(path):
    """
    -> lista de rutas relativas dentro de la ISO, o `None` si no es ISO9660.

    Casi toda imagen UDF (Blu-ray, juegos modernos) trae puente ISO9660, así
    que `BDMV` y `VIDEO_TS` se ven igual. Una UDF pura sin puente devuelve
    `None` y sigue la cascada, que es lo correcto: no sabemos, no inventamos.
    """
    try:
        with open(path, 'rb') as fh:
            descriptors = _volume_descriptors(fh)
            if not descriptors:
                return None

            block, codec = _pick_descriptor(descriptors)
            if block is None:
                return None

            # Registro del directorio raíz: 34 bytes en el offset 156 del
            # descriptor.
            raiz = block[156:190]
            if len(raiz) < 14:
                return None
            lba = struct.unpack('<I', raiz[2:6])[0]
            size = struct.unpack('<I', raiz[10:14])[0]

            out = []
            pila = [('', lba, size, 0)]
            while pila and len(out) < MAX_ENTRIES:
                prefijo, d_lba, d_size, depth = pila.pop()
                for texto, h_lba, h_size, es_dir in _dir_records(fh, d_lba, d_size, codec):
                    if not texto:
                        continue
                    ruta = f"{prefijo}{texto}"
                    if es_dir:
                        # El directorio se anota también: "VIDEO_TS" es un
                        # directorio y es LA señal.
                        out.append(ruta + '/')
                        if depth < MAX_DEPTH:
                            pila.append((ruta + '/', h_lba, h_size, depth + 1))
                    else:
                        out.append(ruta)
                    if len(out) >= MAX_ENTRIES:
                        break

            return out
    except (OSError, ValueError, struct.error):
        return None


# ─── la puerta ───────────────────────────────────────────────────────────────

def entries(path):
    """
    -> `list[str]` con los nombres de dentro, o `None` si no se ha podido abrir.

    `None` es una respuesta legítima y frecuente: un `.7z` sin bsdtar, un
    `.rar` sin rarfile, un `.cue`. Quien llama debe seguir bajando por la
    cascada, no dar por hecho que está vacío.
    """
    extension = ext(path)

    try:
        if extension == '.zip':
            return _zip_entries(path)
        if extension == '.rar':
            return _rar_entries(path)
        if extension in ('.iso', '.img'):
            desde_iso = _iso_entries(path)
            # Un `.img` puede ser cualquier cosa -- un volcado de disquete, una
            # imagen de tarjeta. Si no es ISO9660, que lo intente bsdtar.
            if desde_iso is not None:
                return desde_iso
            return _bsdtar_entries(path) or _7z_entries(path)
        if extension in CONTAINER_EXTS:
            return _bsdtar_entries(path) or _7z_entries(path)
    except Exception:                                           # noqa: BLE001
        # Un envase corrupto o cifrado no puede tumbar la tirada: es un "no sé",
        # y el "no sé" ya tiene su camino.
        return None

    return None


def _markers(nombres):
    """Los marcadores de disco presentes, mirando raíz y un nivel de anidado."""
    vistos = set()
    for nombre in nombres or ():
        partes = str(nombre).lower().strip('/').split('/')
        for parte in partes[:2]:            # raíz, y un nivel por si va anidado
            if parte in VIDEO_DISC_MARKERS or parte == AUDIO_DISC_MARKER:
                vistos.add(parte)
    return vistos


def video_disc_marker(nombres):
    """
    -> el marcador de VÍDEO más fuerte (`'bdmv'`, `'video_ts'`...) o `''`.

    Vale más que cualquier recuento de extensiones: un DVD son cuatro
    directorios y trescientos `.VOB`.

    `AUDIO_TS` no cuenta aquí ni aunque esté: a solas es un DVD-Audio, y
    acompañando a `VIDEO_TS` no aporta nada. Para eso está `audio_disc()`.
    """
    vistos = _markers(nombres)
    for marcador in VIDEO_DISC_MARKERS:     # en orden de fuerza
        if marcador in vistos:
            return marcador
    return ''


def audio_disc(nombres):
    """`AUDIO_TS` sin `VIDEO_TS` ni `BDMV`: eso es un DVD-Audio, o sea música."""
    vistos = _markers(nombres)
    return AUDIO_DISC_MARKER in vistos and not (vistos & set(VIDEO_DISC_MARKERS))


def tooling():
    """Qué backends hay vivos ahora mismo. Para diagnóstico y para el aviso."""
    try:
        import rarfile                                          # noqa: F401
        tiene_rar = True
    except ImportError:
        tiene_rar = False

    return {
        'zip': True,
        'iso': True,
        'rar': tiene_rar,
        '7z': bool(_bsdtar() or _7z()),
        'bsdtar': bool(_bsdtar()),
        'p7zip': bool(_7z()),
    }
