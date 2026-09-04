import argparse
import urllib.parse
import os
import datetime
import re
import traceback

from src.console import console, set_log_level

class Args():
    """
    Class responsible for parsing command-line arguments and processing them into a usable format.
    """
    def __init__(self, config):
        self.config = config

    UUID_PATTERN = r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$'

    @staticmethod
    def validate_mbid(mbid):
        if not re.match(Args.UUID_PATTERN, mbid):
            raise argparse.ArgumentTypeError("Invalid MBID format. Please provide a valid release MBID without quote or removal of -")
        return mbid

    def parse(self, args, meta):
        oldArgs = args
        """
        Parse the command-line arguments and update the meta dictionary with the parsed values.
        """
        parser = argparse.ArgumentParser()

        # Define arguments with options and default values
        parser.add_argument('path', nargs='*', help="Path to file/directory")
        parser.add_argument('-in', '--input', dest='cli_path', nargs='*', help=argparse.SUPPRESS) # Alias for path
        parser.add_argument('-fd', '--full-directory', dest='full_dir', action='store_true', required=False, help="Uploads Folder + ALL Content Within")
        parser.add_argument('-s', '--screens', type=int, required=False, help="Number of screenshots", default=int(self.config['DEFAULT']['screens']))
        parser.add_argument('-c', '--category', nargs='*', required=False, help="Category [MOVIE, TV, FANRES, BOOK, AUDIOBOOK, GAME]", choices=['movie', 'tv', 'fanres', 'book', 'audiobook', 'game'])
        parser.add_argument('-t', '--type', nargs='*', required=False, help="Type [DISC, REMUX, ENCODE, WEBDL, WEBRIP, HDTV]", choices=['disc', 'remux', 'encode', 'webdl', 'web-dl', 'webrip', 'hdtv'])
        parser.add_argument('--source', nargs='*', required=False, help="Source [Blu-ray, BluRay, DVD, HDDVD, WEB, HDTV, UHDTV]", choices=['Blu-ray', 'BluRay', 'DVD', 'HDDVD', 'WEB', 'HDTV', 'UHDTV'], dest="manual_source")
        parser.add_argument('-format' '--format', nargs='*', required=False, help="Audio Source [WEB, CD, VINYL]")
        parser.add_argument('-res', '--resolution', nargs='*', required=False, help="Resolution [2160p, 1080p, 1080i, 720p, 576p, 576i, 480p, 480i, 8640p, 4320p, OTHER]", choices=['2160p', '1080p', '1080i', '720p', '576p', '576i', '480p', '480i', '8640p', '4320p', 'other'])
        parser.add_argument('-tmdb', '--tmdb', nargs='*', required=False, help="TMDb ID", type=str, dest='tmdb_manual')
        parser.add_argument('-imdb', '--imdb', nargs='*', required=False, help="IMDb ID", type=str)
        parser.add_argument('-mal', '--mal', nargs='*', required=False, help="MAL ID", type=str)
        # Output only, never a lookup provider: TVDB v4 is paid with a per-user
        # PIN, which is exactly why id_resolver excludes it. This flag exists so
        # a tracker that asks for a TVDB id can be given one by hand.
        parser.add_argument('-tvdb', '--tvdb', nargs='*', required=False, help="TVDB ID (passed through to the tracker; never queried)", type=str)
        parser.add_argument('-isbn', '--isbn', nargs='*', required=False, help="ISBN of the e-book edition (10 or 13 digits; converted to ISBN-13)", type=str)
        parser.add_argument('-asin', '--asin', nargs='*', required=False, help="Audible ASIN of the audiobook recording", type=str)
        parser.add_argument('-igdb', '--igdb', nargs='*', required=False, help="IGDB game ID", type=str)
        # Sin id determinante no se sube. Esto es la salida de emergencia, y
        # tiene que costar teclearla: es lo único que separa una biblioteca de
        # una carpeta de descargas subida entera por error.
        parser.add_argument('--allow-no-id', dest='allow_no_id', action='store_true', required=False,
                            help="Subir libros/audiolibros/juegos aunque ningún proveedor los reconozca")
        # Acota QUÉ se busca al barrer un árbol. Sin esto, apuntar a una
        # carpeta heterogénea encola todo lo que pilla, que es justo el
        # accidente que nadie quiere.
        parser.add_argument('--only', nargs='*', required=False, dest='only',
                            choices=['video', 'book', 'audiobook', 'game', 'all'],
                            help="Qué tipos buscar al barrer: video, book, audiobook, game, all")
        parser.add_argument('-mbid', '--mbid', nargs='?', help="MusicBrainz Release ID", type=self.validate_mbid, dest='mbid_manual')
        parser.add_argument('-discogs', '--discogs', nargs='*', required=False, help="Discogs Release ID", type=str, dest='discogs_id')
        parser.add_argument('-g', '--tag', nargs='*', required=False, help="Group Tag", type=str)
        parser.add_argument('-serv', '--service', nargs='*', required=False, help="Streaming Service", type=str)
        parser.add_argument('-dist', '--distributor', nargs='*', required=False, help="Disc Distributor e.g.(Criterion, BFI, etc.)", type=str)
        parser.add_argument('-edition', '--edition', '--repack', nargs='*', required=False, help="Edition/Repack String e.g.(Director's Cut, Uncut, Hybrid, REPACK, REPACK3)", type=str, dest='manual_edition', default="")
        parser.add_argument('-season', '--season', nargs='*', required=False, help="Season (number)", type=str)
        parser.add_argument('-episode', '--episode', nargs='*', required=False, help="Episode (number)", type=str)
        parser.add_argument('-daily', '--daily', nargs=1, required=False, help="Air date of this episode (YYYY-MM-DD)", type=datetime.date.fromisoformat, dest="manual_date")
        parser.add_argument('--no-season', dest='no_season', action='store_true', required=False, help="Remove Season from title")
        parser.add_argument('--no-year', dest='no_year', action='store_true', required=False, help="Remove Year from title")
        parser.add_argument('--no-aka', dest='no_aka', action='store_true', required=False, help="Remove AKA from title")
        parser.add_argument('--no-dub', dest='no_dub', action='store_true', required=False, help="Remove Dubbed from title")
        parser.add_argument('--no-tag', dest='no_tag', action='store_true', required=False, help="Remove Group Tag from title")
        parser.add_argument('-ns', '--no-seed', action='store_true', required=False, help="Do not add torrent to the client")
        parser.add_argument('-year', '--year', dest='manual_year', nargs='?', required=False, help="Year", type=int, default=0)
        parser.add_argument('-ptp', '--ptp', nargs='*', required=False, help="PTP torrent id/permalink", type=str)
        parser.add_argument('-blu', '--blu', nargs='*', required=False, help="BLU torrent id/link", type=str)
        parser.add_argument('-hdb', '--hdb', nargs='*', required=False, help="HDB torrent id/link", type=str)
        parser.add_argument('-d', '--desc', nargs='*', required=False, help=r'\"[b]Custom Description[/b]\"')
        parser.add_argument('-pb', '--desclink', nargs='*', required=False, help=r'\"https://pastebin.com/URL\"')
        parser.add_argument('-df', '--descfile', nargs='*', required=False, help=r'\"\path\to\description.txt\"')
        parser.add_argument('-aid', '--auto-insert-desc', dest='auto_desc', action='store_true', help='Uses (file or season folder).txt or description.txt existing in upload path')        
        parser.add_argument('-ih', '--imghost', nargs='*', required=False, help="Image Host", choices=['imgbb', 'ptpimg', 'imgbox', 'pixhost', 'lensdump', 'ptscreens', 'oeimg'])
        parser.add_argument('-siu', '--skip-imagehost-upload', dest='skip_imghost_upload', action='store_true', required=False, help="Skip Uploading to an image host")
        parser.add_argument('-th', '--torrenthash', nargs='*', required=False, help="Torrent Hash to re-use from your client's session directory")
        parser.add_argument('-nfo', '--nfo', action='store_true', required=False, help="Use .nfo in directory for description")
        parser.add_argument('-k', '--keywords', nargs='*', required=False, help="Add comma separated keywords e.g. 'keyword, keyword2, etc'")
        parser.add_argument('-reg', '--region', nargs='*', required=False, help="Region for discs")
        parser.add_argument('-a', '--anon', action='store_true', required=False, help="Upload anonymously")
        parser.add_argument('-st', '--stream', action='store_true', required=False, help="Stream Optimized Upload")
        parser.add_argument('-webdv', '--webdv', action='store_true', required=False, help="Contains a Dolby Vision layer converted using dovi_tool")
        parser.add_argument('-hc', '--hardcoded-subs', action='store_true', required=False, help="Contains hardcoded subs", dest="hardcoded-subs")
        parser.add_argument('-pr', '--personalrelease', action='store_true', required=False, help="Personal Release")
        parser.add_argument('-sdc','--skip-dupe-check', action='store_true', required=False, help="Pass if you know this is a dupe (Skips dupe check)", dest="dupe")
        parser.add_argument('-debug', '--debug', action='store_true', required=False, help="Debug Mode, will run through all the motions providing extra info, but will not upload to trackers.")
        parser.add_argument('-ddebug', '--deep-debug', action='store_true', required=False, help="True debug mode, inc. connections")
        parser.add_argument('-ffdebug', '--ffdebug', action='store_true', required=False, help="Will show info from ffmpeg while taking screenshots.")
        parser.add_argument('-m', '--manual', action='store_true', required=False, help="Manual Mode. Returns link to ddl screens/base.torrent")
        parser.add_argument('-mt', '--manual-title', dest='manual_name', nargs='*', type=str, required=False, help='Manually override NAME, Please: "Warp Full Name with Quotes"') 
        parser.add_argument('-nh', '--nohash', action='store_true', required=False, help="Don't hash .torrent")
        parser.add_argument('-rh', '--rehash', action='store_true', required=False, help="DO hash .torrent")
        parser.add_argument('-ps', '--piece-size-max', dest='piece_size_max', nargs='*', required=False, help="Maximum piece size in MiB", choices=[1, 2, 4, 8, 16], type=int)
        parser.add_argument('-dr', '--draft', action='store_true', required=False, help="Send to drafts (BHD)")
        parser.add_argument('-tc', '--torrent-creation', dest='torrent_creation', nargs='*', required=False, help="What tool should be used to create the base .torrent", choices=['torf', 'torrenttools', 'mktorrent'])
        parser.add_argument('-client', '--client', nargs='*', required=False, help="Use this torrent client instead of default")
        parser.add_argument('-qbt', '--qbit-tag', dest='qbit_tag', nargs='*', required=False, help="Add to qbit with this tag")
        parser.add_argument('-qbc', '--qbit-cat', dest='qbit_cat', nargs='*', required=False, help="Add to qbit with this category")
        parser.add_argument('-rtl', '--rtorrent-label', dest='rtorrent_label', nargs='*', required=False, help="Add to rtorrent with this label")
        parser.add_argument('-tk', '--trackers', nargs='*', required=False, help="Upload to these trackers, space separated (--trackers blu bhd)")
        parser.add_argument('-rt', '--randomized', nargs='*', required=False, help="Number of extra, torrents with random infohash", default=0)
        parser.add_argument('-aq', '--auto-queue', dest='auto_queue', help="Automatically queue files in a directory")
        parser.add_argument('-sq', '--show-queue', dest='show_queue', action='store_true', required=False, help="Show the list of queued files")
        parser.add_argument('-delay', '--delay', dest='delay', type=int, help='Delay between queued torrents in seconds')
        parser.add_argument('-random', '--random', action='store_true', required=False, help="Randomize queue order")     
        parser.add_argument('-fa', '--full-auto', dest='full_auto', nargs='?', const=True, default=False, type=str, help=argparse.SUPPRESS)
        parser.add_argument('-ua', '--unattended', action='store_true', required=False, help=argparse.SUPPRESS)
        parser.add_argument('-vs', '--vapoursynth', action='store_true', required=False, help="Use vapoursynth for screens (requires vs install)")
        parser.add_argument('-cleanup', '--cleanup', action='store_true', required=False, help="Clean up tmp directory")
        parser.add_argument('-reconfig', '--reconfig', action='store_true', required=False, help="Auto Update Config")        
        parser.add_argument('-fl', '--freeleech', nargs='*', required=False, help="Freeleech Percentage", default=0, dest="freeleech")

        args, before_args = parser.parse_known_args(oldArgs)
        args = vars(args)

        # If --input is provided, it should override any positional path arguments
        if args.get('cli_path') and args['cli_path']:
            args['path'] = args['cli_path']

        # Handle cases where path is not found initially (existing logic)
        if len(before_args) >= 1 and not os.path.exists(' '.join(args['path'])):
            for each in before_args:
                args['path'].append(each)
                if os.path.exists(' '.join(args['path'])):
                    if any(".mkv" in x for x in before_args):
                        if ".mkv" in ' '.join(args['path']):
                            break
                    else:
                        break
        
        # Reset manual TMDb and IMDb fields
        if meta.get('tmdb_manual') or meta.get('imdb'):
            meta['tmdb_manual'] = meta['imdb'] = None

        # Process each argument and update the meta dictionary
        for key in args:
            value = args.get(key)
            # Skip processing 'cli_path' directly into meta; it's already handled.
            if key == 'cli_path':
                continue
            if value not in (None, []):
                if isinstance(value, list):
                    value2 = self.list_to_string(value)
                    if key == 'type':
                        meta[key] = value2.upper().replace('-','')
                    elif key == 'tag':
                        meta[key] = f"-{value2}"
                    elif key == 'screens':
                        meta[key] = int(value2)
                    elif key == 'season':
                        meta['manual_season'] = value2
                    elif key == 'episode':
                        meta['manual_episode'] = value2
                    elif key == 'manual_date':
                        meta['manual_date'] = value2
                    elif key == 'tmdb_manual':
                        meta['category'], meta['tmdb_manual'] = self.parse_tmdb_id(value2, meta.get('category'))
                    elif key == 'ptp':
                        if value2.startswith('http'):
                            parsed = urllib.parse.urlparse(value2)
                            try:
                                meta['ptp'] = urllib.parse.parse_qs(parsed.query)['torrentid'][0]
                            except:
                                console.print('[red]Your terminal ate part of the url, please surround in quotes next time, or pass only the torrentid')
                                console.print('[red]Continuing without -ptp')
                        else:
                            meta['ptp'] = value2
                    elif key == 'blu':
                        if value2.startswith('http'):
                            parsed = urllib.parse.urlparse(value2)
                            try:
                                blupath = parsed.path
                                if blupath.endswith('/'):
                                    blupath = blupath[:-1]
                                meta['blu'] = blupath.split('/')[-1]
                            except:
                                console.print('[red]Unable to parse id from url')
                                console.print('[red]Continuing without --blu')
                        else:
                            meta['blu'] = value2
                    elif key == 'hdb':
                        if value2.startswith('http'):
                            parsed = urllib.parse.urlparse(value2)
                            try:
                                meta['hdb'] = urllib.parse.parse_qs(parsed.query)['id'][0]
                            except:
                                console.print('[red]Your terminal ate part of the url, please surround in quotes next time, or pass only the torrentid')
                                console.print('[red]Continuing without -hdb')
                        else:
                            meta['hdb'] = value2
                    else:
                        meta[key] = value2
                else:
                    meta[key] = value
            elif key in ("manual_edition", "freeleech"):
                meta[key] = value if key != "freeleech" else 100
            elif key in ("tag") and value == []:
                meta[key] = ""
            else:
                meta[key] = meta.get(key, None)
            if key in ('trackers'):
                meta[key] = value


        return meta, parser, before_args

    def list_to_string(self, list):
        """
        Converts a list of strings into a single string.
        """
        if len(list) == 1:
            return str(list[0])
        try:
            return " ".join(list)
        except:
            return "None"

    def parse_tmdb_id(self, id, category):
        """
        Parse the TMDb ID and determine the category (TV or Movie).
        """
        id = id.lower().lstrip()
        if id.startswith('tv'):
            id = id.split('/')[1]
            category = 'TV'
        elif id.startswith('movie'):
            id = id.split('/')[1]
            category = 'MOVIE'
        return category, id
