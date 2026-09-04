# -*- coding: utf-8 -*-
"""
library -- de qué tipo es cada cosa de un árbol de ficheros.

Existe como módulo aparte, y no metido en `build_recursive_queue()`, porque hay
al menos TRES consumidores y sólo uno es la cola de subida:

  1. `upload.py`  -- construir la cola y avisar si el directorio es una mezcla.
  2. `rawncher.py` -- decirle al operador qué hay ahí dentro ANTES de disparar.
  3. el triaje y el CSI -- que para vídeo ya generan listas `.txt` que luego
     ceban a RawLoadrr, y que para libros y juegos harán lo mismo.

Si la clasificación vive dentro de la cola, el tercero tiene que reimplementarla
y las tres versiones se desincronizan. Por eso `scan()` devuelve el árbol
clasificado en crudo y cada cual hace con él lo suyo.

Ninguna función de aquí toca la red. Y ninguna abría los ficheros tampoco,
hasta que hizo falta: un **envase** (`.zip`, `.iso`, `.rar`, `.7z`) no declara
su clase por la extensión, porque no la tiene. Un `.zip` es un juego de ScummVM
o son 300.000 libros; una `.iso` es un DVD o el instalador de Red Alert 3.
Decidir por la extensión del envase es decidir por el color de la caja, y así
es como todo `.zip` era un juego y toda `.iso` acababa en el pipeline de vídeo.

Así que hay dos niveles, y conviene no confundirlos:

  - `classify()` sigue siendo **sólo nombres**, barata y sin tocar disco. Ante
    un envase devuelve `None`, que es la verdad: el nombre no lo sabe.
  - `classify_container()` y `classify_path()` **abren** el envase (por la
    cabecera, nunca los datos) y deciden por lo que hay dentro.
"""

import os
import re
import unicodedata

from src import bookinfo
from src import container
from src import gameinfo

VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.m4v')

KINDS = ('video', 'audiobook', 'book', 'game')

# Etiquetas para hablarle a una persona.
LABELS = {
    'video': 'vídeo',
    'book': 'e-book',
    'audiobook': 'audiolibro',
    'game': 'juego',
}


def _game_exts(explicit):
    """
    Con `--only game` entran también las ambiguas (`.rom`, `.dsk`, `.tap`...),
    que colisionan con ficheros de datos corrientes.

    Los ENVASES ya no están en ninguna de las dos listas: no son extensiones de
    juego, son cajas, y las resuelve `classify_container()` abriéndolas.
    """
    return gameinfo.GAME_EXTS if explicit else gameinfo.GAME_EXTS_AUTO


def classify(name, only=None):
    """
    -> 'video' | 'audiobook' | 'book' | 'game' | None

    **Sólo mira el nombre.** No abre nada, así que ante un envase devuelve
    `None` — para eso está `classify_path()`.

    El orden importa. El audiolibro va ANTES que el e-book y que el audio
    porque un `.m4b` es las tres cosas a la vez y gana quien pregunta primero.
    """
    lower = str(name).lower()
    explicit_game = bool(only and 'game' in only)

    # Un envase primero que nada: si no, un `.zip` cae en las extensiones de
    # juego y se acabó la pregunta antes de empezar. Ésta es exactamente la
    # línea por la que un pack de 300.000 libros se resolvía contra IGDB.
    if container.is_wrapper(lower):
        return None

    if bookinfo.looks_like_audiobook(name, declared=bool(only and 'audiobook' in only)):
        return 'audiobook'
    if lower.endswith(VIDEO_EXTS):
        return 'video'
    if lower.endswith(bookinfo.EBOOK_EXTS):
        return 'book'
    if lower.endswith(_game_exts(explicit_game)):
        return 'game'

    return None


# Qué parte de las entradas reconocibles tiene que apuntar al mismo sitio para
# que un envase se resuelva solo. Por debajo de esto es una mezcla y se
# pregunta: un zip mitad películas mitad libros no es ninguna de las dos cosas,
# y elegir por él es elegir mal la mitad de las veces.
MAYORIA = 0.70

# Un instalador es software, y en esta casa software es juego. `autorun.inf` y
# `setup.exe` juntos no son ambiguos: ningún DVD de película los lleva, y son
# exactamente lo que trae la ISO que destapó todo esto.
_MARCAS_INSTALADOR = ('autorun.inf', 'setup.exe', 'install.exe', 'autorun.exe')
_EXTS_INSTALADOR = ('.exe', '.msi', '.bat', '.com', '.dll')


