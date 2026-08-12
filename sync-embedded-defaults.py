#!/usr/bin/env python3
"""Vuelca config/*.example dentro de final-user-install.sh.

El instalador lleva las plantillas de configuración EMBEBIDAS como literales,
para poder funcionar de una sola pieza. Son copias, no referencias: si se toca
un config/*.example y no se regeneran, el instalador sigue escribiendo la
versión vieja en cada instalación nueva. Ya pasó — el `config.py` embebido
mantuvo `img_host_1: 'imgbox'` y un `mass_config.py` sin las claves ME_REGEN_*,
así que una instalación limpia reproducía el fallo y reventaba con ImportError.

Uso:  make sync-defaults        (o: python3 sync-embedded-defaults.py [--check])

--check no escribe nada y sale con código 1 si hay desfase; sirve para CI.
"""

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
INSTALADOR = RAIZ / "final-user-install.sh"

# clave embebida  →  fichero de plantilla que manda
FUENTES = {
    "singularity_config.py": "config/singularity_config.py.example",
    "config.py":             "config/config.py.example",
    "mass_config.py":        "config/mass_config.py.example",
}


def _bloque(texto):
    """Localiza el dict `files = { … }` del heredoc del instalador."""
    ini = texto.find("files = {")
    if ini < 0:
        return None, None
    fin = texto.find("\n}\n", ini)
    if fin < 0:
        return None, None
    return ini, fin + 3


def main():
    solo_comprobar = "--check" in sys.argv[1:]

    if not INSTALADOR.exists():
        print(f"✗ no encuentro {INSTALADOR.name}")
        return 1

    texto = INSTALADOR.read_text(encoding="utf-8")
    ini, fin = _bloque(texto)
    if ini is None:
        print("✗ no encuentro el bloque `files = {` en el instalador")
        return 1

    bloque = nuevo = texto[ini:fin]
    desfasadas = []

    for clave, fuente in FUENTES.items():
        ruta = RAIZ / fuente
        if not ruta.exists():
            print(f"⚠  falta {fuente}; dejo {clave} como está")
            continue
        contenido = ruta.read_text(encoding="utf-8")
        patron = re.compile(
            r"(^\s*'" + re.escape(clave) + r"':\s*)"
            r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')",
            re.M | re.S,
        )
        if not patron.search(nuevo):
            print(f"⚠  no localizo la clave {clave} en el instalador")
            continue
        antes = nuevo
        nuevo = patron.sub(lambda m: m.group(1) + repr(contenido), nuevo, count=1)
        if nuevo != antes:
            desfasadas.append(clave)

    if not desfasadas:
        print("✓ las plantillas embebidas ya están al día")
        return 0

    if solo_comprobar:
        print("✗ plantillas embebidas desfasadas: " + ", ".join(desfasadas))
        print("  ejecuta: make sync-defaults")
        return 1

    INSTALADOR.write_text(texto[:ini] + nuevo + texto[fin:], encoding="utf-8")
    print("✓ regeneradas en final-user-install.sh: " + ", ".join(desfasadas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
