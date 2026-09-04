"""¿Este valor de configuración es una clave de verdad o el hueco donde va?

Existe porque la comprobación estaba escrita a mano en la aduana y sólo
reconocía el prefijo `YOUR_`. Las claves de libros y juegos llegaron con otro
formato de hueco —el propio nombre de la variable— y eso las hacía pasar por
configuradas:

    "google_books_api":    "GOOGLE_BOOKS_API"      <- no empieza por YOUR_
    "igdb_client_id":      "TWITCH_CLIENT_ID"      <- idem
    "igdb_client_secret":  "TWITCH_CLIENT_SECRET"  <- idem

Consecuencia medida: una instalación recién hecha nunca era preguntada por
ellas, y luego mandaba la cadena "GOOGLE_BOOKS_API" a Google como si fuera una
clave. El error que veía el usuario era un 400 del proveedor, no un "te falta
configurar esto".

La lógica vive aquí y no repetida en cada módulo para que añadir un formato de
hueco nuevo se haga en un sitio. La usan la aduana de `rawncher`, el resolver
de libros y el de juegos.
"""

# Huecos que no siguen ningún patrón y hay que reconocer de memoria.
_LITERALES = {
    "CAMBIAME",
    "CHANGEME",
    "TU_CLAVE",
    "TU_CLAVE_TMDB_AQUI",
    "TU_CLAVE_AQUI",
    "NONE",
    "NULL",
    "XXX",
}


def es_placeholder(valor, *nombres):
    """True si `valor` no es una credencial usable.

    `nombres` son los nombres por los que se conoce la clave —el de la variable
    de entorno y el del campo en config.py—, porque el hueco de las claves
    nuevas es literalmente uno de ellos. Se comparan sin distinguir mayúsculas
    y con el sufijo `_KEY` puesto y quitado, que es donde difieren entre sí
    (`TMDB_API` frente a `TMDB_API_KEY`).
    """
    v = str(valor if valor is not None else "").strip()

    if v == "":
        return True

    alto = v.upper()

    if alto in _LITERALES or alto.startswith("YOUR_"):
        return True

    variantes = set()

    for nombre in nombres:
        n = str(nombre or "").strip().upper()

        if not n:
            continue

        variantes.add(n)
        variantes.add(n + "_KEY")

        if n.endswith("_KEY"):
            variantes.add(n[:-4])

    return alto in variantes


def limpiar(valor, *nombres):
    """El valor, o cadena vacía si es un hueco.

    Para el llamante que prefiere «sin clave» a «con una clave inventada»:
    Google Books responde sin clave —con límite más bajo— pero devuelve 400 si
    se le manda una que no vale.
    """
    return "" if es_placeholder(valor, *nombres) else str(valor).strip()
