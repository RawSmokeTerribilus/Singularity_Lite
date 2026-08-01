"""
Preflight checks — catch the container's most confusing failure mode early.

THE PROBLEM THIS SOLVES
-----------------------
docker-compose.yml mounts a number of individual FILES, not directories:

    ./config/mass_config.py:/app/extras/MASS-EDITION-UNIT3D/config.py
    ./work_data/mass_editor/ids.txt:/app/extras/MASS-EDITION-UNIT3D/ids.txt
    ...

When the host-side path of a bind mount does not exist, Docker does not error.
It silently creates it — as a DIRECTORY. The container then starts perfectly,
the dashboard comes up, and the failure only surfaces much later and far from
its cause:

    * `import config` raises ModuleNotFoundError / IsADirectoryError, so all
      four mass-edition stages die at import with no obvious reason.
    * open(ids.txt) raises IsADirectoryError somewhere inside 01_scraper.
    * The atomic os.replace() in 02_indexer fails writing mapeo_maestro.json.

A second, related trap: when a state file is not mounted at all, the container
reads and writes its own copy inside the image layer. Deleting the host-side
file then appears to do nothing ("it still has the old IDs cached"), and every
`docker compose down` throws the real state away.

Running the installer (`make install` / ./final-user-install.sh) before the
first `up` prevents all of this, because it touch()es every one of these paths.
This module exists for when that step is skipped anyway, which is the common
case for anyone who deploys straight from a compose file.

Fail loudly, at startup, with the exact command needed to fix it.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Paths that MUST be regular files if they exist at all. Kept in sync with the
# file-mounts in docker-compose.yml — if you add a file mount there, add it here.
EXPECTED_FILES = [
    ".env",
    "singularity_config.py",
    "RawLoadrr/data/config.py",
    "extras/MASS-EDITION-UNIT3D/config.py",
    "extras/MASS-EDITION-UNIT3D/ids.txt",
    "extras/MASS-EDITION-UNIT3D/completados.txt",
    "extras/MASS-EDITION-UNIT3D/completados_img.txt",
    "extras/MASS-EDITION-UNIT3D/mapeo_maestro.json",
    "extras/MASS-EDITION-UNIT3D/titulos_mapa.json",
    "extras/MASS-EDITION-UNIT3D/mapeo_por_titulo.json",
]

# Paths that must be directories. Listed separately so a wrong-type check on
# either side gives a precise message.
EXPECTED_DIRS = [
    "logs",
    "RawLoadrr/tmp",
    "RawLoadrr/src/trackers",
]


def check(verbose=False):
    """Return a list of human-readable problem strings. Empty list == all good."""
    problems = []

    for rel in EXPECTED_FILES:
        p = BASE_DIR / rel
        if p.is_dir():
            problems.append(
                f"{rel} is a DIRECTORY but must be a file.\n"
                f"      Docker created it because the host-side file was missing "
                f"when the container started."
            )

    for rel in EXPECTED_DIRS:
        p = BASE_DIR / rel
        if p.exists() and not p.is_dir():
            problems.append(f"{rel} is a file but must be a directory.")

    if verbose and not problems:
        print("preflight: config and work_data layout OK")

    return problems


def _format(problems):
    lines = [
        "",
        "=" * 72,
        " PREFLIGHT FAILED — the container's file layout is wrong",
        "=" * 72,
        "",
    ]
    for i, prob in enumerate(problems, 1):
        lines.append(f"  {i}. {prob}")
    lines += [
        "",
        "  HOW TO FIX (on the HOST, in the folder with docker-compose.yml):",
        "",
        "    docker compose down",
        "    find config work_data -type d \\( -name '*.py' -o -name '*.txt' \\",
        "         -o -name '*.json' -o -name '.env' \\) -exec rmdir {} +",
        "    ./final-user-install.sh      # or: make install",
        "    docker compose up -d",
        "",
        "  The installer creates every file the compose file expects to mount.",
        "  Running it BEFORE the first `up` avoids this entirely.",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


def enforce(exit_on_fail=True):
    """Check and, by default, abort the process with an actionable message."""
    problems = check()
    if not problems:
        return True

    sys.stderr.write(_format(problems))
    sys.stderr.flush()

    if exit_on_fail:
        sys.exit(1)
    return False


def warn():
    """Non-fatal variant: report but keep going.

    Used by the dashboard, which is the container's PID-1 command. Killing it
    would put the container in a restart loop and hide the very message we are
    trying to show, so there it prints and carries on.
    """
    return enforce(exit_on_fail=False)


if __name__ == "__main__":
    enforce()
