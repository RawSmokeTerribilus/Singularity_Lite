# -*- coding: utf-8 -*-
"""Ids de categorías, tipos y resoluciones de un UNIT3D, para «[3] Crear nuevo tracker».

Cada instancia de UNIT3D numera por su cuenta (nabs y nobs ya no coinciden), así
que un módulo nuevo no puede nacer con los ids de otro tracker. La API no tiene
endpoint de categorías ni de tipos: con la API key se sacan de los torrents que
devuelve /api/torrents/filter (salen solo los que tienen torrents), y con la
cookie de sesión de la web, de los <select> de /torrents/create (salen todos).
Lo que no aparezca lo pregunta rawncher.

Nunca se muestra el texto de una excepción de requests: lleva la URL con sus
parámetros, y uno de ellos es la api_token.
"""
import re
import time
import unicodedata
from urllib.parse import urlparse, parse_qs

import requests

UA = 'Singularity rawncher'
CABECERAS_API = {'Accept': 'application/json', 'User-Agent': UA}

# Las claves que usan los módulos: meta['category'] (más el anime aparte),
# meta['type'] y meta['resolution'].
CLAVES_CATEGORIA = ['MOVIE', 'TV', 'ANIME_MOVIE', 'ANIME_TV', 'GAME', 'BOOK', 'AUDIOBOOK']
CLAVES_TIPO = ['DISC', 'REMUX', 'ENCODE', 'WEBDL', 'WEBRIP', 'HDTV']
CLAVES_RESOLUCION = ['4320p', '2160p', '1080p', '1080i', '720p', '576p', '576i', '480p', '480i']

DESCRIPCION = {
    'MOVIE': 'películas', 'TV': 'series', 'ANIME_MOVIE': 'películas de anime',
    'ANIME_TV': 'series de anime', 'GAME': 'juegos', 'BOOK': 'libros', 'AUDIOBOOK': 'audiolibros',
    'DISC': 'disco completo', 'REMUX': 'remux', 'ENCODE': 'encode', 'WEBDL': 'WEB-DL',
    'WEBRIP': 'WEBRip', 'HDTV': 'HDTV',
}

# Los seeders de UNIT3D: lo que trae un tracker recién instalado. Solo como
# sugerencia cuando el tracker no deja ver el suyo.
SEMILLA_CATEGORIA = {'MOVIE': '1', 'TV': '2'}
SEMILLA_TIPO = {'DISC': '1', 'REMUX': '2', 'ENCODE': '3', 'WEBDL': '4', 'WEBRIP': '5', 'HDTV': '6'}
SEMILLA_RESOLUCION = {'4320p': '1', '2160p': '2', '1080p': '3', '1080i': '4', '720p': '5',
                      '576p': '6', '576i': '7', '480p': '8', '480i': '9'}
SEMILLA_OTRA = '10'


def detectar(base_url, api_key, timeout=15):
    """¿Es un UNIT3D? Devuelve (estado, detalle).

    unit3d      la API contesta con la forma de UNIT3D (data/links/meta).
    sin_clave   401 en JSON de Laravel y no había clave: probable UNIT3D.
    clave_mala  401 en JSON de Laravel con clave: la clave no vale.
    cloudflare  Cloudflare para la petición con su desafío anti-bots.
    no          contesta otra cosa (HTML, 404...).
    caido       no se pudo conectar.
    """
    params = {'perPage': 1}
    if api_key:
        params['api_token'] = api_key
    try:
        r = requests.get(f'{base_url}/api/torrents/filter', params=params,
                         headers=CABECERAS_API, timeout=timeout)
    except requests.RequestException as e:
        return 'caido', type(e).__name__
    try:
        j = r.json()
    except ValueError:
        j = None
    if r.ok and isinstance(j, dict) and isinstance(j.get('data'), list) and 'meta' in j:
        return 'unit3d', ''
    if r.status_code == 401 and isinstance(j, dict) and 'message' in j:
        return ('clave_mala' if api_key else 'sin_clave'), f"HTTP 401: {j['message']}"
    if r.status_code in (403, 503) and ('cf-ray' in r.headers or 'Just a moment' in r.text[:2000]):
        return 'cloudflare', f'HTTP {r.status_code}'
    tipo = r.headers.get('content-type', '?').split(';')[0]
    return 'no', f'HTTP {r.status_code}, {tipo}'


