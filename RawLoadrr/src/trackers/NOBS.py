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


class NOBS():
    def __init__(self, config):
        self.config = config
        self.tracker = 'NOBS'
        self.source_flag = 'NOBS'
        self.upload_url = 'https://nobs.rawsmoke.net/api/torrents/upload'
        self.search_url = 'https://nobs.rawsmoke.net/api/torrents/filter'
        self.banned_groups = [""]
        self.logger = get_logger(self.tracker)
        pass
    
    # Read from the tracker's filter API on 2026-08-22, never from the admin
    # panel: that column is a display index, and taking it literally once put a
    # findOrFail(0) 404 in front of every MILNU upload.
    CATEGORY_IDS = {
        'MOVIE': '1', 'TV': '2', 'GAME': '3',
        'ANIME_MOVIE': '4', 'ANIME_TV': '5',
        'BOOK': '7', 'AUDIOBOOK': '8',
    }

    async def get_cat_id(self, category_name, meta=None):
        is_anime = bool(meta and (meta.get('anime') or int(meta.get('mal_id') or 0) != 0))
        if category_name == 'MOVIE':
            return self.CATEGORY_IDS['ANIME_MOVIE'] if is_anime else self.CATEGORY_IDS['MOVIE']
        elif category_name == 'TV':
            return self.CATEGORY_IDS['ANIME_TV'] if is_anime else self.CATEGORY_IDS['TV']
        return self.CATEGORY_IDS.get(category_name, '0')

    def is_non_video(self, meta):
        """A book, an audiobook or a game: no disc, no resolution, no mediainfo."""
        return (meta.get('category') in ('BOOK', 'AUDIOBOOK', 'GAME')
                or meta.get('is_book') or meta.get('is_audiobook')
                or meta.get('is_game'))

    async def get_type_id(self, type):
        type_id = {
            'DISC': '1', 
            'REMUX': '2',
            'WEBDL': '4', 
            'WEBRIP': '5', 
            'HDTV': '6',
            'ENCODE': '3',
            # Books are typed by container, not by "book": the tracker keeps
            # EPUB and PDF as different things because they are.
            'EPUB': '7',
            'PDF': '8',
            'MOBI': '9',
            'AZW3': '10',
            'CBZ/CBR': '11',
            'M4B': '12',
            'MP3': '13',
            # Games, by how you run them rather than by platform.
            'SCUMMVM': '14',
            'ROM': '15',
            'PC': '16',
            }.get(type, '0')
        return type_id

    async def get_res_id(self, resolution):
        # id 10 is "Other" on this tracker, not 8640p -- checked against the
        # resolutions table, not inferred from the ordering.
        resolution_id = {
            '8640p':'10', 
            '4320p': '1', 
            '2160p': '2', 
            '1440p' : '3',
            '1080p': '3',
            '1080i':'4', 
            '720p': '5',  
            '576p': '6', 
            '576i': '7',
            '480p': '8', 
            '480i': '9'
            }.get(resolution, '10')
        return resolution_id

    ###############################################################
    ######   STOP HERE UNLESS EXTRA MODIFICATION IS NEEDED   ######
    ###############################################################

    @staticmethod
    def _isbn13(meta):
        """
        13 dígitos y nada más; la API valida la forma, no el libro.

        `isbn13_obra` es el del LIBRO cuando lo que se sube es una lectura
        libre: la grabación no está en ningún catálogo y no tiene ASIN, pero
        la obra sí tiene ISBN y con él el tracker saca portada, sinopsis y
        autor. Va el último porque sólo aplica cuando no hay nada mejor.
        """
        for clave in ('isbn13', 'isbn', 'isbn13_obra'):
            raw = re.sub(r'[^0-9]', '', str(meta.get(clave) or ''))
            if len(raw) == 13:
                return raw

        return None

    @staticmethod
    def _asin(meta):
        """Audible ASINs are exactly 10 alphanumerics, uppercased."""
        raw = re.sub(r'[^A-Za-z0-9]', '', str(meta.get('asin') or '')).upper()
        return raw if len(raw) == 10 else None

    async def upload(self, meta):
        common = COMMON(config=self.config)
        await common.edit_torrent(meta, self.tracker, self.source_flag)
        cat_id = await self.get_cat_id(meta['category'], meta)
        type_id = await self.get_type_id(meta['type'])
        non_video = self.is_non_video(meta)

        # The API takes resolution_id as nullable outside movie/tv categories,
        # and it does NOT coerce it by category the way it coerces the id
        # fields. Sending the fallback would file every book under "Other",
        # which then answers a resolution filter it has no business in.
        resolution_id = None if non_video else await self.get_res_id(meta['resolution'])

        await common.unit3d_edit_desc(meta, self.tracker)
        region_id = await common.unit3d_region_ids(meta.get('region'))
        distributor_id = await common.unit3d_distributor_ids(meta.get('distributor'))
        if meta['anon'] != 0 or self.config['TRACKERS'][self.tracker].get('anon', False):
            anon = 1
        else:
            anon = 0

        # A book has no mediainfo and a ROM has no disc summary, and the book
        # branch of prep never reaches the code that writes either file. Reading
        # them unconditionally is what killed the upload before it ever posted.
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
        # Every field below used to be meta['...'], which is fine on the video
        # path where prep fills them all and fatal everywhere else.
        data = {
            'name' : manual_name or self.nobs_name(meta),
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
            # The tracker keys a game on IGDB, a book on its ISBN-13 and an
            # audiobook on its Audible ASIN, and it fills the header card from
            # whichever one arrives. It nulls the ids that do not belong to the
            # category, so an id sent to the wrong one is dropped, not rejected.
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
            if meta.get('tag') and (meta['tag'][1:] in self.config['TRACKERS'][self.tracker].get('internal_groups', [])):
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
                            console.print(f"[blue]Server Response[/blue]: {response.text}")
                            self.logger.error(f"HTTP {response.status_code} - Could not parse response, raw text below")
                            self.logger.error(response.text) # Log raw text if not parsed
                    except Exception as parse_error:
                        console.print(f"[red]Failed to parse error response: {parse_error}[/red]")
                        console.print(f"[blue]Server Response[/blue]: {response.text}")
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
            # No tmdbId and no resolution to filter on -- and meta['tmdb'] does
            # not even exist on the book path, so reading it raised before the
            # request was ever made.
            #
            # The sharp filter would be the edition id, because two uploads of
            # one ISBN really are the same edition. The filter API does not
            # take one: TorrentSearchFiltersDTO accepts tmdbId/imdbId/tvdbId/
            # malId and nothing for isbn13, asin or igdb. Unknown params are
            # dropped silently, so asking would quietly match the whole
            # category. Title it is, until the tracker grows the filter.
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
                    self.logger.error("Failed to decode JSON from response. Response text:")
                    self.logger.error(response.text)
                    console.print('[bold red]Unable to search for existing torrents on site. Either the site is down or your API key is incorrect')
                    break

            except requests.exceptions.Timeout:
                self.logger.warning(f"Dupe search timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    console.print(f"[yellow]Timeout searching dupes, retrying... (attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(2)  # Wait before retry
                else:
                    console.print('[bold yellow]Timeout: Proceeding without dupe check')
                    self.logger.warning("Skipping dupe check due to repeated timeouts")
                    
            except Exception as e:
                console.print('[bold red]Unable to search for existing torrents on site. Either the site is down or your API key is incorrect')
                self.logger.error(f"Dupe search error: {str(e)}")
                break
        
        return dupes


    def nobs_name(self, meta):
        built_name = meta['name']
        title = meta.get('title', '')
        aka = meta.get('aka', "")
        og_title = meta.get('original_title', "")
        
        if meta.get('original_language', '') in ('es', 'spa') and og_title:
            nobs_name = built_name.replace(title, og_title).strip()
        else: 
            nobs_name = built_name
            
        # Validate length
        while len(nobs_name) > 255:
            original_len = len(nobs_name)
            if aka:
                nobs_name = nobs_name.replace(aka, '')
            if len(nobs_name) <= 255:
                break
            
            resolution = meta.get('resolution', '')
            if resolution:
                nobs_name = nobs_name.replace(resolution, '')
            
            if len(nobs_name) <= 255:
                break
            type = meta.get('type', '')
            if type:
                nobs_name = nobs_name.replace(type, '')
            if len(nobs_name) <= 255:
                break
            
            if len(nobs_name) == original_len:
                # Break if no change in length to prevent infinite loop
                break
        return nobs_name[:255]