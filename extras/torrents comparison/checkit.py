#!/usr/bin/env python3
"""Checkit — forense de ficheros .torrent.

Todo lo que se puede averiguar de un .torrent sin tocar la red ni leer un solo
byte de los datos. Cuatro modos, elegidos por lo que se le pase:

    checkit.py fichero.torrent              -> ficha del torrent
    checkit.py a.torrent b.torrent [...]    -> diff forense entre torrents
    checkit.py /una/carpeta                 -> índice: duplicados y gemelos
    checkit.py fichero.torrent --data DIR   -> ¿siguen los datos en disco?

Sin argumentos pregunta la ruta, que es como lo lanza el menú de Extras.

La idea central: el infohash es sha1(bencode(info)), así que CUALQUIER campo
que un tracker meta dentro de 'info' lo cambia. UNIT3D inyecta 'entropy',
'source' y 'private', de modo que el mismo contenido subido dos veces jamás
colisiona por infohash y el propio tracker no ve el duplicado.

En cambio 'pieces' es la concatenación de los sha1 de cada pieza, y depende
sólo de los datos. Por eso la clave que aquí llamamos "gemela",

    (piece length, sha1(pieces))

atraviesa entropy/source/name y detecta contenido idéntico aunque el infohash
no se parezca en nada.

Ojo con un matiz: 'pieces' trocea el flujo de bytes CONCATENADO, no fichero a
fichero. Dos torrents pueden compartir clave gemela y repartir esos mismos
bytes en ficheros distintos. Por eso, cuando la clave coincide, se compara
además la lista de ficheros y se distingue "idénticos" de "mismos bytes,
reempaquetados".
"""

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import bencode
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# Campos que los trackers privados manipulan a propósito. Que cambien no
# significa que el contenido sea distinto, sólo que el torrent es "suyo".
TRACKER_FIELDS = ("entropy", "source", "private")

# Un passkey es un pegote alfanumérico largo dentro de la URL de announce.
PASSKEY_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{20,}(?![A-Za-z0-9])")


# ------------------------------------------------------------------ #
#  Lectura y primitivas                                               #
# ------------------------------------------------------------------ #

def load(path):
    with open(path, "rb") as fh:
        return bencode.decode(fh.read())


def as_bytes(value):
    """bencode.py devuelve str para los blobs binarios; sha1 necesita bytes."""
    return value.encode("latin-1") if isinstance(value, str) else value


def infohash(info):
    return hashlib.sha1(bencode.encode(info)).hexdigest()


def twin_key(info):
    """Identidad de los DATOS, ciega a entropy/source/name."""
    return (info["piece length"], hashlib.sha1(as_bytes(info["pieces"])).hexdigest())


def file_list(info):
    """[(ruta relativa, tamaño)], igual para torrents de uno o varios ficheros."""
    if "files" in info:
        return [
            ("/".join(str(part) for part in entry["path"]), entry["length"])
            for entry in info["files"]
        ]
    return [(str(info["name"]), info["length"])]


def piece_count(info):
    return len(as_bytes(info["pieces"])) // 20


def trackers(data):
    urls = []
    if data.get("announce"):
        urls.append(str(data["announce"]))
    for tier in data.get("announce-list") or []:
        for url in tier:
            url = str(url)
            if url not in urls:
                urls.append(url)
    return urls


def find_passkeys(urls):
    """Devuelve [(url, passkey)] de lo que parezca una credencial."""
    found = []
    for url in urls:
        # El host y el esquema no cuentan: sólo ruta y query.
        tail = url.split("://", 1)[-1]
        tail = tail[tail.find("/"):] if "/" in tail else ""
        for candidate in PASSKEY_RE.findall(tail):
            found.append((url, candidate))
    return found


def mask(secret):
    if len(secret) <= 12:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def torrent_files_in(root):
    paths = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".torrent"):
                paths.append(Path(dirpath) / name)
    return sorted(paths)


