#!/usr/bin/env bash
# Mass-Edition dev-sync — push local code into the RUNNING container without an
# image rebuild. Same idea as Recordrr/dev-sync.sh: the scripts are launched as
# fresh subprocesses (`python3 extras/MASS-EDITION-UNIT3D/NN_*.py`), so a plain
# `docker cp` over the baked path is enough during iteration. Real deployment
# still bakes the code via the Dockerfile COPY.
#
# NOT synced on purpose:
#   config.py       — bind-mounted from RaW_Suite_Docker/config/mass_config.py
#   ids.txt, mapeo_*.json, completados*.txt — bind-mounted state in work_data/
#
# Usage:  bash extras/MASS-EDITION-UNIT3D/dev-sync.sh [container_name]
set -euo pipefail
CONTAINER="${1:-singularity_core}"
SRC="$(cd "$(dirname "$0")" && pwd)"          # .../RaW_Suite/extras/MASS-EDITION-UNIT3D
SUITE="$(cd "$SRC/../.." && pwd)"             # .../RaW_Suite
DEST="/app/extras/MASS-EDITION-UNIT3D"

echo "Mass-Edition → $CONTAINER"

CODE=(
  01_scraper.py
  02_indexer.py
  03_mass_updater.py
  04_image_resurrector.py
  05_image_regenerator.py
  MASS-EDIT-README.md
)
for f in "${CODE[@]}"; do
  docker cp "$SRC/$f" "$CONTAINER:$DEST/$f" >/dev/null && echo "  ✓ $f"
done

# The launcher carries the menu entry that calls into these scripts.
docker cp "$SUITE/singularity.py" "$CONTAINER:/app/singularity.py" >/dev/null \
  && echo "  ✓ singularity.py"

# Stale bytecode from the baked image shadows a freshly copied module.
docker exec "$CONTAINER" find "$DEST" /app -maxdepth 2 -name '__pycache__' -type d \
  -exec rm -rf {} + >/dev/null 2>&1 || true

echo "Done. Re-run the launcher (singularity → 3) to pick up changes."
