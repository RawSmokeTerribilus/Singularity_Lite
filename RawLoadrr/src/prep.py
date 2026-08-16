# -*- coding: utf-8 -*- 
import sys
from src.args import Args
from src.console import console, log
from src.id_resolver import (resolve as _resolve_ids,
                             save_override as _save_override,
                             report_pending as _report_pending)
from src.discparse import DiscParse
from src.exceptions import ManualDateException, WeirdSystem, XEMNotFound
from src.trackers.PTP import PTP
from src.trackers.BLU import BLU
from src.trackers.HDB import HDB
from src.trackers.COMMON import COMMON
from src.musicbrainz import MusicBrainzAPI
from src.discogs import DiscogsAPI

try:
    import aiofiles
    import aiohttp
    import anitopy
    import asyncio
    import base64
    import ffmpeg
    import glob
    import itertools
    import json
    import langcodes
    import logging
    import math
    import multiprocessing
    import nest_asyncio
    import ntpath
    import os
    import platform
    import pyimgbox
    import random
    import re
    import requests
    import shutil
    import subprocess
    import time
    import traceback
    import tmdbsimple as tmdb
    import urllib
    import urllib.parse
    from os.path import basename
    from datetime import datetime, date, timedelta
    from collections import defaultdict
    from difflib import SequenceMatcher
    from guessit import guessit
    from imdb import Cinemagoer
    from pathlib import Path
    from pymediainfo import MediaInfo
    from rich.prompt import Prompt
    from rich.progress import Progress, TextColumn, BarColumn, TimeRemainingColumn
    from rich.traceback import install, Traceback
    from requests.exceptions import HTTPError
    from torf import Torrent
    from tinytag import TinyTag
    from typing import Dict, Any, Optional


except ModuleNotFoundError:
    console.print(traceback.print_exc())
    console.print('[bold red]Missing Module Found. Please reinstall required dependencies.')
    console.print('[yellow]pip3 install --user -U -r requirements.txt')
    exit()
except KeyboardInterrupt:
    exit()