# ------------------------------------------------------------------ #
#  Modo 1 — ficha                                                     #
# ------------------------------------------------------------------ #

def inspect(paths, show_passkey=False):
    failed = 0
    for path in paths:
        try:
            data = load(path)
            info = data["info"]
        except Exception as exc:
            console.print(f"[red]✗ {path}: {type(exc).__name__}: {exc}[/red]")
            failed += 1
            continue

        files = file_list(info)
        total = sum(size for _name, size in files)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("InfoHash", f"[bold cyan]{infohash(info)}[/bold cyan]")
        table.add_row("Nombre", str(info.get("name", "?")))
        table.add_row("Contenido", f"{len(files)} fichero(s), {human(total)}")
        table.add_row(
            "Piezas",
            f"{piece_count(info)} x {human(info['piece length'])}",
        )
        table.add_row("Clave gemela", twin_key(info)[1])
        table.add_row("Privado", "sí" if info.get("private") else "no")
        if info.get("source"):
            table.add_row("Source", str(info["source"]))
        if "entropy" in info:
            table.add_row("Entropy", f"[yellow]presente[/yellow] (infohash único por subida)")
        for key in sorted(k for k in info if k not in
                          ("files", "length", "name", "piece length", "pieces") + TRACKER_FIELDS):
            table.add_row(key, str(info[key])[:80])
        if data.get("comment"):
            table.add_row("Comentario", str(data["comment"])[:80])
        if data.get("created by"):
            table.add_row("Creado por", str(data["created by"]))

        urls = trackers(data)
        table.add_row("Announce", "\n".join(urls) if urls else "[dim](ninguno)[/dim]")

        console.print(Panel(table, title=f"[bold]{Path(path).name}[/bold]",
                            border_style="blue"))

        leaks = find_passkeys(urls)
        if leaks:
            lines = []
            for url, secret in leaks:
                shown = secret if show_passkey else mask(secret)
                lines.append(f"{url.split('://')[0]}://…  →  [bold]{shown}[/bold]")
            console.print(Panel(
                "Este .torrent lleva una credencial incrustada en el announce.\n"
                + "\n".join(lines)
                + ("\n\n[dim]Enmascarada. Usa --show-passkey para verla entera "
                   "y buscarla en el panel de usuarios del tracker.[/dim]"
                   if not show_passkey else ""),
                title="[bold yellow]⚠ Passkey[/bold yellow]", border_style="yellow"))

        if len(files) > 1:
            listing = Table("Fichero", "Tamaño", box=None, padding=(0, 2))
            for name, size in files[:20]:
                listing.add_row(name, human(size))
            if len(files) > 20:
                listing.add_row(f"[dim]… y {len(files) - 20} más[/dim]", "")
            console.print(listing)
        console.print()
    return 1 if failed else 0


# ------------------------------------------------------------------ #
#  Modo 2 — diff forense                                              #
# ------------------------------------------------------------------ #