def classify_container(path, only=None):
    """
    -> `(clase|None, motivo)` mirando lo que hay DENTRO del envase.

    El `motivo` no es decorado: es lo que se le enseña al operador cuando hay
    que preguntarle, y lo que queda en el log cuando no.

    `None` significa "no lo sé", nunca "no es nada". Quien llama debe seguir
    bajando por la cascada -- nombre, y finalmente preguntar -- y jamás caer al
    pipeline de vídeo por descarte, que es como una ISO de datos acababa
    pidiéndole el FrameRate a una pista que no existe.
    """
    nombres = container.entries(path)

    if nombres is None:
        return None, "no he podido abrir el envase"
    if not nombres:
        return None, "el envase está vacío"

    # 1. Estructura de disco. Vale más que cualquier recuento: un DVD son
    #    cuatro directorios y trescientos .VOB, y `.VOB` no está en ninguna
    #    lista de extensiones de vídeo de la casa.
    marcador = container.video_disc_marker(nombres)
    if marcador:
        return 'video', f"trae {marcador.upper()}/ dentro: es un disco de vídeo"

    if container.audio_disc(nombres):
        return None, "trae AUDIO_TS sin VIDEO_TS: parece un DVD-Audio, no vídeo"

    # 2. Recuento por extensión, con el MISMO clasificador que se usa para un
    #    directorio. Un envase es un directorio, sólo que cerrado.
    por_tipo = {}
    for nombre in nombres:
        kind = classify(os.path.basename(str(nombre).rstrip('/')), only)
        if kind:
            por_tipo[kind] = por_tipo.get(kind, 0) + 1

    total = sum(por_tipo.values())
    if total:
        gana, n = max(por_tipo.items(), key=lambda kv: kv[1])
        if n / total >= MAYORIA:
            return gana, f"{n} de {total} entradas reconocibles son de {LABELS[gana]}"
        detalle = ", ".join(f"{c} de {LABELS[k]}" for k, c in
                            sorted(por_tipo.items(), key=lambda kv: -kv[1]))
        return None, f"dentro hay mezcla ({detalle})"

    # 3. Ninguna extensión conocida. Es el caso normal de un juego: ni un zip de
    #    ScummVM ni un instalador de DOS traen extensiones que estén en ninguna
    #    lista. Se le pregunta a gameinfo, que sabe de sistemas.
    sistema = gameinfo._detect_system(path, [{'nombre': n} for n in nombres])
    if sistema and sistema not in ('Disco', 'Disco (CHD)'):
        return 'game', f"el contenido es de {sistema}"

    # 4. Un instalador. Es la señal que resuelve el caso original --
    #    "Command and Conquer Red Alert 3.iso" trae autorun.inf y setup.exe --
    #    y va la ÚLTIMA porque un .exe suelto dentro de otra cosa no convierte
    #    esa cosa en un juego.
    base = {os.path.basename(str(n).rstrip('/')).lower() for n in nombres}
    marcas = base & set(_MARCAS_INSTALADOR)
    if marcas:
        return 'game', f"trae {sorted(marcas)[0]}: es un instalador"

    ejecutables = sum(1 for n in base if n.endswith(_EXTS_INSTALADOR))
    if ejecutables and ejecutables / len(base) >= 0.10:
        return 'game', f"{ejecutables} ejecutables de {len(base)} entradas: es software"

    return None, "abierto, pero nada de dentro dice qué es"


def classify_path(path, only=None):
    """
    -> `(clase|None, motivo)` para CUALQUIER cosa: envase o fichero suelto.

    La puerta única. Un directorio no entra aquí: para eso está `scan()`, que
    tiene su propia precedencia.
    """
    if container.is_wrapper(path):
        if container.is_opaque(path):
            return None, f"un {container._ext(path)} no se puede abrir para mirar dentro"
        return classify_container(path, only)

    kind = classify(os.path.basename(str(path)), only)
    if kind:
        return kind, f"por la extensión ({container._ext(path)})"
    return None, "la extensión no dice de qué clase es"


# Precedencia por directorio, la MISMA que aplica la cola de subida. El orden
# no es estético: un `.m4b` es audio y es libro a la vez, y una carpeta de
# películas con un `caratulas.zip` dentro es una carpeta de películas.
_PRECEDENCIA = ('audiobook', 'book', 'video', 'game')

# ...salvo entre ellos dos. Una carpeta con el audiolibro y el e-book de la
# misma obra tiene DOS cosas que subir, no una: son ediciones distintas y cada
# una es su propio torrent. Medido con "El arte de la guerra", donde el .m4a y
# el .pdf viven juntos y sólo subía el pdf.
_CONVIVEN = {'audiobook', 'book'}