def ids_desde_api(base_url, api_key, paginas=10, timeout=15):
    """{'categories'|'types'|'resolutions': {id: nombre}} de los torrents del tracker."""
    vistos = {'categories': {}, 'types': {}, 'resolutions': {}}
    params = {'api_token': api_key, 'perPage': 100}
    campos = (('category_id', 'category', 'categories'),
              ('type_id', 'type', 'types'),
              ('resolution_id', 'resolution', 'resolutions'))
    for _ in range(paginas):
        try:
            r = requests.get(f'{base_url}/api/torrents/filter', params=params,
                             headers=CABECERAS_API, timeout=timeout)
            j = r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            break
        for t in j.get('data') or []:
            a = t.get('attributes') or {}
            for campo_id, campo_nombre, destino in campos:
                if a.get(campo_id) is not None and a.get(campo_nombre):
                    vistos[destino][str(a[campo_id])] = str(a[campo_nombre])
        siguiente = (j.get('links') or {}).get('next')
        if not siguiente:
            break
        # cursor o page según la versión de UNIT3D: se copia lo que traiga.
        params.update({k: v[0] for k, v in parse_qs(urlparse(siguiente).query).items()
                       if k != 'api_token'})
        time.sleep(1)
    return vistos


def ids_desde_web(base_url, cookie, timeout=20):
    """Los <select> de /torrents/create con la cabecera Cookie del navegador.

    Devuelve (vistos, '') o (None, motivo).
    """
    from bs4 import BeautifulSoup
    cookie = re.sub(r'^\s*cookie\s*:\s*', '', cookie or '', flags=re.I).strip()
    try:
        r = requests.get(f'{base_url}/torrents/create', timeout=timeout, allow_redirects=False,
                         headers={'User-Agent': UA, 'Cookie': cookie})
    except requests.RequestException as e:
        return None, type(e).__name__
    except UnicodeEncodeError:
        return None, 'la cookie lleva caracteres raros: cópiala otra vez'
    if 300 <= r.status_code < 400:
        destino = urlparse(r.headers.get('location', '')).path or '?'
        return None, f'HTTP {r.status_code} → {destino}; {_diagnostico_cookie(cookie)}'
    if r.status_code != 200:
        return None, f'HTTP {r.status_code}'
    soup = BeautifulSoup(r.text, 'html.parser')
    vistos = {}
    for campo, destino in (('category_id', 'categories'), ('type_id', 'types'),
                           ('resolution_id', 'resolutions')):
        sel = soup.find('select', attrs={'name': campo})
        opciones = sel.find_all('option') if sel else []
        vistos[destino] = {o['value']: o.get_text(' ', strip=True)
                           for o in opciones if str(o.get('value', '')).isdigit()}
    if not vistos['categories']:
        return None, 'la página no tiene el formulario de subida (¿cookie de otra web?)'
    return vistos, ''


def _diagnostico_cookie(cookie):
    """Lo que llegó, sin enseñar ningún valor: tamaño y nombres de las cookies."""
    nombres = [p.split('=', 1)[0].strip() for p in cookie.split(';') if '=' in p]
    pistas = []
    if not nombres and cookie.startswith('eyJ'):
        # Prueba rc3: se pegó el valor de Almacenamiento -> Cookies, sin nombre.
        pistas.append('parece solo el VALOR de una cookie: hace falta la cabecera «Cookie» '
                      'entera, con los nombres («nombre=valor; nombre=valor»)')
    elif not any(n.endswith('_session') for n in nombres):
        pistas.append('falta la de sesión («…_session»)')
    if len(cookie) >= 4000:
        pistas.append('el terminal corta lo pegado a 4095 caracteres: puede haber llegado cortada')
    texto = f"llegaron {len(cookie)} caracteres, {len(nombres)} cookies ({', '.join(nombres[:10]) or 'ninguna'})"
    return texto + ('; ' + '; '.join(pistas) if pistas else '; la sesión no vale (¿caducada, o de otra web?)')


def _norm(texto):
    sin_tildes = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', ' ', sin_tildes.lower()).strip()