def diff(paths):
    loaded = []
    for path in paths:
        try:
            data = load(path)
            loaded.append((path, data, data["info"]))
        except Exception as exc:
            console.print(f"[red]✗ {path}: {type(exc).__name__}: {exc}[/red]")
    if len(loaded) < 2:
        console.print("[red]Hacen falta al menos dos torrents legibles para comparar.[/red]")
        return 1

    summary = Table("Fichero", "InfoHash", "Clave gemela", "Ficheros", "Tamaño")
    for path, _data, info in loaded:
        files = file_list(info)
        summary.add_row(
            Path(path).name[:34],
            infohash(info)[:16] + "…",
            twin_key(info)[1][:12] + "…",
            str(len(files)),
            human(sum(s for _n, s in files)),
        )
    console.print(summary)
    console.print()

    hashes = {infohash(info) for _p, _d, info in loaded}
    twins = {twin_key(info) for _p, _d, info in loaded}

    if len(hashes) == 1:
        console.print(Panel(
            "Mismo infohash: son el MISMO torrent, byte a byte en su sección 'info'.",
            title="[bold green]Idénticos[/bold green]", border_style="green"))
        return 0

    if len(twins) > 1:
        console.print(Panel(
            "Claves gemelas distintas: los DATOS son distintos. No hay reempaquetado "
            "que valga, el contenido no es el mismo.",
            title="[bold red]Contenido distinto[/bold red]", border_style="red"))
        _report_file_differences(loaded)
        return 0

    # Misma clave gemela, distinto infohash: mismos bytes, distinto envoltorio.
    layouts = {tuple(file_list(info)) for _p, _d, info in loaded}
    names = {str(info.get("name")) for _p, _d, info in loaded}

    if len(layouts) == 1:
        verdict = ("Mismos datos y mismo reparto en ficheros. Sólo cambian los metadatos "
                   "del torrent.")
        if len(names) > 1:
            verdict += ("\n\n[bold]El nombre cambia[/bold] — resubida del trabajo de otro "
                        "bajo otro título.")
        title = "[bold yellow]Mismos datos, distinto infohash[/bold yellow]"
        style = "yellow"
    else:
        verdict = ("Mismos bytes, pero repartidos en ficheros distintos: el contenido se "
                   "reempaquetó. Un cliente NO puede hacer cross-seed directo entre ellos.")
        title = "[bold yellow]Mismos bytes, reempaquetado[/bold yellow]"
        style = "yellow"

    console.print(Panel(verdict, title=title, border_style=style))

    # Qué campos de 'info' movieron el infohash. Es la respuesta exacta al "¿por qué?".
    keys = set()
    for _p, _d, info in loaded:
        keys |= set(info.keys())
    keys.discard("pieces")

    changed = Table("Campo en 'info'", *[Path(p).name[:26] for p, _d, _i in loaded])
    any_row = False
    for key in sorted(keys):
        values = [info.get(key, "—") for _p, _d, info in loaded]
        rendered = [str(v)[:38] if key != "files" else f"{len(v)} ficheros" for v in values]
        if len(set(rendered)) > 1:
            label = f"[bold]{key}[/bold]" if key in TRACKER_FIELDS else key
            changed.add_row(label, *rendered)
            any_row = True
    if any_row:
        console.print()
        console.print(changed)
        console.print("[dim]En negrita, los campos que los trackers privados cambian a "
                      "propósito para que el infohash sea suyo.[/dim]")
    if len(layouts) > 1:
        _report_file_differences(loaded)
    return 0


def _report_file_differences(loaded):
    sets = [{name for name, _size in file_list(info)} for _p, _d, info in loaded]
    common = set.intersection(*sets)
    console.print()
    console.print(f"[dim]Ficheros en común: {len(common)}[/dim]")
    for (path, _data, info), names in zip(loaded, sets):
        only = names - common
        if only:
            console.print(f"[dim]Sólo en {Path(path).name}:[/dim] {len(only)}")
            for name in sorted(only)[:5]:
                console.print(f"    {name}")
            if len(only) > 5:
                console.print(f"    [dim]… y {len(only) - 5} más[/dim]")


# ------------------------------------------------------------------ #
#  Modo 3 — índice de una carpeta                                     #
# ------------------------------------------------------------------ #

