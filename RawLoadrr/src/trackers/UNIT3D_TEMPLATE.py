# -*- coding: utf-8 -*-
import asyncio
import re
import requests
import json
import os
import platform
from rich.pretty import Pretty
from src.trackers.COMMON import COMMON
from src.console import console
from src.rate_limiter import rate_limiter
from src.logger import get_logger


class UNIT3D_TEMPLATE():
    def __init__(self, config):
        self.config = config
        self.tracker = 'UNIT3D_TEMPLATE'
        self.source_flag = 'UNIT3D_TEMPLATE'
        self.upload_url = f'{self.BASE_URL}/api/torrents/upload'
        self.search_url = f'{self.BASE_URL}/api/torrents/filter'
        self.banned_groups = [""]
        self.logger = get_logger(self.tracker)
        pass
    
    # Plantilla UNIT3D de la suite: «[3] Crear nuevo tracker» la copia y
    # rellena BASE_URL y el bloque de ids con lo que lee del tracker (API, o
    # la página de subida con cookie) o con lo que conteste el usuario. Cada
    # instancia de UNIT3D numera por su cuenta: aquí van los ids de los seeders
    # de UNIT3D, que solo valen tal cual para un tracker recién instalado.
    BASE_URL = 'https://tracker.example'

    # >>> ids del tracker (los escribe rawncher [3])
    CATEGORY_IDS = {'MOVIE': '1', 'TV': '2'}
    TYPE_IDS = {'DISC': '1', 'REMUX': '2', 'ENCODE': '3', 'WEBDL': '4', 'WEBRIP': '5', 'HDTV': '6'}
    RESOLUTION_IDS = {'4320p': '1', '2160p': '2', '1440p': '3', '1080p': '3', '1080i': '4', '720p': '5',
                      '576p': '6', '576i': '7', '480p': '8', '480i': '9'}
    RESOLUTION_OTHER = '10'
    # <<< ids del tracker

    async def get_cat_id(self, category_name, meta=None):
        is_anime = bool(meta and (meta.get('anime') or int(meta.get('mal_id') or 0) != 0))
        if category_name == 'MOVIE':
            if is_anime and 'ANIME_MOVIE' in self.CATEGORY_IDS:
                return self.CATEGORY_IDS['ANIME_MOVIE']
            return self.CATEGORY_IDS.get('MOVIE', '0')
        elif category_name == 'TV':
            if is_anime and 'ANIME_TV' in self.CATEGORY_IDS:
                return self.CATEGORY_IDS['ANIME_TV']
            return self.CATEGORY_IDS.get('TV', '0')
        return self.CATEGORY_IDS.get(category_name, '0')

    def is_non_video(self, meta):
        """Un libro, un audiolibro o un juego: sin disco, sin resolución, sin mediainfo."""
        return (meta.get('category') in ('BOOK', 'AUDIOBOOK', 'GAME')
                or meta.get('is_book') or meta.get('is_audiobook')
                or meta.get('is_game'))

    @staticmethod
    def _isbn13(meta):
        """13 dígitos; `isbn13_obra` es el del libro en una lectura libre."""
        for clave in ('isbn13', 'isbn', 'isbn13_obra'):
            raw = re.sub(r'[^0-9]', '', str(meta.get(clave) or ''))
            if len(raw) == 13:
                return raw

        return None

    @staticmethod
    def _asin(meta):
        """Los ASIN de Audible son exactamente 10 alfanuméricos, en mayúsculas."""
        raw = re.sub(r'[^A-Za-z0-9]', '', str(meta.get('asin') or '')).upper()
        return raw if len(raw) == 10 else None

    async def get_type_id(self, type):
        return self.TYPE_IDS.get(type, '0')

    async def get_res_id(self, resolution):
        return self.RESOLUTION_IDS.get(resolution, self.RESOLUTION_OTHER)

    ###############################################################
    ######   STOP HERE UNLESS EXTRA MODIFICATION IS NEEDED   ######
    ###############################################################

    async def upload(self, meta):
        common = COMMON(config=self.config)
        await common.edit_torrent(meta, self.tracker, self.source_flag)
        cat_id = await self.get_cat_id(meta['category'], meta)
        type_id = await self.get_type_id(meta['type'])
        non_video = self.is_non_video(meta)

        # La API admite resolution_id nulo fuera de las categorías de vídeo, y
        # NO lo corrige por categoría: mandar el fallback archivaría cada libro
        # bajo "Other" y saldría en un filtro de resolución que no le toca.
        resolution_id = None if non_video else await self.get_res_id(meta['resolution'])
        await common.unit3d_edit_desc(meta, self.tracker)
        region_id = await common.unit3d_region_ids(meta.get('region'))
        distributor_id = await common.unit3d_distributor_ids(meta.get('distributor'))
        if meta['anon'] != 0 or self.config['TRACKERS'][self.tracker].get('anon', False):
            anon = 1
        else:
            anon = 0

        # Un libro no tiene mediainfo y una ROM no tiene resumen de disco, y la
        # rama de libro de prep no llega al código que escribe ninguno de los
        # dos ficheros. Leerlos sin mirar es lo que mataba la subida.
        if non_video:
            mi_dump = bd_dump = None
        elif meta.get('bdinfo') is not None:
            mi_dump = None
            bd_dump = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_SUMMARY_00.txt", 'r', encoding='utf-8').read()
        else:
            mi_dump = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt", 'r', encoding='utf-8').read()
            bd_dump = None
        desc = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt", 'r', encoding='utf-8').read()
        open_torrent = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]{meta['clean_name']}.torrent", 'rb')
        nfo_file = meta.get('nfo_file', None)
        files = {'torrent': open_torrent}
        if nfo_file:
            open_nfo = open(nfo_file, 'rb') 
            files['nfo'] = open_nfo
        manual_name = meta.get('manual_name')
        data = {
            'name' : manual_name or self.nombre_tracker(meta),
            'description' : desc,
            'mediainfo' : mi_dump,
            'bdinfo' : bd_dump, 
            'category_id' : cat_id,
            'type_id' : type_id,
            'resolution_id' : resolution_id,
            'tmdb' : meta.get('tmdb') or 0,
            'imdb' : str(meta.get('imdb_id') or '').replace('tt', ''),
            'tvdb' : None if meta.get('anime') else (meta.get('tvdb') or meta.get('tvdb_id')),
            'mal' : meta.get('mal_id') or 0,
            # El tracker anula por categoría los ids que no le tocan, así que
            # un id enviado a la categoría equivocada se descarta, no revienta.
            'igdb' : int(meta.get('igdb') or 0),
            'isbn13' : self._isbn13(meta),
            'asin' : self._asin(meta),
            'anonymous' : anon,
            'stream' : meta.get('stream', 0),
            'sd' : meta.get('sd', 0),
            'keywords' : meta.get('keywords', ''),
            'personal_release' : int(meta.get('personalrelease', False)),
            'internal' : 0,
            'featured' : 0,
            'free' : 0,
            'doubleup' : 0,
            'sticky' : 0,
        }
        # Internal
        if self.config['TRACKERS'][self.tracker].get('internal', False):
            if meta['tag'] != "" and (meta['tag'][1:] in self.config['TRACKERS'][self.tracker].get('internal_groups', [])):
                data['internal'] = 1
                
        if region_id != 0:
            data['region_id'] = region_id
        if distributor_id != 0:
            data['distributor_id'] = distributor_id
        if meta.get('category') == "TV":
            data['season_number'] = int(meta.get('season_int', '0'))
            data['episode_number'] = int(meta.get('episode_int', '0'))
        headers = {
            'User-Agent': f'Uploadrr / v1.0 ({platform.system()} {platform.release()})'
        }
        params = {
            'api_token' : self.config['TRACKERS'][self.tracker]['api_key'].strip()
        }
        
        if meta['debug']:
            self.logger.info(f"DATA 2 SEND: {data}")
            # --debug promete ensayo: se enseña lo que se mandaría y no se manda.
            # False hace que upload.py lo apunte como «(DEBUG MODE)», no como rechazo.
            console.print(f"[bold yellow]DEBUG: no se sube a {self.tracker}.[/bold yellow]")
            return False

        return_value = False # Default return value
        try:
            # Respect rate limiter
            await rate_limiter.acquire(self.tracker)
            
            response = requests.post(url=self.upload_url, files=files, data=data, headers=headers, params=params, timeout=60)
            
            if response.status_code >= 200 and response.status_code < 300:
                response_json = response.json()
                if meta['debug']:
                    self.logger.info(f"Full upload response from tracker: {response_json}")
                success = response_json.get('success', False)
                
                if success:
                    self.logger.upload_result(meta['clean_name'], True)
                    return_value = response_json # Return the full JSON response on success
                else:
                    message = response_json.get('message', 'No message provided')
                    console.print(f"[red]Upload failed: {message}[/red]")
                    self.logger.upload_result(meta['clean_name'], False, message)
                    response_data = response_json.get('data', {})
                    if response_data:
                        console.print(f"[cyan]Error details:[/cyan] {response_data}")
                        self.logger.info(f"Error details: {response_data}")
                    return_value = False # Explicitly return False on API-reported failure

            else: # This block is executed if response.status_code >= 400 (like a 404 or 500)
                try:
                    response_json = response.json()
                    success = response_json.get('success', False)
                    message = response_json.get('message', 'No message provided')
                    response_data = response_json.get('data', {})

                    console.print(f"[red]Upload failed: {message}[/red]")
                    self.logger.upload_result(meta['clean_name'], False, message)
                    if response_data:
                        console.print(f"[cyan]Error details:[/cyan] {response_data}")
                        self.logger.info(f"Error details: {response_data}")
                    return_value = False # Explicitly return False
                except json.JSONDecodeError:
                    # Fallback to HTML parsing if not JSON
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(response.text, 'html.parser')
                        error_heading = soup.find(class_='error__heading')
                        error_body = soup.find(class_='error__body')
                        
                        if error_heading and error_body:
                            console.print(f"[red]{error_heading.text.strip()}[/red]")
                            console.print(f"[b][yellow]{error_body.text.strip()}[/yellow][/b]")
                            self.logger.error(f"HTTP {response.status_code}: {error_heading.text.strip()}")
                        else:
                            console.print(f"[red]Encountered HTTP Error: {response.status_code}[/red]")
                            self._volcar_respuesta(meta, response)
                    except Exception as parse_error:
                        console.print(f"[red]Failed to parse error response: {parse_error}[/red]")
                        self._volcar_respuesta(meta, response)
                        self.logger.error(f"Failed to parse error response: {str(parse_error)}")
                    
                    return_value = False # Explicitly return False
                    
        except requests.exceptions.Timeout:
            console.print(f"[red]Upload timeout - connection took too long[/red]")
            self.logger.error(f"Upload timeout for {meta['clean_name']}")
            return_value = False
        except requests.exceptions.ConnectionError as e:
            console.print(f"[red]Connection error: {e}[/red]")
            self.logger.error(f"Connection error: {str(e)}")
            return_value = False
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Request error: {e}[/red]")
            self.logger.error(f"Request error: {str(e)}")
            return_value = False

        if return_value == False: # Only print failed message if it wasn't a success dictionary
            console.print("[bold red]Torrent upload failed.")
        elif return_value == 'Unknown': # This state should no longer be reached with explicit returns
             console.print("[bold yellow]Status of upload is unknown, please go check..")
             self.logger.warning(f"Upload status unknown for {meta['clean_name']}")
        else: # This means return_value is a dict (success)
            console.print("[bold green]Torrent uploaded successfully!")
        
        try:
            open_torrent.close()
        except Exception as e:
            console.print(f"[red]Failed to close torrent file: {e}[/red]")
            self.logger.warning(f"Failed to close torrent file: {str(e)}")

        return return_value

    def _volcar_respuesta(self, meta, response):
        """Una línea en pantalla y en el log; la respuesta entera, a un fichero.

        Antes salía la página HTML completa (login, error 500...) en pantalla y
        en el log, una vez por intento.
        """
        inicio = ' '.join(response.text[:300].split())
        try:
            ruta = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]respuesta_HTTP{response.status_code}.html"
            with open(ruta, 'w', encoding='utf-8') as f:
                f.write(response.text)
        except (OSError, KeyError):
            ruta = '(no se pudo guardar)'
        console.print(f"[blue]Server Response[/blue]: {inicio}… [dim](entera en {ruta})[/dim]")
        self.logger.error(f"HTTP {response.status_code}: {inicio}… (entera en {ruta})")

    async def search_existing(self, meta):
        dupes = {}
        console.print(f"[yellow]Searching for existing torrents on {self.tracker}...")
        params = {
            'api_token' : self.config['TRACKERS'][self.tracker]['api_key'].strip(),
            'categories[]' : await self.get_cat_id(meta['category'], meta),
            'types[]' : await self.get_type_id(meta['type']),
            'name' : ""
        }

        if self.is_non_video(meta):
            # Ni tmdbId ni resolución que filtrar aquí -- y meta['tmdb'] ni
            # siquiera existe en la rama de libro, así que leerlo reventaba
            # antes de hacer la petición.
            #
            # El filtro fino sería el id de edición, pero la filter API no lo
            # acepta: toma tmdbId/imdbId/tvdbId/malId y nada para isbn13, asin
            # ni igdb. Los parámetros desconocidos se tiran en silencio, así
            # que preguntarlo casaría con la categoría entera. Por título.
            params['name'] = meta.get('title') or meta.get('name') or ""
        else:
            params['tmdbId'] = meta['tmdb']
            params['resolutions[]'] = await self.get_res_id(meta['resolution'])
        if meta.get('edition', "") != "":
            params['name'] = params['name'] + f" {meta['edition']}"
        
        # Retry logic for dupe search (timeout issues on beta)
        max_retries = 2
        timeout = 15  # seconds
        
        for attempt in range(max_retries):
            try:
                # Respect rate limiter
                await rate_limiter.acquire(self.tracker)
                
                response = requests.get(url=self.search_url, params=params, timeout=timeout)
                try:
                    response_json = response.json()
                    for each in response_json['data']:
                        result = each['attributes']['name']
                        size = each['attributes']['size']
                        dupes[result] = size
                    self.logger.info(f"Dupe search found {len(dupes)} results")
                    break  # Success, exit retry loop
                except json.JSONDecodeError:
                    # Una línea, no la página entera (suele ser el HTML del login).
                    inicio = ' '.join(response.text[:200].split())
                    self.logger.error(f"Dupe search: HTTP {response.status_code}, no es JSON: {inicio}")
                    console.print('[bold red]Unable to search for existing torrents on site. Either the site is down or your API key is incorrect')
                    meta['dupe_busqueda_fallida'] = self.tracker
                    break

            except requests.exceptions.Timeout:
                self.logger.warning(f"Dupe search timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    console.print(f"[yellow]Timeout searching dupes, retrying... (attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(2)  # Wait before retry
                else:
                    console.print('[bold yellow]Timeout: Proceeding without dupe check')
                    self.logger.warning("Skipping dupe check due to repeated timeouts")
                    meta['dupe_busqueda_fallida'] = self.tracker

            except Exception as e:
                console.print('[bold red]Unable to search for existing torrents on site. Either the site is down or your API key is incorrect')
                # El texto de una excepción de requests lleva la URL con la api_token.
                motivo = type(e).__name__ if isinstance(e, requests.exceptions.RequestException) else f"{type(e).__name__}: {e}"
                self.logger.error(f"Dupe search error: {motivo}")
                meta['dupe_busqueda_fallida'] = self.tracker
                break
        
        return dupes


    def nombre_tracker(self, meta):
        built_name = meta['name']
        title = meta.get('title', '')
        aka = meta.get('aka', "")
        og_title = meta.get('original_title', "")
        
        if meta.get('original_language', '') in ('es', 'spa') and og_title:
            nombre = built_name.replace(title, og_title).strip()
        else: 
            nombre = built_name
            
        # Validate length
        while len(nombre) > 255:
            original_len = len(nombre)
            if aka:
                nombre = nombre.replace(aka, '')
            if len(nombre) <= 255:
                break
            
            resolution = meta.get('resolution', '')
            if resolution:
                nombre = nombre.replace(resolution, '')
            
            if len(nombre) <= 255:
                break
            type = meta.get('type', '')
            if type:
                nombre = nombre.replace(type, '')
            if len(nombre) <= 255:
                break
            
            if len(nombre) == original_len:
                # Break if no change in length to prevent infinite loop
                break
        return nombre[:255]