def _clave_categoria(nombre):
    n = _norm(nombre)
    palabras = set(n.split())
    peli = any(x in n for x in ('movie', 'pelicula', 'film', 'cine'))
    serie = 'tv' in palabras or any(x in n for x in ('serie', 'show'))
    libro = any(x in n for x in ('book', 'libro'))
    if 'audio' in n and libro:
        return 'AUDIOBOOK'
    if libro:
        return 'BOOK'
    if 'anime' in n:
        return 'ANIME_MOVIE' if peli else 'ANIME_TV' if serie else None
    if peli:
        return 'MOVIE'
    if serie:
        return 'TV'
    if any(x in n for x in ('game', 'juego', 'arcade', 'retro', 'rom')):
        return 'GAME'
    return None


def _clave_tipo(nombre):
    n = _norm(nombre).replace(' ', '')
    for trozo, clave in (('remux', 'REMUX'), ('encode', 'ENCODE'), ('webdl', 'WEBDL'),
                         ('webrip', 'WEBRIP'), ('hdtv', 'HDTV'), ('disc', 'DISC')):
        if trozo in n:
            return clave
    # Formatos de libro, audio o juego (EPUB, M4B, ROM...): el nombre tal cual,
    # que es lo que pone prep en meta['type'], sin lo que vaya entre paréntesis
    # («MP3 (audiolibro)» -> MP3).
    return re.sub(r'\s*\([^)]*\)', '', nombre).strip().upper()


def proponer(vistos):
    """Reparte lo visto en el tracker entre las claves de los módulos.

    Devuelve (categorias, tipos, resoluciones, otra, sin_asignar) con
    categorias/tipos/resoluciones como {clave: id} y sin_asignar como lista de
    'id nombre' de categorías del tracker que no casan con ninguna clave.
    """
    categorias, sin_asignar = {}, []
    for id_, nombre in sorted(vistos.get('categories', {}).items(), key=lambda x: int(x[0])):
        clave = _clave_categoria(nombre)
        if clave and clave not in categorias:
            categorias[clave] = id_
        else:
            sin_asignar.append(f'{id_} {nombre}')
    tipos = {}
    for id_, nombre in sorted(vistos.get('types', {}).items(), key=lambda x: int(x[0])):
        tipos.setdefault(_clave_tipo(nombre), id_)
    resoluciones, otra = {}, None
    for id_, nombre in sorted(vistos.get('resolutions', {}).items(), key=lambda x: int(x[0])):
        nombre = nombre.strip()
        if re.fullmatch(r'\d{3,4}[pi]', nombre):
            resoluciones.setdefault(nombre, id_)
        elif _norm(nombre) in ('other', 'otra', 'otro', 'otras'):
            otra = id_
    return categorias, tipos, resoluciones, otra, sin_asignar


def ordenado(d, claves):
    """Primero las claves conocidas en su orden, luego el resto como vinieron."""
    return {**{k: d[k] for k in claves if k in d}, **{k: v for k, v in d.items() if k not in claves}}


def bloque_ids(categorias, tipos, resoluciones, otra):
    """Las líneas que van entre los marcadores de ids de UNIT3D_TEMPLATE.py."""
    categorias = ordenado(categorias, CLAVES_CATEGORIA)
    tipos = ordenado(tipos, CLAVES_TIPO)
    # UNIT3D no tiene 1440p: se archiva como 1080p (igual que en NOBS/NABS).
    claves_res = CLAVES_RESOLUCION[:2] + ['1440p'] + CLAVES_RESOLUCION[2:]
    resoluciones = dict(resoluciones)
    if '1080p' in resoluciones:
        resoluciones.setdefault('1440p', resoluciones['1080p'])
    resoluciones = ordenado(resoluciones, claves_res)

    def dic(nombre, d):
        cuerpo = ''.join(f'        {k!r}: {v!r},\n' for k, v in d.items())
        return f'    {nombre} = {{\n{cuerpo}    }}\n'

    return (dic('CATEGORY_IDS', categorias) + dic('TYPE_IDS', tipos)
            + dic('RESOLUTION_IDS', resoluciones)
            + f'    RESOLUTION_OTHER = {(otra or "0")!r}\n')