def index(root, limit=15, show_all=False):
    paths = torrent_files_in(root)
    if not paths:
        console.print(f"[yellow]No hay ningún .torrent en {root}[/yellow]")
        return 1

    by_hash = defaultdict(list)
    by_twin = defaultdict(list)
    failed = []

    with console.status(f"Leyendo {len(paths)} torrents…"):
        for path in paths:
            try:
                info = load(path)["info"]
                record = (path, infohash(info), str(info.get("name", "?")))
                by_hash[record[1]].append(record)
                by_twin[twin_key(info)].append(record)
            except Exception as exc:
                failed.append((path, f"{type(exc).__name__}: {exc}"))

    parsed = len(paths) - len(failed)
    stats = Table(show_header=False, box=None, padding=(0, 2))
    stats.add_column(style="dim")
    stats.add_column()
    stats.add_row("Torrents leídos", f"{parsed}" + (f"  ([red]{len(failed)} ilegibles[/red])" if failed else ""))
    stats.add_row("InfoHashes distintos", str(len(by_hash)))
    stats.add_row("Conjuntos de datos distintos", str(len(by_twin)))
    console.print(Panel(stats, title=f"[bold]{root}[/bold]", border_style="blue"))

    for path, why in failed[:5]:
        console.print(f"[red]✗ {Path(path).name}: {why}[/red]")

    exact = {h: rs for h, rs in by_hash.items() if len(rs) > 1}
    if exact:
        console.print(Panel(
            f"{len(exact)} infohash(es) aparecen en más de un fichero. Un cliente no "
            "admite el mismo torrent dos veces, así que esto suele significar copias "
            "sueltas de una migración. Merece un recheck de esos datos.",
            title="[bold red]Duplicados exactos[/bold red]", border_style="red"))
        for _h, records in list(exact.items())[:limit]:
            console.print(f"  [bold]{records[0][1][:16]}…[/bold]  {records[0][2][:60]}")
            for path, _ih, _name in records:
                console.print(f"      {path}")
    else:
        console.print("[green]✓ Sin duplicados exactos de infohash.[/green]")

    groups = [(key, rs) for key, rs in by_twin.items()
              if len({ih for _p, ih, _n in rs}) > 1]
    groups.sort(key=lambda item: -len(item[1]))

    if not groups:
        console.print("[green]✓ Ningún contenido repetido bajo infohashes distintos.[/green]")
        return 0

    extra = sum(len(rs) - 1 for _k, rs in groups)
    console.print()
    console.print(Panel(
        f"[bold]{len(groups)}[/bold] grupo(s) de torrents con DATOS IDÉNTICOS pero "
        f"infohash distinto — [bold]{extra}[/bold] torrent(s) de más apuntando a datos "
        "que ya tienes.\n\n"
        "Ningún tracker privado ve esto: mete campos propios dentro de 'info' "
        "(entropy, source, private) y eso garantiza que los infohashes nunca choquen.\n\n"
        "[bold]Lee el resultado según de dónde venga la carpeta:[/bold]\n"
        "  • carpeta de un cliente → normalmente es [green]cross-seed a propósito[/green]: "
        "los mismos ficheros sirviendo a varios trackers. Sano.\n"
        "  • almacén de un tracker → son [yellow]resubidas del mismo contenido[/yellow]. "
        "El enjambre se reparte entre copias y nadie las ve como duplicadas.",
        title="[bold yellow]Mismo contenido, distinto infohash[/bold yellow]",
        border_style="yellow"))

    shown = groups if show_all else groups[:limit]
    for key, records in shown:
        console.print(f"\n  [dim]piezas de {human(key[0])} · datos {key[1][:12]}…[/dim]")
        for path, ih, name in records:
            console.print(f"    [cyan]{ih[:16]}…[/cyan]  {name[:64]}")
            console.print(f"        [dim]{path}[/dim]")
    if not show_all and len(groups) > limit:
        console.print(f"\n[dim]… y {len(groups) - limit} grupos más. Usa --all para verlos todos.[/dim]")
    return 0


# ------------------------------------------------------------------ #
#  Modo 4 — ¿siguen los datos en disco?                               #
# ------------------------------------------------------------------ #