install ()
class Prep():
    """
    Prepare for upload:
        Mediainfo/BDInfo
        Screenshots
        Database Identifiers (TMDB/IMDB/MAL/etc)
        Create Name
    """
    def __init__(self, screens, img_host, config):
        self.screens = screens
        self.config = config
        self.img_host = img_host.lower()
        tmdb.API_KEY = config['DEFAULT']['tmdb_api']


    async def gather_prep(self, meta, mode):        
        meta.update(
            mode=mode,
            isdir=os.path.isdir(meta['path']),
            base_dir=os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
            is_music=False,
            is_video=False,
            filelist={},
            user_images=[],
            log_files=[],
            nfo_file='',
            uuid=meta.get('uuid') or os.path.basename(meta['path'])  
        )

        # Create temp directory
        tmp_dir = Path(f"{meta['base_dir']}/tmp/{meta['uuid']}")
        tmp_dir.mkdir(parents=True, exist_ok=True)

        log.debug(f"[cyan]ID: {meta['uuid']}")

        def collect_files_recursive(directory, meta):
            """Recursively collect files in a directory."""
            return {
                f"{path.parent.name}/{path.name}" if path.parent.name != meta['uuid'] else f"BASE/{path.name}": str(path)
                for path in Path(directory).rglob('*')
                if path.is_file() and not path.name.startswith('._')
            }


        if meta['isdir']:
            try:
                all_files = collect_files_recursive(meta['path'], meta)
                base_name = os.path.basename(meta['path'])
                nfo_candidates = []
                for file_name, full_path in all_files.items():
                    ext = os.path.splitext(file_name)[1].lower()

                    if ext in ['.aac', 'ac3', '.alac', '.eac3', '.flac', '.m4a', '.mp3', '.ogg', '.ogm', '.oga', '.ogv', '.ogx', '.opus', '.spx', '.wav']:
                        key = file_name
                        meta['filelist'][key] = full_path
                        meta['is_music'] = True
                    elif ext in ['.jpg', '.jpeg', '.png', '.svg', '.ico', '.gif']:
                        meta['user_images'].append(full_path)   
                    elif ext == '.nfo':
                        nfo_candidates.append((file_name, full_path))

                    if nfo_candidates:
                        for file_name, full_path in nfo_candidates:
                            if file_name == f"{base_name}.nfo":
                                meta['nfo_file'] = full_path
                                break  
                        else:                            
                            meta['nfo_file'] = nfo_candidates[0][1]
                        
                    elif ext == '.log':
                        meta['log_files'].append(full_path)

                video_extensions = ['.mkv', '.mp4', '.ts', '.avi']
                if not meta['is_music']:
                    meta['is_video'] = any(full_path.lower().endswith(tuple(video_extensions)) for full_path in all_files.values())

                if meta['is_music']:
                    console.print('[red]Processing as music')
                    await self.get_music(meta, mode)

            except Exception as e:
                log.error(f"Error processing directory: {e}")

        else:  # Single file scenario
            file_extension = os.path.splitext(meta['path'])[1].lower()
            if file_extension in ['.aac', '.alac', '.flac', '.m4a', '.mp3', '.opus', '.wav']:
                meta['is_music'] = True
                console.print('[red]Processing as music')
                await self.process_single_music_file(meta, mode)
            elif file_extension in ['.mkv', '.mp4', '.ts', '.avi']:
                meta['is_video'] = True
                base_name = os.path.splitext(meta['path'])[0]
                match_nfo = f"{base_name}.nfo"
                if os.path.exists(match_nfo):
                    meta['nfo_file'] = match_nfo
                elif not os.path.exists(match_nfo) and meta.get('nfo'):
                    directory = os.path.dirname(meta['path'])
                    nfo_files = [f for f in os.listdir(directory) if f.endswith('.nfo')]
                    if nfo_files:
                        meta['nfo_file'] = os.path.join(directory, nfo_files[0])
        if meta['is_music']:
            meta = await self.gen_desc(meta)
            return meta

        else:
            # Video processing logic
            console.print('[red]Processing as video')
            meta['is_disc'], videoloc, bdinfo, meta['discs'] = await self.get_disc(meta)        
            
            # If BD:
            if meta['is_disc'] == "BDMV":
                video, meta['scene'], meta['imdb'] = self.is_scene(meta['path'], meta.get('imdb', None))
                meta['filelist'] = []
                try:
                    guess_name = bdinfo['title'].replace('-',' ')
                    filename = guessit(re.sub(r"[^0-9a-zA-Z\[\]]+", " ", guess_name), {"excludes" : ["country", "language"]})['title']
                    untouched_filename = bdinfo['title']
                    try:
                        meta['search_year'] = guessit(bdinfo['title'])['year']
                    except Exception:
                        meta['search_year'] = ""
                except Exception:
                    guess_name = bdinfo['label'].replace('-',' ')
                    filename = guessit(re.sub(r"[^0-9a-zA-Z\[\]]+", " ", guess_name), {"excludes" : ["country", "language"]})['title']
                    untouched_filename = bdinfo['label']
                    try:
                        meta['search_year'] = guessit(bdinfo['label'])['year']
                    except Exception:
                        meta['search_year'] = ""

                if meta.get('resolution', None) is None:
                    meta['resolution'] = self.mi_resolution(bdinfo['video'][0]['res'], guessit(video), width="OTHER", scan="p", height="OTHER", actual_height=0)
                # if meta.get('sd', None) is None:
                meta['sd'] = self.is_sd(meta['resolution'])

                mi = None
                mi_dump = None
            #IF DVD
            elif meta['is_disc'] == "DVD":
                video, meta['scene'], meta['imdb'] = self.is_scene(meta['path'], meta.get('imdb', None))
                meta['filelist'] = []
                guess_name = meta['discs'][0]['path'].replace('-',' ')
                # filename = guessit(re.sub("[^0-9a-zA-Z]+", " ", guess_name))['title']
                filename = guessit(guess_name, {"excludes" : ["country", "language"]})['title']
                untouched_filename = os.path.basename(os.path.dirname(meta['discs'][0]['path']))
                try:
                    meta['search_year'] = guessit(meta['discs'][0]['path'])['year']
                except Exception:
                    meta['search_year'] = ""
                if not meta.get('edit', False):
                    mi = self.exportInfo(f"{meta['discs'][0]['path']}/VTS_{meta['discs'][0]['main_set'][0][:2]}_1.VOB", False, meta['uuid'], meta['base_dir'], export_text=False)
                    meta['mediainfo'] = mi
                else:
                    mi = meta['mediainfo']
                
                #NTSC/PAL
                meta['dvd_size'] = await self.get_dvd_size(meta['discs'])
                meta['resolution'] = self.get_resolution(guessit(video), meta['uuid'], meta['base_dir'])
                meta['sd'] = self.is_sd(meta['resolution'])
            elif meta['is_disc'] == "HDDVD":
                video, meta['scene'], meta['imdb'] = self.is_scene(meta['path'], meta.get('imdb', None))
                meta['filelist'] = []
                guess_name = meta['discs'][0]['path'].replace('-','')
                filename = guessit(guess_name, {"excludes" : ["country", "language"]})['title']
                untouched_filename = os.path.basename(meta['discs'][0]['path'])
                videopath = meta['discs'][0]['largest_evo']
                try:
                    meta['search_year'] = guessit(meta['discs'][0]['path'])['year']
                except Exception:
                    meta['search_year'] = ""
                if not meta.get('edit', False):
                    mi = self.exportInfo(meta['discs'][0]['largest_evo'], False, meta['uuid'], meta['base_dir'], export_text=False)
                    meta['mediainfo'] = mi
                else:
                    mi = meta['mediainfo']
                meta['resolution'] = self.get_resolution(guessit(video), meta['uuid'], meta['base_dir'])
                meta['sd'] = self.is_sd(meta['resolution'])
            #If NOT BD/DVD/HDDVD
            else:
                videopath, meta['filelist'] = self.get_video(videoloc, meta.get('mode', 'discord')) 
                video, meta['scene'], meta['imdb'] = self.is_scene(videopath, meta.get('imdb', None))
                guess_name = ntpath.basename(video).replace('-',' ')
                try:
                    cleaned = re.sub(r"[^0-9a-zA-Z\[\]]+", " ", guess_name)
                    parsed = guessit(cleaned, {"excludes": ["country", "language"]})
                    filename = parsed.get("title") or guessit(
                        re.sub("[^0-9a-zA-Z]+", " ", guess_name),
                        {"excludes": ["country", "language"]}
                    ).get("title") or guess_name
                except Exception:
                    filename = guess_name
                _parent = re.sub(r'\s*[-_]?\s*(?<![a-zA-Z])(?:s|season|temporada|saison|staffel|stagione|seizoen|sezon|сезон|시즌|シーズン|季)\s*\d+\s*$', '', ntpath.basename(ntpath.dirname(video)), flags=re.IGNORECASE).strip()
                _grandparent = re.sub(r'\s*\(\d{4}\)', '', ntpath.basename(ntpath.dirname(ntpath.dirname(video)))).strip()
                if filename and filename.lower() not in _parent.lower() and filename.lower() not in _grandparent.lower():
                    filename = _parent if _parent else _grandparent
                untouched_filename = os.path.basename(video)
                try:
                    meta['search_year'] = guessit(video)['year']
                except Exception:
                    meta['search_year'] = ""
                
                if not meta.get('edit', False):
                    mi = self.exportInfo(videopath, meta['isdir'], meta['uuid'], meta['base_dir'], export_text=True)
                    meta['mediainfo'] = mi
                else:
                    mi = meta['mediainfo']

                if meta.get('resolution', None) is None:
                    meta['resolution'] = self.get_resolution(guessit(video), meta['uuid'], meta['base_dir'])
                # if meta.get('sd', None) is None:
                meta['sd'] = self.is_sd(meta['resolution'])

                
            
            if " AKA " in filename.replace('.',' '):
                filename = filename.split('AKA')[0]
            meta['filename'] = filename
            meta['title'] = filename
            meta['year'] = meta['search_year']

            meta['bdinfo'] = bdinfo
            


            # Reuse information from other trackers
            if str(self.config['TRACKERS'].get('PTP', {}).get('useAPI')).lower() == "true":
                ptp = PTP(config=self.config)
                if meta.get('ptp', None) is not None:
                    meta['ptp_manual'] = meta['ptp']
                    meta['imdb'], meta['ext_torrenthash'] = await ptp.get_imdb_from_torrent_id(meta['ptp'])
                else:
                    if meta['is_disc'] in [None, ""]:
                        ptp_search_term = os.path.basename(meta['filelist'][0])
                        search_file_folder = 'file'
                    else:
                        search_file_folder = 'folder'
                        ptp_search_term = os.path.basename(meta['path'])
                    ptp_imdb, ptp_id, meta['ext_torrenthash'] = await ptp.get_ptp_id_imdb(ptp_search_term, search_file_folder)
                    if ptp_imdb is not None:
                        meta['imdb'] = ptp_imdb
                    if ptp_id is not None:
                        meta['ptp'] = ptp_id
            
            if str(self.config['TRACKERS'].get('HDB', {}).get('useAPI')).lower() == "true":
                hdb = HDB(config=self.config)
                if meta.get('ptp', None) is None or meta.get('hdb', None) is not None:
                    hdb_imdb = hdb_tvdb = hdb_id = None
                    hdb_id = meta.get('hdb')
                    if hdb_id is not None:
                        meta['hdb_manual'] = hdb_id
                        hdb_imdb, hdb_tvdb, meta['hdb_name'], meta['ext_torrenthash'] = await hdb.get_info_from_torrent_id(hdb_id)
                    else:
                        if meta['is_disc'] in [None, ""]:
                            hdb_imdb, hdb_tvdb, meta['hdb_name'], meta['ext_torrenthash'], hdb_id = await hdb.search_filename(meta['filelist'])
                        else:
                            # Somehow search for disc
                            pass
                    if hdb_imdb is not None:
                        meta['imdb'] = str(hdb_imdb)
                    if hdb_tvdb is not None:
                        meta['tvdb_id'] = str(hdb_tvdb)
                    if hdb_id is not None:
                        meta['hdb'] = hdb_id
            
            if str(self.config['TRACKERS'].get('BLU', {}).get('useAPI')).lower() == "true":
                blu = BLU(config=self.config)
                if meta.get('blu', None) is not None:
                    meta['blu_manual'] = meta['blu']
                    blu_tmdb, blu_imdb, blu_tvdb, blu_mal, blu_desc, blu_category, meta['ext_torrenthash'], blu_imagelist = await COMMON(self.config).unit3d_torrent_info("BLU", blu.torrent_url, meta['blu'])
                    if blu_tmdb not in [None, '0']:
                        meta['tmdb_manual'] = blu_tmdb
                    if blu_imdb not in [None, '0']:
                        meta['imdb'] = str(blu_imdb)
                    if blu_tvdb not in [None, '0']:
                        meta['tvdb_id'] = blu_tvdb
                    if blu_mal not in [None, '0']:
                        meta['mal'] = blu_mal
                    if blu_desc not in [None, '0', '']:
                        meta['blu_desc'] = blu_desc
                    if blu_category.upper() in ['MOVIE', 'TV SHOW', 'FANRES']:
                        if blu_category.upper() == 'TV SHOW':
                            meta['category'] = 'TV'
                        else:
                            meta['category'] = blu_category.upper()
                    if meta.get('image_list', []) == []:
                        meta['image_list'] = blu_imagelist
                else:
                    # Seach automatically
                    pass

            # Take Screenshots
            # Las subidas de RawLoadrr tonemapean el HDR por defecto (la puerta
            # real está en screenshots(): sólo actúa si la fuente es PQ/HLG con
            # capa base compatible). Se respeta un False explícito del llamante.
            # BDMV queda fuera a propósito: disc_screenshots() es otra ruta.
            meta['tonemap'] = meta.get('tonemap', True)
            if meta['is_disc'] == "BDMV":
                if not meta.get('edit', False):
                    if meta.get('vapoursynth', False):
                        use_vs = True
                    else:
                        use_vs = False
                    try:
                        ds = multiprocessing.Process(target=self.disc_screenshots, args=(filename, bdinfo, meta['uuid'], meta['base_dir'], use_vs, meta.get('image_list', []), meta.get('ffdebug', False), None))
                        ds.start()
                        while ds.is_alive():
                            await asyncio.sleep(1)
                    except KeyboardInterrupt:
                        ds.terminate() 
            elif meta['is_disc'] == "DVD":
                if not meta.get('edit', False):
                    try:
                        ds = multiprocessing.Process(target=self.dvd_screenshots, args=(meta, 0, None))
                        ds.start()
                        while ds.is_alive():
                            await asyncio.sleep(1)
                    except KeyboardInterrupt:
                        ds.terminate()
            else:
                if not meta.get('edit', False):
                    try:
                        s = multiprocessing.Process(target=self.screenshots, args=(videopath, filename, meta['uuid'], meta['base_dir'], meta))
                        s.start()
                        while s.is_alive():
                            await asyncio.sleep(3)
                    except KeyboardInterrupt:
                        s.terminate()


            meta['tmdb'] = meta.get('tmdb_manual', None)
            if meta.get('type', None) is None:
                meta['type'] = self.get_type(video, meta['scene'], meta['is_disc'])
            if meta.get('category', None) is None:
                meta['category'] = self.get_cat(video)
            else:
                meta['category'] = meta['category'].upper()
            if meta.get('tmdb', None) is None and meta.get('imdb', None) is None:
                meta['category'], meta['tmdb'], meta['imdb'] = self.get_tmdb_imdb_from_mediainfo(mi, meta['category'], meta['is_disc'], meta['tmdb'], meta['imdb'])      
            if meta.get('tmdb', None) is None and meta.get('imdb', None) is None:
                meta = await self.resolve_ids(meta, filename, aliases=[_parent, _grandparent])
            elif meta.get('imdb', None) is not None and meta.get('tmdb_manual', None) is None:
                meta['imdb_id'] = str(meta['imdb']).replace('tt', '')
                meta = await self.get_tmdb_from_imdb(meta, filename)
            else:
                meta['tmdb_manual'] = meta.get('tmdb', None)

            # Check for TMDb not found
            uuid = meta.get('uuid', '')
            if meta.get('unattended') and (meta.get('tmdb') is None or int(meta['tmdb']) == 0):
                meta['tmdb_not_found'] = True
                console.print(f"[red]Unable to find TMDb match for {Path(uuid).stem}")
                return meta
                
            # If no tmdb, use imdb for meta
            if int(meta['tmdb']) == 0:
                meta = await self.imdb_other_meta(meta)
            else:
                meta = await self.tmdb_other_meta(meta)
            # Search tvmaze
            meta['tvmaze_id'], meta['imdb_id'], meta['tvdb_id'] = await self.search_tvmaze(filename, meta['search_year'], meta.get('imdb_id','0'), meta.get('tvdb_id', 0))
            # If no imdb, search for it
            if meta.get('imdb_id', None) is None:
                meta['imdb_id'] = await self.search_imdb(filename, meta['search_year'])
            if meta.get('imdb_info', None) is None and int(meta['imdb_id']) != 0:
                meta['imdb_info'] = await self.get_imdb_info(meta['imdb_id'], meta)            
            if meta.get('tag', None) is None:
                meta['tag'] = self.get_tag(video, meta)
            else:
                if not meta['tag'].startswith('-') and meta['tag'] != "":
                    meta['tag'] = f"-{meta['tag']}"
            meta = await self.get_season_episode(video, meta)
            meta = await self.tag_override(meta)

            meta['video'] = video
            meta['audio'], meta['channels'], meta['has_commentary'] = self.get_audio_v2(mi, meta, bdinfo)
            if meta['tag'][1:].startswith(meta['channels']):
                meta['tag'] = meta['tag'].replace(f"-{meta['channels']}", '')
            if meta.get('no_tag', False):
                meta['tag'] = ""
            meta['3D'] = self.is_3d(mi, bdinfo)
            meta['source'], meta['type'] = self.get_source(meta['type'], video, meta['path'], meta['is_disc'], meta)
            if meta.get('service', None) in (None, ''):
                meta['service'], meta['service_longname'] = self.get_service(video, meta.get('tag', ''), meta['audio'], meta['filename'])
            meta['uhd'] = self.get_uhd(meta['type'], guessit(meta['path']), meta['resolution'], meta['path'])
            meta['hdr'] = self.get_hdr(mi, bdinfo)
            meta['distributor'] = self.get_distributor(meta['distributor'])
            if meta.get('is_disc', None) == "BDMV": #Blu-ray Specific
                meta['region'] = self.get_region(bdinfo, meta.get('region', None))
                meta['video_codec'] = self.get_video_codec(bdinfo)
            else:
                meta['video_encode'], meta['video_codec'], meta['has_encode_settings'], meta['bit_depth'] = self.get_video_encode(mi, meta['type'], bdinfo)
            
            meta['edition'], meta['repack'], meta['cut'], meta['ratio'] = self.get_edition(meta['title'], meta['path'], bdinfo, meta['filelist'], meta.get('manual_edition'))
            if "REPACK" in meta.get('edition', ""):
                meta['repack'] = re.search(r"REPACK[\d]?", meta['edition'])[0]
                meta['edition'] = re.sub(r"REPACK[\d]?", "", meta['edition']).strip().replace('  ', ' ')
            
            
            
            #WORK ON THIS
            meta.get('stream', False)
            meta['stream'] = self.stream_optimized(meta['stream'])
            meta.get('anon', False)
            meta['anon'] = self.is_anon(meta['anon'])
                
            
            
            meta = await self.gen_desc(meta)
            return meta


    """
    Determine if disc and if so, get bdinfo
    """
    async def get_disc(self, meta):
        is_disc = None
        videoloc = meta['path']
        bdinfo = None
        bd_summary = None
        discs = []
        parse = DiscParse()
        for path, directories, files in os. walk(meta['path']):
            for each in directories:
                if each.upper() == "BDMV": #BDMVs
                    is_disc = "BDMV"
                    disc = {
                        'path' : f"{path}/{each}",
                        'name' : os.path.basename(path),
                        'type' : 'BDMV',
                        'summary' : "",
                        'bdinfo' : ""
                    }
                    discs.append(disc)
                elif each == "VIDEO_TS": #DVDs
                    is_disc = "DVD"
                    disc = {
                        'path' : f"{path}/{each}",
                        'name' : os.path.basename(path),
                        'type' : 'DVD',
                        'vob_mi' : '',
                        'ifo_mi' : '',
                        'main_set' : [],
                        'size' : ""
                    }
                    discs.append(disc)
                elif each == "HVDVD_TS":
                    is_disc = "HDDVD"
                    disc = {
                        'path' : f"{path}/{each}",
                        'name' : os.path.basename(path),
                        'type' : 'HDDVD',
                        'evo_mi' : '',
                        'largest_evo' : ""
                    }
                    discs.append(disc)
        if is_disc == "BDMV":
            if not meta.get('edit', False):
                discs, bdinfo = await parse.get_bdinfo(discs, meta['uuid'], meta['base_dir'], meta.get('discs', []))
            else:
                discs, bdinfo = await parse.get_bdinfo(meta['discs'], meta['uuid'], meta['base_dir'], meta['discs'])
        elif is_disc == "DVD":
            discs = await parse.get_dvdinfo(discs)
            export = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt", 'w', newline="", encoding='utf-8')
            export.write(discs[0]['ifo_mi'])
            export.close()
        elif is_disc == "HDDVD":
            discs = await parse.get_hddvd_info(discs)
            export = open(f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt", 'w', newline="", encoding='utf-8')
            export.write(discs[0]['evo_mi'])
            export.close()
        discs = sorted(discs, key=lambda d: d['name'])
        return is_disc, videoloc, bdinfo, discs



    """
    Get video files

    """

    def get_video(self, videoloc, mode):
        filelist = []
        dirlist = []
        videoloc = os.path.normpath(os.path.abspath(videoloc))
        video_extensions = ['.mkv', '.mp4', '.ts', '.avi']
        
        if os.path.isdir(videoloc):
            for file in os.listdir(videoloc):
                full_path = os.path.join(videoloc, file)
                if not file.startswith('._'):
                    if os.path.isfile(full_path) and any(file.lower().endswith(ext) for ext in video_extensions):
                        if not (file.lower().endswith('sample.mkv') or file.lower().startswith('!sample')):
                            filelist.append(full_path)
                    elif os.path.isdir(full_path) and 'trickplay' not in file.lower():
                        dirlist.append(full_path)

            # If no videos are found, try all subdirectories
            if not filelist and dirlist:
                    for dir in dirlist:
                        # Get videos in subdir
                        _, subdir_filelist = self.get_video(dir, mode)
                        if subdir_filelist:
                            filelist += subdir_filelist

            if filelist:
                video = sorted(filelist)[0]
            else:
                console.print(f"[red]No video files found in {videoloc}")
                if mode == 'cli':
                    exit()
                return None, []
        else:
            video = videoloc
            if not os.path.basename(videoloc).startswith('._') and os.path.splitext(videoloc)[1].lower() in video_extensions:
                filelist.append(videoloc)
        
        return video, filelist

    async def get_music(self, meta, mode):
        log.debug("Starting get_music")
        meta['is_music'] = True
        base_dir = meta['base_dir']
        musicloc = meta['path']
        
        console.print(f"[yellow]Checking music directory: {musicloc}")
        
        # Sort tracks, covers, and proof
        tracks, cover, back_cover, proof = self.sort_music(meta, musicloc)
        meta.update(
            track_count=len(tracks),
            user_cover=cover,
            back_cover=back_cover,
            user_proof=proof,
        )
        
        if not self.has_sufficient_music_metadata(meta):
            await self.process_music(meta, base_dir, tracks)
            log.debug("Music processing completed")
        
        meta = await self.gen_desc(meta)
        
        return meta

    """SORT MUSIC"""

    def sort_music(self, meta, musicloc):
        cover, back_cover, proof = None, None, None
        tracks = []

        has_disc_folders = any(
            any(folder_name.lower() in key.lower() for folder_name in ['disc', 'cd', 'record'])
            for key in meta['filelist'].keys()
        ) and not any(key.startswith("BASE/") for key in meta['filelist'].keys())

        log.debug(f"[blue]Has disc folders: {has_disc_folders}")

        for key, path in meta['filelist'].items():
            if isinstance(key, str):
                log.debug(f"[magenta]Processing file: {key}")

                if key.startswith("BASE/"):
                    file = key.replace("BASE/", "")
                    track_match = re.match(r'([A-Z]|\d+)(?:[\s.-])?(.*)', file, re.IGNORECASE)
                    if track_match:
                        track_num = track_match.group(1)
                        rest_of_file = track_match.group(2).strip()
                    else:
                        log.warning(f"[yellow]Could not extract track number from: {file}")
                        continue

                    if len(track_num) == 3 and track_num.isdigit():
                        disc_num = track_num[0]
                        disc = f"Disc {int(disc_num)}"
                        track = track_num[1:]
                        file_parts = file.split('-')
                        file = '-'.join([track] + file_parts[1:])
                        log.debug(f"[yellow]Detected multi-disc album: {disc}, File: {file}")
                    else:
                        disc = "Disc 1"
                        log.debug(f"[yellow]Detected single disc album: {disc}, File: {file}")

                else:
                    disc, file = key.split('/', 1) if '/' in key else ("Disc 1", key)

                ext = os.path.splitext(file)[1].lower()

                if ext in ['.aac', 'ac3', '.alac', '.flac', '.m4a', '.mp3', '.ogg', '.ogm', '.oga', '.ogv', '.ogx', '.opus', '.spx', '.wav']:
                    if not (file.lower().endswith('sample.mp3') or file.lower().startswith('!sample')):
                        if len(file.split()[0]) > 2 and file.split()[0].isdigit():
                            file = re.sub(r'^\d{3}-', '01-', file)
                            log.debug(f"[purple]Adjusted file for multi-disc: {file}")
                        tracks.append((f"{disc}", file, path))
                        log.debug(f"[green]Added music file: {disc}, File: {file}")

            else:
                disc = "Disc 1"
                file = key

        # Check for alphanumeric track numbers to set source and adjust disc label
        has_alpha_tracks = any(re.match(r'^[A-Z]\d*$', os.path.splitext(x[1])[0].split()[0], re.IGNORECASE) for x in tracks)
        if has_alpha_tracks:
            meta['source'] = 'VINYL'
            tracks = [(disc.replace("Disc 1", "Record"), file, path) for disc, file, path in tracks]
        
        #Sort tracks
        def sort_key(x):
            disc_num = re.search(r'\d+', x[0].split()[-1])
            disc_number = int(disc_num.group()) if disc_num else 1  # Default to 1 if no number found
            
            track_part = os.path.splitext(x[1])[0].split()[0]
            match = re.match(r'^([A-Z])?(\d+)(?:[\s\.\-_\,](\d+))?', track_part, re.IGNORECASE)
            
            if match:
                letter, number, sub_number = match.groups()
                primary = (ord(letter.upper()) - ord('A') + 1) * 1000 if letter else 0
                secondary = int(number or '0') * 10
                tertiary = int(sub_number or '0')
                return (disc_number, primary + secondary + tertiary)
            elif track_part.isdigit():
                return (disc_number, int(track_part))
            else:
                return (disc_number, 999)


        # Handle cover and proof images

        front_cover = ['front', 'cover', 'album', 'disc1', 'cd1']
        back_covers = ['back_cover', 'rear', 'backside', 'back']

        for path in meta['user_images']:
            file = os.path.basename(path).lower().replace(' ', '')

            if 'proof' in file:
                proof = path
                log.debug(f"[magenta]Assigned proof image: {proof}")
                continue

            ext = os.path.splitext(file)[1].lower()
            if ext not in ['.jpg', '.jpeg']:
                continue

            if any(variant in file for variant in back_covers):
                back_cover = path
                log.debug(f"[yellow]Assigned back cover image: {back_cover}")
                continue

            if cover is None:
                if any(variant in file for variant in front_cover):
                    cover = path
                    log.debug(f"[cyan]Found cover image: {cover}")
                    continue

            if cover is None and 'proof' not in file:
                cover = path
                log.debug(f"[cyan]Fallback cover image: {cover}")

        tracks.sort(key=sort_key)
        log.debug(f"[blue]Sorted tracks: {tracks}")
        return tracks, cover, back_cover, proof


    #SINGLES NEED TO BE REVAMPED TO NEW STRUCTURE
    async def process_single_music_file(self, meta, mode):
        musicloc = meta['path']
        if not os.path.basename(musicloc).startswith('._'):
            meta['filelist'] = {musicloc: musicloc}
            meta['track_count'] = 1
            
            parent_dir = os.path.dirname(musicloc)
            for entry in os.listdir(parent_dir):
                if not entry.startswith('._'):
                    full_path = os.path.join(parent_dir, entry)
                    if os.path.isfile(full_path):
                        if entry.lower() in ('cover.jpg', 'cover.jpeg', 'front.jpg', 'front.jpeg', 'album.jpg', 'album.jpeg'):
                            meta['user_cover'] = full_path
                            break  # Only need one cover
                        elif entry.lower().endswith(('.jpg', '.jpeg')) and 'proof' in entry.lower():
                            meta['user_proof'] = full_path
                            break  # Only need one proof, likely not needed as singles mostly digital

            if not self.has_sufficient_music_metadata(meta) or meta.get('mbid_manual'):
                await self.process_music(meta, meta['base_dir'])
            
            meta = await self.gen_desc(meta)
        return meta


    """
    Process Music

    """
    def has_sufficient_music_metadata(self, meta):
        required_keys = ['artist', 'album', 'year', 'source', 'type', 'bitrate', 'duration', 'mbid', 'album_cover']
        return all(meta.get(key, None) for key in required_keys)

    async def process_music(self, meta, base_dir, tracks):  
        log.debug("Starting process_music") 
        mb = MusicBrainzAPI("Art2Album / 1.0")

        if self.has_sufficient_music_metadata(meta):
            return meta

        #INITIALIZE META 
        log.debug('About to first_audio_file')
        first_audio_file = tracks[0][2]
        mi = self.exportInfo(first_audio_file, meta['isdir'], meta['uuid'], base_dir, export_text=True)
        meta.update(
            is_disc=False,
            screens=0,
            edition='',
            bdinfo=None,
            tmdb=0,
            imdb=0,
            tvdb_id=0,
            mal_id=0,
            sd=0,
            keywords='',
            category="MUSIC",
            mediainfo=mi
        )

        album_fname = meta['uuid']   
        album_fname = album_fname.replace('&', 'AANDD')
        normalized_af = re.sub(r'[^a-zA-Z0-9\sAANDD]', ' ', album_fname)
        normalized_af = normalized_af.replace('AANDD', '&')
        meta['source'] = meta.get('format', self.get_music_source(normalized_af))
        user_tag = meta.get('tag')
        meta['tag'] = user_tag if user_tag else self.extract_tag(album_fname)

        track = next((track for track in mi['media']['track'] if track['@type'] == 'General'), None)
        if track:
            meta['type'] = (track.get('Format').replace('MPEG Audio', 'MP3').replace('-', '')).upper()
            meta['duration'] = track.get('Duration', 'Unknown')
            meta['artist'] = track.get('Album_Performer', track.get('Performer', None))
            meta['album'] = track.get('Album', None)
            recorded_date = track.get('Recorded_Date', track.get('Released_Date', track.get('Year', None)))
            if recorded_date and '-' in recorded_date:
                meta['year'] = recorded_date.split('-')[0]
                meta['release_date'] = recorded_date
            else:
                meta['year'] = recorded_date
                meta['release_date'] = ''
            extra = track.get('extra', {}) if track else {}
            meta['artist'] = meta['artist'] or extra.get('ARTISTS', extra.get('Performer', None))
            meta['album'] = meta['album'] or extra.get('Album', None)
            meta['year'] = meta['year'] or extra.get('Year', extra.get('Recorded_Date', extra.get('Released_Date', None)))
            
            meta['barcode'] = next(
                (value for key, value in track.items() if key.lower() in ['barcode', 'upc', 'scanid', 'scan_id']), 
                next((value for key, value in extra.items() if key.lower() in ['barcode', 'upc', 'scanid', 'scan_id']), None)
            )

        audio_track = next((track for track in mi['media']['track'] if track['@type'] == 'Audio'), None)
        if audio_track: 
            meta['bit_depth'] = audio_track.get('BitDepth', '')
            meta['bitrate'] = audio_track.get('BitRate_String', '').replace('/', '').replace(' ', '') 

        meta['catalog_number'] = self.extract_catalog_number(meta['uuid'], extra)
        mbid =  meta.get('mbid_manual') or meta.get('mbid')
        if mbid:
            await mb.fetch_musicbrainz_mbid(mbid, meta)
        else:
            # Try different methods of searching in sequence if no MBID is present
            methods_to_try = [
                ("barcode", self.search_by_barcode),
                ("catalog_number", self.search_by_catalog_number),
                ("title_artist", self.search_by_title_artist)
            ]

            success = False
            for method_name, search_func in methods_to_try:
                if await self.try_search_method(meta, mb, method_name, search_func):
                    success = True
                    break  # Exit the loop if a successful search was performed

            if not success:
                console.print("[red]WARN[/red]: No MusicBrainz data found using any method.")                 

        await self.process_with_discogs(meta)

        self.upload_proof_covers(meta, 'album_cover', 'user_cover', ('.jpg', '.jpeg'))
        self.upload_proof_covers(meta, 'album_back', 'back_cover')
        self.upload_proof_covers(meta, 'album_proof', 'user_proof')

        if not meta.get('tracklist', {}):
            log.debug('[magenta]MANUAL TRACKLIST')
            tracklist = {}
            for disc, file, path in tracks:
                clean_track_name, _ = os.path.splitext(file)

                try:
                    # Get track duration using TinyTag
                    tag = TinyTag.get(path)
                    track_duration = tag.duration
                    minutes = int(track_duration // 60)
                    seconds = int(track_duration % 60)
                    time = f"{minutes:02}:{seconds:02}"

                    if disc not in tracklist:
                        tracklist[disc] = {}

                    tracklist[disc][clean_track_name] = time 

                except Exception as e:
                    log.error(f"Error reading track {file}: {e}")
                    if disc not in tracklist:
                        tracklist[disc] = {}
                    tracklist[disc][clean_track_name] = "00:00"  # Default to "00:00" if there's an error

            log.debug(f"Generated tracklist: {tracklist}")
            meta['tracklist'] = tracklist

        if not os.path.exists(f"{base_dir}/tmp/{meta['uuid']}/MediaInfo.json"):
            self.export_audio_info(tracks, meta['uuid'], meta['base_dir'])

        if 'discogs_id' in meta and meta['discogs_id']:
            meta['keywords'] += f", Discogs: {meta['discogs_id']}"

        if 'catalog_number' in meta and meta['catalog_number']:
            meta['keywords'] += f", CATNO: {meta['catalog_number']}"

        if 'barcode' in meta and meta['barcode']:
            meta['keywords'] += f", UPC: {meta['barcode']}"

        log.debug('Finishing process_music, retunring meta')
        return meta

    def upload_proof_covers(self, meta, image_key, file_key, file_exts=('jpg', '.jpeg')):
        if image_key in meta and meta[image_key]:
            return
        image_path = meta.get(file_key)
        if image_path and os.path.isfile(image_path) and image_path.lower().endswith(file_exts):
            console.print(f"[yellow]Uploading {file_key.replace('_', ' ')} image: {image_path}")
            
            image_list, _ = self.upload_screens(meta, 1, 1, 0, 1, [image_path], {})
            
            if image_list:
                meta[image_key] = image_list[0]['raw_url']
                console.print(f"[green]Successfully uploaded and set {file_key.replace('_', ' ')} image: {meta[image_key]}")
            else:
                console.print(f"[red]Failed to upload {file_key.replace('_', ' ')} image.")


    async def try_search_method(self, meta, mb, method_name, search_func):
        try:
            if method_name == "barcode":
                barcode = meta.get('barcode')
                if not barcode:
                    barcode_match = re.search(r'(barcode|upc)-([0-9]+)', meta['uuid'], re.IGNORECASE)
                    if barcode_match:
                        barcode = barcode_match.group(2)
                        meta['barcode'] = barcode
                if barcode:
                    await search_func(barcode, meta, mb)
            elif method_name == "catalog_number" and meta.get('catalog_number'):
                await search_func(meta.get('catalog_number'), meta, mb)
            elif method_name == "title_artist":
                release_title = meta.get('album', '')
                artist_name = meta.get('artist', '')
                track_count = meta.get('track_count', 0)
                if release_title and artist_name:
                    await search_func(release_title, artist_name, track_count, meta, mb)
            
            if meta.get('mbid'):  # Check if we've found an mbid
                log.info(f"MusicBrainz data found using {method_name}.")
                return True
        except Exception as e:
            log.error(f"Error occurred while trying to search by {method_name}: {e}")
        return False

    async def search_by_barcode(self, barcode, meta, mb):
        await mb.search_musicbrainz_by_barcode(barcode, meta)

    async def search_by_catalog_number(self, catalog_number, meta, mb):
        await mb.search_by_catalog_number(catalog_number, meta)

    async def search_by_title_artist(self, release_title, artist_name, track_count, meta, mb):
        await mb.search_releases_by_title_artist(release_title, artist_name, track_count, meta)


    """ 
      Music TAG Processing 
    """
    def extract_tag(self, album_fname):
        album_fname = re.sub(r'hi-res', '', album_fname, flags=re.IGNORECASE)
        last_hyphen_index = album_fname.rfind('-')

        tag_match = ''

        if last_hyphen_index != -1:
            after_last_hyphen = album_fname[last_hyphen_index + 1:].strip()
            outside_parentheses = re.sub(r'\(.*?\)', '', after_last_hyphen).strip()

            if outside_parentheses:
                bracket_tag_match = re.search(r'\[([^\[\]]+)\]$', outside_parentheses)
                if bracket_tag_match:
                    tag = bracket_tag_match.group(1).strip()

                    if self.is_valid_tag(tag):
                        tag_match = f'-{tag}'

                else:
                    tag = re.search(r'^[^-\[\]]+$', outside_parentheses)
                    if tag:
                        tag = tag.group(0).strip()

                        if self.is_valid_tag(tag):
                            tag_match = f'-{tag}'

        return tag_match if tag_match else ''

    def is_valid_tag(self, tag):
        reject_terms = ['web', 'flac', 'alac', 'mp3', 'm4a', 'wav', 'vinyl']
        if any(term in tag.lower() for term in reject_terms):
            return False
        if re.search(r'\d{2,}', tag):
            return False
        if 'khz' in tag.lower():
            return False
        if ' ' in tag: 
            return False        
        if not re.match(r'^[a-zA-Z0-9\s&]+$', tag):
            return False        
        return True


    def extract_catalog_number(self, album_fname: str, extra: Dict[str, Any]) -> Optional[str]:
        """
        Extract CATALOG_NUMBER from album_fname or MediaInfo.
        """
        EXCLUSION_KEYWORDS = ['web', 'vinyl', 'cd', 'mp3', 'flac', 'wav', 'aac', 'alac']
        LABEL_WORDS = ['records', 'label', 'music', 'productions', 'studio', 'group', 'band', 'sound', 'entertainment']
        log.debug(f"Extracting catalog number from: {album_fname}")

        catalog_number = None

        catalog_number = extra.get('CATALOG_NUMBER') or extra.get('CATALOGNUMBER') or extra.get('CATNO') or extra.get('LABELNO')
        if catalog_number:
            log.debug(f"Found catalog number in extra dictionary: {catalog_number}")
            return re.sub(r'[ -]', '', catalog_number.strip())

        catalog_match = re.search(r'\{([^}]+)\}', album_fname)
        if catalog_match:
            catalog_number = catalog_match.group(1).strip()
            log.debug(f"Found catalog number in curly braces: {catalog_number}")
            
            if any(word in catalog_number.lower() for word in LABEL_WORDS):
                log.debug(f"Excluded as label or descriptor: {catalog_number}")
                return None

        if not catalog_number:
            catalog_match = re.search(r'\{([^}]+)\}', album_fname)
            if catalog_match:
                catalog_candidate = catalog_match.group(1).strip()
                parts = [part.strip() for part in catalog_candidate.split(',') if part.strip()]
                
                for part in parts:
                    if not any(word in part.lower() for word in LABEL_WORDS):
                        catalog_number = part
                        log.debug(f"Found valid catalog number: {catalog_number}")
                        break
                else:
                    log.debug(f"No valid catalog number found from parts: {parts}")
            
            if not catalog_number:
                catalog_match_title = re.search(r'\[([A-Za-z0-9\s,]+)\]', album_fname)
                if catalog_match_title:
                    catalog_candidate = catalog_match_title.group(1).strip()
                    parts = [part.strip() for part in catalog_candidate.split(',') if part.strip()]
                    
                    for part in parts:
                        if re.search(r'\w', part) and not any(word in part.lower() for word in LABEL_WORDS):
                            catalog_number = part
                            log.debug(f"Selected valid catalog number: {catalog_number}")
                            break
                    else:
                        log.debug(f"No valid catalog number found from parts: {parts}")

        # Check for exclusion keywords in the selected catalog number
        if catalog_number and any(excl in catalog_number.lower() for excl in EXCLUSION_KEYWORDS):
            log.debug(f"Excluded due to keyword in catalog number: {catalog_number}")
            return None
        
        # Return the cleaned catalog number
        if catalog_number:
            return re.sub(r'[ -]', '', catalog_number.strip())
        
        log.debug("No catalog number found.")
        return None

    async def process_with_discogs(self, meta: Dict[str, Any]) -> None:
        """
        Query Discogs API to find metadata when barcode or MBID is missing.
        """
        dc = DiscogsAPI("Art2Album / 1.0")
        mb = MusicBrainzAPI("Art2Album / 1.0")
        discogs_id = meta.get('discogs_id') or meta.get('DISCOGS_RELEASE_ID')
        discogs_url = meta.get('discogs_url')

        if not discogs_id and not discogs_url:
            print("No Discogs ID or URL found in meta.")
            return

        # Extract Discogs ID from URL if no ID is present
        if not discogs_id and discogs_url:
            master_match = re.search(r"discogs\.com/master/(\d+)", discogs_url)
            if master_match:
                master_id = master_match.group(1)
                master_data = await dc.fetch_master_data(master_id)
                if master_data:
                    main_release_url = master_data.get("main_release_url", "")
                    discogs_id_match = re.search(r"releases/(\d+)", main_release_url)
                    if discogs_id_match:
                        discogs_id = discogs_id_match.group(1)
                        meta['discogs_id'] = discogs_id
                        log.debug(f"Extracted Discogs ID from master URL: {discogs_id}")
                    else:
                        log.error("Failed to extract Discogs ID from master release URL.")
                else:
                    log.error(f"Failed to fetch master data for master ID {master_id}")

        if discogs_id:
            release_data = await dc.fetch_discogs_release(discogs_id)
            if release_data:
                barcode = dc.extract_barcode(release_data)
                if barcode and not meta.get('mbid'):
                    meta['barcode'] = barcode
                    print(f"Extracted Barcode: {barcode}")
                    # Retry MusicBrainz search if there's no MBID
                    await mb.search_musicbrainz_by_barcode(barcode, meta)

                dc.extract_metadata(release_data, meta)
        else:
            log.error("No valid Discogs ID found or could not extract it from the URL.")


    def get_music_source(self, normalized_af):
        nafu = normalized_af.upper()
        if ' CD ' in nafu:
            return 'CD' 
        elif ' VINYL ' in nafu:
            return 'VINYL'    
        elif ' WEB ' in nafu:
            return 'WEB'
        else:
            return ''


    """
    Get and parse mediainfo
    """
    def exportInfo(self, media_path, isdir, folder_id, base_dir, export_text=True):
        media_path = os.path.normpath(media_path)
        try:
            if export_text:
                if not os.path.exists(f"{base_dir}/tmp/{folder_id}/MEDIAINFO.txt"):
                    console.print("[bold yellow]Exporting MediaInfo...")
                    # Change to the directory of the media file for MediaInfo to work correctly with relative paths
                    os.chdir(os.path.dirname(media_path))
                    media_info = MediaInfo.parse(media_path, output="STRING", full=False, mediainfo_options={'inform_version' : '1'})
                    with open(f"{base_dir}/tmp/{folder_id}/MEDIAINFO.txt", 'w', newline="", encoding='utf-8') as export:
                        export.write(media_info)
                    with open(f"{base_dir}/tmp/{folder_id}/MEDIAINFO_CLEANPATH.txt", 'w', newline="", encoding='utf-8') as export_cleanpath:
                        # Replace full path with just the filename to sanitize the path
                        export_cleanpath.write(media_info.replace(media_path, os.path.basename(media_path)))
                    console.print("[bold green]MediaInfo text Exported.")

            if not os.path.exists(f"{base_dir}/tmp/{folder_id}/MediaInfo.json"):
                # MediaInfo to JSON
                media_info = MediaInfo.parse(media_path, output="JSON", mediainfo_options={'inform_version' : '1'})
                with open(f"{base_dir}/tmp/{folder_id}/MediaInfo.json", 'w', encoding='utf-8') as export:
                    export.write(media_info)
                console.print("[bold green]MediaInfo JSON Exported.")

            with open(f"{base_dir}/tmp/{folder_id}/MediaInfo.json", 'r', encoding='utf-8') as f:
                return json.load(f)

        except FileNotFoundError:
            console.print(f"[bold red]File not found: {media_path}")
            sys.exit()


    """
    Get Resolution
    """

    def get_resolution(self, guess, folder_id, base_dir):
        with open(f'{base_dir}/tmp/{folder_id}/MediaInfo.json', 'r', encoding='utf-8') as f:
            mi = json.load(f)
            try:
                width = mi['media']['track'][1]['Width']
                height = mi['media']['track'][1]['Height']
            except:
                width = 0
                height = 0
            framerate = mi['media']['track'][1].get('FrameRate', '')
            try:
                scan = mi['media']['track'][1]['ScanType']
            except:
                scan = "Progressive"
            if scan == "Progressive":
                scan = "p"
            elif framerate == "25.000":
                scan = "p"
            else:
                scan = "i"
            width_list = [3840, 2560, 1920, 1280, 1024, 854, 720, 15360, 7680, 0]
            height_list = [2160, 1440, 1080, 720, 576, 540, 480, 8640, 4320, 0]
            width = self.closest(width_list, int(width))
            actual_height = int(height)
            height = self.closest(height_list, int(height))
            res = f"{width}x{height}{scan}"
            resolution = self.mi_resolution(res, guess, width, scan, height, actual_height)
        return resolution

    def closest(self, lst, K):
        # Get closest, but not over
        lst = sorted(lst)
        mi_input = K
        res = 0
        for each in lst:
            if mi_input > each:
                pass
            else:
                res = each
                break
        return res
            
        # return lst[min(range(len(lst)), key = lambda i: abs(lst[i]-K))]

    def mi_resolution(self, res, guess, width, scan, height, actual_height):
        res_map = {
            "3840x2160p" : "2160p", "2160p" : "2160p",
            "2560x1440p" : "1440p", "1440p" : "1440p",
            "1920x1080p" : "1080p", "1080p" : "1080p",
            "1920x1080i" : "1080i", "1080i" : "1080i", 
            "1280x720p" : "720p", "720p" : "720p",
            "1280x540p" : "720p", "1280x576p" : "720p",
            "1024x576p" : "576p", "576p" : "576p",
            "1024x576i" : "576i", "576i" : "576i",
            "854x480p" :  "480p", "480p" : "480p",
            "854x480i" : "480i", "480i" : "480i",
            "720x576p" : "576p", "576p" : "576p",
            "720x576i" : "576i", "576i" : "576i",
            "720x480p" :  "480p", "480p" : "480p",
            "720x480i" : "480i", "480i" : "480i",
            "15360x8640p" : "8640p", "8640p" : "8640p",
            "7680x4320p" : "4320p", "4320p" : "4320p",
            "OTHER" : "OTHER"}
        resolution = res_map.get(res, None)
        if actual_height == 540:
            resolution = "OTHER"
        if resolution is None:
            try:     
                resolution = guess['screen_size']
            except:
                width_map = {
                    '3840p' : '2160p',
                    '2560p' : '1550p',
                    '1920p' : '1080p',
                    '1920i' : '1080i',
                    '1280p' : '720p',
                    '1024p' : '576p',
                    '1024i' : '576i',
                    '854p' : '480p',
                    '854i' : '480i',
                    '720p' : '576p',
                    '720i' : '576i',
                    '15360p' : '4320p',
                    'OTHERp' : 'OTHER'
                }
                resolution = width_map.get(f"{width}{scan}", "OTHER")
            resolution = self.mi_resolution(resolution, guess, width, scan, height, actual_height)
        
        return resolution
           
               

    def is_sd(self, resolution):
        if resolution in ("480i", "480p", "576i", "576p", "540p"):
            sd = 1
        else:
            sd = 0
        return sd

    """
    Is a scene release?
    """
    def is_scene(self, video, imdb=None):
        scene = False
        base = os.path.basename(video)
        base = os.path.splitext(base)[0]
        base = urllib.parse.quote(base)
        url = f"https://api.srrdb.com/v1/search/r:{base}"
        try:
            response = requests.get(url, timeout=30)
            response = response.json()
            if int(response.get('resultsCount', 0)) != 0:
                video = f"{response['results'][0]['release']}.mkv"
                scene = True
                r = requests.get(f"https://api.srrdb.com/v1/imdb/{base}")
                r = r.json()
                if r['releases'] != [] and imdb is None:
                    imdb = r['releases'][0].get('imdb', imdb) if r['releases'][0].get('imdb') is not None else imdb
                console.print(f"[green]SRRDB: Matched to {response['results'][0]['release']}")
        except Exception:
            video = video
            scene = False
            console.print("[yellow]SRRDB: No match found, or request has timed out")
        return video, scene, imdb




    """
    Generate Screenshots
    """

    def disc_screenshots(self, filename, bdinfo, folder_id, base_dir, use_vs, image_list, ffdebug, num_screens=None):
        if num_screens is None:
            num_screens = self.screens
        if num_screens == 0 or len(image_list) >= num_screens:
            return
        #Get longest m2ts
        length = 0 
        for each in bdinfo['files']:
            int_length = sum(int(float(x)) * 60 ** i for i, x in enumerate(reversed(each['length'].split(':'))))
            if int_length > length:
                length = int_length
                for root, dirs, files in os.walk(bdinfo['path']):
                    for name in files:
                        if name.lower() == each['file'].lower():
                            file = f"{root}/{name}"
                            
        
        if "VC-1" in bdinfo['video'][0]['codec'] or bdinfo['video'][0]['hdr_dv'] != "":
            keyframe = 'nokey'
        else:
            keyframe = 'none'

        os.chdir(f"{base_dir}/tmp/{folder_id}")    
        i = len(glob.glob(f"{filename}-*.png"))        
        if i >= num_screens:
            i = num_screens
            console.print('[bold green]Reusing screenshots')
        else:
            console.print("[bold yellow]Saving Screens...")
            if use_vs:
                from src.vs import vs_screengn
                vs_screengn(source=file, encode=None, filter_b_frames=False, num=num_screens, dir=f"{base_dir}/tmp/{folder_id}/")
            else:
                if bool(ffdebug):
                    loglevel = 'verbose'
                    debug = False
                else:
                    loglevel = 'quiet'
                    debug = True
                retake = False    
                with Progress(
                    TextColumn("[bold green]Saving Screens..."),
                    BarColumn(),
                    "[cyan]{task.completed}/{task.total}",
                    TimeRemainingColumn()
                ) as progress:
                    screen_task = progress.add_task("[green]Saving Screens...", total=num_screens)
                    ss_times = []
                    smallest_image_path = None
                    smallest_image_size = float('inf')

                    for _ in range(num_screens):
                        image_path = f"{base_dir}/tmp/{folder_id}/{filename}-{i}.png"
                        if not os.path.exists(image_path) or retake:                       
                            try:
                                ss_times = self.valid_ss_time(ss_times, num_screens, length)
                                proc = (
                                    ffmpeg
                                    .input(file, ss=ss_times[-1], skip_frame=keyframe)
                                    .output(image_path, vframes=1, pix_fmt="rgb24")
                                    .overwrite_output()
                                    .global_args('-loglevel', loglevel)
                                    .run_async(quiet=debug)
                                )
                                deadline = time.time() + 60
                                while proc.poll() is None:
                                    if time.time() > deadline:
                                        proc.kill()
                                        try:
                                            proc.wait(timeout=5)
                                        except subprocess.TimeoutExpired:
                                            pass
                                        raise Exception(f"ffmpeg timed out after 60s on screenshot {i}")
                                    time.sleep(0.5)
                            except Exception:
                                console.print(traceback.format_exc())

                            if os.path.exists(image_path):
                                self.optimize_images(image_path)
                                if os.path.getsize(Path(image_path)) <= 75000:
                                    console.print("[bold yellow]Image is incredibly small, retaking")
                                    time.sleep(1)
                                elif os.path.getsize(Path(image_path)) <= 31000000 and self.img_host == "imgbb":
                                    i += 1
                                elif os.path.getsize(Path(image_path)) <= 10000000 and self.img_host in ["imgbox", "pixhost", "ptscreens", "oeimg"]:
                                    i += 1
                                elif self.img_host in ["ptpimg", "lensdump", "ptscreens", "oeimg"] and not retake:
                                    i += 1
                                elif retake:
                                    pass
                        else:
                            screenshot_size = os.path.getsize(image_path)
                            if screenshot_size < smallest_image_size:
                                smallest_image_size = screenshot_size
                                smallest_image_path = image_path

                        i += 1
                        progress.advance(screen_task)
                        
                    # Remove the smallest image
                    if smallest_image_path:
                        os.remove(smallest_image_path)
                    
        
    def dvd_screenshots(self, meta, disc_num, num_screens=None):
        if num_screens is None:
            num_screens = self.screens
        if num_screens == 0 or (len(meta.get('image_list', [])) >= num_screens and disc_num == 0):
            return
        ifo_mi = MediaInfo.parse(f"{meta['discs'][disc_num]['path']}/VTS_{meta['discs'][disc_num]['main_set'][0][:2]}_0.IFO", mediainfo_options={'inform_version': '1'})
        sar = 1
        for track in ifo_mi.tracks:
            if track.track_type == "Video":
                length = float(track.duration) / 1000
                par = float(track.pixel_aspect_ratio)
                dar = float(track.display_aspect_ratio)
                width = float(track.width)
                height = float(track.height)
        if par < 1:
            new_height = dar * height
            sar = width / new_height
            w_sar = 1
            h_sar = sar
        else:
            sar = par
            w_sar = sar
            h_sar = 1

        main_set_length = len(meta['discs'][disc_num]['main_set'])
        if main_set_length >= 3:
            main_set = meta['discs'][disc_num]['main_set'][1:-1]
        elif main_set_length == 2:
            main_set = meta['discs'][disc_num]['main_set'][1:]
        elif main_set_length == 1:
            main_set = meta['discs'][disc_num]['main_set']
        n = 0
        os.chdir(f"{meta['base_dir']}/tmp/{meta['uuid']}")
        i = 0
        existing_screenshots = glob.glob(f"{meta['base_dir']}/tmp/{meta['uuid']}/{meta['discs'][disc_num]['name']}-*.png")

        if len(existing_screenshots) >= num_screens:
            i = num_screens
            console.print('[bold green]Reusing screenshots')
        else:
            if meta.get('ffdebug', False):
                loglevel = 'verbose'
                debug = False
            looped = 0
            retake = False
            with Progress(
                TextColumn("[bold green]Saving Screens..."),
                BarColumn(),
                "[cyan]{task.completed}/{task.total}",
                TimeRemainingColumn()
            ) as progress:
                screen_task = progress.add_task("[green]Saving Screens...", total=num_screens)
                ss_times = []
                smallest_image_path = None
                smallest_image_size = float('inf')
                for i in range(num_screens):
                    if n >= len(main_set):
                        n = 0
                    if n >= num_screens:
                        n -= num_screens
                    image = f"{meta['base_dir']}/tmp/{meta['uuid']}/{meta['discs'][disc_num]['name']}-{i}.png"
                    if not os.path.exists(image) or retake:
                        try:
                            retake = False
                            loglevel = 'quiet'
                            debug = True
                            if bool(meta.get('debug', False)):
                                loglevel = 'error'
                                debug = False
                            def _is_vob_good(n, num_screens):
                                voblength = 300
                                loops = 0
                                while loops < 6:
                                    vob_mi = MediaInfo.parse(f"{meta['discs'][disc_num]['path']}/VTS_{main_set[n]}", output='JSON')
                                    vob_mi = json.loads(vob_mi)
                                    try:
                                        voblength = float(vob_mi['media']['track'][1]['Duration'])
                                        return voblength, n
                                    except Exception:
                                        try:
                                            voblength = float(vob_mi['media']['track'][2]['Duration'])
                                            return voblength, n
                                        except Exception:
                                            n += 1
                                            if n >= len(main_set):
                                                n = 0
                                            if n >= num_screens:
                                                n -= num_screens
                                            loops += 1
                                return 300, n

                            try:
                                voblength, n = _is_vob_good(n, num_screens)
                                m = i
                                min_time = 0.01 * voblength
                                base_time = max(min_time, random.randint(round(voblength / 5), round(voblength - voblength / 5)))
                                while True:
                                    img_time = max(min_time, base_time / (2 ** m) + random.uniform(0, 20))
                                    if img_time < voblength:
                                        break
                                ff = ffmpeg.input(f"{meta['discs'][disc_num]['path']}/VTS_{main_set[n]}", ss=img_time)
                                if w_sar != 1 or h_sar != 1:
                                    ff = ff.filter('scale', int(round(width * w_sar)), int(round(height * h_sar))) 
                                proc = (
                                    ff
                                    .output(image, vframes=1, pix_fmt="rgb24")
                                    .overwrite_output()
                                    .global_args('-loglevel', loglevel)
                                    .run_async(quiet=debug)
                                )
                                deadline = time.time() + 60
                                while proc.poll() is None:
                                    if time.time() > deadline:
                                        proc.kill()
                                        try:
                                            proc.wait(timeout=5)
                                        except subprocess.TimeoutExpired:
                                            pass
                                        raise Exception(f"ffmpeg timed out after 60s on screenshot {i}")
                                    time.sleep(0.5)
                            except Exception:
                                console.print(traceback.format_exc())

                            self.optimize_images(image)
                            n += 1

                            try:
                                if os.path.getsize(Path(image)) <= 31000000 and self.img_host == "imgbb":
                                    i += 1
                                elif os.path.getsize(Path(image)) <= 10000000 and self.img_host in ["imgbox", 'pixhost', "ptscreens", "oeimg"]:
                                    i += 1
                                elif os.path.getsize(Path(image)) <= 75000:
                                    console.print("[yellow]Image is incredibly small (and is most likely to be a single color), retaking")
                                    retake = True
                                    time.sleep(1)
                                elif self.img_host == "ptpimg":
                                    i += 1
                                elif self.img_host == "lensdump":
                                    i += 1
                                elif self.img_host == "ptscreens":
                                    i += 1
                                elif self.img_host == "oeimg":
                                    i += 1
                                else:
                                    i += 1
                                looped = 0
                            except Exception:
                                if looped >= 25:
                                    console.print('[red]Failed to take screenshots')
                                    exit()
                                looped += 1

                            progress.advance(screen_task)

                        except Exception:
                            pass
                    else:
                        screenshot_size = os.path.getsize(image)
                        if screenshot_size < smallest_image_size:
                            smallest_image_size = screenshot_size
                            smallest_image_path = image

                    i += 1

            # Remove the smallest image
            if smallest_image_path:
                os.remove(smallest_image_path)


    def hdr_needs_tonemap(self, path):
        """True si las capturas de este fichero saldrían lavadas sin convertir.

        Criterio: transferencia PQ/HLG (smpte2084 / arib-std-b67). Para Dolby
        Vision manda la capa base, no el perfil: dv_bl_signal_compatibility_id=1
        es HDR10 legítimo y se tonemapea; =0 (perfil 5, IPT-PQ-c2) no tiene base
        visible y esta cadena le sacaría los colores invertidos, así que se deja
        tal cual. Ante cualquier duda devuelve False: el fallo seguro es no
        tocar (una captura SDR sin convertir es correcta; una SDR convertida
        queda destrozada). Misma lógica que Mass-Edition 05/06, medida en
        HDR10 puro, DV P8.1 y DV P10 sobre AV1.
        """
        try:
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=color_transfer",
                 "-show_entries", "stream_side_data=dv_profile,dv_bl_signal_compatibility_id",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=120)
            flujo = json.loads(pr.stdout or "{}").get("streams", [{}])[0]
            if (flujo.get("color_transfer") or "").lower() not in ("smpte2084", "arib-std-b67"):
                return False
            dv = [x for x in flujo.get("side_data_list", []) if "dv_profile" in x]
            if dv and dv[0].get("dv_bl_signal_compatibility_id") == 0:
                console.print("[yellow]Dolby Vision profile 5 (no compatible base layer): skipping tonemap")
                return False
            return True
        except Exception:
            return False

    def apply_hdr_tonemap(self, ff):
        """Encadena el HDR→SDR sobre un nodo de ffmpeg-python.

        Operador `mobius` y npl=100, medidos frame a frame contra `hable` (que
        oscurece por debajo del original: Y=40→27 en acción real) y contra npl
        altos (aplastan). Tabla completa de medidas en Mass-Edition
        05_image_regenerator._filtro_tonemap().
        """
        return (ff.filter('zscale', t='linear', npl=100)
                  .filter('format', 'gbrpf32le')
                  .filter('zscale', p='bt709')
                  .filter('tonemap', tonemap='mobius', desat=0)
                  .filter('zscale', t='bt709', m='bt709', r='tv')
                  .filter('format', 'yuv420p'))

    def screenshots(self, path, filename, folder_id, base_dir, meta, num_screens=None):
        if num_screens is None:
            num_screens = self.screens - len(meta.get('image_list', []))
        if num_screens == 0:
            return
        # Puerta explícita: sólo tonemapea quien pide `meta['tonemap']` (el flujo
        # de subida de RawLoadrr la activa en gather_prep). Mass-Edition 05 llama
        # aquí SIN la clave porque ya convierte él mismo al recodificar a JPG:
        # si esta función también tonemapeara, la captura pasaría dos veces por
        # la curva y saldría lavada otra vez, por la causa contraria.
        tonemap_hdr = bool(meta.get('tonemap', False)) and self.hdr_needs_tonemap(path)
        if tonemap_hdr:
            console.print("[green]HDR source: tonemapping screenshots to SDR (mobius)")
        with open(f"{base_dir}/tmp/{folder_id}/MediaInfo.json", encoding='utf-8') as f:
            mi = json.load(f)
            video_track = mi['media']['track'][1]
            length = float(video_track.get('Duration', mi['media']['track'][0]['Duration']))
            width = float(video_track.get('Width'))
            height = float(video_track.get('Height'))
            par = float(video_track.get('PixelAspectRatio', 1))
            dar = float(video_track.get('DisplayAspectRatio'))

            if par == 1:
                sar = w_sar = h_sar = 1
            elif par < 1:
                new_height = dar * height
                sar = width / new_height
                w_sar = 1
                h_sar = sar
            else:
                sar = w_sar = par 
                h_sar = 1
            length = round(length)
            os.chdir(f"{base_dir}/tmp/{folder_id}")
            i = 0
            if len(glob.glob(f"{filename}-*.png")) >= num_screens:
                i = num_screens
                console.print('[bold green]Reusing screenshots')
            else:
                loglevel = 'quiet'
                debug = True
                if bool(meta.get('ffdebug', False)):
                    loglevel = 'verbose'
                    debug = False
                if meta.get('vapoursynth', False):
                    from src.vs import vs_screengn
                    vs_screengn(source=path, encode=None, filter_b_frames=False, num=num_screens, dir=f"{base_dir}/tmp/{folder_id}/")
                else:
                    retake = False
                    with Progress(
                        TextColumn("[bold green]Saving Screens..."),
                        BarColumn(),
                        "[cyan]{task.completed}/{task.total}",
                        TimeRemainingColumn()
                    ) as progress:
                        ss_times = []
                        screen_task = progress.add_task("[green]Saving Screens...", total=num_screens)
                        smallest_image_path = None
                        smallest_image_size = float('inf')
                        
                        for _ in range(num_screens):
                            image_path = os.path.abspath(f"{base_dir}/tmp/{folder_id}/{filename}-{i}.png")
                            
                            if not os.path.exists(image_path) or retake:
                                try:
                                    ss_times = self.valid_ss_time(ss_times, num_screens, length)
                                    ff = ffmpeg.input(path, ss=ss_times[-1])
                                    if w_sar != 1 or h_sar != 1:
                                        ff = ff.filter('scale', int(round(width * w_sar)), int(round(height * h_sar)))
                                    if tonemap_hdr:
                                        ff = self.apply_hdr_tonemap(ff)
                                    proc = (
                                        ff
                                        .output(image_path, vframes=1, pix_fmt="rgb24")
                                        .overwrite_output()
                                        .global_args('-loglevel', loglevel)
                                        .run_async(quiet=debug)
                                    )
                                    deadline = time.time() + 60
                                    while proc.poll() is None:
                                        if time.time() > deadline:
                                            proc.kill()
                                            try:
                                                proc.wait(timeout=5)
                                            except subprocess.TimeoutExpired:
                                                pass
                                            raise Exception(f"ffmpeg timed out after 60s on screenshot {i}")
                                        time.sleep(0.5)
                                except Exception:
                                    console.print(Traceback.extract())
                                    self.optimize_images(image_path)
                                    if os.path.exists(image_path):
                                        if os.path.getsize(Path(image_path)) <= 75000 or self.is_black_image(image_path):
                                            console.print("[yellow]Image is incredibly small or black, retaking")
                                            retake = True
                                            os.remove(image_path)
                                            time.sleep(1)
                                        elif os.path.getsize(Path(image_path)) <= 31000000 and self.img_host == "imgbb" and not retake:
                                            i += 1
                                        elif os.path.getsize(Path(image_path)) <= 10000000 and self.img_host in ["imgbox", 'pixhost', "ptscreens", "oeimg"] and not retake:
                                            i += 1
                                        elif self.img_host in ["ptpimg", "lensdump", "ptscreens", "oeimg"] and not retake:
                                            i += 1
                                        elif self.img_host == "freeimage.host":
                                            console.print("[bold red]Support for freeimage.host has been removed. Please remove from your config")
                                            exit()
                                        elif retake:
                                            pass
                                        else:
                                            i += 1
                            else:
                                screenshot_size = os.path.getsize(image_path)
                                if screenshot_size < smallest_image_size:
                                    smallest_image_size = screenshot_size
                                    smallest_image_path = image_path

                            i += 1
                            progress.advance(screen_task)
                            
                        # Remove the smallest image
                        if smallest_image_path:
                            os.remove(smallest_image_path)

    def is_black_image(image_path, threshold=0.98):
        try:
            command = [
                'ffmpeg', '-i', image_path, '-vf', 
                'blackdetect=d=0.1:pic_th=%f' % threshold, '-f', 'null', '-'
            ]
            result = subprocess.run(command, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            return 'black_start' in result.stderr
        except Exception as e:
            console.print(f"[red]Error checking black image: {e}")
            return False                     

    def valid_ss_time(self, ss_times, num_screens, length, min_time_diff=10):
        valid_time = False
        while not valid_time:
            valid_time = True
            if ss_times:
                sst = random.randint(round(length / 5), round(length / 2))
                tolerance = length / 10 / num_screens
                for each in ss_times:
                    if abs(sst - each) <= tolerance or any(abs(sst - t) < min_time_diff for t in ss_times):
                        valid_time = False
                if valid_time:
                    ss_times.append(sst)
            else:
                ss_times.append(random.randint(round(length / 5), round(length / 2)))
        return ss_times

    def optimize_images(self, image):
        if self.config['DEFAULT'].get('optimize_images', True):
            if os.path.exists(image):
                try:
                    pyver = platform.python_version_tuple()
                    if int(pyver[0]) == 3 and int(pyver[1]) >= 7:
                        import oxipng 
                    if os.path.getsize(image) >= 31000000:
                        oxipng.optimize(image, level=6)
                    else:
                        oxipng.optimize(image, level=1)
                except:
                    pass
        return
    """
    Get type and category
    """

    def get_type(self, video, scene, is_disc):
        filename = os.path.basename(video).lower()
        if "remux" in filename:
            type = "REMUX"
        elif any(word in filename for word in [" web ", ".web.", "web-dl"]):
            type = "WEBDL"
        elif "webrip" in filename:
            type = "WEBRIP"
        # elif scene:
            # type = "ENCODE"
        elif "hdtv" in filename:
            type = "HDTV"
        elif is_disc is not None:
            type = "DISC"
        else:
            type = "ENCODE"
        return type

    def get_cat(self, video):
        # if category is None:
        category = guessit(video.replace('1.0', ''))['type']
        if category.lower() == "movie":
            category = "MOVIE" #1
        elif category.lower() in ("tv", "episode"):
            category = "TV" #2
        else:
            category = "MOVIE"
        return category

    async def get_tmdb_from_imdb(self, meta, filename):
        if meta.get('tmdb_manual') is not None:
            meta['tmdb'] = meta['tmdb_manual']
            return meta
        imdb_id = meta['imdb']
        if str(imdb_id)[:2].lower() != "tt":
            imdb_id = f"tt{imdb_id}"
        find = tmdb.Find(id=imdb_id)
        info = find.info(external_source="imdb_id")
        if len(info['movie_results']) >= 1:
            meta['category'] = "MOVIE"
            meta['tmdb'] =  info['movie_results'][0]['id']
        elif len(info['tv_results']) >= 1:
            meta['category'] = "TV"
            meta['tmdb'] =  info['tv_results'][0]['id']
        else:
            imdb_info = await self.get_imdb_info(imdb_id.replace('tt', ''), meta)
            title = imdb_info.get("title")
            if title is None:
                title = filename
            year = imdb_info.get('year')
            if year is None:
                year = meta['search_year']
            console.print(f"[yellow]TMDb was unable to find anything with that IMDb, searching TMDb for {title}")
            meta = await self.get_tmdb_id(title, year, meta, meta['category'], imdb_info.get('original title', imdb_info.get('localized title', meta['uuid'])))
            if meta.get('tmdb') in ('None', '', None, 0, '0'):
                if meta.get('mode', 'discord') == 'cli':
                    console.print('[yellow]Unable to find a matching TMDb entry')
                    tmdb_id = console.input("Please enter tmdb id: ")
                    parser = Args(config=self.config)
                    meta['category'], meta['tmdb'] = parser.parse_tmdb_id(id=tmdb_id, category=meta.get('category'))
        await asyncio.sleep(2)
        return meta

    def _looks_anime(self, meta, filename):
        """Cheap heuristic so the anime providers are only queried for anime."""
        blob = f"{meta.get('path', '')} {filename}".lower()
        if re.search(r'[぀-ヿ一-鿿]', blob):
            return True                          # hiragana / katakana / kanji
        if 'anime' in blob:
            return True
        base = os.path.basename(meta.get('path', '') or '')
        return base.startswith('[')              # fansub [Group] convention

    def _override_key(self, meta):
        """Stable key for the saved-decision store: the fed release folder."""
        return os.path.basename(str(meta.get('path', '')).rstrip('/\\'))

    def _apply_resolved(self, meta, res):
        """Copy a resolved id-set onto meta."""
        meta['category'] = res['category']
        meta['tmdb'] = res['tmdb'] or 0
        if res['imdb']:
            meta['imdb'] = res['imdb']
            meta['imdb_id'] = res['imdb'].replace('tt', '')
        if res['tvdb']:
            meta['tvdb_id'] = res['tvdb']
        if res['mal']:
            meta['mal'] = res['mal']
        if meta['tmdb']:
            meta['tmdb_manual'] = meta['tmdb']
        return meta

    async def resolve_ids(self, meta, filename, aliases=None):
        """
        Identify the release by cross-referencing several metadata providers
        and only trust an id-set that >=2 of them agree on. Replaces the old
        'blindly take TMDB search.results[0]' logic, which mis-identified
        releases whose real entry was missing or out-ranked on TMDB.

        High consensus -> used directly. No consensus -> the release is never
        guessed silently: unattended runs log it to the pending-IDs report and
        skip; interactive runs prompt, and the answer is saved so the same
        conflictive release is never asked about again.
        """
        year = meta.get('search_year') or None
        category = (meta.get('category') or 'TV').upper()
        aliases = [a for a in (aliases or []) if a and str(a).strip()]
        mal_hint = self._looks_anime(meta, filename)
        okey = self._override_key(meta)
        try:
            res = _resolve_ids(filename, year, category, self.config,
                               aliases=aliases, mal_hint=mal_hint,
                               override_key=okey,
                               log=lambda m: log.info(f"[id_resolver] {m}"))
        except Exception as e:
            console.print(f"[yellow]ID resolver error: {e} — "
                          f"falling back to TMDB search.")
            return await self.get_tmdb_id(filename, year, meta, category)

        if res['confidence'] == 'high' and (res['tmdb'] or res['imdb']):
            self._apply_resolved(meta, res)
            if res.get('source') == 'override':
                src = "saved override"
            else:
                src = f"{res['votes'] or res['mal_votes']} providers agree"
            console.print(
                f"[bold green]✓ ID confirmed[/bold green] ([cyan]{src}[/cyan]): "
                f"{res['title']} ({res['year']}) — tmdb={meta['tmdb']} "
                f"imdb={meta.get('imdb', '0')} tvdb={meta.get('tvdb_id', 0)} "
                f"mal={meta.get('mal', 0)}")
            return meta

        # no >=2 provider agreement -> never guess silently
        console.print(f"[bold yellow]⚠ ID not confirmed[/bold yellow] for "
                      f"[white]{filename}[/white] — providers disagree.")
        if res['confidence'] == 'low':
            console.print(f"[yellow]  Best guess "
                          f"({res['votes'] or res['mal_votes']} source): "
                          f"{res['title']} tmdb={res['tmdb']} "
                          f"imdb={res['imdb']}[/yellow]")
        for c in res['detail'][:5]:
            console.print(f"[dim]   {c['provider']:8} {c['title']} "
                          f"({c['year']}) imdb={c['imdb']} tmdb={c['tmdb']} "
                          f"mal={c['mal']}[/dim]")

        # unattended (list/auto-upload): log to the pending report, skip item,
        # let the queue keep moving -- triage happens in one batched pass later
        if meta.get('unattended'):
            console.print("[red]  Logged to pending-IDs report — skipped; "
                          "the queue continues.[/red]")
            _report_pending({
                "path": meta.get('path', ''),
                "override_key": okey,
                "filename": filename, "title": filename,
                "year": year, "category": res['category'],
                "confidence": res['confidence'],
                "best_guess": {"tmdb": res['tmdb'], "imdb": res['imdb'],
                               "title": res['title']},
                "candidates": [
                    {"provider": c['provider'], "title": c['title'],
                     "year": c['year'], "imdb": c['imdb'],
                     "tmdb": c['tmdb'], "mal": c['mal'],
                     "score": round(c['score'], 2)}
                    for c in res['detail'][:8]],
            })
            meta['tmdb_not_found'] = True
            meta['tmdb'] = meta.get('tmdb') or 0
            return meta

        # interactive: ask once, then remember the decision forever
        default = f"{(res['category'] or 'tv').lower()}/{res['tmdb']}" \
            if res['tmdb'] else ""
        tmdb_id = Prompt.ask(
            "Please enter tmdb id in this format: tv/12345 or movie/12345",
            default=default)
        parser = Args(config=self.config)
        meta['category'], meta['tmdb'] = parser.parse_tmdb_id(
            id=tmdb_id, category=meta.get('category'))
        meta['tmdb_manual'] = meta['tmdb']
        if okey and meta.get('tmdb'):
            _save_override(okey, {"tmdb": meta['tmdb'],
                                  "imdb": meta.get('imdb', ''),
                                  "tvdb": meta.get('tvdb_id', 0),
                                  "mal": meta.get('mal', 0),
                                  "category": meta['category'],
                                  "title": filename, "year": year})
            console.print("[dim]  Saved — this release won't be asked "
                          "about again.[/dim]")
        return meta

    async def get_tmdb_id(self, filename, search_year, meta, category, untouched_filename="", attempted=0):
        search = tmdb.Search()
        try:
            if category == "MOVIE":
                search.movie(query=filename, year=search_year)
            elif category == "TV":
                search.tv(query=filename, first_air_date_year=search_year)
            
            if meta.get('tmdb_manual') is not None:
                meta['tmdb'] = meta['tmdb_manual']
            else:
                meta['tmdb'] = search.results[0]['id']
                meta['category'] = category
        except IndexError:
            try:
                if category == "MOVIE":
                    search.movie(query=filename)
                elif category == "TV":
                    search.tv(query=filename)
                meta['tmdb'] = search.results[0]['id']
                meta['category'] = category
            except IndexError:
                if category == "MOVIE":
                    category = "TV"
                else:
                    category = "MOVIE"
                
                if attempted <= 1:
                    attempted += 1
                    meta = await self.get_tmdb_id(filename, search_year, meta, category, untouched_filename, attempted)
                elif attempted == 2:
                    attempted += 1
                    parsed_title = anitopy.parse(guessit(untouched_filename, {"excludes": ["country", "language"]})['title'])['anime_title']
                    meta = await self.get_tmdb_id(parsed_title, search_year, meta, meta['category'], untouched_filename, attempted)

                if meta.get('tmdb') in (None, ""):
                    console.print(f"[red]Unable to find TMDb match for {filename}")
                    if meta.get('unattended'):
                        meta['tmdb_not_found'] = True
                        return meta
                    else:
                        tmdb_id = Prompt.ask("Please enter tmdb id in this format: tv/12345 or movie/12345\n")
                        parser = Args(config=self.config)
                        meta['category'], meta['tmdb'] = parser.parse_tmdb_id(id=tmdb_id, category=meta.get('category'))
                        meta['tmdb_manual'] = meta['tmdb']
                        return meta

        return meta



    
    async def tmdb_other_meta(self, meta):
        
        if meta['tmdb'] == "0":
            try:
                title = guessit(meta['path'], {"excludes" : ["country", "language"]})['title'].lower()
                title = title.split('aka')[0]
                meta = await self.get_tmdb_id(guessit(title, {"excludes" : ["country", "language"]})['title'], meta['search_year'], meta)
                if meta['tmdb'] == "0":
                    meta = await self.get_tmdb_id(title, "", meta, meta['category'])
            except:
                if meta.get('mode', 'discord') == 'cli':
                    console.print("[bold red]Unable to find tmdb entry. Exiting.")
                    exit()
                else:
                    console.print("[bold red]Unable to find tmdb entry")
                    return meta
        if meta['category'] == "MOVIE":
            movie = tmdb.Movies(meta['tmdb'])
            while True:  # Keep looping until a valid response is obtained
                try:
                    response = movie.info(language=meta.get('title_lang'))  # Fetch movie info
                    break 
                except HTTPError as e:
                    if e.response.status_code == 404:
                        console.print("[red]The TMDb ID you entered could not be found. Please make sure the ID is correct and try again.")
                        tmdb_id = Prompt.ask("Please enter tmdb id in this format: tv/12345 or movie/12345\n")
                        parser = Args(config=self.config)
                        meta['category'], meta['tmdb'] = parser.parse_tmdb_id(id=tmdb_id, category=meta.get('category'))
                        meta['tmdb_manual'] = meta['tmdb']
                        movie = tmdb.Movies(meta['tmdb'])  # Update the movie object with the new TMDb ID
                    else:
                        raise
            
            # Extract general movie information
            meta['title'] = response['title']
            if response['release_date']:
                try:
                    meta['year'] = datetime.strptime(response['release_date'],'%Y-%m-%d').year
                    full_date = datetime.strptime(response['release_date'],'%Y-%m-%d')
                    meta['full_date'] = full_date.strftime('%Y-%m-%d')
                except Exception:
                    meta['full_date'] = ""
            else:
                console.print('[yellow]TMDB does not have a release date, using year from filename instead (if it exists)')
                meta['year'] = meta['search_year']
            
            external = movie.external_ids()
            if meta.get('imdb', None) is None:
                imdb_id = external.get('imdb_id', "0")
                meta['imdb_id'] = str(int(imdb_id.replace('tt', ''))).zfill(7) if imdb_id else '0'
            else:
                meta['imdb_id'] = str(meta['imdb']).replace('tt', '').zfill(7)

            if meta.get('tvdb_id', '0') in ['', ' ', None, 'None', '0']:
                meta['tvdb_id'] = external.get('tvdb_id', '0') or '0'

            # Fetch Trailer
            try:
                videos = movie.videos()
                for each in videos.get('results', []):
                    if each.get('site', "") == 'YouTube' and each.get('type', "") == "Trailer":
                        meta['youtube'] = f"{each.get('key')}"
                        break
            except Exception:
                console.print('[yellow]Unable to grab videos from TMDb.')
            
            # Additional metadata
            meta['aka'], original_language = await self.get_imdb_aka(meta['imdb_id'])
            meta['original_language'] = original_language if original_language is not None else response['original_language']
            meta['original_title'] = response.get('original_title', meta['title'])
            meta['keywords'] = self.get_keywords(movie)
            meta['genres'] = self.get_genres(response)
            meta['adult'] = response['adult']
            meta['tmdb_directors'] = self.get_directors(movie)
            
            if not meta.get('anime', False):
                meta['mal_id'], meta['aka'], meta['anime'] = self.get_anime(response, meta)
            
            meta['poster'] = response.get('poster_path', "")

            # Fetch logos from the images endpoint
            logo_response = movie.images()
            logos = logo_response.get('logos', [])
            first_logo = next((logo for logo in logos if logo.get('iso_639_1') == 'en'), None) or (logos[0] if logos else None)

            if first_logo:
                logo_path = first_logo['file_path']
                meta['logo'] = f"https://image.tmdb.org/t/p/original{logo_path}"
            else:
                meta['logo'] = None

            #Get backdrop for non-tmdb linked categories
            backdrop_response = movie.images()
            backdrops = backdrop_response.get('backdrops', [])  
            first_backdrop = next((backdrop for backdrop in backdrops if backdrop.get('iso_639_1') == 'en'), None) or (backdrops[0] if backdrops else None)
            if first_backdrop:
                backdrop_path = first_backdrop['file_path']
                meta['backdrop'] = f"https://image.tmdb.org/t/p/original{backdrop_path}"
            else:
                meta['backdrop'] = None

            meta['overview'] = response['overview']
            meta['tmdb_type'] = 'Movie'
            meta['runtime'] = response.get('episode_run_time', 60)

        elif meta['category'] == "TV":
            tv = tmdb.TV(meta['tmdb'])
            response = tv.info(language=meta.get('title_lang'))
            meta['title'] = response['name']
            if response['first_air_date']:
                try:
                    meta['year'] = datetime.strptime(response['first_air_date'],'%Y-%m-%d').year
                    full_date = datetime.strptime(response['first_air_date'],'%Y-%m-%d')
                    meta['full_date'] = full_date.strftime('%Y-%m-%d')
                except Exception:
                    meta['full_date'] = ""
            else:
                console.print('[yellow]TMDB does not have a release date, using year from filename instead (if it exists)')
                meta['year'] = meta['search_year']    
            external = tv.external_ids()
            if meta.get('imdb', None) is None:
                imdb_id = external.get('imdb_id', "0")
                if imdb_id == "" or imdb_id is None:
                    meta['imdb_id'] = '0'
                else:
                    meta['imdb_id'] = str(int(imdb_id.replace('tt', ''))).zfill(7)
            else:
                meta['imdb_id'] = str(int(meta['imdb'].replace('tt', ''))).zfill(7)
            if meta.get('tvdb_id', '0') in ['', ' ', None, 'None', '0']:
                meta['tvdb_id'] = external.get('tvdb_id', '0')
                if meta['tvdb_id'] in ["", None, " ", "None"]:
                    meta['tvdb_id'] = '0'
            try:
                videos = tv.videos()
                for each in videos.get('results', []):
                    if each.get('site', "") == 'YouTube' and each.get('type', "") == "Trailer":
                        meta['youtube'] = f"{each.get('key')}"
                        break
            except Exception:
                console.print('[yellow]Unable to grab videos from TMDb.')

            # meta['aka'] = f" AKA {response['original_name']}"
            meta['aka'], original_language = await self.get_imdb_aka(meta['imdb_id'])
            if original_language is not None:
                meta['original_language'] = original_language
            else:
                meta['original_language'] = response['original_language']
            meta['original_title'] = response.get('original_name', meta['title'])
            meta['keywords'] = self.get_keywords(tv)
            meta['genres'] = self.get_genres(response)
            meta['adult'] = response['adult']
            meta['tmdb_directors'] = self.get_directors(tv)
            meta['mal_id'], meta['aka'], meta['anime'] = self.get_anime(response, meta)
            meta['poster'] = response.get('poster_path', '')
            meta['overview'] = response['overview']

            meta['tmdb_type'] = response.get('type', 'Scripted')
            runtime = response.get('episode_run_time', [60])
            if runtime == []:
                runtime = [60]
            meta['runtime'] = runtime[0]

            logo_response = tv.images() 
            logos = logo_response.get('logos', [])
            first_logo = next((logo for logo in logos if logo.get('iso_639_1') == 'en'), None) or (logos[0] if logos else None)

            if first_logo:
                logo_path = first_logo['file_path']
                meta['logo'] = f"https://image.tmdb.org/t/p/original{logo_path}"
            else:
                meta['logo'] = None 
                
            #Get backdrop for non-tmdb linked categories
            backdrop_response = tv.images()
            backdrops = backdrop_response.get('backdrops', [])
            first_backdrop = next((backdrop for backdrop in backdrops if backdrop.get('iso_639_1') == 'en'), None) or (backdrops[0] if backdrops else None)
            if first_backdrop:
                backdrop_path = first_backdrop['file_path']
                meta['backdrop'] = f"https://image.tmdb.org/t/p/original{backdrop_path}"
            else:
                meta['backdrop'] = None

        if meta['poster'] not in (None, ''):
            meta['poster'] = f"https://image.tmdb.org/t/p/original{meta['poster']}"

        difference = SequenceMatcher(None, meta['title'].lower(), meta['aka'][5:].lower()).ratio()
        if difference >= 0.9 or meta['aka'][5:].strip() == "" or meta['aka'][5:].strip().lower() in meta['title'].lower():
            meta['aka'] = ""
        if f"({meta['year']})" in meta['aka']:
            meta['aka'] = meta['aka'].replace(f"({meta['year']})", "").strip()
            
        
        return meta



    def get_keywords(self, tmdb_info):
        if tmdb_info is not None:
            tmdb_keywords = tmdb_info.keywords()
            if tmdb_keywords.get('keywords') is not None:
                keywords=[f"{keyword['name'].replace(',',' ')}" for keyword in tmdb_keywords.get('keywords')]
            elif tmdb_keywords.get('results') is not None:
                keywords=[f"{keyword['name'].replace(',',' ')}" for keyword in tmdb_keywords.get('results')]
            return(', '.join(keywords))
        else:
            return ''

    def get_genres(self, tmdb_info):
        if tmdb_info is not None:
            tmdb_genres = tmdb_info.get('genres', [])
            if tmdb_genres:
                genres = [genre['name'].replace(',', ' ') for genre in tmdb_genres]
                return ', '.join(genres)
        return ''

    def get_directors(self, tmdb_info):
        if tmdb_info is not None:
            tmdb_credits = tmdb_info.credits()
            directors = []
            if tmdb_credits.get('cast', []) != []:
                for each in tmdb_credits['cast']:
                    if each.get('known_for_department', '') == "Directing":
                        directors.append(each.get('original_name', each.get('name')))
            return directors
        else:
            return ''

    def get_anime(self, response, meta):
        tmdb_name = meta['title']
        if meta.get('aka', "") == "":
            alt_name = ""
        else:
            alt_name = meta['aka']
        anime = False
        animation = False
        for each in response['genres']:
            if each['id'] == 16:
                animation = True
        if response['original_language'] == 'ja' and animation:
            romaji, mal_id, eng_title, season_year, episodes = self.get_romaji(tmdb_name, meta.get('mal', None))
            alt_name = f" AKA {romaji}"
            
            anime = True
            # mal = AnimeSearch(romaji)
            # mal_id = mal.results[0].mal_id
        else:
            mal_id = 0
        if meta.get('mal_id', 0) != 0:
            mal_id = meta.get('mal_id')
        if meta.get('mal') not in ('0', 0, None):
            mal_id = meta.get('mal', 0)
        return mal_id, alt_name, anime

    def get_romaji(self, tmdb_name, mal):
        if mal is None:
            mal = 0
            tmdb_name = tmdb_name.replace('-', "").replace("The Movie", "")
            tmdb_name = ' '.join(tmdb_name.split())
            query = '''
                query ($search: String) {
                    Page (page: 1) {
                        pageInfo {
                            total
                        }
                    media (search: $search, type: ANIME, sort: SEARCH_MATCH) {
                        id
                        idMal
                        title {
                            romaji
                            english
                            native
                        }
                        seasonYear
                        episodes
                    }
                }
            }
            '''
            # Define our query variables and values that will be used in the query request
            variables = {
                'search': tmdb_name
            }
        else:
            query = '''
                query ($search: Int) {
                    Page (page: 1) {
                        pageInfo {
                            total
                        }
                    media (idMal: $search, type: ANIME, sort: SEARCH_MATCH) {
                        id
                        idMal
                        title {
                            romaji
                            english
                            native
                        }
                        seasonYear
                        episodes
                    }
                }
            }
            '''
            # Define our query variables and values that will be used in the query request
            variables = {
                'search': mal
            }

        # Make the HTTP Api request
        url = 'https://graphql.anilist.co'
        try:
            response = requests.post(url, json={'query': query, 'variables': variables})
            json = response.json()
            media = json['data']['Page']['media']
        except:
            console.print('[red]Failed to get anime specific info from anilist. Continuing without it...')
            media = []
        if media not in (None, []):
            result = {'title' : {}}
            difference = 0
            for anime in media:
                search_name = re.sub(r"[^0-9a-zA-Z\[\]]+", "", tmdb_name.lower().replace(' ', ''))
                for title in anime['title'].values():
                    if title is not None:
                        title = re.sub(u'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf]+ (?=[A-Za-z ]+–)', "", title.lower().replace(' ', ''), re.U)
                        diff = SequenceMatcher(None, title, search_name).ratio()
                        if diff >= difference:
                            result = anime
                            difference = diff

            romaji = result['title'].get('romaji', result['title'].get('english', ""))
            mal_id = result.get('idMal', 0)
            eng_title = result['title'].get('english', result['title'].get('romaji', ""))
            season_year = result.get('season_year', "")
            episodes = result.get('episodes', 0)
        else:
            romaji = eng_title = season_year  = ""
            episodes = mal_id = 0
        if mal_id in [None, 0]:
            mal_id = mal
        if not episodes:
            episodes = 0
        return romaji, mal_id, eng_title, season_year, episodes



    """
    Mediainfo/Bdinfo > meta
    """
    def get_audio_v2(self, mi, meta, bdinfo):
        if meta.get('is_music', False):  
            if not mi or 'media' not in mi or 'track' not in mi.get('media', {}):
                return "Unknown", "Unknown", False

            audio_tracks = mi['media'].get('track', [])
            if not audio_tracks:
                return "No Audio", "Unknown", False

            track = audio_tracks[0]
            format = track.get('Format', 'Unknown')
            channels = track.get('Channels', 'Unknown')
            if channels.isdigit():
                channels = f"{channels}.0"

            if track.get('Language', '') == "zxx":
                meta['silent'] = True

            # Simple codec mapping for music
            codec_map = {
                "DTS": "DTS",
                "AAC": "AAC",
                "AC-3": "DD",
                "E-AC-3": "DD+",
                "MLP FBA": "TrueHD",
                "FLAC": "FLAC",
                "Opus": "Opus",
                "Vorbis": "Vorbis",
                "PCM": "LPCM",
            }
            codec = codec_map.get(format, format)

            return f"{codec} {channels}", channels, False
        else:
            extra = dual = ""
            has_commentary = False
            #Get formats
            if bdinfo is not None: #Disks
                format_settings = ""
                format = bdinfo['audio'][0]['codec']
                commercial = format
                try:
                    additional = bdinfo['audio'][0]['atmos_why_you_be_like_this']
                except:
                    additional = ""
                #Channels
                chan = bdinfo['audio'][0]['channels']


            else: 
                track_num = 2
                for i in range(len(mi['media']['track'])):
                    t = mi['media']['track'][i]
                    if t['@type'] != "Audio":
                        pass
                    else: 
                        if t.get('Language', "") == meta['original_language'] and "commentary" not in t.get('Title', '').lower():
                            track_num = i
                            break
                format = mi['media']['track'][track_num]['Format']
                commercial = mi['media']['track'][track_num].get('Format_Commercial', '')
                if mi['media']['track'][track_num].get('Language', '') == "zxx":
                    meta['silent'] = True
                try:
                    additional = mi['media']['track'][track_num]['Format_AdditionalFeatures']
                    # format = f"{format} {additional}"
                except:
                    additional = ""
                try:
                    format_settings = mi['media']['track'][track_num]['Format_Settings']
                    if format_settings in ['Explicit']:
                        format_settings = ""
                except:
                    format_settings = ""
                #Channels
                channels = mi['media']['track'][track_num].get('Channels_Original', mi['media']['track'][track_num]['Channels'])
                if not str(channels).isnumeric():
                    channels = mi['media']['track'][track_num]['Channels']
                try:
                    channel_layout = mi['media']['track'][track_num]['ChannelLayout']
                except:
                    try:
                        channel_layout = mi['media']['track'][track_num]['ChannelLayout_Original']
                    except:
                        channel_layout = ""
                if "LFE" in channel_layout:
                    chan = f"{int(channels) - 1}.1"
                elif channel_layout == "":
                    if int(channels) <= 2:
                        chan = f"{int(channels)}.0"
                    else:
                        chan = f"{int(channels) - 1}.1"
                else:
                    chan = f"{channels}.0"
                
                if meta['original_language'] != 'en':
                    eng, orig = False, False
                    try:
                        for t in mi['media']['track']:
                            if t['@type'] != "Audio":
                                pass
                            else: 
                                audio_language = t.get('Language', '')
                                # Check for English Language Track
                                if audio_language == "en" and "commentary" not in t.get('Title', '').lower():
                                    eng = True
                                # Check for original Language Track
                                if audio_language == meta['original_language'] and "commentary" not in t.get('Title', '').lower():
                                    orig = True
                                # Catch Chinese / Norwegian variants
                                variants = ['zh', 'cn', 'cmn', 'no', 'nb']
                                if audio_language in variants and meta['original_language'] in variants:
                                    orig = True
                                # Check for additional, bloated Tracks
                                if audio_language != meta['original_language'] and audio_language != "en":
                                    if meta['original_language'] not in variants and audio_language not in variants:
                                        audio_language = "und" if audio_language == "" else audio_language
                                        console.print(f"[bold red]This release has a(n) {audio_language} audio track, and may be considered bloated")
                                        time.sleep(5)
                        if eng and orig:
                            dual = "Dual-Audio"
                        elif eng and not orig and meta['original_language'] not in ['zxx', 'xx', None] and not meta.get('no_dub', False):
                            dual = "Dubbed"
                    except Exception:
                        console.print(traceback.print_exc())
                        pass
            
                
                for t in mi['media']['track']:
                    if t['@type'] != "Audio":
                        pass
                    else: 
                        if "commentary" in t.get('Title', '').lower():
                            has_commentary = True
            
            
            #Convert commercial name to naming conventions
            audio = {
                #Format
                "DTS": "DTS",
                "AAC": "AAC",
                "AAC LC": "AAC",
                "AC-3": "DD",
                "E-AC-3": "DD+",
                "MLP FBA": "TrueHD",
                "FLAC": "FLAC",
                "Opus": "Opus",
                "Vorbis": "Vorbis",
                "PCM": "LPCM",

                #BDINFO AUDIOS
                "LPCM Audio" : "LPCM",
                "Dolby Digital Audio" : "DD",
                "Dolby Digital Plus Audio" : "DD+",
                # "Dolby TrueHD" : "TrueHD",
                "Dolby TrueHD Audio" : "TrueHD",
                "DTS Audio" : "DTS", 
                "DTS-HD Master Audio" : "DTS-HD MA",
                "DTS-HD High-Res Audio" : "DTS-HD HRA",
                "DTS:X Master Audio" : "DTS:X"
            }
            audio_extra = {
                "XLL": "-HD MA",
                "XLL X": ":X",
                "ES": "-ES",
            }
            format_extra = {
                "JOC": " Atmos",
                "16-ch": " Atmos",
                "Atmos Audio": " Atmos",
            }
            format_settings_extra = {
                "Dolby Surround EX" : "EX"
            }

            commercial_names = {
                "Dolby Digital" : "DD",
                "Dolby Digital Plus" : "DD+",
                "Dolby TrueHD" : "TrueHD",
                "DTS-ES" : "DTS-ES",
                "DTS-HD High" : "DTS-HD HRA",
                "Free Lossless Audio Codec" : "FLAC",
                "DTS-HD Master Audio" : "DTS-HD MA"
                }

            
            search_format = True
            for key, value in commercial_names.items():
                if key in commercial:
                    codec = value
                    search_format = False
                if "Atmos" in commercial or format_extra.get(additional, "") == " Atmos":
                    extra = " Atmos"
            if search_format:
                codec = audio.get(format, "") + audio_extra.get(additional, "")
                extra = format_extra.get(additional, "")
            format_settings = format_settings_extra.get(format_settings, "")
            if format_settings == "EX" and chan == "5.1":
                format_settings = "EX"
            else:
                format_settings = ""

            if codec == "":
                codec = format
            
            if format.startswith("DTS"):
                if additional.endswith("X"):
                    codec = "DTS:X"
                    chan = f"{int(channels) - 1}.1"
            if format == "MPEG Audio":
                codec = mi['media']['track'][2].get('CodecID_Hint', '')

            

            audio = f"{dual} {codec} {format_settings} {chan}{extra}"
            audio = ' '.join(audio.split())
            return audio, chan, has_commentary


    def is_3d(self, mi, bdinfo):
        if bdinfo is not None:
            if bdinfo['video'][0]['3d'] != "":
                return "3D"
            else:
                return ""
        else:
            return ""

    def get_tag(self, video, meta):
        try:
            tag = guessit(video)['release_group']
            tag = f"-{tag}"
        except:
            tag = ""
        
        # Adjust to only keep the last part after the last dash
        if tag.startswith("-"):
            parts = tag[1:].split('-')
            tag = f"-{parts[-1]}"  # Keep only the last part

        if tag == "-":
            tag = ""
        if tag[1:].lower() in ["nogroup", 'nogrp']:
            tag = ""
        return tag


    def get_source(self, type, video, path, is_disc, meta):
        try:
            try:
                source = guessit(video)['source']
            except:
                try:
                    source = guessit(path)['source']
                except:
                    source = "BluRay"
            if meta.get('manual_source', None):
                source = meta['manual_source']
            if source in ("Blu-ray", "Ultra HD Blu-ray", "BluRay", "BR") or is_disc == "BDMV":
                if type == "DISC":
                    source = "Blu-ray"
                elif type in ('ENCODE', 'REMUX'):
                    source = "BluRay"
            if is_disc == "DVD" or source in ("DVD", "dvd"):
                try:
                    if is_disc == "DVD":
                        mediainfo = MediaInfo.parse(f"{meta['discs'][0]['path']}/VTS_{meta['discs'][0]['main_set'][0][:2]}_0.IFO")
                    else:
                        mediainfo = MediaInfo.parse(video)
                    for track in mediainfo.tracks:
                        if track.track_type == "Video":
                            system = track.standard
                    if system not in ("PAL", "NTSC"):
                        raise WeirdSystem
                except:
                    try:
                        other = guessit(video)['other']
                        if "PAL" in other:
                            system = "PAL"
                        elif "NTSC" in other:
                            system = "NTSC"
                    except:
                        system = ""
                finally:
                    if system is None:
                        system = ""        
                    if type == "REMUX":
                        system = f"{system} DVD".strip()
                    source = system
            if source in ("Web", "WEB"):
                if type == "ENCODE":
                    type = "WEBRIP"
            if source in ("HD-DVD", "HD DVD", "HDDVD"):
                if is_disc == "HDDVD":
                    source = "HD DVD"
                if type in ("ENCODE", "REMUX"):
                    source = "HDDVD"
            if type in ("WEBDL", 'WEBRIP'):
                source = "Web"
            if source == "Ultra HDTV":
                source = "UHDTV"
        except Exception:
            console.print(traceback.format_exc())
            source = "BluRay"

        return source, type

    def get_uhd(self, type, guess, resolution, path):
        try:
            source = guess['Source']
            other = guess['Other']
        except:
            source = ""
            other = ""
        uhd = ""
        if source == 'Blu-ray' and other == "Ultra HD" or source == "Ultra HD Blu-ray":
            uhd = "UHD"
        elif "UHD" in path:
            uhd = "UHD"
        elif type in ("DISC", "REMUX", "ENCODE", "WEBRIP"):
            uhd = ""
            
        if type in ("DISC", "REMUX", "ENCODE") and resolution == "2160p":
            uhd = "UHD"

        return uhd

    def get_hdr(self, mi, bdinfo):
        hdr = ""
        dv = ""
        if bdinfo is not None: #Disks
            hdr_mi = bdinfo['video'][0]['hdr_dv']
            if "HDR10+" in hdr_mi:
                hdr = "HDR10+"
            elif hdr_mi == "HDR10":
                hdr = "HDR"
            try:
                if bdinfo['video'][1]['hdr_dv'] == "Dolby Vision":
                    dv = "DV"
            except:
                pass
        else: 
            video_track = mi['media']['track'][1]
            try:
                hdr_mi = video_track['colour_primaries']
                if hdr_mi in ("BT.2020", "REC.2020"):
                    hdr = ""
                    hdr_format_string = video_track.get('HDR_Format_Compatibility', video_track.get('HDR_Format_String', video_track.get('HDR_Format', "")))
                    if "HDR10" in hdr_format_string:
                        hdr = "HDR"
                    if "HDR10+" in hdr_format_string:
                        hdr = "HDR10+"
                    if hdr_format_string == "" and "PQ" in (video_track.get('transfer_characteristics'), video_track.get('transfer_characteristics_Original', None)):
                        hdr = "PQ10"
                    transfer_characteristics = video_track.get('transfer_characteristics_Original', None)
                    if "HLG" in transfer_characteristics:
                        hdr = "HLG"
                    if hdr != "HLG" and "BT.2020 (10-bit)" in transfer_characteristics:
                        hdr = "WCG"
            except:
                pass

            try:
                if "Dolby Vision" in video_track.get('HDR_Format', '') or "Dolby Vision" in video_track.get('HDR_Format_String', ''):
                    dv = "DV"
            except:
                pass

        hdr = f"{dv} {hdr}".strip()
        return hdr

    def get_region(self, bdinfo, region=None):
        label = bdinfo.get('label', bdinfo.get('title', bdinfo.get('path', ''))).replace('.', ' ')
        if region is not None:
            region = region.upper()
        else: 
            regions = {
            'AFG': 'AFG', 'AIA': 'AIA', 'ALA': 'ALA', 'ALG': 'ALG', 'AND': 'AND', 'ANG': 'ANG', 'ARG': 'ARG', 
            'ARM': 'ARM', 'ARU': 'ARU', 'ASA': 'ASA', 'ATA': 'ATA', 'ATF': 'ATF', 'ATG': 'ATG', 'AUS': 'AUS', 
            'AUT': 'AUT', 'AZE': 'AZE', 'BAH': 'BAH', 'BAN': 'BAN', 'BDI': 'BDI', 'BEL': 'BEL', 'BEN': 'BEN', 
            'BER': 'BER', 'BES': 'BES', 'BFA': 'BFA', 'BHR': 'BHR', 'BHU': 'BHU', 'BIH': 'BIH', 'BLM': 'BLM', 
            'BLR': 'BLR', 'BLZ': 'BLZ', 'BOL': 'BOL', 'BOT': 'BOT', 'BRA': 'BRA', 'BRB': 'BRB', 'BRU': 'BRU', 
            'BVT': 'BVT', 'CAM': 'CAM', 'CAN': 'CAN', 'CAY': 'CAY', 'CCK': 'CCK', 'CEE': 'CEE', 'CGO': 'CGO', 
            'CHA': 'CHA', 'CHI': 'CHI', 'CHN': 'CHN', 'CIV': 'CIV', 'CMR': 'CMR', 'COD': 'COD', 'COK': 'COK', 
            'COL': 'COL', 'COM': 'COM', 'CPV': 'CPV', 'CRC': 'CRC', 'CRO': 'CRO', 'CTA': 'CTA', 'CUB': 'CUB', 
            'CUW': 'CUW', 'CXR': 'CXR', 'CYP': 'CYP', 'DJI': 'DJI', 'DMA': 'DMA', 'DOM': 'DOM', 'ECU': 'ECU', 
            'EGY': 'EGY', 'ENG': 'ENG', 'EQG': 'EQG', 'ERI': 'ERI', 'ESH': 'ESH', 'ESP': 'ESP', 'ETH': 'ETH', 
            'FIJ': 'FIJ', 'FLK': 'FLK', 'FRA': 'FRA', 'FRO': 'FRO', 'FSM': 'FSM', 'GAB': 'GAB', 'GAM': 'GAM', 
            'GBR': 'GBR', 'GEO': 'GEO', 'GER': 'GER', 'GGY': 'GGY', 'GHA': 'GHA', 'GIB': 'GIB', 'GLP': 'GLP', 
            'GNB': 'GNB', 'GRE': 'GRE', 'GRL': 'GRL', 'GRN': 'GRN', 'GUA': 'GUA', 'GUF': 'GUF', 'GUI': 'GUI', 
            'GUM': 'GUM', 'GUY': 'GUY', 'HAI': 'HAI', 'HKG': 'HKG', 'HMD': 'HMD', 'HON': 'HON', 'HUN': 'HUN', 
            'IDN': 'IDN', 'IMN': 'IMN', 'IND': 'IND', 'IOT': 'IOT', 'IRL': 'IRL', 'IRN': 'IRN', 'IRQ': 'IRQ', 
            'ISL': 'ISL', 'ISR': 'ISR', 'ITA': 'ITA', 'JAM': 'JAM', 'JEY': 'JEY', 'JOR': 'JOR', 'JPN': 'JPN', 
            'KAZ': 'KAZ', 'KEN': 'KEN', 'KGZ': 'KGZ', 'KIR': 'KIR', 'KNA': 'KNA', 'KOR': 'KOR', 'KSA': 'KSA', 
            'KUW': 'KUW', 'KVX': 'KVX', 'LAO': 'LAO', 'LBN': 'LBN', 'LBR': 'LBR', 'LBY': 'LBY', 'LCA': 'LCA', 
            'LES': 'LES', 'LIE': 'LIE', 'LKA': 'LKA', 'LUX': 'LUX', 'MAC': 'MAC', 'MAD': 'MAD', 'MAF': 'MAF', 
            'MAR': 'MAR', 'MAS': 'MAS', 'MDA': 'MDA', 'MDV': 'MDV', 'MEX': 'MEX', 'MHL': 'MHL', 'MKD': 'MKD', 
            'MLI': 'MLI', 'MLT': 'MLT', 'MNG': 'MNG', 'MNP': 'MNP', 'MON': 'MON', 'MOZ': 'MOZ', 'MRI': 'MRI', 
            'MSR': 'MSR', 'MTN': 'MTN', 'MTQ': 'MTQ', 'MWI': 'MWI', 'MYA': 'MYA', 'MYT': 'MYT', 'NAM': 'NAM', 
            'NCA': 'NCA', 'NCL': 'NCL', 'NEP': 'NEP', 'NFK': 'NFK', 'NIG': 'NIG', 'NIR': 'NIR', 'NIU': 'NIU', 
            'NLD': 'NLD', 'NOR': 'NOR', 'NRU': 'NRU', 'NZL': 'NZL', 'OMA': 'OMA', 'PAK': 'PAK', 'PAN': 'PAN', 
            'PAR': 'PAR', 'PCN': 'PCN', 'PER': 'PER', 'PHI': 'PHI', 'PLE': 'PLE', 'PLW': 'PLW', 'PNG': 'PNG', 
            'POL': 'POL', 'POR': 'POR', 'PRK': 'PRK', 'PUR': 'PUR', 'QAT': 'QAT', 'REU': 'REU', 'ROU': 'ROU', 
            'RSA': 'RSA', 'RUS': 'RUS', 'RWA': 'RWA', 'SAM': 'SAM', 'SCO': 'SCO', 'SDN': 'SDN', 'SEN': 'SEN', 
            'SEY': 'SEY', 'SGS': 'SGS', 'SHN': 'SHN', 'SIN': 'SIN', 'SJM': 'SJM', 'SLE': 'SLE', 'SLV': 'SLV', 
            'SMR': 'SMR', 'SOL': 'SOL', 'SOM': 'SOM', 'SPM': 'SPM', 'SRB': 'SRB', 'SSD': 'SSD', 'STP': 'STP', 
            'SUI': 'SUI', 'SUR': 'SUR', 'SWZ': 'SWZ', 'SXM': 'SXM', 'SYR': 'SYR', 'TAH': 'TAH', 'TAN': 'TAN', 
            'TCA': 'TCA', 'TGA': 'TGA', 'THA': 'THA', 'TJK': 'TJK', 'TKL': 'TKL', 'TKM': 'TKM', 'TLS': 'TLS', 
            'TOG': 'TOG', 'TRI': 'TRI', 'TUN': 'TUN', 'TUR': 'TUR', 'TUV': 'TUV', 'TWN': 'TWN', 'UAE': 'UAE', 
            'UGA': 'UGA', 'UKR': 'UKR', 'UMI': 'UMI', 'URU': 'URU', 'USA': 'USA', 'UZB': 'UZB', 'VAN': 'VAN', 
            'VAT': 'VAT', 'VEN': 'VEN', 'VGB': 'VGB', 'VIE': 'VIE', 'VIN': 'VIN', 'VIR': 'VIR', 'WAL': 'WAL', 
            'WLF': 'WLF', 'YEM': 'YEM', 'ZAM': 'ZAM', 'ZIM': 'ZIM', "EUR" : "EUR"
            }
            for key, value in regions.items():
                if f" {key} " in label:
                    region = value
                    
        if region is None:
            region = ""
        return region

    def get_distributor(self, distributor_in):
        distributor_list = [
            '01 DISTRIBUTION', '100 DESTINATIONS TRAVEL FILM', '101 FILMS', '1FILMS', '2 ENTERTAIN VIDEO', '20TH CENTURY FOX', '2L', '3D CONTENT HUB', '3D MEDIA', '3L FILM', '4DIGITAL', '4DVD', '4K ULTRA HD MOVIES', '4K UHD', '8-FILMS', '84 ENTERTAINMENT', '88 FILMS', '@ANIME', 'ANIME', 'A CONTRACORRIENTE', 'A CONTRACORRIENTE FILMS', 'A&E HOME VIDEO', 'A&E', 'A&M RECORDS', 'A+E NETWORKS', 'A+R', 'A-FILM', 'AAA', 'AB VIDÉO', 'AB VIDEO', 'ABC - (AUSTRALIAN BROADCASTING CORPORATION)', 'ABC', 'ABKCO', 'ABSOLUT MEDIEN', 'ABSOLUTE', 'ACCENT FILM ENTERTAINMENT', 'ACCENTUS', 'ACORN MEDIA', 'AD VITAM', 'ADA', 'ADITYA VIDEOS', 'ADSO FILMS', 'AFM RECORDS', 'AGFA', 'AIX RECORDS', 
            'ALAMODE FILM', 'ALBA RECORDS', 'ALBANY RECORDS', 'ALBATROS', 'ALCHEMY', 'ALIVE', 'ALL ANIME', 'ALL INTERACTIVE ENTERTAINMENT', 'ALLEGRO', 'ALLIANCE', 'ALPHA MUSIC', 'ALTERDYSTRYBUCJA', 'ALTERED INNOCENCE', 'ALTITUDE FILM DISTRIBUTION', 'ALUCARD RECORDS', 'AMAZING D.C.', 'AMAZING DC', 'AMMO CONTENT', 'AMUSE SOFT ENTERTAINMENT', 'ANCONNECT', 'ANEC', 'ANIMATSU', 'ANIME HOUSE', 'ANIME LTD', 'ANIME WORKS', 'ANIMEIGO', 'ANIPLEX', 'ANOLIS ENTERTAINMENT', 'ANOTHER WORLD ENTERTAINMENT', 'AP INTERNATIONAL', 'APPLE', 'ARA MEDIA', 'ARBELOS', 'ARC ENTERTAINMENT', 'ARP SÉLECTION', 'ARP SELECTION', 'ARROW', 'ART SERVICE', 'ART VISION', 'ARTE ÉDITIONS', 'ARTE EDITIONS', 'ARTE VIDÉO', 
            'ARTE VIDEO', 'ARTHAUS MUSIK', 'ARTIFICIAL EYE', 'ARTSPLOITATION FILMS', 'ARTUS FILMS', 'ASCOT ELITE HOME ENTERTAINMENT', 'ASIA VIDEO', 'ASMIK ACE', 'ASTRO RECORDS & FILMWORKS', 'ASYLUM', 'ATLANTIC FILM', 'ATLANTIC RECORDS', 'ATLAS FILM', 'AUDIO VISUAL ENTERTAINMENT', 'AURO-3D CREATIVE LABEL', 'AURUM', 'AV VISIONEN', 'AV-JET', 'AVALON', 'AVENTI', 'AVEX TRAX', 'AXIOM', 'AXIS RECORDS', 'AYNGARAN', 'BAC FILMS', 'BACH FILMS', 'BANDAI VISUAL', 'BARCLAY', 'BBC', 'BRITISH BROADCASTING CORPORATION', 'BBI FILMS', 'BBI', 'BCI HOME ENTERTAINMENT', 'BEGGARS BANQUET', 'BEL AIR CLASSIQUES', 'BELGA FILMS', 'BELVEDERE', 'BENELUX FILM DISTRIBUTORS', 'BENNETT-WATT MEDIA', 'BERLIN CLASSICS', 'BERLINER PHILHARMONIKER RECORDINGS', 'BEST ENTERTAINMENT', 'BEYOND HOME ENTERTAINMENT', 'BFI VIDEO', 'BFI', 'BRITISH FILM INSTITUTE', 'BFS ENTERTAINMENT', 'BFS', 'BHAVANI', 'BIBER RECORDS', 'BIG HOME VIDEO', 'BILDSTÖRUNG', 
            'BILDSTORUNG', 'BILL ZEBUB', 'BIRNENBLATT', 'BIT WEL', 'BLACK BOX', 'BLACK HILL PICTURES', 'BLACK HILL', 'BLACK HOLE RECORDINGS', 'BLACK HOLE', 'BLAQOUT', 'BLAUFIELD MUSIC', 'BLAUFIELD', 'BLOCKBUSTER ENTERTAINMENT', 'BLOCKBUSTER', 'BLU PHASE MEDIA', 'BLU-RAY ONLY', 'BLU-RAY', 'BLURAY ONLY', 'BLURAY', 'BLUE GENTIAN RECORDS', 'BLUE KINO', 'BLUE UNDERGROUND', 'BMG/ARISTA', 'BMG', 'BMGARISTA', 'BMG ARISTA', 'ARISTA', 'ARISTA/BMG', 'ARISTABMG', 'ARISTA BMG', 'BONTON FILM', 'BONTON', 'BOOMERANG PICTURES', 'BOOMERANG', 'BQHL ÉDITIONS', 'BQHL EDITIONS', 'BQHL', 'BREAKING GLASS', 'BRIDGESTONE', 'BRINK', 'BROAD GREEN PICTURES', 'BROAD GREEN', 'BUSCH MEDIA GROUP', 'BUSCH', 'C MAJOR', 'C.B.S.', 'CAICHANG', 'CALIFÓRNIA FILMES', 'CALIFORNIA FILMES', 'CALIFORNIA', 'CAMEO', 'CAMERA OBSCURA', 'CAMERATA', 'CAMP MOTION PICTURES', 'CAMP MOTION', 'CAPELIGHT PICTURES', 'CAPELIGHT', 'CAPITOL', 'CAPITOL RECORDS', 'CAPRICCI', 'CARGO RECORDS', 'CARLOTTA FILMS', 'CARLOTTA', 'CARLOTA', 'CARMEN FILM', 'CASCADE', 'CATCHPLAY', 'CAULDRON FILMS', 'CAULDRON', 'CBS TELEVISION STUDIOS', 'CBS', 'CCTV', 'CCV ENTERTAINMENT', 'CCV', 'CD BABY', 'CD LAND', 'CECCHI GORI', 'CENTURY MEDIA', 'CHUAN XUN SHI DAI MULTIMEDIA', 'CINE-ASIA', 'CINÉART', 'CINEART', 'CINEDIGM', 'CINEFIL IMAGICA', 'CINEMA EPOCH', 'CINEMA GUILD', 'CINEMA LIBRE STUDIOS', 'CINEMA MONDO', 'CINEMATIC VISION', 'CINEPLOIT RECORDS', 'CINESTRANGE EXTREME', 'CITEL VIDEO', 'CITEL', 'CJ ENTERTAINMENT', 'CJ', 'CLASSIC MEDIA', 'CLASSICFLIX', 'CLASSICLINE', 'CLAUDIO RECORDS', 'CLEAR VISION', 'CLEOPATRA', 'CLOSE UP', 'CMS MEDIA LIMITED', 'CMV LASERVISION', 'CN ENTERTAINMENT', 'CODE RED', 'COHEN MEDIA GROUP', 'COHEN', 'COIN DE MIRE CINÉMA', 'COIN DE MIRE CINEMA', 'COLOSSEO FILM', 'COLUMBIA', 'COLUMBIA PICTURES', 'COLUMBIA/TRI-STAR', 'TRI-STAR', 'COMMERCIAL MARKETING', 'CONCORD MUSIC GROUP', 'CONCORDE VIDEO', 'CONDOR', 'CONSTANTIN FILM', 'CONSTANTIN', 'CONSTANTINO FILMES', 'CONSTANTINO', 'CONSTRUCTIVE MEDIA SERVICE', 'CONSTRUCTIVE', 'CONTENT ZONE', 'CONTENTS GATE', 'COQUEIRO VERDE', 'CORNERSTONE MEDIA', 'CORNERSTONE', 'CP DIGITAL', 'CREST MOVIES', 'CRITERION', 'CRITERION COLLECTION', 'CC', 'CRYSTAL CLASSICS', 'CULT EPICS', 'CULT FILMS', 'CULT VIDEO', 'CURZON FILM WORLD', 'D FILMS', "D'AILLY COMPANY", 'DAILLY COMPANY', 'D AILLY COMPANY', "D'AILLY", 'DAILLY', 'D AILLY', 'DA CAPO', 'DA MUSIC', "DALL'ANGELO PICTURES", 'DALLANGELO PICTURES', "DALL'ANGELO", 'DALL ANGELO PICTURES', 'DALL ANGELO', 'DAREDO', 'DARK FORCE ENTERTAINMENT', 'DARK FORCE', 'DARK SIDE RELEASING', 'DARK SIDE', 'DAZZLER MEDIA', 'DAZZLER', 'DCM PICTURES', 'DCM', 'DEAPLANETA', 'DECCA', 'DEEPJOY', 'DEFIANT SCREEN ENTERTAINMENT', 'DEFIANT SCREEN', 'DEFIANT', 'DELOS', 'DELPHIAN RECORDS', 'DELPHIAN', 'DELTA MUSIC & ENTERTAINMENT', 'DELTA MUSIC AND ENTERTAINMENT', 'DELTA MUSIC ENTERTAINMENT', 'DELTA MUSIC', 'DELTAMAC CO. LTD.', 'DELTAMAC CO LTD', 'DELTAMAC CO', 'DELTAMAC', 'DEMAND MEDIA', 'DEMAND', 'DEP', 'DEUTSCHE GRAMMOPHON', 'DFW', 'DGM', 'DIAPHANA', 'DIGIDREAMS STUDIOS', 'DIGIDREAMS', 'DIGITAL ENVIRONMENTS', 'DIGITAL', 'DISCOTEK MEDIA', 'DISCOVERY CHANNEL', 'DISCOVERY', 'DISK KINO', 'DISNEY / BUENA VISTA', 'DISNEY', 'BUENA VISTA', 'DISNEY BUENA VISTA', 'DISTRIBUTION SELECT', 'DIVISA', 'DNC ENTERTAINMENT', 'DNC', 'DOGWOOF', 'DOLMEN HOME VIDEO', 'DOLMEN', 'DONAU FILM', 'DONAU', 'DORADO FILMS', 'DORADO', 'DRAFTHOUSE FILMS', 'DRAFTHOUSE', 'DRAGON FILM ENTERTAINMENT', 'DRAGON ENTERTAINMENT', 'DRAGON FILM', 'DRAGON', 'DREAMWORKS', 'DRIVE ON RECORDS', 'DRIVE ON', 'DRIVE-ON', 'DRIVEON', 'DS MEDIA', 'DTP ENTERTAINMENT AG', 'DTP ENTERTAINMENT', 'DTP AG', 'DTP', 'DTS ENTERTAINMENT', 'DTS', 'DUKE MARKETING', 'DUKE VIDEO DISTRIBUTION', 'DUKE', 'DUTCH FILMWORKS', 'DUTCH', 'DVD INTERNATIONAL', 'DVD', 'DYBEX', 'DYNAMIC', 'DYNIT', 'E1 ENTERTAINMENT', 'E1', 'EAGLE ENTERTAINMENT', 'EAGLE HOME ENTERTAINMENT PVT.LTD.', 'EAGLE HOME ENTERTAINMENT PVTLTD', 'EAGLE HOME ENTERTAINMENT PVT LTD', 'EAGLE HOME ENTERTAINMENT', 'EAGLE PICTURES', 'EAGLE ROCK ENTERTAINMENT', 'EAGLE ROCK', 'EAGLE VISION MEDIA', 'EAGLE VISION', 'EARMUSIC', 'EARTH ENTERTAINMENT', 'EARTH', 'ECHO BRIDGE ENTERTAINMENT', 'ECHO BRIDGE', 'EDEL GERMANY GMBH', 'EDEL GERMANY', 'EDEL RECORDS', 'EDITION TONFILM', 'EDITIONS MONTPARNASSE', 'EDKO FILMS LTD.', 'EDKO FILMS LTD', 'EDKO FILMS', 
            'EDKO', "EIN'S M&M CO", 'EINS M&M CO', "EIN'S M&M", 'EINS M&M', 'ELEA-MEDIA', 'ELEA MEDIA', 'ELEA', 'ELECTRIC PICTURE', 'ELECTRIC', 'ELEPHANT FILMS', 'ELEPHANT', 'ELEVATION', 'EMI', 'EMON', 'EMS', 'EMYLIA', 'ENE MEDIA', 'ENE', 'ENTERTAINMENT IN VIDEO', 'ENTERTAINMENT IN', 'ENTERTAINMENT ONE', 'ENTERTAINMENT ONE FILMS CANADA INC.', 'ENTERTAINMENT ONE FILMS CANADA INC', 'ENTERTAINMENT ONE FILMS CANADA', 'ENTERTAINMENT ONE CANADA INC', 'ENTERTAINMENT ONE CANADA', 'ENTERTAINMENTONE', 'EONE', 'EOS', 'EPIC PICTURES', 'EPIC', 'EPIC RECORDS', 'ERATO', 'EROS', 'ESC EDITIONS', 'ESCAPI MEDIA BV', 'ESOTERIC RECORDINGS', 'ESPN FILMS', 'EUREKA ENTERTAINMENT', 'EUREKA', 'EURO PICTURES', 'EURO VIDEO', 'EUROARTS', 'EUROPA FILMES', 'EUROPA', 'EUROPACORP', 'EUROZOOM', 'EXCEL', 'EXPLOSIVE MEDIA', 'EXPLOSIVE', 'EXTRALUCID FILMS', 'EXTRALUCID', 'EYE SEE MOVIES', 'EYE SEE', 'EYK MEDIA', 'EYK', 'FABULOUS FILMS', 'FABULOUS', 'FACTORIS FILMS', 'FACTORIS', 'FARAO RECORDS', 'FARBFILM HOME ENTERTAINMENT', 'FARBFILM ENTERTAINMENT', 'FARBFILM HOME', 'FARBFILM', 'FEELGOOD ENTERTAINMENT', 'FEELGOOD', 'FERNSEHJUWELEN', 'FILM CHEST', 'FILM MEDIA', 'FILM MOVEMENT', 'FILM4', 'FILMART', 'FILMAURO', 'FILMAX', 'FILMCONFECT HOME ENTERTAINMENT', 'FILMCONFECT ENTERTAINMENT', 'FILMCONFECT HOME', 'FILMCONFECT', 'FILMEDIA', 'FILMJUWELEN', 'FILMOTEKA NARODAWA', 'FILMRISE', 'FINAL CUT ENTERTAINMENT', 'FINAL CUT', 'FIREHOUSE 12 RECORDS', 'FIREHOUSE 12', 'FIRST INTERNATIONAL PRODUCTION', 'FIRST INTERNATIONAL', 'FIRST LOOK STUDIOS', 'FIRST LOOK', 'FLAGMAN TRADE', 'FLASHSTAR FILMES', 'FLASHSTAR', 'FLICKER ALLEY', 'FNC ADD CULTURE', 'FOCUS FILMES', 'FOCUS', 'FOKUS MEDIA', 'FOKUSA', 'FOX PATHE EUROPA', 'FOX PATHE', 'FOX EUROPA', 'FOX/MGM', 'FOX MGM', 'MGM', 'MGM/FOX', 'FOX', 'FPE', 'FRANCE TÉLÉVISIONS DISTRIBUTION', 'FRANCE TELEVISIONS DISTRIBUTION', 'FRANCE TELEVISIONS', 'FRANCE', 'FREE DOLPHIN ENTERTAINMENT', 'FREE DOLPHIN', 'FREESTYLE DIGITAL MEDIA', 'FREESTYLE DIGITAL', 'FREESTYLE', 'FREMANTLE HOME ENTERTAINMENT', 'FREMANTLE ENTERTAINMENT', 'FREMANTLE HOME', 'FREMANTL', 'FRENETIC FILMS', 'FRENETIC', 'FRONTIER WORKS', 'FRONTIER', 'FRONTIERS MUSIC', 'FRONTIERS RECORDS', 'FS FILM OY', 'FS FILM', 'FULL MOON FEATURES', 'FULL MOON', 'FUN CITY EDITIONS', 'FUN CITY', 
            'FUNIMATION ENTERTAINMENT', 'FUNIMATION', 'FUSION', 'FUTUREFILM', 'G2 PICTURES', 'G2', 'GAGA COMMUNICATIONS', 'GAGA', 'GAIAM', 'GALAPAGOS', 'GAMMA HOME ENTERTAINMENT', 'GAMMA ENTERTAINMENT', 'GAMMA HOME', 'GAMMA', 'GARAGEHOUSE PICTURES', 'GARAGEHOUSE', 'GARAGEPLAY (車庫娛樂)', '車庫娛樂', 'GARAGEPLAY (Che Ku Yu Le )', 'GARAGEPLAY', 'Che Ku Yu Le', 'GAUMONT', 'GEFFEN', 'GENEON ENTERTAINMENT', 'GENEON', 'GENEON UNIVERSAL ENTERTAINMENT', 'GENERAL VIDEO RECORDING', 'GLASS DOLL FILMS', 'GLASS DOLL', 'GLOBE MUSIC MEDIA', 'GLOBE MUSIC', 'GLOBE MEDIA', 'GLOBE', 'GO ENTERTAIN', 'GO', 'GOLDEN HARVEST', 'GOOD!MOVIES', 'GOOD! MOVIES', 'GOOD MOVIES', 'GRAPEVINE VIDEO', 'GRAPEVINE', 'GRASSHOPPER FILM', 'GRASSHOPPER FILMS', 'GRASSHOPPER', 'GRAVITAS VENTURES', 'GRAVITAS', 'GREAT MOVIES', 'GREAT', 'GREEN APPLE ENTERTAINMENT', 'GREEN ENTERTAINMENT', 'GREEN APPLE', 'GREEN', 'GREENNARAE MEDIA', 'GREENNARAE', 'GRINDHOUSE RELEASING', 'GRINDHOUSE', 'GRIND HOUSE', 'GRYPHON ENTERTAINMENT', 'GRYPHON', 'GUNPOWDER & SKY', 'GUNPOWDER AND SKY', 'GUNPOWDER SKY', 'GUNPOWDER + SKY', 'GUNPOWDER', 'HANABEE ENTERTAINMENT', 'HANABEE', 'HANNOVER HOUSE', 'HANNOVER', 'HANSESOUND', 'HANSE SOUND', 'HANSE', 'HAPPINET', 'HARMONIA MUNDI', 'HARMONIA', 'HBO', 'HDC', 'HEC', 'HELL & BACK RECORDINGS', 'HELL AND BACK RECORDINGS', 'HELL & BACK', 'HELL AND BACK', "HEN'S TOOTH VIDEO", 'HENS TOOTH VIDEO', "HEN'S TOOTH", 'HENS TOOTH', 'HIGH FLIERS', 'HIGHLIGHT', 'HILLSONG', 'HISTORY CHANNEL', 'HISTORY', 'HK VIDÉO', 'HK VIDEO', 'HK', 'HMH HAMBURGER MEDIEN HAUS', 'HAMBURGER MEDIEN HAUS', 'HMH HAMBURGER MEDIEN', 'HMH HAMBURGER', 'HMH', 'HOLLYWOOD CLASSIC ENTERTAINMENT', 'HOLLYWOOD CLASSIC', 'HOLLYWOOD PICTURES', 'HOLLYWOOD', 'HOPSCOTCH ENTERTAINMENT', 'HOPSCOTCH', 'HPM', 'HÄNNSLER CLASSIC', 'HANNSLER CLASSIC', 'HANNSLER', 'I-CATCHER', 'I CATCHER', 'ICATCHER', 'I-ON NEW MEDIA', 'I ON NEW MEDIA', 'ION NEW MEDIA', 'ION MEDIA', 'I-ON', 'ION', 'IAN PRODUCTIONS', 'IAN', 'ICESTORM', 'ICON FILM DISTRIBUTION', 'ICON DISTRIBUTION', 'ICON FILM', 'ICON', 'IDEALE AUDIENCE', 'IDEALE', 'IFC FILMS', 'IFC', 'IFILM', 'ILLUSIONS UNLTD.', 'ILLUSIONS UNLTD', 'ILLUSIONS', 'IMAGE ENTERTAINMENT', 'IMAGE', 'IMAGEM FILMES', 'IMAGEM', 'IMOVISION', 'IMPERIAL CINEPIX', 'IMPRINT', 'IMPULS HOME ENTERTAINMENT', 'IMPULS ENTERTAINMENT', 'IMPULS HOME', 'IMPULS', 'IN-AKUSTIK', 'IN AKUSTIK', 'INAKUSTIK', 'INCEPTION MEDIA GROUP', 'INCEPTION MEDIA', 'INCEPTION GROUP', 'INCEPTION', 'INDEPENDENT', 'INDICAN', 'INDIE RIGHTS', 'INDIE', 'INDIGO', 'INFO', 'INJOINGAN', 'INKED PICTURES', 'INKED', 'INSIDE OUT MUSIC', 'INSIDE MUSIC', 'INSIDE OUT', 'INSIDE', 'INTERCOM', 'INTERCONTINENTAL VIDEO', 'INTERCONTINENTAL', 'INTERGROOVE', 'INTERSCOPE', 'INVINCIBLE PICTURES', 'INVINCIBLE', 'ISLAND/MERCURY', 'ISLAND MERCURY', 'ISLANDMERCURY', 'ISLAND & MERCURY', 'ISLAND AND MERCURY', 'ISLAND', 'ITN', 'ITV DVD', 'ITV', 'IVC', 'IVE ENTERTAINMENT', 'IVE', 'J&R ADVENTURES', 'J&R', 'JR', 'JAKOB', 'JONU MEDIA', 'JONU', 'JRB PRODUCTIONS', 'JRB', 'JUST BRIDGE ENTERTAINMENT', 'JUST BRIDGE', 'JUST ENTERTAINMENT', 'JUST', 'KABOOM ENTERTAINMENT', 'KABOOM', 'KADOKAWA ENTERTAINMENT', 'KADOKAWA', 'KAIROS', 'KALEIDOSCOPE ENTERTAINMENT', 'KALEIDOSCOPE', 'KAM & RONSON ENTERPRISES', 'KAM & RONSON', 'KAM&RONSON ENTERPRISES', 'KAM&RONSON', 'KAM AND RONSON ENTERPRISES', 'KAM AND RONSON', 'KANA HOME VIDEO', 'KARMA FILMS', 'KARMA', 'KATZENBERGER', 'KAZE', 
            'KBS MEDIA', 'KBS', 'KD MEDIA', 'KD', 'KING MEDIA', 'KING', 'KING RECORDS', 'KINO LORBER', 'KINO', 'KINO SWIAT', 'KINOKUNIYA', 'KINOWELT HOME ENTERTAINMENT/DVD', 'KINOWELT HOME ENTERTAINMENT', 'KINOWELT ENTERTAINMENT', 'KINOWELT HOME DVD', 'KINOWELT ENTERTAINMENT/DVD', 'KINOWELT DVD', 'KINOWELT', 'KIT PARKER FILMS', 'KIT PARKER', 'KITTY MEDIA', 'KNM HOME ENTERTAINMENT', 'KNM ENTERTAINMENT', 'KNM HOME', 'KNM', 'KOBA FILMS', 'KOBA', 'KOCH ENTERTAINMENT', 'KOCH MEDIA', 'KOCH', 'KRAKEN RELEASING', 'KRAKEN', 'KSCOPE', 'KSM', 'KULTUR', "L'ATELIER D'IMAGES", "LATELIER D'IMAGES", "L'ATELIER DIMAGES", 'LATELIER DIMAGES', "L ATELIER D'IMAGES", "L'ATELIER D IMAGES", 
            'L ATELIER D IMAGES', "L'ATELIER", 'L ATELIER', 'LATELIER', 'LA AVENTURA AUDIOVISUAL', 'LA AVENTURA', 'LACE GROUP', 'LACE', 'LASER PARADISE', 'LAYONS', 'LCJ EDITIONS', 'LCJ', 'LE CHAT QUI FUME', 'LE PACTE', 'LEDICK FILMHANDEL', 'LEGEND', 'LEOMARK STUDIOS', 'LEOMARK', 'LEONINE FILMS', 'LEONINE', 'LICHTUNG MEDIA LTD', 'LICHTUNG LTD', 'LICHTUNG MEDIA LTD.', 'LICHTUNG LTD.', 'LICHTUNG MEDIA', 'LICHTUNG', 'LIGHTHOUSE HOME ENTERTAINMENT', 'LIGHTHOUSE ENTERTAINMENT', 'LIGHTHOUSE HOME', 'LIGHTHOUSE', 'LIGHTYEAR', 'LIONSGATE FILMS', 'LIONSGATE', 'LIZARD CINEMA TRADE', 'LLAMENTOL', 'LOBSTER FILMS', 'LOBSTER', 'LOGON', 'LORBER FILMS', 'LORBER', 'LOS BANDITOS FILMS', 'LOS BANDITOS', 'LOUD & PROUD RECORDS', 'LOUD AND PROUD RECORDS', 'LOUD & PROUD', 'LOUD AND PROUD', 'LSO LIVE', 'LUCASFILM', 'LUCKY RED', 'LUMIÈRE HOME ENTERTAINMENT', 'LUMIERE HOME ENTERTAINMENT', 'LUMIERE ENTERTAINMENT', 'LUMIERE HOME', 'LUMIERE', 'M6 VIDEO', 'M6', 'MAD DIMENSION', 'MADMAN ENTERTAINMENT', 'MADMAN', 'MAGIC BOX', 'MAGIC PLAY', 'MAGNA HOME ENTERTAINMENT', 'MAGNA ENTERTAINMENT', 'MAGNA HOME', 'MAGNA', 'MAGNOLIA PICTURES', 'MAGNOLIA', 'MAIDEN JAPAN', 'MAIDEN', 'MAJENG MEDIA', 'MAJENG', 'MAJESTIC HOME ENTERTAINMENT', 'MAJESTIC ENTERTAINMENT', 'MAJESTIC HOME', 'MAJESTIC', 'MANGA HOME ENTERTAINMENT', 'MANGA ENTERTAINMENT', 'MANGA HOME', 'MANGA', 'MANTA LAB', 'MAPLE STUDIOS', 'MAPLE', 'MARCO POLO PRODUCTION', 'MARCO POLO', 'MARIINSKY', 'MARVEL STUDIOS', 'MARVEL', 'MASCOT RECORDS', 'MASCOT', 'MASSACRE VIDEO', 'MASSACRE', 'MATCHBOX', 'MATRIX D', 'MAXAM', 'MAYA HOME ENTERTAINMENT', 'MAYA ENTERTAINMENT', 'MAYA HOME', 'MAYAT', 'MDG', 'MEDIA BLASTERS', 'MEDIA FACTORY', 'MEDIA TARGET DISTRIBUTION', 'MEDIA TARGET', 'MEDIAINVISION', 'MEDIATOON', 'MEDIATRES ESTUDIO', 'MEDIATRES STUDIO', 'MEDIATRES', 'MEDICI ARTS', 'MEDICI CLASSICS', 'MEDIUMRARE ENTERTAINMENT', 'MEDIUMRARE', 'MEDUSA', 'MEGASTAR', 'MEI AH', 'MELI MÉDIAS', 'MELI MEDIAS', 'MEMENTO FILMS', 'MEMENTO', 'MENEMSHA FILMS', 'MENEMSHA', 'MERCURY', 'MERCURY STUDIOS', 'MERGE SOFT PRODUCTIONS', 'MERGE PRODUCTIONS', 'MERGE SOFT', 'MERGE', 'METAL BLADE RECORDS', 'METAL BLADE', 'METEOR', 'METRO-GOLDWYN-MAYER', 'METRO GOLDWYN MAYER', 'METROGOLDWYNMAYER', 'METRODOME VIDEO', 'METRODOME', 'METROPOLITAN', 'MFA+', 'MFA', 'MIG FILMGROUP', 'MIG', 'MILESTONE', 'MILL CREEK ENTERTAINMENT', 'MILL CREEK', 'MILLENNIUM MEDIA', 'MILLENNIUM', 'MIRAGE ENTERTAINMENT', 'MIRAGE', 'MIRAMAX', 'MISTERIYA ZVUKA', 'MK2', 'MODE RECORDS', 'MODE', 'MOMENTUM PICTURES', 'MONDO HOME ENTERTAINMENT', 'MONDO ENTERTAINMENT', 'MONDO HOME', 'MONDO MACABRO', 'MONGREL MEDIA', 'MONOLIT', 'MONOLITH VIDEO', 'MONOLITH', 'MONSTER PICTURES', 'MONSTER', 'MONTEREY VIDEO', 'MONTEREY', 'MONUMENT RELEASING', 'MONUMENT', 'MORNINGSTAR', 'MORNING STAR', 'MOSERBAER', 'MOVIEMAX', 'MOVINSIDE', 'MPI MEDIA GROUP', 'MPI MEDIA', 'MPI', 'MR. BONGO FILMS', 'MR BONGO FILMS', 'MR BONGO', 'MRG (MERIDIAN)', 'MRG MERIDIAN', 'MRG', 'MERIDIAN', 'MUBI', 'MUG SHOT PRODUCTIONS', 'MUG SHOT', 'MULTIMUSIC', 'MULTI-MUSIC', 'MULTI MUSIC', 'MUSE', 'MUSIC BOX FILMS', 'MUSIC BOX', 'MUSICBOX', 'MUSIC BROKERS', 'MUSIC THEORIES', 'MUSIC VIDEO DISTRIBUTORS', 'MUSIC VIDEO', 'MUSTANG ENTERTAINMENT', 'MUSTANG', 'MVD VISUAL', 'MVD', 'MVD/VSC', 'MVL', 'MVM ENTERTAINMENT', 'MVM', 'MYNDFORM', 'MYSTIC NIGHT PICTURES', 'MYSTIC NIGHT', 'NAMELESS MEDIA', 'NAMELESS', 'NAPALM RECORDS', 'NAPALM', 'NATIONAL ENTERTAINMENT MEDIA', 'NATIONAL ENTERTAINMENT', 'NATIONAL MEDIA', 'NATIONAL FILM ARCHIVE', 'NATIONAL ARCHIVE', 'NATIONAL FILM', 'NATIONAL GEOGRAPHIC', 'NAT GEO TV', 'NAT GEO', 'NGO', 'NAXOS', 'NBCUNIVERSAL ENTERTAINMENT JAPAN', 'NBC UNIVERSAL ENTERTAINMENT JAPAN', 'NBCUNIVERSAL JAPAN', 'NBC UNIVERSAL JAPAN', 'NBC JAPAN', 'NBO ENTERTAINMENT', 'NBO', 'NEOS', 'NETFLIX', 'NETWORK', 'NEW BLOOD', 'NEW DISC', 'NEW KSM', 'NEW LINE CINEMA', 'NEW LINE', 'NEW MOVIE TRADING CO. LTD', 'NEW MOVIE TRADING CO LTD', 'NEW MOVIE TRADING CO', 'NEW MOVIE TRADING', 'NEW WAVE FILMS', 'NEW WAVE', 'NFI', 'NHK', 'NIPPONART', 'NIS AMERICA', 'NJUTAFILMS', 'NOBLE ENTERTAINMENT', 'NOBLE', 'NORDISK FILM', 'NORDISK', 'NORSK FILM', 'NORSK', 'NORTH AMERICAN MOTION PICTURES', 'NOS AUDIOVISUAIS', 'NOTORIOUS PICTURES', 'NOTORIOUS', 'NOVA MEDIA', 'NOVA', 'NOVA SALES AND DISTRIBUTION', 'NOVA SALES & DISTRIBUTION', 'NSM', 'NSM RECORDS', 'NUCLEAR BLAST', 'NUCLEUS FILMS', 'NUCLEUS', 'OBERLIN MUSIC', 'OBERLIN', 'OBRAS-PRIMAS DO CINEMA', 'OBRAS PRIMAS DO CINEMA', 'OBRASPRIMAS DO CINEMA', 'OBRAS-PRIMAS CINEMA', 'OBRAS PRIMAS CINEMA', 'OBRASPRIMAS CINEMA', 'OBRAS-PRIMAS', 'OBRAS PRIMAS', 'OBRASPRIMAS', 'ODEON', 'OFDB FILMWORKS', 'OFDB', 'OLIVE FILMS', 'OLIVE', 'ONDINE', 'ONSCREEN FILMS', 'ONSCREEN', 'OPENING DISTRIBUTION', 'OPERA AUSTRALIA', 'OPTIMUM HOME ENTERTAINMENT', 'OPTIMUM ENTERTAINMENT', 'OPTIMUM HOME', 'OPTIMUM', 'OPUS ARTE', 'ORANGE STUDIO', 'ORANGE', 'ORLANDO EASTWOOD FILMS', 'ORLANDO FILMS', 'ORLANDO EASTWOOD', 'ORLANDO', 'ORUSTAK PICTURES', 'ORUSTAK', 'OSCILLOSCOPE PICTURES', 'OSCILLOSCOPE', 'OUTPLAY', 'PALISADES TARTAN', 'PAN VISION', 'PANVISION', 'PANAMINT CINEMA', 'PANAMINT', 'PANDASTORM ENTERTAINMENT', 'PANDA STORM ENTERTAINMENT', 'PANDASTORM', 'PANDA STORM', 'PANDORA FILM', 'PANDORA', 'PANEGYRIC', 'PANORAMA', 'PARADE DECK FILMS', 'PARADE DECK', 'PARADISE', 'PARADISO FILMS', 'PARADOX', 'PARAMOUNT PICTURES', 'PARAMOUNT', 'PARIS FILMES', 'PARIS FILMS', 'PARIS', 'PARK CIRCUS', 'PARLOPHONE', 'PASSION RIVER', 'PATHE DISTRIBUTION', 'PATHE', 'PBS', 'PEACE ARCH TRINITY', 'PECCADILLO PICTURES', 'PEPPERMINT', 'PHASE 4 FILMS', 'PHASE 4', 'PHILHARMONIA BAROQUE', 'PICTURE HOUSE ENTERTAINMENT', 'PICTURE ENTERTAINMENT', 'PICTURE HOUSE', 'PICTURE', 'PIDAX', 
            'PINK FLOYD RECORDS', 'PINK FLOYD', 'PINNACLE FILMS', 'PINNACLE', 'PLAIN', 'PLATFORM ENTERTAINMENT LIMITED', 'PLATFORM ENTERTAINMENT LTD', 'PLATFORM ENTERTAINMENT LTD.', 'PLATFORM ENTERTAINMENT', 'PLATFORM', 'PLAYARTE', 'PLG UK CLASSICS', 'PLG UK', 'PLG', 'POLYBAND & TOPPIC VIDEO/WVG', 'POLYBAND AND TOPPIC VIDEO/WVG', 'POLYBAND & TOPPIC VIDEO WVG', 'POLYBAND & TOPPIC VIDEO AND WVG', 'POLYBAND & TOPPIC VIDEO & WVG', 'POLYBAND AND TOPPIC VIDEO WVG', 'POLYBAND AND TOPPIC VIDEO AND WVG', 'POLYBAND AND TOPPIC VIDEO & WVG', 'POLYBAND & TOPPIC VIDEO', 'POLYBAND AND TOPPIC VIDEO', 'POLYBAND & TOPPIC', 'POLYBAND AND TOPPIC', 'POLYBAND', 'WVG', 'POLYDOR', 'PONY', 'PONY CANYON', 'POTEMKINE', 'POWERHOUSE FILMS', 'POWERHOUSE', 'POWERSTATIOM', 'PRIDE & JOY', 'PRIDE AND JOY', 'PRINZ MEDIA', 'PRINZ', 'PRIS AUDIOVISUAIS', 'PRO VIDEO', 'PRO-VIDEO', 'PRO-MOTION', 'PRO MOTION', 'PROD. JRB', 'PROD JRB', 'PRODISC', 'PROKINO', 'PROVOGUE RECORDS', 'PROVOGUE', 'PROWARE', 'PULP VIDEO', 'PULP', 'PULSE VIDEO', 'PULSE', 'PURE AUDIO RECORDINGS', 'PURE AUDIO', 'PURE FLIX ENTERTAINMENT', 'PURE FLIX', 'PURE ENTERTAINMENT', 'PYRAMIDE VIDEO', 'PYRAMIDE', 'QUALITY FILMS', 'QUALITY', 'QUARTO VALLEY RECORDS', 'QUARTO VALLEY', 'QUESTAR', 'R SQUARED FILMS', 'R SQUARED', 'RAPID EYE MOVIES', 'RAPID EYE', 'RARO VIDEO', 'RARO', 'RAROVIDEO U.S.', 'RAROVIDEO US', 'RARO VIDEO US', 'RARO VIDEO U.S.', 'RARO U.S.', 'RARO US', 'RAVEN BANNER RELEASING', 'RAVEN BANNER', 'RAVEN', 'RAZOR DIGITAL ENTERTAINMENT', 'RAZOR DIGITAL', 'RCA', 'RCO LIVE', 'RCO', 'RCV', 'REAL GONE MUSIC', 'REAL GONE', 'REANIMEDIA', 'REANI MEDIA', 'REDEMPTION', 'REEL', 'RELIANCE HOME VIDEO & GAMES', 'RELIANCE HOME VIDEO AND GAMES', 'RELIANCE HOME VIDEO', 'RELIANCE VIDEO', 'RELIANCE HOME', 'RELIANCE', 'REM CULTURE', 'REMAIN IN LIGHT', 'REPRISE', 'RESEN', 'RETROMEDIA', 'REVELATION FILMS LTD.', 'REVELATION FILMS LTD', 'REVELATION FILMS', 'REVELATION LTD.', 'REVELATION LTD', 'REVELATION', 'REVOLVER ENTERTAINMENT', 'REVOLVER', 'RHINO MUSIC', 'RHINO', 'RHV', 'RIGHT STUF', 'RIMINI EDITIONS', 'RISING SUN MEDIA', 'RLJ ENTERTAINMENT', 'RLJ', 'ROADRUNNER RECORDS', 'ROADSHOW ENTERTAINMENT', 'ROADSHOW', 'RONE', 'RONIN FLIX', 'ROTANA HOME ENTERTAINMENT', 'ROTANA ENTERTAINMENT', 'ROTANA HOME', 'ROTANA', 'ROUGH TRADE', 
            'ROUNDER', 'SAFFRON HILL FILMS', 'SAFFRON HILL', 'SAFFRON', 'SAMUEL GOLDWYN FILMS', 'SAMUEL GOLDWYN', 'SAN FRANCISCO SYMPHONY', 'SANDREW METRONOME', 'SAPHRANE', 'SAVOR', 'SCANBOX ENTERTAINMENT', 'SCANBOX', 'SCENIC LABS', 'SCHRÖDERMEDIA', 'SCHRODERMEDIA', 'SCHRODER MEDIA', 'SCORPION RELEASING', 'SCORPION', 'SCREAM TEAM RELEASING', 'SCREAM TEAM', 'SCREEN MEDIA', 'SCREEN', 'SCREENBOUND PICTURES', 'SCREENBOUND', 'SCREENWAVE MEDIA', 'SCREENWAVE', 'SECOND RUN', 'SECOND SIGHT', 'SEEDSMAN GROUP', 'SELECT VIDEO', 'SELECTA VISION', 'SENATOR', 'SENTAI FILMWORKS', 'SENTAI', 'SEVEN7', 'SEVERIN FILMS', 'SEVERIN', 'SEVILLE', 'SEYONS ENTERTAINMENT', 'SEYONS', 'SF STUDIOS', 'SGL ENTERTAINMENT', 'SGL', 'SHAMELESS', 'SHAMROCK MEDIA', 'SHAMROCK', 'SHANGHAI EPIC MUSIC ENTERTAINMENT', 'SHANGHAI EPIC ENTERTAINMENT', 'SHANGHAI EPIC MUSIC', 'SHANGHAI MUSIC ENTERTAINMENT', 'SHANGHAI ENTERTAINMENT', 'SHANGHAI MUSIC', 'SHANGHAI', 'SHEMAROO', 'SHOCHIKU', 'SHOCK', 'SHOGAKU KAN', 'SHOUT FACTORY', 'SHOUT! FACTORY', 'SHOUT', 'SHOUT!', 'SHOWBOX', 'SHOWTIME ENTERTAINMENT', 'SHOWTIME', 'SHRIEK SHOW', 'SHUDDER', 'SIDONIS', 'SIDONIS CALYSTA', 'SIGNAL ONE ENTERTAINMENT', 'SIGNAL ONE', 'SIGNATURE ENTERTAINMENT', 'SIGNATURE', 'SILVER VISION', 'SINISTER FILM', 'SINISTER', 'SIREN VISUAL ENTERTAINMENT', 'SIREN VISUAL', 'SIREN ENTERTAINMENT', 'SIREN', 'SKANI', 'SKY DIGI', 
            'SLASHER // VIDEO', 'SLASHER / VIDEO', 'SLASHER VIDEO', 'SLASHER', 'SLOVAK FILM INSTITUTE', 'SLOVAK FILM', 'SFI', 'SM LIFE DESIGN GROUP', 'SMOOTH PICTURES', 'SMOOTH', 'SNAPPER MUSIC', 'SNAPPER', 'SODA PICTURES', 'SODA', 'SONO LUMINUS', 'SONY MUSIC', 'SONY PICTURES', 'SONY', 'SONY PICTURES CLASSICS', 'SONY CLASSICS', 'SOUL MEDIA', 'SOUL', 'SOULFOOD MUSIC DISTRIBUTION', 'SOULFOOD DISTRIBUTION', 'SOULFOOD MUSIC', 'SOULFOOD', 'SOYUZ', 'SPECTRUM', 'SPENTZOS FILM', 'SPENTZOS', 'SPIRIT ENTERTAINMENT', 'SPIRIT', 'SPIRIT MEDIA GMBH', 'SPIRIT MEDIA', 'SPLENDID ENTERTAINMENT', 'SPLENDID FILM', 'SPO', 'SQUARE ENIX', 'SRI BALAJI VIDEO', 'SRI BALAJI', 'SRI', 'SRI VIDEO', 'SRS CINEMA', 'SRS', 'SSO RECORDINGS', 'SSO', 'ST2 MUSIC', 'ST2', 'STAR MEDIA ENTERTAINMENT', 'STAR ENTERTAINMENT', 'STAR MEDIA', 'STAR', 'STARLIGHT', 'STARZ / ANCHOR BAY', 'STARZ ANCHOR BAY', 'STARZ', 'ANCHOR BAY', 'STER KINEKOR', 'STERLING ENTERTAINMENT', 'STERLING', 'STINGRAY', 'STOCKFISCH RECORDS', 'STOCKFISCH', 'STRAND RELEASING', 'STRAND', 'STUDIO 4K', 'STUDIO CANAL', 'STUDIO GHIBLI', 'GHIBLI', 'STUDIO HAMBURG ENTERPRISES', 'HAMBURG ENTERPRISES', 'STUDIO HAMBURG', 'HAMBURG', 'STUDIO S', 'SUBKULTUR ENTERTAINMENT', 'SUBKULTUR', 'SUEVIA FILMS', 'SUEVIA', 'SUMMIT ENTERTAINMENT', 'SUMMIT', 'SUNFILM ENTERTAINMENT', 'SUNFILM', 'SURROUND RECORDS', 'SURROUND', 'SVENSK FILMINDUSTRI', 'SVENSK', 'SWEN FILMES', 'SWEN FILMS', 'SWEN', 'SYNAPSE FILMS', 'SYNAPSE', 'SYNDICADO', 'SYNERGETIC', 'T- SERIES', 'T-SERIES', 'T SERIES', 'TSERIES', 'T.V.P.', 'TVP', 'TACET RECORDS', 'TACET', 'TAI SENG', 'TAI SHENG', 'TAKEONE', 'TAKESHOBO', 'TAMASA DIFFUSION', 'TC ENTERTAINMENT', 'TC', 'TDK', 'TEAM MARKETING', 'TEATRO REAL', 'TEMA DISTRIBUCIONES', 'TEMPE DIGITAL', 'TF1 VIDÉO', 'TF1 VIDEO', 'TF1', 'THE BLU', 'BLU', 'THE ECSTASY OF FILMS', 'THE FILM DETECTIVE', 'FILM DETECTIVE', 'THE JOKERS', 'JOKERS', 'THE ON', 'ON', 'THIMFILM', 'THIM FILM', 'THIM', 'THIRD WINDOW FILMS', 'THIRD WINDOW', '3RD WINDOW FILMS', '3RD WINDOW', 'THUNDERBEAN ANIMATION', 'THUNDERBEAN', 'THUNDERBIRD RELEASING', 'THUNDERBIRD', 'TIBERIUS FILM', 'TIME LIFE', 'TIMELESS MEDIA GROUP', 'TIMELESS MEDIA', 'TIMELESS GROUP', 'TIMELESS', 'TLA RELEASING', 'TLA', 'TOBIS FILM', 'TOBIS', 'TOEI', 'TOHO', 'TOKYO SHOCK', 'TOKYO', 'TONPOOL MEDIEN GMBH', 'TONPOOL MEDIEN', 'TOPICS ENTERTAINMENT', 'TOPICS', 'TOUCHSTONE PICTURES', 'TOUCHSTONE', 'TRANSMISSION FILMS', 'TRANSMISSION', 'TRAVEL VIDEO STORE', 'TRIART', 'TRIGON FILM', 'TRIGON', 'TRINITY HOME ENTERTAINMENT', 'TRINITY ENTERTAINMENT', 'TRINITY HOME', 'TRINITY', 'TRIPICTURES', 'TRI-PICTURES', 'TRI PICTURES', 'TROMA', 'TURBINE MEDIEN', 'TURTLE RECORDS', 'TURTLE', 'TVA FILMS', 'TVA', 'TWILIGHT TIME', 'TWILIGHT', 'TT', 'TWIN CO., LTD.', 'TWIN CO, LTD.', 'TWIN CO., LTD', 'TWIN CO, LTD', 'TWIN CO LTD', 'TWIN LTD', 'TWIN CO.', 'TWIN CO', 'TWIN', 'UCA', 'UDR', 'UEK', 'UFA/DVD', 'UFA DVD', 'UFADVD', 'UGC PH', 'ULTIMATE3DHEAVEN', 'ULTRA', 'UMBRELLA ENTERTAINMENT', 'UMBRELLA', 'UMC', "UNCORK'D ENTERTAINMENT", 'UNCORKD ENTERTAINMENT', 'UNCORK D ENTERTAINMENT', "UNCORK'D", 'UNCORK D', 'UNCORKD', 'UNEARTHED FILMS', 'UNEARTHED', 'UNI DISC', 'UNIMUNDOS', 'UNITEL', 'UNIVERSAL MUSIC', 'UNIVERSAL SONY PICTURES HOME ENTERTAINMENT', 'UNIVERSAL SONY PICTURES ENTERTAINMENT', 'UNIVERSAL SONY PICTURES HOME', 'UNIVERSAL SONY PICTURES', 'UNIVERSAL HOME ENTERTAINMENT', 'UNIVERSAL ENTERTAINMENT', 
            'UNIVERSAL HOME', 'UNIVERSAL STUDIOS', 'UNIVERSAL', 'UNIVERSE LASER & VIDEO CO.', 'UNIVERSE LASER AND VIDEO CO.', 'UNIVERSE LASER & VIDEO CO', 'UNIVERSE LASER AND VIDEO CO', 'UNIVERSE LASER CO.', 'UNIVERSE LASER CO', 'UNIVERSE LASER', 'UNIVERSUM FILM', 'UNIVERSUM', 'UTV', 'VAP', 'VCI', 'VENDETTA FILMS', 'VENDETTA', 'VERSÁTIL HOME VIDEO', 'VERSÁTIL VIDEO', 'VERSÁTIL HOME', 'VERSÁTIL', 'VERSATIL HOME VIDEO', 'VERSATIL VIDEO', 'VERSATIL HOME', 'VERSATIL', 'VERTICAL ENTERTAINMENT', 'VERTICAL', 'VÉRTICE 360º', 'VÉRTICE 360', 'VERTICE 360o', 'VERTICE 360', 'VERTIGO BERLIN', 'VÉRTIGO FILMS', 'VÉRTIGO', 'VERTIGO FILMS', 'VERTIGO', 'VERVE PICTURES', 'VIA VISION ENTERTAINMENT', 'VIA VISION', 'VICOL ENTERTAINMENT', 'VICOL', 'VICOM', 'VICTOR ENTERTAINMENT', 'VICTOR', 'VIDEA CDE', 'VIDEO FILM EXPRESS', 'VIDEO FILM', 'VIDEO EXPRESS', 'VIDEO MUSIC, INC.', 'VIDEO MUSIC, INC', 'VIDEO MUSIC INC.', 'VIDEO MUSIC INC', 'VIDEO MUSIC', 'VIDEO SERVICE CORP.', 'VIDEO SERVICE CORP', 'VIDEO SERVICE', 'VIDEO TRAVEL', 'VIDEOMAX', 'VIDEO MAX', 'VII PILLARS ENTERTAINMENT', 'VII PILLARS', 'VILLAGE FILMS', 'VINEGAR SYNDROME', 'VINEGAR', 'VS', 'VINNY MOVIES', 'VINNY', 'VIRGIL FILMS & ENTERTAINMENT', 'VIRGIL FILMS AND ENTERTAINMENT', 'VIRGIL ENTERTAINMENT', 'VIRGIL FILMS', 'VIRGIL', 'VIRGIN RECORDS', 'VIRGIN', 'VISION FILMS', 'VISION', 'VISUAL ENTERTAINMENT GROUP', 
            'VISUAL GROUP', 'VISUAL ENTERTAINMENT', 'VISUAL', 'VIVENDI VISUAL ENTERTAINMENT', 'VIVENDI VISUAL', 'VIVENDI', 'VIZ PICTURES', 'VIZ', 'VLMEDIA', 'VL MEDIA', 'VL', 'VOLGA', 'VVS FILMS', 'VVS', 'VZ HANDELS GMBH', 'VZ HANDELS', 'WARD RECORDS', 'WARD', 'WARNER BROS.', 'WARNER BROS', 'WARNER ARCHIVE', 'WARNER ARCHIVE COLLECTION', 'WAC', 'WARNER', 'WARNER MUSIC', 'WEA', 'WEINSTEIN COMPANY', 'WEINSTEIN', 'WELL GO USA', 'WELL GO', 'WELTKINO FILMVERLEIH', 'WEST VIDEO', 'WEST', 'WHITE PEARL MOVIES', 'WHITE PEARL', 'WICKED-VISION MEDIA', 'WICKED VISION MEDIA', 'WICKEDVISION MEDIA', 'WICKED-VISION', 'WICKED VISION', 'WICKEDVISION', 'WIENERWORLD', 'WILD BUNCH', 'WILD EYE RELEASING', 'WILD EYE', 'WILD SIDE VIDEO', 'WILD SIDE', 'WME', 'WOLFE VIDEO', 'WOLFE', 'WORD ON FIRE', 'WORKS FILM GROUP', 'WORLD WRESTLING', 'WVG MEDIEN', 'WWE STUDIOS', 'WWE', 'X RATED KULT', 'X-RATED KULT', 'X RATED CULT', 'X-RATED CULT', 'X RATED', 'X-RATED', 'XCESS', 'XLRATOR', 'XT VIDEO', 'XT', 'YAMATO VIDEO', 'YAMATO', 'YASH RAJ FILMS', 'YASH RAJS', 'ZEITGEIST FILMS', 'ZEITGEIST', 'ZENITH PICTURES', 'ZENITH', 'ZIMA', 'ZYLO', 'ZYX MUSIC', 'ZYX',
            'MASTERS OF CINEMA', 'MOC'
        ]
        distributor_out = ""
        if distributor_in not in [None, "None", ""]:
            for each in distributor_list:
                if distributor_in.upper() == each:
                    distributor_out = each
        return distributor_out


    def get_video_codec(self, bdinfo):
        codecs = {
            "MPEG-2 Video" : "MPEG-2",
            "MPEG-4 AVC Video" : "AVC",
            "MPEG-H HEVC Video" : "HEVC",
            "VC-1 Video" : "VC-1"
        }
        codec = codecs.get(bdinfo['video'][0]['codec'], "")
        return codec

    def get_video_encode(self, mi, type, bdinfo):
        video_encode = ""
        codec = ""
        bit_depth = '0'
        has_encode_settings = False
        try:
            format = mi['media']['track'][1]['Format']
            format_profile = mi['media']['track'][1].get('Format_Profile', format)
            if mi['media']['track'][1].get('Encoded_Library_Settings', None):
                has_encode_settings = True
            bit_depth = mi['media']['track'][1].get('BitDepth', '0')
        except KeyError:
            format = bdinfo['video'][0]['codec']
            format_profile = bdinfo['video'][0]['profile']

        # Determine codec based on type and format
        if type in ("ENCODE", "WEBRIP"):  # ENCODE or WEBRIP
            if format == 'AVC':
                codec = 'x264'
            elif format == 'HEVC':
                codec = 'x265'
        elif type in ('WEBDL', 'HDTV'):  # WEB-DL or HDTV
            if format == 'AVC':
                codec = 'H.264'
            elif format == 'HEVC':
                codec = 'H.265'
            if type == 'HDTV' and has_encode_settings:
                codec = codec.replace('H.', 'x')
        elif format == "VP9":
            codec = "VP9"
        elif format == "VC-1":
            codec = "VC-1"
        elif format == "AV1":
            codec = "AV1"

        # Determine profile
        if format_profile == 'High 10':
            profile = "Hi10P"
        else:
            profile = ""

        # Format video_encode based on codec and profile
        if profile:
            if codec:
                if profile != codec:
                    video_encode = f"{profile} {codec}"
                else:
                    video_encode = codec
            else:
                video_encode = profile
        else:
            if codec:
                video_encode = codec
            else:
                video_encode = format

        # Handle special case for MPEG Video
        video_codec = format
        if video_codec == "MPEG Video":
            video_codec = f"MPEG-{mi['media']['track'][1].get('Format_Version')}"

        return video_encode, video_codec, has_encode_settings, bit_depth


    def get_edition(self, title, video, bdinfo, filelist, manual_edition):
        if video.lower().startswith('dc'):
            video = video.replace('dc', '', 1)
        guess = guessit(video)
        tag = guess.get('release_group', 'NOGROUP')
        repack = ""
        cut = ""
        ratio = ""
        edition = ""
        if bdinfo is not None:
            try:
                edition = guessit(bdinfo['label'])['edition']
            except:
                edition = ""
        else:
            try:
                edition = guess['edition']
            except:
                edition = ""
        if isinstance(edition, list):
            # time.sleep(2)
            edition = " ".join(edition)
        if len(filelist) == 1:
            video = os.path.basename(video)

        video = video.upper().replace('.', ' ').replace(tag, '').replace('-', '')

        cuts = {
            "director cut": "Director's Cut",
            "international cut": "International Cut",
            "extended": "Extended",
            "special edition": "Special Edition",
            "unrated": "Unrated",
            "uncut": "Uncut",
            "super duper": "Super Duper Cut"
        }
        for key, value in cuts.items():
            if key in video.lower():
                cut = value

        ratios = {"IMAX": "IMAX", "OPEN MATTE": "Open Matte", " MAR ": "MAR"}
        for key, value in ratios.items():
            if key in video.upper():
                ratio = value

        repack_suffixes = ["REPACK", "REPACK2", "REPACK3"]
        repack_mappings = {
            " REPACK ": "REPACK",
            "[REPACK]": "REPACK",
            "V2": "REPACK",
            " REPACK2 ": "REPACK2",
            "[REPACK2]": "REPACK2",
            "V3": "REPACK2",
            " REPACK3 ": "REPACK3",
            "[REPACK3]": "REPACK3",
            "V4": "REPACK3",
        }
        for key, value in repack_mappings.items():
            if key in (video or edition) or any(video.endswith(suffix) for suffix in repack_suffixes):
                repack = value
                break 

        if manual_edition is not None:
            if isinstance(manual_edition, list):
                manual_edition = " ".join(manual_edition)
            edition = str(manual_edition)
        if manual_edition is not None and ("AI" in manual_edition.upper() or "UPSCALE" in manual_edition.upper()):
            manual_edition = " AI UPSCALE"              
        if "AI" in edition.upper() or "UPSCALE" in edition.upper():
            edition = "AI UPSCALE" 
        if "AI" in video.upper() and "UPSCALE" in video.upper():
            edition = "AI UPSCALE"            
        if " PROPER " in (video or edition):
            repack = "PROPER"
        if " RERIP " in (video.upper() or edition):
            repack = "RERIP"
        if " HYBRID " in video.upper() and "HYBRID" not in title.upper():
            edition = "Hybrid " + edition
        edition = re.sub(r"(REPACK\d?)?(RERIP)?(PROPER)?", "", edition, flags=re.IGNORECASE).strip()
        bad = ['internal', 'limited', 'retail']

        if edition.lower() in bad:
            edition = ""

        def normalize(text):
            return re.sub(r'[^a-z0-9]', '', text.lower())

        if normalize(edition) == normalize(cut):
            edition = ""
        return edition, repack, cut, ratio



    """
    Create Torrent
    """
    def create_torrent(self, meta, path, output_filename, piece_size_max):
        piece_size_max = int(piece_size_max) if piece_size_max is not None else 0
        if not meta['full_dir']:
            if meta['isdir']:
                os.chdir(path)
                globs = glob.glob1(path, "*.mkv") + glob.glob1(path, "*.mp4") + glob.glob1(path, "*.ts")
                no_sample_globs = []
                for file in globs:
                    if not file.lower().endswith('sample.mkv') or "!sample" in file.lower():
                        no_sample_globs.append(os.path.abspath(f"{path}{os.sep}{file}"))
                if len(no_sample_globs) == 1:
                    path = meta['filelist'][0]
        if meta['full_dir'] or meta['is_disc'] or meta['is_music']:
            desc = Path(meta['uuid']).stem
            include = ""
            exclude = ['._*', 'description.txt', desc + '.txt']
        else:
            exclude = ["*.*", "*sample.mkv", "!sample*.*", "._*"] 
            include = ["*.mkv", "*.mp4", "*.ts", "*.avi"]
        torrent = Torrent(path,
            trackers = ["https://fake.tracker"],
            source = "", #unstamped for easy storage finding for reuse via hash
            private = True,
            exclude_globs = exclude or [],
            include_globs = include or [],
            creation_date = datetime.now(),
            comment = "Created by Uploadrr",
            created_by = "Created by Uploadrr")
        file_size = torrent.size
        if file_size < 268435456: # 256 MiB File / 256 KiB Piece Size
            piece_size = 18
            piece_size_text = "256KiB"
        elif file_size < 1073741824:  # 1 GiB File/512 KiB Piece Size
            piece_size = 19
            piece_size_text = "512KiB"
        elif file_size < 2147483648 or piece_size_max == 1:  # 2 GiB File/1 MiB Piece Size
            piece_size = 20
            piece_size_text = "1MiB"
        elif file_size < 4294967296 or piece_size_max == 2:  # 4 GiB File/2 MiB Piece Size
            piece_size = 21
            piece_size_text = "2MiB"
        elif file_size < 8589934592 or piece_size_max == 4:  # 8 GiB File/4 MiB Piece Size
            piece_size = 22
            piece_size_text = "4MiB"
        elif file_size < 17179869184 or piece_size_max == 8:  # 16 GiB File/8 MiB Piece Size
            piece_size = 23
            piece_size_text = "8MiB"
        else: # 16MiB Piece Size
            piece_size = 24
            piece_size_text = "16MiB"
        console.print(f"[bold yellow]Creating .torrent with a piece size of {piece_size_text}... (No valid --torrenthash was provided to reuse)")
        if meta.get('torrent_creation') is not None:
            torrent_creation = meta['torrent_creation']
        else:
            torrent_creation = self.config['DEFAULT'].get('torrent_creation', 'torf')
        if torrent_creation == 'torrenttools':
            args = ['torrenttools', 'create', '-a', 'https://fake.tracker', '--private', 'on', '--piece-size', str(2**piece_size), '--created-by', "Created by Uploadrr", '--no-cross-seed','-o', f"{meta['base_dir']}/tmp/{meta['uuid']}/{output_filename}.torrent"]
            if not meta['full_dir'] or meta['is_disc']:
                args.extend(['--include', r'^.*\.(mkv|mp4|ts|avi)$'])
            args.append(path)
            err = subprocess.call(args)
            if err != 0:
                args[3] = "OMITTED"
                log.error(f"[bold red]Process execution {args} returned with error code {err}.") 
        elif torrent_creation == 'mktorrent':
            args = ['mktorrent', '-a', 'https://fake.tracker', '-p', f'-l {piece_size}', '-o', f"{meta['base_dir']}/tmp/{meta['uuid']}/{output_filename}.torrent", path]
            err = subprocess.call(args)
            if err != 0:
                args[2] = "OMITTED"
                log.error(f"[bold red]Process execution {args} returned with error code {err}.")
        else:
            torrent.piece_size = 2**piece_size
            torrent.piece_size_max = 16777216
            torrent.generate(callback=self.torf_cb, interval=5)
            torrent.write(f"{meta['base_dir']}/tmp/{meta['uuid']}/{output_filename}.torrent", overwrite=True)
            torrent.verify_filesize(path)
        console.print("[bold green].torrent created", end="\r")
        return torrent

    
    def torf_cb(self, torrent, filepath, pieces_done, pieces_total):
        if not hasattr(self, 'progress'):
            self.progress = Progress(
                "[progress.description]{task.description}",
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.1f}%",
                TimeRemainingColumn(),
            )
            self.task = self.progress.add_task("Hashing...", total=pieces_total)
            self.progress.start()
        self.progress.update(self.task, completed=pieces_done)
        if pieces_done >= pieces_total:
            self.progress.stop()
            del self.progress

    def create_random_torrents(self, base_dir, uuid, num, path):
        manual_name = re.sub(r"[^0-9a-zA-Z\[\]\'\-]+", ".", os.path.basename(path))
        base_torrent = Torrent.read(f"{base_dir}/tmp/{uuid}/BASE.torrent")
        for i in range(1, int(num) + 1):
            new_torrent = base_torrent
            new_torrent.metainfo['info']['entropy'] = random.randint(1, 999999)
            Torrent.copy(new_torrent).write(f"{base_dir}/tmp/{uuid}/[RAND-{i}]{manual_name}.torrent", overwrite=True)

    def create_base_from_existing_torrent(self, torrentpath, base_dir, uuid):
        if os.path.exists(torrentpath):
            base_torrent = Torrent.read(torrentpath)
            base_torrent.creation_date = datetime.now()
            base_torrent.trackers = ['https://fake.tracker']
            base_torrent.comment = "Created by Uploadrr"
            base_torrent.created_by = "Created by Uploadrr"
            #Remove Un-whitelisted info from torrent
            for each in list(base_torrent.metainfo['info']):
                if each not in ('files', 'length', 'name', 'piece length', 'pieces', 'private', 'source'):
                    base_torrent.metainfo['info'].pop(each, None)
            for each in list(base_torrent.metainfo):
                if each not in ('announce', 'comment', 'creation date', 'created by', 'encoding', 'info'):
                    base_torrent.metainfo.pop(each, None)
            base_torrent.source = '' #unstamped for easy storage finding for reuse via hash
            base_torrent.private = True
            Torrent.copy(base_torrent).write(f"{base_dir}/tmp/{uuid}/BASE.torrent", overwrite=True)
##TODO work on hash finding should iterate through hashes using stamp list.


    """
    Upload Screenshots
    """
    def upload_screens(self, meta, screens, img_host_num, i, total_screens, custom_img_list, return_dict):
        if int(total_screens) != 0 or len(meta.get('image_list', [])) > total_screens:
            if custom_img_list == []:
                console.print('[yellow]Uploading Screens')   
        os.chdir(f"{meta['base_dir']}/tmp/{meta['uuid']}")
        img_host = self.config['DEFAULT'][f'img_host_{img_host_num}']  
        if img_host != self.img_host and meta.get('imghost', None) is None: 
            img_host = self.img_host
            i -= 1
        if img_host_num == 1 and meta.get('imghost') != img_host:
            img_host = meta.get('imghost')
            img_host_num = 0
        image_list = []
        newhost_list = []
        if custom_img_list != []:
            image_glob = custom_img_list
            existing_images = []
        else:
            image_glob = glob.glob("*.png")
            if 'POSTER.png' in image_glob:
                image_glob.remove('POSTER.png')
            existing_images = meta.get('image_list', [])
        if len(existing_images) < total_screens:
            if img_host == 'imgbox':
                nest_asyncio.apply()
                console.print("[green]Uploading Screens to Imgbox...")
                image_list = asyncio.run(self.imgbox_upload(f"{meta['base_dir']}/tmp/{meta['uuid']}", image_glob))
                if image_list == []:
                    if img_host_num == 0:
                        img_host_num = 1
                    console.print("[yellow]Imgbox failed, trying next image host")
                    image_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)        
            else:
                with Progress(
                    TextColumn("[bold green]Uploading Screens..."),
                    BarColumn(),
                    "[cyan]{task.completed}/{task.total}",
                    TimeRemainingColumn()
                ) as progress:
                    upload_task = progress.add_task(f"[green]Uploading Screens to {img_host}...", total = len(image_glob[-screens:]))
                    timeout=60
                    for image in image_glob[-screens:]:        
                        if img_host == "imgbb":
                            url = "https://api.imgbb.com/1/upload"
                            data = {
                                'key': self.config['DEFAULT']['imgbb_api'],
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            try:
                                response = requests.post(url, data = data,timeout=timeout)
                                response = response.json()
                                if response.get('success') is not True:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]imgbb failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "oeimg":
                            url = "https://onlyimage.org/api/1/upload"
                            data = {
                                'key': self.config['DEFAULT']['oeimg_api'],
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            try:
                                response = requests.post(url, data = data,timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]oeimg failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)                                
                        elif img_host == "ptscreens":
                            url = "https://ptscreens.com/api/1/upload"
                            data = {
                                'key': self.config['DEFAULT']['ptscreens_api'],
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            try:
                                response = requests.post(url, data = data,timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]PT Screens failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)        
                        elif img_host == "freeimage.host":
                            # Upstream marcó este host como "soporte eliminado" y dejó
                            # un aviso con time.sleep(15) ANTES de pasar al siguiente.
                            # Con una cascada de 10 hosts eso es un minuto por torrent
                            # tirado sólo en esperas. La API sigue viva y es Chevereto
                            # normal, así que se sube de verdad.
                            #
                            # OJO: en el plan gratuito no hay clave por cuenta — la
                            # única que existe es la publicada en su documentación, así
                            # que el límite se comparte con todo el que la copie y las
                            # subidas no son gestionables. Va al fondo de la cascada.
                            # Además recodifica: no devuelve los bytes que le mandas.
                            url = "https://freeimage.host/api/1/upload"
                            try:
                                # La clave se lee DENTRO del try: una instalación
                                # vieja sin 'freeimage_api' en su config pasaba de
                                # "aviso + salto" a un KeyError seco. Así, faltar
                                # la clave = cascada al siguiente, como cualquier
                                # otro fallo del host.
                                clave = self.config['DEFAULT']['freeimage_api']
                                if not clave or str(clave).upper().endswith('_API_KEY'):
                                    raise KeyError('freeimage_api sin rellenar')
                                data = {
                                    'key': clave,
                                    'action': 'upload',
                                    'format': 'json',
                                    'source': base64.b64encode(open(image, "rb").read()).decode('utf8'),
                                }
                                response = requests.post(url, data=data, timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                raw_url = response['image']['url']
                                img_url = response['image'].get('medium', response['image'])['url']
                                web_url = response['image']['url_viewer']
                            except Exception:
                                progress.console.print("[yellow]freeimage.host failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "pixhost":
                            url = "https://api.pixhost.to/images"
                            data = {
                                'content_type': '0',
                                'max_th_size': 350,
                            }
                            files = {
                                'img': ('file-upload[0]', open(image, 'rb')),
                            }
                            try:
                                response = requests.post(url, data=data, files=files,timeout=timeout)
                                if response.status_code != 200:
                                    progress.console.print(response)
                                response = response.json()
                                raw_url = response['th_url'].replace('https://t', 'https://img').replace('/thumbs/', '/images/')
                                img_url = response['th_url']
                                web_url = response['show_url']
                            except Exception:
                                progress.console.print("[yellow]pixhost failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "ptpimg":
                            payload = {
                                'format' : 'json',
                                'api_key' : self.config['DEFAULT']['ptpimg_api'] # API key is obtained from inspecting element on the upload page. 
                            }
                            files = [('file-upload[0]', open(image, 'rb'))] 
                            headers = { 'referer': 'https://ptpimg.me/index.php'} 
                            url = "https://ptpimg.me/upload.php"

                            # tasks.append(asyncio.ensure_future(self.upload_image(session, url, data, headers, files=None)))
                            try:
                                response = requests.post("https://ptpimg.me/upload.php", headers=headers, data=payload, files=files)
                                response = response.json()
                                ptpimg_code = response[0]['code'] 
                                ptpimg_ext = response[0]['ext'] 
                                img_url = f"https://ptpimg.me/{ptpimg_code}.{ptpimg_ext}" 
                                web_url = f"https://ptpimg.me/{ptpimg_code}.{ptpimg_ext}" 
                                raw_url = f"https://ptpimg.me/{ptpimg_code}.{ptpimg_ext}" 
                            except:
                                progress.console.print("[yellow]ptpimg failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "lensdump":
                            url = "https://lensdump.com/api/1/upload"
                            data = {
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            headers = {
                                'X-API-Key': self.config['DEFAULT']['lensdump_api'],
                            }
                            try:
                                response = requests.post(url, data=data, headers=headers, timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]lensdump failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "ptscreens":
                            url = "https://ptscreens.com/api/1/upload"
                            data = {
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            headers = {
                                'X-API-Key': self.config['DEFAULT']['ptscreens_api'],
                            }
                            try:
                                response = requests.post(url, data=data, headers=headers, timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]PT Screens failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "oeimg":
                            url = "https://onlyimage.org/api/1/upload"
                            data = {
                                'image': base64.b64encode(open(image, "rb").read()).decode('utf8')
                            }
                            headers = {
                                'X-API-Key': self.config['DEFAULT']['oeimg_api'],
                            }
                            try:
                                response = requests.post(url, data=data, headers=headers, timeout=timeout)
                                response = response.json()
                                if response.get('status_code') != 200:
                                    progress.console.print(response)
                                img_url = response['data'].get('medium', response['data']['image'])['url']
                                web_url = response['data']['url_viewer']
                                raw_url = response['data']['image']['url']
                            except Exception:
                                progress.console.print("[yellow]Only Image failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i , img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "imgchest":
                            # Crea un "post" con la imagen. El campo multipart es
                            # images[] (en plural y con corchetes): con 'image' la
                            # API devuelve la web en HTML y no un JSON.
                            url = "https://api.imgchest.com/v1/post"
                            try:
                                # Clave dentro del try por lo mismo que freeimage:
                                # sin ella, cascada al siguiente en vez de reventar.
                                clave = self.config['DEFAULT']['imgchest_api']
                                if not clave or str(clave).upper().endswith('_API_KEY'):
                                    raise KeyError('imgchest_api sin rellenar')
                                headers = {
                                    'Authorization': f"Bearer {clave}",
                                }
                                response = requests.post(
                                    url, headers=headers,
                                    files={'images[]': (os.path.basename(image), open(image, 'rb'), 'image/jpeg')},
                                    timeout=timeout)
                                response = response.json()
                                datos = response['data']
                                raw_url = datos['images'][0]['link']
                                img_url = raw_url
                                web_url = f"https://imgchest.com/p/{datos['id']}"
                            except Exception:
                                progress.console.print("[yellow]imgchest failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "thumbsnap":
                            # El campo del fichero es 'media', no 'file' ni 'image'.
                            url = "https://thumbsnap.com/api/upload"
                            try:
                                response = requests.post(
                                    url, data={'key': self.config['DEFAULT']['thumbsnap_api']},
                                    files={'media': (os.path.basename(image), open(image, 'rb'), 'image/jpeg')},
                                    timeout=timeout)
                                response = response.json()
                                if not response.get('success'):
                                    progress.console.print(response)
                                raw_url = response['data']['media']
                                img_url = response['data'].get('thumb', raw_url)
                                web_url = response['data']['url']
                            except Exception:
                                progress.console.print("[yellow]thumbsnap failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "imghippo":
                            # Sólo sube: no expone borrado, y 'url' y 'view_url'
                            # son la misma URL directa (no hay visualizador).
                            url = "https://api.imghippo.com/v1/upload"
                            try:
                                response = requests.post(
                                    url, data={'api_key': self.config['DEFAULT']['imghippo_api']},
                                    files={'file': (os.path.basename(image), open(image, 'rb'), 'image/jpeg')},
                                    timeout=timeout)
                                response = response.json()
                                if not response.get('success'):
                                    progress.console.print(response)
                                raw_url = response['data']['url']
                                img_url = raw_url
                                web_url = response['data'].get('view_url', raw_url)
                            except Exception:
                                progress.console.print("[yellow]imghippo failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "beeimg":
                            # La clave es OPCIONAL: sin ella la subida es anónima y
                            # funciona igual. El plan gratis topa en 1 MB por imagen,
                            # así que hay que subir JPG, no PNG.
                            # Si Cloudflare devuelve 403, se reintenta por IPv6.
                            datos = {}
                            clave = self.config['DEFAULT'].get('beeimg_api', '')
                            if clave and not clave.endswith('_API_KEY'):
                                datos['apikey'] = clave
                            try:
                                response = requests.post(
                                    "https://beeimg.com/api/upload/file/json/", data=datos,
                                    files={'file': (os.path.basename(image), open(image, 'rb'), 'image/jpeg')},
                                    timeout=timeout)
                                if response.status_code == 403:
                                    response = requests.post(
                                        "https://ipv6.beeimg.com/api/upload/file/json/", data=datos,
                                        files={'file': (os.path.basename(image), open(image, 'rb'), 'image/jpeg')},
                                        timeout=timeout)
                                response = response.json()['files']
                                # La documentación avisa de que los códigos numéricos
                                # pueden cambiar; el criterio es el campo 'status'.
                                if not response.get('url'):
                                    progress.console.print(response)
                                raw_url = response['url']
                                img_url = raw_url
                                web_url = response.get('view_url', raw_url)
                            except Exception:
                                progress.console.print("[yellow]beeimg failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        elif img_host == "catbox":
                            # Devuelve la URL en texto plano, no JSON. No hay página
                            # de visualizador ni miniatura: las tres urls son la misma.
                            # El 'userhash' es opcional y sólo sirve para poder borrar
                            # después; sin él la subida es anónima.
                            datos = {'reqtype': 'fileupload'}
                            uh = self.config['DEFAULT'].get('catbox_userhash', '')
                            if uh and not uh.endswith('_USERHASH'):
                                datos['userhash'] = uh
                            try:
                                response = requests.post(
                                    "https://catbox.moe/user/api.php", data=datos,
                                    files={'fileToUpload': (os.path.basename(image), open(image, 'rb'))},
                                    timeout=timeout)
                                texto = response.text.strip()
                                if not texto.startswith("https://"):
                                    progress.console.print(texto[:200])
                                    raise ValueError(texto[:200])
                                raw_url = img_url = web_url = texto
                            except Exception:
                                progress.console.print("[yellow]catbox failed, trying next image host")
                                progress.stop()
                                newhost_list, i = self.upload_screens(meta, screens - i, img_host_num + 1, i, total_screens, custom_img_list, return_dict)
                        else:
                            console.print("[bold red]Please choose a supported image host in your config")
                            exit()


                    
                        if len(newhost_list) >=1:
                            image_list.extend(newhost_list)
                        else:
                            image_dict = {}
                            image_dict['web_url'] = web_url
                            image_dict['img_url'] = img_url
                            image_dict['raw_url'] = raw_url
                            image_list.append(image_dict)
                            progress.advance(upload_task)
                            i += 1
                        time.sleep(0.5)
                        if i >= total_screens:
                            break
            return_dict['image_list'] = image_list
            return image_list, i
        else:
            return meta.get('image_list', []), total_screens

    async def imgbox_upload(self, chdir, image_glob):
        os.chdir(chdir)
        image_list = []
        # image_glob = glob.glob("*.png")
        async with pyimgbox.Gallery(thumb_width=350, square_thumbs=False) as gallery:
            async for submission in gallery.add(image_glob):
                if not submission['success']:
                    console.print(f"[red]There was an error uploading to imgbox: [yellow]{submission['error']}[/yellow][/red]")
                    return []
                else:
                    image_dict = {}
                    image_dict['web_url'] = submission['web_url']
                    image_dict['img_url'] = submission['thumbnail_url']
                    image_dict['raw_url'] = submission['image_url']
                    image_list.append(image_dict)
        return image_list

    async def get_name(self, meta):
        manual_name = meta.get('manual_name')
        type = meta.get('type', "")
        title = meta.get('title',"")
        alt_title = meta.get('aka', "")
        artist = meta.get('artist', "")
        album = meta.get('album', "")
        year = meta.get('year', "")
        resolution = meta.get('resolution', "")
        if resolution == "OTHER":
            resolution = ""
        audio = meta.get('audio', "")
        bit_depth = meta.get('bit_depth', "")
        bitrate = meta.get('bitrate', "")
        service = meta.get('service', "")
        season = meta.get('season', "")
        cut = meta.get ('cut', "")
        ratio = meta.get ('ratio', "")
        episode = meta.get('episode', "")
        part = meta.get('part', "")
        repack = meta.get('repack', "")
        three_d = meta.get('3D', "")
        tag = meta.get('tag', "")
        source = meta.get('source', "")
        uhd = meta.get('uhd', "")
        hdr = meta.get('hdr', "")
        episode_title = meta.get('episode_title', '')
        if meta.get('is_disc', "") == "BDMV": #Disk
            video_codec = meta.get('video_codec', "")
            region = meta.get('region', "")
        elif meta.get('is_disc', "") == "DVD":
            region = meta.get('region', "")
            dvd_size = meta.get('dvd_size', "")
        else:
            video_codec = meta.get('video_codec', "")
            video_encode = meta.get('video_encode', video_codec)
        edition = meta.get('edition', "")

        if meta['category'] == "TV":
            if meta['search_year'] != "":
                year = meta['year']
            else:
                year = ""
                
        if meta.get('no_season', False):
            season = ''
        if meta.get('no_year', False):
            year = ''
        if meta.get('no_aka', False):
            alt_title = ''
        if meta['debug']:
            console.log("[cyan]get_name cat/type")
            console.log(f"CATEGORY: {meta['category']}")
            console.log(f"TYPE: {meta['type']}")
            if meta.get('is_music', False):
                console.log(f"ARTIST: {meta['artist']}")
                console.log(f"ALBUM: {meta['album']}")
                console.log(f"YEAR: {meta['year']}") 
                if meta.get('source'):          
                    console.log(f"SOURCE: {meta['source']}")
            console.log("[cyan]get_name meta:")
            console.log(meta)

            
        # name_notag = ''
        # name = ''
        # clean_name = ''
        # potential_missing = []

        #YAY NAMING FUN
        if meta['category'] == "MOVIE": #MOVIE SPECIFIC
            if type == "DISC": #Disk
                if meta['is_disc'] == 'BDMV':
                    name = f"{title} {alt_title} {year} {three_d} {cut} {ratio} {edition} {repack} {resolution} {region} {uhd} {source} {hdr} {video_codec} {audio}"
                    potential_missing = ['edition', 'region', 'distributor']
                elif meta['is_disc'] == 'DVD': 
                    name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {source} {dvd_size} {audio}"
                    potential_missing = ['edition', 'distributor']
                elif meta['is_disc'] == 'HDDVD':
                    name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {source} {video_codec} {audio}"
                    potential_missing = ['edition', 'region', 'distributor']
            elif type == "REMUX" and source in ("BluRay", "HDDVD"): #BluRay/HDDVD Remux
                name = f"{title} {alt_title} {year} {three_d} {cut} {ratio} {edition} {repack} {resolution} {uhd} {source} REMUX {hdr} {video_codec} {audio}" 
                potential_missing = ['edition', 'description']
            elif type == "REMUX" and source in ("PAL DVD", "NTSC DVD", "DVD"): #DVD Remux
                name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {source} REMUX  {audio}" 
                potential_missing = ['edition', 'description']
            elif type == "ENCODE": #Encode
                name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {uhd} {source} {audio} {hdr} {video_encode}"  
                potential_missing = ['edition', 'description']
            elif type == "WEBDL": #WEB-DL
                name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {uhd} {service} WEB-DL {audio} {hdr} {video_encode}"
                potential_missing = ['edition', 'service']
            elif type == "WEBRIP": #WEBRip
                name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {uhd} {service} WEBRip {audio} {hdr} {video_encode}"
                potential_missing = ['edition', 'service']
            elif type == "HDTV": #HDTV
                name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {source} {audio} {video_encode}"
                potential_missing = []
        elif meta['category'] == "TV": #TV SPECIFIC
            if type == "DISC": #Disk
                if meta['is_disc'] == 'BDMV':
                    name = f"{title} {year} {alt_title} {season}{episode} {three_d} {cut} {ratio} {edition} {repack} {resolution} {region} {uhd} {source} {hdr} {video_codec} {audio}"
                    potential_missing = ['edition', 'region', 'distributor']
                elif meta['is_disc'] == 'DVD':
                    name = f"{title} {alt_title} {season}{episode}{three_d} {cut} {ratio} {edition} {repack} {source} {dvd_size} {audio}"
                    potential_missing = ['edition', 'distributor']
                elif meta['is_disc'] == 'HDDVD':
                    name = f"{title} {alt_title} {year} {cut} {ratio} {edition} {repack} {resolution} {source} {video_codec} {audio}"
                    potential_missing = ['edition', 'region', 'distributor']
            elif type == "REMUX" and source in ("BluRay", "HDDVD"): #BluRay Remux
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {three_d} {cut} {ratio} {edition} {repack} {resolution} {uhd} {source} REMUX {hdr} {video_codec} {audio}" #SOURCE
                potential_missing = ['edition', 'description']
            elif type == "REMUX" and source in ("PAL DVD", "NTSC DVD"): #DVD Remux
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {cut} {ratio} {edition} {repack} {source} REMUX {audio}" #SOURCE
                potential_missing = ['edition', 'description']
            elif type == "ENCODE": #Encode
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {cut} {ratio} {edition} {repack} {resolution} {uhd} {source} {audio} {hdr} {video_encode}" #SOURCE
                potential_missing = ['edition', 'description']
            elif type == "WEBDL": #WEB-DL
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {cut} {ratio} {edition} {repack} {resolution} {uhd} {service} WEB-DL {audio} {hdr} {video_encode}"
                potential_missing = ['edition', 'service']
            elif type == "WEBRIP": #WEBRip
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {cut} {ratio} {edition} {repack} {resolution} {uhd} {service} WEBRip {audio} {hdr} {video_encode}"
                potential_missing = ['edition', 'service']
            elif type == "HDTV": #HDTV
                name = f"{title} {year} {alt_title} {season}{episode} {episode_title} {part} {cut} {ratio} {edition} {repack} {resolution} {source} {audio} {video_encode}"
                potential_missing = []
        elif meta['category'] == "MUSIC": #MUSIC SPECIFIC
            source = f'{source} ' if source else ''
            no_tag = meta.get('no_tag')
            tag = '' if no_tag or not tag else tag
            manual_edition = meta.get('manual_edition')
            edition = manual_edition if manual_edition else edition
            ed = f"[{edition}]" if edition else ''
            bd = f"{bit_depth}bit" if bit_depth else ''  
            potential_missing = []

            if type in ['ALAC', 'FLAC', 'WAV']:
                name = f"{artist} - {album} ({year}) {ed} [{source}{type} {bd}]"
            else:
                name =  f"{artist} - {album} ({year}) {ed} [{source}{type} {bitrate}]"   
        try:  
            name = ' '.join(name.split())
            name_notag = name
            name = name_notag + tag
            clean_name = self.clean_filename(name)
            name = name if not manual_name else manual_name             
            
        except:
            console.print("[bold red]Unable to generate name. Please re-run and correct any of the following args if needed.")
            console.print(f"--category [yellow]{meta['category']}")
            console.print(f"--type [yellow]{meta['type']}")
            console.print(f"--source [yellow]{meta['source']}")
            name = "Error in Name Generation"
            # exit()
        return name_notag, name, clean_name, potential_missing




    async def get_season_episode(self, video, meta):
        if meta['category'] == 'TV':
            filelist = meta['filelist']
            meta['tv_pack'] = 0
            is_daily = False
            if meta['anime'] == False:
                try:
                    if meta.get('manual_date'):
                        raise ManualDateException
                    try:
                        guess_year = guessit(video)['year']
                    except Exception:
                        guess_year = ""
                    if guessit(video)["season"] == guess_year:
                        if f"s{guessit(video)['season']}" in video.lower():
                            season_int =  str(guessit(video)["season"])
                            season = "S" + season_int.zfill(2)
                        else:
                            season_int = "1"
                            season = "S01"
                    else:
                        season_int =  str(guessit(video)["season"])
                        season = "S" + season_int.zfill(2)

                except Exception:
                    try:
                        guess_date = meta.get('manual_date', guessit(video)['date']) if meta.get('manual_date') else guessit(video)['date']
                        season_int, episode_int = self.daily_to_tmdb_season_episode(meta.get('tmdb'), guess_date)
                        # season = f"S{season_int.zfill(2)}"
                        # episode = f"E{episode_int.zfill(2)}"
                        season = str(guess_date)
                        episode = ""
                        is_daily = True
                    except Exception:
                        console.print_exception()
                        season_int = "1"
                        season = "S01"
                try:
                    if not is_daily:
                        episodes = ""
                        if len(filelist) == 1:
                            episodes = guessit(video)['episode']
                            if type(episodes) == list:
                                episode = ""
                                for item in guessit(video)["episode"]:
                                    ep = (str(item).zfill(2))
                                    episode += f"E{ep}"
                                episode_int = episodes[0]
                            else:
                                episode_int = str(episodes)
                                episode = "E" + str(episodes).zfill(2)
                        else:
                            episode = ""
                            episode_int = "0"
                            meta['tv_pack'] = 1
                except Exception:
                    episode = ""
                    episode_int = "0"
                    meta['tv_pack'] = 1
            else:
                #If Anime
                parsed = anitopy.parse(Path(video).name)
                anime_title = parsed.get('anime_title', '')
                import re as _re
                parent_name = _re.sub(r'\s*-?\s*[Ss]eason\s*\d+', '', Path(video).parent.name).strip()
                grandparent_name = _re.sub(r'\s*\(\d{4}\)', '', Path(video).parent.parent.name).strip()
                if not anime_title or (anime_title.lower() not in parent_name.lower() and anime_title.lower() not in grandparent_name.lower()):
                    anime_title = parent_name if parent_name else grandparent_name
                parsed['anime_title'] = anime_title
                # romaji, mal_id, eng_title, seasonYear, anilist_episodes = self.get_romaji(guessit(parsed['anime_title'], {"excludes" : ["country", "language"]})['title'])
                romaji, mal_id, eng_title, seasonYear, anilist_episodes = self.get_romaji(parsed['anime_title'], meta.get('mal', None))
                if mal_id:
                    meta['mal_id'] = mal_id
                if meta.get('tmdb_manual', None) is None:
                    year = parsed.get('anime_year', str(seasonYear))
                    if not year or year == '0':
                        grandparent = Path(video).parent.parent.name
                        year_match = _re.search(r'\((\d{4})\)', grandparent)
                        if year_match:
                            year = year_match.group(1)
                    meta = await self.get_tmdb_id(guessit(parsed['anime_title'], {"excludes" : ["country", "language"]})['title'], year, meta, meta['category'])
                meta = await self.tmdb_other_meta(meta)
                if meta['category'] != "TV":
                    return meta

                # meta['title'] = eng_title
                # difference = SequenceMatcher(None, eng_title, romaji.lower()).ratio()
                # if difference >= 0.8:
                #     meta['aka'] = ""
                # else:
                #     meta['aka'] = f" AKA {romaji}"
                tag = parsed.get('release_group', "")
                if tag != "":
                    meta['tag'] = f"-{tag}"
                if len(filelist) == 1:
                    try:
                        episodes = parsed.get('episode_number', guessit(video).get('episode', '1'))
                        if not isinstance(episodes, list) and not str(episodes).isnumeric():
                            episodes = guessit(video)['episode']
                        if type(episodes) == list:
                            episode = ""
                            for item in episodes:
                                ep = (str(item).zfill(2))
                                episode += f"E{ep}"
                            episode_int = episodes[0]
                        else:
                            episode_int = str(int(episodes))
                            episode = f"E{str(int(episodes)).zfill(2)}"
                    except Exception:
                        episode = "E01"
                        episode_int = "1"
                        console.print('[bold yellow]There was an error guessing the episode number. Guessing E01. Use [bold green]--episode #[/bold green] to correct if needed')
                        await asyncio.sleep(1.5)
                else:
                    episode = ""
                    episode_int = "0"
                    meta['tv_pack'] = 1
                    
                try:
                    if meta.get('season_int'):
                        season = meta.get('season_int')
                    else:
                        season = parsed.get('anime_season', guessit(video)['season'])
                    season_int = season
                    season = f"S{season.zfill(2)}"
                except Exception:
                    try:
                        if int(episode_int) >= anilist_episodes:
                            params = {
                                'id' : str(meta['tvdb_id']),
                                'origin' : 'tvdb',
                                'absolute' : str(episode_int),
                                # 'destination' : 'tvdb'
                            }
                            url = "https://thexem.info/map/single"
                            response = requests.post(url, params=params).json()
                            if response['result'] == "failure":
                                raise XEMNotFound
                            if meta['debug']:
                                console.log(f"[cyan]TheXEM Absolute -> Standard[/cyan]\n{response}")
                            season_int = str(response['data']['scene']['season'])
                            season = f"S{str(response['data']['scene']['season']).zfill(2)}"
                            if len(filelist) == 1:
                                episode_int = str(response['data']['scene']['episode'])
                                episode = f"E{str(response['data']['scene']['episode']).zfill(2)}"
                        else:
                            #Get season from xem name map
                            season = "S01"
                            season_int = "1"
                            names_url = f"https://thexem.info/map/names?origin=tvdb&id={str(meta['tvdb_id'])}"
                            names_response = requests.get(names_url).json()
                            if meta['debug']:
                                console.log(f'[cyan]Matching Season Number from TheXEM\n{names_response}')
                            difference = 0
                            if names_response['result'] == "success":
                                for season_num, values in names_response['data'].items():
                                    for lang, names in values.items():
                                        if lang == "jp":
                                            for name in names:
                                                romaji_check = re.sub(r"[^0-9a-zA-Z\[\]]+", "", romaji.lower().replace(' ', ''))
                                                name_check = re.sub(r"[^0-9a-zA-Z\[\]]+", "", name.lower().replace(' ', ''))
                                                diff = SequenceMatcher(None, romaji_check, name_check).ratio()
                                                if romaji_check in name_check:
                                                    if diff >= difference:
                                                        if season_num != "all":
                                                            season_int = season_num
                                                            season = f"S{season_num.zfill(2)}"
                                                        else:
                                                            season_int = "1"
                                                            season = "S01"
                                                        difference = diff
                                        if lang == "us":
                                            for name in names:
                                                eng_check = re.sub(r"[^0-9a-zA-Z\[\]]+", "", eng_title.lower().replace(' ', ''))
                                                name_check = re.sub(r"[^0-9a-zA-Z\[\]]+", "", name.lower().replace(' ', ''))
                                                diff = SequenceMatcher(None, eng_check, name_check).ratio()
                                                if eng_check in name_check:
                                                    if diff >= difference:
                                                        if season_num != "all":
                                                            season_int = season_num
                                                            season = f"S{season_num.zfill(2)}"
                                                        else:
                                                            season_int = "1"
                                                            season = "S01"
                                                        difference = diff
                            else:
                                raise XEMNotFound
                    except Exception:
                        if meta['debug']:
                            console.print_exception()
                        try:
                            season = guessit(video)['season']
                            season_int = season
                        except Exception:
                            season_int = "1"
                            season = "S01"
                        console.print(f"[bold yellow]{meta['title']} does not exist on thexem, guessing {season}")
                        console.print(f"[bold yellow]If [green]{season}[/green] is incorrect, use --season to correct")
                        await asyncio.sleep(3)

            if meta.get('manual_season', None) is None:
                meta['season'] = season
            else:
                season_int = meta['manual_season'].lower().replace('s', '')
                meta['season'] = f"S{meta['manual_season'].lower().replace('s', '').zfill(2)}"
            if meta.get('manual_episode', None) is None:
                meta['episode'] = episode
            else:
                episode_int = meta['manual_episode'].lower().replace('e', '')
                meta['episode'] = f"E{meta['manual_episode'].lower().replace('e', '').zfill(2)}"
                meta['tv_pack'] = 0
            
            # if " COMPLETE " in Path(video).name.replace('.', ' '):
            #     meta['season'] = "COMPLETE"
            meta['season_int'] = season_int
            meta['episode_int'] = episode_int

            
            meta['episode_title_storage'] = guessit(video,{"excludes" : "part"}).get('episode_title', '')
            if meta['season'] == "S00" or meta['episode'] == "E00":
                meta['episode_title'] = meta['episode_title_storage']
            
            # Guess the part of the episode (if available)
            meta['part'] = ""
            if meta['tv_pack'] == 1:
                part = guessit(os.path.dirname(video)).get('part')
                meta['part'] = f"Part {part}" if part else ""

        return meta


    def get_service(self, video, tag, audio, guess_title):
        service = guessit(video).get('streaming_service', "")
        services = {
            '9NOW': '9NOW', '9Now': '9NOW', 'AE': 'AE', 'A&E': 'AE', 'AJAZ': 'AJAZ', 'Al Jazeera English': 'AJAZ', 
            'ALL4': 'ALL4', 'Channel 4': 'ALL4', 'AMBC': 'AMBC', 'ABC': 'AMBC', 'AMC': 'AMC', 'AMZN': 'AMZN', 'Amazon': 'AMZN',
            'Amazon Prime': 'AMZN', 'ANLB': 'ANLB', 'AnimeLab': 'ANLB', 'ANPL': 'ANPL', 'Animal Planet': 'ANPL', 
            'AOL': 'AOL', 'ARD': 'ARD', 'AS': 'AS', 'Adult Swim': 'AS', 'ATK': 'ATK', "America's Test Kitchen": 'ATK', 
            'ATVP': 'ATVP', 'AppleTV': 'ATVP', 'AUBC': 'AUBC', 'ABC Australia': 'AUBC', 'BCORE': 'BCORE', 'BKPL': 'BKPL', 
            'Blackpills': 'BKPL', 'BluTV': 'BLU', 'Binge': 'BNGE', 'BOOM': 'BOOM', 'Boomerang': 'BOOM', 'BRAV': 'BRAV', 
            'BravoTV': 'BRAV', 'CBC': 'CBC', 'CBS': 'CBS', 'CC': 'CC', 'Comedy Central': 'CC', 'CCGC': 'CCGC', 
            'Comedians in Cars Getting Coffee': 'CCGC', 'CHGD': 'CHGD', 'CHRGD': 'CHGD', 'CMAX': 'CMAX', 'Cinemax': 'CMAX', 
            'CMOR': 'CMOR', 'CMT': 'CMT', 'Country Music Television': 'CMT', 'CN': 'CN', 'Cartoon Network': 'CN', 'CNBC': 'CNBC', 
            'CNLP': 'CNLP', 'Canal+': 'CNLP', 'COOK': 'COOK', 'CORE': 'CORE', 'CR': 'CR', 'Crunchy Roll': 'CR', 'CRAVE': 'CRAV', 
            'CRIT': 'CRIT', 'Criterion' : 'CRIT', 'CRKL': 'CRKL', 'Crackle': 'CRKL', 'CSPN': 'CSPN', 'CSpan': 'CSPN', 'CTV': 'CTV', 'CUR': 'CUR', 
            'CuriosityStream': 'CUR', 'CW': 'CW', 'The CW': 'CW', 'CWS': 'CWS', 'CWSeed': 'CWS', 'DAZN': 'DAZN', 'DCU': 'DCU', 
            'DC Universe': 'DCU', 'DDY': 'DDY', 'Digiturk Diledigin Yerde': 'DDY', 'DEST': 'DEST', 'DramaFever': 'DF', 'DHF': 'DHF', 
            'Deadhouse Films': 'DHF', 'DISC': 'DISC', 'Discovery': 'DISC', 'DIY': 'DIY', 'DIY Network': 'DIY', 'DOCC': 'DOCC', 
            'Doc Club': 'DOCC', 'DPLY': 'DPLY', 'DPlay': 'DPLY', 'DRPO': 'DRPO', 'Discovery Plus': 'DSCP', 'DSKI': 'DSKI', 
            'Daisuki': 'DSKI', 'DSNP': 'DSNP', 'Disney+': 'DSNP', 'DSNY': 'DSNY', 'Disney': 'DSNY', 'DTV': 'DTV', 
            'EPIX': 'EPIX', 'ePix': 'EPIX', 'ESPN': 'ESPN', 'ESQ': 'ESQ', 'Esquire': 'ESQ', 'ETTV': 'ETTV', 'El Trece': 'ETTV', 
            'ETV': 'ETV', 'E!': 'ETV', 'FAM': 'FAM', 'Fandor': 'FANDOR', 'Facebook Watch': 'FBWatch', 'FJR': 'FJR', 
            'Family Jr': 'FJR', 'FOOD': 'FOOD', 'Food Network': 'FOOD', 'FOX': 'FOX', 'Fox': 'FOX', 'Fox Premium': 'FOXP', 
            'UFC Fight Pass': 'FP', 'FPT': 'FPT', 'FREE': 'FREE', 'Freeform': 'FREE', 'FTV': 'FTV', 'FUNI': 'FUNI', 'FUNi' : 'FUNI',
            'Foxtel': 'FXTL', 'FYI': 'FYI', 'FYI Network': 'FYI', 'GC': 'GC', 'NHL GameCenter': 'GC', 'GLBL': 'GLBL', 
            'Global': 'GLBL', 'GLOB': 'GLOB', 'GloboSat Play': 'GLOB', 'GO90': 'GO90', 'GagaOOLala': 'Gaga', 'HBO': 'HBO', 
            'HBO Go': 'HBO', 'HGTV': 'HGTV', 'HIDI': 'HIDI', 'HIST': 'HIST', 'History': 'HIST', 'HLMK': 'HLMK', 'Hallmark': 'HLMK', 
            'HMAX': 'HMAX', 'HBO Max': 'HMAX', 'HS': 'HTSR', 'HTSR' : 'HTSR', 'HSTR': 'Hotstar', 'HULU': 'HULU', 'Hulu': 'HULU', 'hoichoi': 'HoiChoi', 'ID': 'ID', 
            'Investigation Discovery': 'ID', 'IFC': 'IFC', 'iflix': 'IFX', 'National Audiovisual Institute': 'INA', 'ITV': 'ITV', 
            'KAYO': 'KAYO', 'KNOW': 'KNOW', 'Knowledge Network': 'KNOW', 'KNPY': 'KNPY', 'Kanopy' : 'KNPY', 'LIFE': 'LIFE', 'Lifetime': 'LIFE', 'LN': 'LN', 
            'MA' : 'MA', 'Movies Anywhere' : 'MA', 'MAX' : 'MAX', 'MBC': 'MBC', 'MNBC': 'MNBC', 'MSNBC': 'MNBC', 'MTOD': 'MTOD', 'Motor Trend OnDemand': 'MTOD', 'MTV': 'MTV', 'MUBI': 'MUBI', 
            'NATG': 'NATG', 'National Geographic': 'NATG', 'NBA': 'NBA', 'NBA TV': 'NBA', 'NBC': 'NBC', 'NF': 'NF', 'Netflix': 'NF', 
            'National Film Board': 'NFB', 'NFL': 'NFL', 'NFLN': 'NFLN', 'NFL Now': 'NFLN', 'NICK': 'NICK', 'Nickelodeon': 'NICK', 'NRK': 'NRK', 
            'Norsk Rikskringkasting': 'NRK', 'OnDemandKorea': 'ODK', 'Opto': 'OPTO', 'Oprah Winfrey Network': 'OWN', 'PA': 'PA', 'PBS': 'PBS', 
            'PBSK': 'PBSK', 'PBS Kids': 'PBSK', 'PCOK': 'PCOK', 'Peacock': 'PCOK', 'PLAY': 'PLAY', 'PlayerPL' : 'PL','Player-PL' : 'PL', 'player.pl' : 'PL', 'PLUZ': 'PLUZ', 'Pluzz': 'PLUZ', 'PMNP': 'PMNP', 
            'Paramount': 'PMNT', 'PMNT': 'PMNT','Paramount+': 'PMTP', 'PMTP' : 'PMTP', 'POGO': 'POGO', 'PokerGO': 'POGO', 'PSN': 'PSN', 'Playstation Network': 'PSN', 'PUHU': 'PUHU', 'QIBI': 'QIBI', 
            'RED': 'RED', 'YouTube Red': 'RED', 'RKTN': 'RKTN', 'Rakuten TV': 'RKTN', 'The Roku Channel': 'ROKU', 'RSTR': 'RSTR', 'RTE': 'RTE', 
            'RTE One': 'RTE', 'RUUTU': 'RUUTU', 'SBS': 'SBS', 'Science Channel': 'SCI', 'SESO': 'SESO', 'SeeSo': 'SESO', 'SHMI': 'SHMI', 'Shomi': 'SHMI', 'SKST' : 'SKST', 'SkyShowtime': 'SKST',
            'SHO': 'SHO', 'Showtime': 'SHO', 'SNET': 'SNET', 'Sportsnet': 'SNET', 'Sony': 'SONY', 'SPIK': 'SPIK', 'Spike': 'SPIK', 'Spike TV': 'SPKE', 
            'SPRT': 'SPRT', 'Sprout': 'SPRT', 'STAN': 'STAN', 'Stan': 'STAN', 'STARZ': 'STARZ', 'STRP': 'STRP', 'Star+' : 'STRP', 'STZ': 'STZ', 'Starz': 'STZ', 'SVT': 'SVT', 
            'Sveriges Television': 'SVT', 'SWER': 'SWER', 'SwearNet': 'SWER', 'SYFY': 'SYFY', 'Syfy': 'SYFY', 'TBS': 'TBS', 'TEN': 'TEN', 
            'TFOU': 'TFOU', 'TFou': 'TFOU', 'TIMV': 'TIMV', 'TLC': 'TLC', 'TOU': 'TOU', 'TRVL': 'TRVL', 'TUBI': 'TUBI', 'TubiTV': 'TUBI', 
            'TV3': 'TV3', 'TV3 Ireland': 'TV3', 'TV4': 'TV4', 'TV4 Sweeden': 'TV4', 'TVING': 'TVING', 'TVL': 'TVL', 'TV Land': 'TVL', 
            'TVNZ': 'TVNZ', 'UFC': 'UFC', 'UKTV': 'UKTV', 'UNIV': 'UNIV', 'Univision': 'UNIV', 'USAN': 'USAN', 'USA Network': 'USAN', 
            'VH1': 'VH1', 'VIAP': 'VIAP', 'VICE': 'VICE', 'Viceland': 'VICE', 'Viki': 'VIKI', 'VIMEO': 'VIMEO', 'VLCT': 'VLCT', 
            'Velocity': 'VLCT', 'VMEO': 'VMEO', 'Vimeo': 'VMEO', 'VRV': 'VRV', 'VUDU': 'VUDU', 'WME': 'WME', 'WatchMe': 'WME', 'WNET': 'WNET', 
            'W Network': 'WNET', 'WWEN': 'WWEN', 'WWE Network': 'WWEN', 'XBOX': 'XBOX', 'Xbox Video': 'XBOX', 'YHOO': 'YHOO', 'Yahoo': 'YHOO', 
            'YT': 'YT', 'ZDF': 'ZDF', 'iP': 'iP', 'BBC iPlayer': 'iP', 'iQIYI': 'iQIYI', 'iT': 'iT', 'iTunes': 'iT'
            }
        
        
        video_name = re.sub("[.()]", " ", video.replace(tag, '').replace(guess_title, ''))
        if "DTS-HD MA" in audio:
            video_name = video_name.replace("DTS-HD.MA.", "").replace("DTS-HD MA ", "")
        for key, value in services.items():
            if (' ' + key + ' ') in video_name and key not in guessit(video, {"excludes" : ["country", "language"]}).get('title', ''):
                service = value
            elif key == service:
                service = value
        service_longname = service
        for key, value in services.items():
            if value == service and len(key) > len(service_longname):
                service_longname = key
        if service_longname == "Amazon Prime":
            service_longname = "Amazon"
        return service, service_longname



    def stream_optimized(self, stream_opt):
        if stream_opt:
            stream = 1
        else:
            stream = 0
        return stream

    def is_anon(self, anon_in):
        anon = self.config['DEFAULT'].get("Anon", "False")
        if anon.lower() == "true":
            console.print("[bold red]Global ANON has been removed in favor of per-tracker settings. Please update your config accordingly.")
            time.sleep(10)
        if anon_in:
            anon_out = 1
        else:
            anon_out = 0
        return anon_out

    async def upload_image(self, session, url, data, headers, files):
        if headers is None and files is None:
            async with session.post(url=url, data=data) as resp:
                response = await resp.json()
                return response
        elif headers is None and files is not None:
            async with session.post(url=url, data=data, files=files) as resp:
                response = await resp.json()
                return response
        elif headers is not None and files is None:
            async with session.post(url=url, data=data, headers=headers) as resp:
                response = await resp.json()
                return response
        else:
            async with session.post(url=url, data=data, headers=headers, files=files) as resp:
                response = await resp.json()
                return response
            
    
    def clean_filename(self, name):
        invalid = r'<>:"/\|?*'
        for char in invalid:
            name = name.replace(char, '-')
        return name

    async def gen_desc(self, meta):
        desclink = meta.get('desclink', None)
        auto_desc = meta.get('auto_desc', False)
        nfo_file = meta.get('nfo_file', None)
        description_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/DESCRIPTION.txt"
        descfile_content = ""
        custom_desc_written = False

        with open(description_path, 'w', newline="", encoding='utf8') as description:
            if (desclink, meta.get('descfile', None), meta['desc']) == (None, None, None):
                desc_source = []
                if meta.get('ptp_manual') is not None:
                    desc_source.append('PTP')
                if meta.get('blu_manual') is not None:
                    desc_source.append('BLU')
                desc_source = desc_source[0] if len(desc_source) == 1 else None

                if meta.get('ptp') and str(self.config['TRACKERS'].get('PTP', {}).get('useAPI')).lower() == "true" and desc_source in ['PTP', None]:
                    ptp = PTP(config=self.config)
                    ptp_desc = await ptp.get_ptp_description(meta['ptp'], meta['is_disc'])
                    if ptp_desc.strip():
                        description.write(ptp_desc)
                        description.write("\n")
                        meta['description'] = 'PTP'
                        custom_desc_written = True

                if not custom_desc_written and meta.get('blu_desc', '').strip() and desc_source in ['BLU', None]:
                    blu_desc = meta['blu_desc'].strip()
                    if blu_desc:
                        description.write(blu_desc)
                        meta['description'] = 'BLU'
                        custom_desc_written = True

            if meta.get('desc_template'):
                from jinja2 import Template
                with open(f"{meta['base_dir']}/data/templates/{meta['desc_template']}.txt", 'r') as f:
                    desc_template = Template(f.read())
                    template_desc = desc_template.render(meta)
                    if template_desc.strip():
                        description.write(template_desc)
                        description.write("\n")
                        custom_desc_written = True

            if meta['nfo']:
                with open(nfo_file, 'r', encoding="utf-8") as nfo_file:
                    nfo_content = nfo_file.read()
                description.write("\n" + "[code]")
                description.write(nfo_content)
                description.write("[/code]" + "\n")
                meta['description'] = "CUSTOM"
                custom_desc_written = True

            if desclink:
                parsed = urllib.parse.urlparse(desclink.replace('/raw/', '/'))
                split = os.path.split(parsed.path)
                raw = parsed._replace(path=f"{split[0]}/raw/{split[1]}") if split[0] != '/' else parsed._replace(path=f"/raw{parsed.path}")
                raw = urllib.parse.urlunparse(raw)
                description.write(requests.get(raw).text)
                description.write("\n")
                meta['description'] = "CUSTOM"
                custom_desc_written = True

            if auto_desc:
                path = Path(meta.get('path', ''))
                uuid = meta.get('uuid', '')
                if uuid:
                    uuid = Path(uuid).stem
                desc_folder = self.config['AUTO']['description_folder']
                descfile = None

                if desc_folder:
                    desc_folder = Path(desc_folder)
                    if desc_folder.is_dir():
                        potential_file = desc_folder / f"{uuid}.txt"
                        if potential_file.is_file():
                            descfile = potential_file

                if descfile is None and path.exists():
                    if path.is_file():
                        descfile = path.with_suffix('.txt')
                        if not descfile.is_file():
                            descfile = path.parent / f"{uuid}.txt"
                    elif path.is_dir():
                        descfile = path / f"{uuid}.txt"
                        if not descfile.is_file():
                            descfile = path / 'description.txt'

                if descfile and descfile.is_file():
                    with descfile.open('r') as f:
                        descfile_content = f.read()
                        description.write("\n" + descfile_content + "\n")
                        meta['description'] = "CUSTOM"
                        custom_desc_written = True

                desc = meta.get('desc')
                if desc and desc != descfile_content:
                    description.write("\n" + desc + "\n")
                    meta['description'] = "CUSTOM"
                    custom_desc_written = True

            if meta.get('descfile') and os.path.isfile(meta['descfile']):
                with open(meta['descfile'], 'r') as f:
                    descfile_text = f.read()
                    description.write("\n" + descfile_text + "\n")
                meta['description'] = "CUSTOM"
                custom_desc_written = True

            if meta.get('desc'):
                description.write("\n" + meta['desc'] + "\n")
                meta['description'] = "CUSTOM"
                custom_desc_written = True

        return meta

    
    async def tag_override(self, meta):
        with open(f"{meta['base_dir']}/data/tags.json", 'r', encoding="utf-8") as f:
            tags = json.load(f)
            f.close()
        
        for tag in tags:
            value = tags.get(tag)
            if value.get('in_name', "") == tag and tag in meta['path']:
                meta['tag'] = f"-{tag}"
            if meta['tag'][1:] == tag:
                for key in value:
                    if key == 'type':
                        if meta[key] == "ENCODE":
                            meta[key] = value.get(key)
                        else:
                            pass
                    elif key == 'personalrelease':
                        meta[key] = bool(value.get(key, False))
                    elif key == 'template':
                        meta['desc_template'] = value.get(key)
                    else:
                        meta[key] = value.get(key)
        return meta

    async def package(self, meta):
        if meta['tag'] == "":
            tag = ""
        else:
            tag = f" / {meta['tag'][1:]}"
        if meta['is_disc'] == "DVD":
            res = meta['source']
        else:
            res = meta['resolution']

        with open(f"{meta['base_dir']}/tmp/{meta['uuid']}/GENERIC_INFO.txt", 'w', encoding="utf-8") as generic:
            generic.write(f"Name: {meta['name']}\n\n")
            generic.write(f"Overview: {meta['overview']}\n\n")
            generic.write(f"{res} / {meta['type']}{tag}\n\n")
            generic.write(f"Category: {meta['category']}\n")
            generic.write(f"TMDB: https://www.themoviedb.org/{meta['category'].lower()}/{meta['tmdb']}\n")
            if meta['imdb_id'] != "0":
                generic.write(f"IMDb: https://www.imdb.com/title/tt{meta['imdb_id']}\n")
            if meta['tvdb_id'] != "0":
                generic.write(f"TVDB: https://www.thetvdb.com/?id={meta['tvdb_id']}&tab=series\n")
            poster_img = f"{meta['base_dir']}/tmp/{meta['uuid']}/POSTER.png"
            if meta.get('poster', None) not in ['', None] and not os.path.exists(poster_img):
                if meta.get('rehosted_poster', None) is None:
                    r = requests.get(meta['poster'], stream=True)
                    if r.status_code == 200:
                        console.print("[bold yellow]Rehosting Poster")
                        r.raw.decode_content = True
                        with open(poster_img, 'wb') as f:
                            shutil.copyfileobj(r.raw, f)
                        poster, dummy = self.upload_screens(meta, 1, 1, 0, 1, [poster_img], {})
                        poster = poster[0]
                        generic.write(f"TMDB Poster: {poster.get('raw_url', poster.get('img_url'))}\n")
                        meta['rehosted_poster'] = poster.get('raw_url', poster.get('img_url'))
                        with open (f"{meta['base_dir']}/tmp/{meta['uuid']}/meta.json", 'w', encoding='utf-8') as metafile:
                            json.dump(meta, metafile, indent=4)
                            metafile.close()
                    else:
                        console.print("[bold yellow]Poster could not be retrieved")
            elif os.path.exists(poster_img) and meta.get('rehosted_poster') is not None:
                generic.write(f"TMDB Poster: {meta.get('rehosted_poster')}\n")
            if len(meta['image_list']) > 0:
                generic.write("\nImage Webpage:\n")
                for each in meta['image_list']:
                    generic.write(f"{each['web_url']}\n")
                generic.write("\nThumbnail Image:\n")
                for each in meta['image_list']:
                    generic.write(f"{each['img_url']}\n")
        title = re.sub(r"[^0-9a-zA-Z\[\]]+", "", meta['title'])
        archive = f"{meta['base_dir']}/tmp/{meta['uuid']}/{title}"
        torrent_files = glob.glob1(f"{meta['base_dir']}/tmp/{meta['uuid']}","*.torrent")
        if isinstance(torrent_files, list) and len(torrent_files) > 1:
            for each in torrent_files:
                if not each.startswith(('BASE', '[RAND')):
                    os.remove(os.path.abspath(f"{meta['base_dir']}/tmp/{meta['uuid']}/{each}"))
        try:
            if os.path.exists(f"{meta['base_dir']}/tmp/{meta['uuid']}/BASE.torrent"):
                base_torrent = Torrent.read(f"{meta['base_dir']}/tmp/{meta['uuid']}/BASE.torrent")
                manual_name = re.sub(r"[^0-9a-zA-Z\[\]\'\-]+", ".", os.path.basename(meta['path']))
                Torrent.copy(base_torrent).write(f"{meta['base_dir']}/tmp/{meta['uuid']}/{manual_name}.torrent", overwrite=True)
                # shutil.copy(os.path.abspath(f"{meta['base_dir']}/tmp/{meta['uuid']}/BASE.torrent"), os.path.abspath(f"{meta['base_dir']}/tmp/{meta['uuid']}/{meta['name'].replace(' ', '.')}.torrent").replace(' ', '.'))
            filebrowser = self.config['TRACKERS'].get('MANUAL', {}).get('filebrowser', None)
            shutil.make_archive(archive, 'tar', f"{meta['base_dir']}/tmp/{meta['uuid']}")
            if filebrowser is not None:
                url = '/'.join(s.strip('/') for s in (filebrowser, f"/tmp/{meta['uuid']}"))
                url = urllib.parse.quote(url, safe="https://")
            else:
                files = {
                    "files[]" : (f"{meta['title']}.tar", open(f"{archive}.tar", 'rb'))
                }
                response = requests.post("https://uguu.se/upload.php", files=files).json()
                if meta['debug']:
                    console.print(f"[cyan]{response}")
                url = response['files'][0]['url']
            return url
        except Exception:
            return False
        return 

    async def get_imdb_aka(self, imdb_id):
        if imdb_id == "0":
            return "", None
        ia = Cinemagoer()
        result = ia.get_movie(imdb_id.replace('tt', ''))
        
        original_language = result.get('language codes')
        if isinstance(original_language, list):
            if len(original_language) > 1:
                original_language = None
            elif len(original_language) == 1:
                original_language = original_language[0]
        aka = result.get('original title', result.get('localized title', "")).replace(' - IMDb', '').replace('\u00ae', '')
        if aka != "":
            aka = f" AKA {aka}"
        return aka, original_language

    async def get_dvd_size(self, discs):
        sizes = []
        dvd_sizes = []
        for each in discs:
            sizes.append(each['size'])
        grouped_sizes = [list(i) for j, i in itertools.groupby(sorted(sizes))]
        for each in grouped_sizes:
            if len(each) > 1:
                dvd_sizes.append(f"{len(each)}x{each[0]}")
            else:
                dvd_sizes.append(each[0])
        dvd_sizes.sort()
        compact = " ".join(dvd_sizes)
        return compact
    

    def get_tmdb_imdb_from_mediainfo(self, mediainfo, category, is_disc, tmdbid, imdbid):
        if not is_disc:
            if mediainfo['media']['track'][0].get('extra'):
                extra = mediainfo['media']['track'][0]['extra']
                for each in extra:
                    if each.lower().startswith('tmdb'):
                        parser = Args(config=self.config)
                        category, tmdbid = parser.parse_tmdb_id(id = extra[each], category=category)
                    if each.lower().startswith('imdb'):
                        try:
                            imdbid = str(int(extra[each].replace('tt', ''))).zfill(7)
                        except Exception:
                            pass
        return category, tmdbid, imdbid


    def daily_to_tmdb_season_episode(self, tmdbid, date):
        show = tmdb.TV(tmdbid)
        seasons = show.info().get('seasons')
        season = '1'
        episode = '1'
        date = datetime.fromisoformat(str(date))
        for each in seasons:
            air_date = datetime.fromisoformat(each['air_date'])
            if air_date <= date:
                season = str(each['season_number'])
        season_info = tmdb.TV_Seasons(tmdbid, season).info().get('episodes')
        for each in season_info:
            if str(each['air_date']) == str(date):
                episode = str(each['episode_number'])
                break
        else:
            console.print(f"[yellow]Unable to map the date ([bold yellow]{str(date)}[/bold yellow]) to a Season/Episode number")
        return season, episode




    async def get_imdb_info(self, imdbID, meta):
        imdb_info = {}
        if int(str(imdbID).replace('tt', '')) != 0:
            ia = Cinemagoer()
            info = ia.get_movie(imdbID)
            ia.update(info, ['technical'])        
            
            imdb_info['title'] = info.get('title')
            imdb_info['year'] = info.get('year')
            
            # --- BLINDAJE AKA ---
            raw_aka = info.get('original title') or info.get('localized title') or imdb_info.get('title') or ""
            if isinstance(raw_aka, (list, tuple)):
                raw_aka = raw_aka[0] if raw_aka else ""
            imdb_info['aka'] = str(raw_aka).replace(' - IMDb', '').replace('"', '')
            
            imdb_info['type'] = info.get('kind')
            imdb_info['imdbID'] = info.get('imdbID')
            
            # --- BLINDAJE RUNTIME ---
            rt = info.get('runtimes')
            imdb_info['runtime'] = rt[0] if rt and isinstance(rt, list) else '0'
            
            # --- BLINDAJE COVER ---
            cvr = info.get('full-size cover url')
            imdb_info['cover'] = str(cvr).replace(".jpg", "._V1_FMjpg_UX750_.jpg") if cvr else ''
            
            # --- BLINDAJE PLOT ---
            plt = info.get('plot')
            imdb_info['plot'] = plt[0] if plt and isinstance(plt, list) else (plt if isinstance(plt, str) else '')
            
            # --- BLINDAJE GENRES ---
            gnr = info.get('genres')
            imdb_info['genres'] = ', '.join(gnr) if gnr and isinstance(gnr, list) else ''
            
            imdb_info['soundmix'] = info.get('sound mix')
            
            # --- BLINDAJE COUNTRIES ---
            co = info.get('countries')
            imdb_info['countries_of_origin'] = co if co and isinstance(co, list) else []
            
            # --- BLINDAJE LENGUAJE ---
            lc = info.get('language codes')
            if isinstance(lc, list) and len(lc) == 1:
                imdb_info['original_language'] = lc[0]
            else:
                imdb_info['original_language'] = None        
            
            if not imdb_info['cover']:
                imdb_info['cover'] = meta.get('poster', '')
                
            # --- BLINDAJE DIRECTORES ---
            dirs = info.get('directors')
            if dirs and isinstance(dirs, list):
                imdb_info['directors'] = []
                for director in dirs:
                    if hasattr(director, 'getID'):
                        imdb_info['directors'].append(f"nm{director.getID()}")
        else:
            imdb_info = {
                'title' : meta['title'],
                'year' : meta['year'],
                'aka' : '',
                'type' : None,
                'runtime' : meta.get('runtime', '60'),
                'cover' : meta.get('poster'),
            }
            if len(meta.get('tmdb_directors', [])) >= 1:
                imdb_info['directors'] = meta['tmdb_directors']

        return imdb_info
        

    async def search_imdb(self, filename, search_year):
        imdbID = '0'
        ia = Cinemagoer()
        search = ia.search_movie(filename)
        for movie in search:
            if filename in movie.get('title', ''):
                if movie.get('year') == search_year:
                    imdbID = str(movie.movieID).replace('tt', '')
        return imdbID


    async def imdb_other_meta(self, meta):
        imdb_info = meta['imdb_info'] = await self.get_imdb_info(meta['imdb_id'], meta)
        meta['title'] = imdb_info['title']
        meta['year'] = imdb_info['year']
        meta['aka'] = imdb_info['aka']
        meta['poster'] = imdb_info['cover']
        meta['original_language'] = imdb_info['original_language']
        meta['overview'] = imdb_info['plot']

        difference = SequenceMatcher(None, meta['title'].lower(), meta['aka'][5:].lower()).ratio()
        if difference >= 0.9 or meta['aka'][5:].strip() == "" or meta['aka'][5:].strip().lower() in meta['title'].lower():
            meta['aka'] = ""
        if f"({meta['year']})" in meta['aka']:
            meta['aka'] = meta['aka'].replace(f"({meta['year']})", "").strip()
        return meta

    async def search_tvmaze(self, filename, year, imdbID, tvdbID):
        tvdbID = int(tvdbID)
        tvmazeID = 0
        lookup = False
        show = None
        if imdbID is None:
            imdbID = '0'
        if tvdbID is None:
            tvdbID = 0
        if int(tvdbID) != 0:
            params = {
                "thetvdb" : tvdbID
            }
            url = "https://api.tvmaze.com/lookup/shows"
            lookup = True
        elif int(imdbID) != 0:
            params = {
                "imdb" : f"tt{imdbID}"
            }
            url = "https://api.tvmaze.com/lookup/shows"
            lookup = True
        else:
            params = {
                "q" : filename
            }
            url = "https://api.tvmaze.com/search/shows"
        resp = requests.get(url=url, params=params)
        if resp.ok:
            resp = resp.json()
            if resp is None:
                return tvmazeID, imdbID, tvdbID
            if lookup:
                show = resp
            else:
                if year not in (None, ''):
                    for each in resp:
                        premier_date = each['show'].get('premiered', '')
                        if premier_date is not None:
                            if premier_date.startswith(str(year)):
                                show = each['show']
                elif len(resp) >= 1:
                    show = resp[0]['show']
            if show is not None:
                tvmazeID = show.get('id')
                if int(imdbID) == 0:
                    if show.get('externals', {}).get('imdb', '0') is not None:
                        imdbID = str(show.get('externals', {}).get('imdb', '0')).replace('tt', '')
                if int(tvdbID) == 0:
                    if show.get('externals', {}).get('tvdb', '0') is not None:
                        tvdbID = show.get('externals', {}).get('tvdb', '0')
        return tvmazeID, imdbID, tvdbID
    