def scan(root, only=None, deep=True):
    """
    Recorre un árbol y devuelve `{tipo: [rutas]}`.

    Clasifica **por directorio, no por fichero**, y ésa es la parte que
    importa: contando ficheros sueltos, una carpeta con `peli.mkv` y
    `caratulas.zip` sale como "1 de vídeo, 1 de juego" y dispara un aviso de
    mezcla que no existe. Las bibliotecas guardan carátulas, subtítulos y
    metadatos junto al vídeo; si eso cuenta como otro tipo, el aviso salta
    siempre y deja de significar nada.

    Un directorio es de UN tipo, el primero de `_PRECEDENCIA` que aparezca en
    él, que es exactamente cómo decide `build_recursive_queue()`.

    Pensado también para el sistema de listados: quien quiera escribir un
    `.txt` de rutas para cebar a RawLoadrr sólo tiene que volcar la lista del
    tipo que le interese.

    `deep=False` se queda en los nombres y no abre ni un envase. Para cuando se
    quiere un barrido barato y da igual perderse lo que haya dentro de un zip.
    """
    found = {kind: [] for kind in KINDS}

    for dirpath, _dirnames, filenames in os.walk(root):
        # Dos cestas, y separarlas importa. Un envase que convive con obra
        # suelta es un accesorio, no la obra: `caratulas.zip` dentro de la
        # carpeta de una película no la convierte en otra cosa.
        #
        # Antes esto salía gratis porque todo `.zip` clasificaba como 'game', y
        # 'game' es el último de _PRECEDENCIA. En cuanto los envases se abren de
        # verdad, un `extras.zip` con un PDF dentro sale 'book', y 'book' va por
        # ENCIMA de 'video': medido, el .mkv de la película desaparecía del
        # barrido y ganaba el zip. Los envases juegan en su propio escalón, y
        # sólo cuando no hay nada suelto que subir.
        por_tipo = {}
        por_envase = {}

        for name in filenames:
            ruta = os.path.join(dirpath, name)

            # Los envases hay que abrirlos: desde que `.zip` dejó de ser una
            # extensión de juego, quedarse en el nombre dejaría una carpeta con
            # 77 zips de ScummVM como "aquí no hay nada que subir". Es de
            # cabecera, no descomprime, y sólo pasa por aquí lo que es envase.
            if deep and container.is_container(name):
                kind, _motivo = classify_container(ruta, only)
                cesta = por_envase
            else:
                kind = classify(name, only)
                cesta = por_tipo

            if kind and (not only or kind in only):
                cesta.setdefault(kind, []).append(ruta)

        if not por_tipo:
            por_tipo = por_envase

        if not por_tipo:
            continue

        gana = next(k for k in _PRECEDENCIA if k in por_tipo)

        for kind in ({gana} | (_CONVIVEN & set(por_tipo)) if gana in _CONVIVEN else {gana}):
            found[kind].extend(por_tipo[kind])

    return {k: v for k, v in found.items() if v}


def counts(found):
    """`{tipo: [rutas]}` -> `[(tipo, n), ...]`, en el orden de KINDS."""
    return [(k, len(found[k])) for k in KINDS if found.get(k)]


def describe(found):
    """"16 de e-book, 5 de juego" -- para hablarle a una persona."""
    return ", ".join(f"{n} de {LABELS[k]}" for k, n in counts(found))


def is_mixed(found):
    """
    ¿Hay tipos que NO deberían ir en la misma tirada?

    Éste es el caso peligroso y el único que justifica parar: una biblioteca
    ordenada es homogénea y nunca lo ve. Una carpeta de descargas, sí.

    Libro y audiolibro juntos NO cuentan como mezcla: es la forma normal de
    guardar una obra de la que se tienen las dos ediciones, y ambas se suben.
    Avisar ahí sería avisar siempre, que es como un aviso deja de leerse.
    """
    tipos = {k for k, _n in counts(found)}

    if tipos <= _CONVIVEN:
        return False

    return len(tipos) > 1


# ─── ¿esto es el nombre de una obra? ─────────────────────────────────────────
#
# La extensión no distingue "Contrato.pdf" de "El Quijote.pdf", así que no es
# ahí donde se puede cribar. Quien sentencia es el proveedor: si Google Books
# no conoce el libro y IGDB no conoce el juego, no hay id y no se sube.
#
# Esto de aquí NO sentencia. Sólo evita gastar una petición en lo que no puede
# ser el título de nada, y permite dar un motivo entendible en vez de un "no
# encontrado" seco. Es a propósito CORTO y conservador: ante la duda, deja
# pasar y que decida el proveedor.

_HEX = re.compile(r'^[0-9a-f]{16,}$')
# "YTMR-YG13-006", "SH3B174A5": bloques de letras y dígitos pegados, sin
# espacios. Ningún título comercial se escribe así.
_SERIAL = re.compile(r'^[a-z0-9]*\d[a-z0-9]*([-_][a-z0-9]*\d[a-z0-9]*)+$')


def _fold(text):
    text = unicodedata.normalize('NFKD', str(text).lower())
    return ''.join(c for c in text if not unicodedata.combining(c))


def looks_like_work(name):
    """
    -> (bool, motivo)

    `False` sólo cuando es IMPOSIBLE que sea el nombre de una obra. Todo lo
    demás devuelve `True` y se lo come el proveedor, que es quien sabe.
    """
    stem = _fold(os.path.splitext(os.path.basename(str(name)))[0]).strip()

    if len(stem) < 3:
        return False, "el nombre no llega a tres caracteres"

    if not any(c.isalpha() for c in stem):
        return False, "el nombre no tiene ni una letra"

    compact = re.sub(r'[\s._-]+', '', stem)

    if _HEX.match(compact):
        return False, "el nombre es un hash, no un título"

    if _SERIAL.match(re.sub(r'[\s.]+', '-', stem)):
        return False, "el nombre es un número de serie, no un título"

    # Un título necesita vocales. "SH3B174A5" no las tiene donde toca.
    letters = [c for c in compact if c.isalpha()]
    if letters and sum(c in 'aeiou' for c in letters) / len(letters) < 0.15:
        return False, "el nombre no tiene vocales suficientes para ser un título"

    return True, ""