def check_data(path, data_root):
    try:
        info = load(path)["info"]
    except Exception as exc:
        console.print(f"[red]✗ {path}: {type(exc).__name__}: {exc}[/red]")
        return 1

    root = Path(data_root)
    name = str(info.get("name", ""))
    # Vale tanto apuntar a la carpeta que CONTIENE los datos como a los datos mismos.
    base = root if root.name == name else root / name
    # En un torrent de un solo fichero, file_list() ya devuelve ese nombre, y 'base'
    # apunta a él; en uno de varios, 'base' es la carpeta y hay que colgar la ruta.
    multi = "files" in info

    ok = missing = wrong = 0
    problems = []
    for rel, size in file_list(info):
        target = base / rel if multi else base
        try:
            actual = target.stat().st_size
        except FileNotFoundError:
            missing += 1
            problems.append(("falta", rel, ""))
            continue
        if actual != size:
            wrong += 1
            problems.append(("tamaño", rel, f"{human(actual)} ≠ {human(size)}"))
        else:
            ok += 1

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("Torrent", name)
    summary.add_row("Buscado en", str(base))
    summary.add_row("Correctos", f"[green]{ok}[/green]")
    summary.add_row("Ausentes", f"[red]{missing}[/red]" if missing else "0")
    summary.add_row("Tamaño distinto", f"[red]{wrong}[/red]" if wrong else "0")
    border = "green" if not (missing or wrong) else "red"
    console.print(Panel(summary, title=f"[bold]{Path(path).name}[/bold]", border_style=border))

    for kind, rel, detail in problems[:25]:
        console.print(f"  [red]{kind:8}[/red] {rel} {detail}")
    if len(problems) > 25:
        console.print(f"  [dim]… y {len(problems) - 25} más[/dim]")

    if not (missing or wrong):
        console.print("[dim]Sólo se han comparado nombres y tamaños. La verificación real "
                      "de las piezas la hace el cliente con un recheck.[/dim]")
    return 0 if not (missing or wrong) else 2


# ------------------------------------------------------------------ #
#  Entrada                                                            #
# ------------------------------------------------------------------ #

def build_parser():
    parser = argparse.ArgumentParser(
        prog="checkit",
        description="Forense de ficheros .torrent, sin red y sin leer los datos.",
    )
    parser.add_argument("paths", nargs="*",
                        help="uno o varios .torrent, o una carpeta que los contenga")
    parser.add_argument("--data", metavar="DIR",
                        help="comprueba que los datos del torrent siguen en DIR")
    parser.add_argument("--all", action="store_true",
                        help="en el índice, muestra todos los grupos y no sólo los primeros")
    parser.add_argument("--limit", type=int, default=15,
                        help="cuántos grupos mostrar en el índice (por defecto 15)")
    parser.add_argument("--show-passkey", action="store_true",
                        help="muestra los passkeys sin enmascarar")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    paths = args.paths

    if not paths:
        console.print(Panel(
            "Dame una ruta:\n\n"
            "  • un [bold].torrent[/bold] → su ficha\n"
            "  • varios [bold].torrent[/bold] separados por espacios → comparación forense\n"
            "  • una [bold]carpeta[/bold] → índice de duplicados y contenido repetido",
            title="[bold blue]Checkit[/bold blue]", border_style="blue"))
        try:
            raw = input("ruta> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        import shlex
        paths = shlex.split(raw)
        if not paths:
            return 1

    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        for path in missing:
            console.print(f"[red]✗ No existe: {path}[/red]")
        return 1

    if args.data:
        if len(paths) != 1:
            console.print("[red]--data trabaja con un solo torrent.[/red]")
            return 1
        return check_data(paths[0], args.data)

    if len(paths) == 1 and Path(paths[0]).is_dir():
        return index(paths[0], limit=args.limit, show_all=args.all)

    directories = [p for p in paths if Path(p).is_dir()]
    if directories:
        console.print("[red]Pásame una sola carpeta, o sólo ficheros .torrent.[/red]")
        return 1

    if len(paths) == 1:
        return inspect(paths, show_passkey=args.show_passkey)

    return diff(paths)


if __name__ == "__main__":
    sys.exit(main())
