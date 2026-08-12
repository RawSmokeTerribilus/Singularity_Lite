# 🚀 UNIT3D Mass Edition Suite

Un conjunto de herramientas en Python diseñadas para la **curación y mantenimiento masivo** de tus torrents en trackers basados en **UNIT3D**.

Su misión es automatizar tareas de edición tediosas, como restaurar metadatos perdidos, inyectar banners, arreglar errores de "Info Providers" (TMDB/IMDB) y, sobre todo, **resucitar imágenes rotas** en las descripciones. Todo ello de forma automática y respetuosa con los límites del servidor.

---

## 🧬 Filosofía: El Pipeline de 4 Fases

La suite funciona como una cadena de montaje. Cada script realiza una tarea específica y prepara el terreno para el siguiente, asegurando un proceso ordenado y robusto.

1.  **`01_scraper.py` (El Cosechador):**
    *   **Misión:** Navega por tu perfil en el tracker y extrae los IDs numéricos de todos los torrents que has subido.
    *   **Resultado:** Genera un archivo `ids.txt`, que es la lista de objetivos para el resto del pipeline.

2.  **`02_indexer.py` (El Archivista):**
    *   **Misión:** Lee el `ids.txt` y, por cada ID, consulta al tracker para obtener el nombre exacto del torrent. Luego, busca en tu carpeta temporal (`TMP_ROOT`) el archivo `meta.json` que corresponde a esa subida.
    *   **Resultado:** Crea `mapeo_maestro.json`, un "mapa" que vincula cada ID de torrent con la ruta absoluta a su `meta.json` local. Este mapa es crucial para que los siguientes scripts sepan de dónde sacar la información original.

3.  **`03_mass_updater.py` (El Cirujano de Metadatos):**
    *   **Misión:** Usando el `mapeo_maestro.json`, recorre cada torrent y realiza una edición "quirúrgica". Inyecta banners, limpia firmas antiguas de la descripción y, lo más importante, fuerza la reinserción de los metadatos correctos (IMDb ID, TMDb ID, etc.) desde el `meta.json` local. Esto es ideal para arreglar torrents que se quedaron sin metadatos por un fallo del tracker.
    *   **Resultado:** Torrents en el tracker con descripciones y metadatos actualizados. Genera `completados.txt` para poder reanudar el proceso si se interrumpe.

4.  **`04_image_resurrector.py` (El Resucitador de Imágenes):**
    *   **Misión:** La fase final de embellecimiento y saneamiento. No solo arregla imágenes, **reconstruye** el post entero.
        1.  **Limpieza de Firmas:** Detecta y elimina bloques de texto obsoletos (ej. "PLEASE SEED", firmas de grupos antiguos) y banners viejos definidos en `FIRMAS_VIEJAS`.
        2.  **Purga de Hosts Muertos:** Elimina específicamente rastros de hostings caídos o problemáticos (como `pixhost.to`) mediante expresiones regulares.
        3.  **Upload Fresco:** Toma las imágenes `.jpg`/`.png` originales de tu carpeta local y las sube de nuevo a hostings fiables (ImgBB, PtScreens), generando URLs nuevas y permanentes.
        4.  **Reestructuración:** Reordena el contenido en un formato estándar: **Trailer → Galería Nueva → Banner Nuevo → Sinopsis Limpia**.
    *   **Resultado:** Una descripción visualmente perfecta, sincronizada tanto en el tracker como en tu archivo local `[MILNU]DESCRIPTION.txt`.

---

## 🚀 Flujo de Trabajo (Workflow)

Existen dos maneras de ejecutar la suite:

### A. Modo Orquestador (Recomendado vía `singularity.py`)

Esta es la forma más sencilla y segura.

1.  Lanza `singularity.py` desde la raíz del proyecto.
2.  Selecciona la opción **`[3] UNIT3D Edition (Orquestador 01-04)`**.
3.  La interfaz te guiará para configurar el **banner**, el **ID de inicio** y el **ID de fin**.
4.  Singularity se encargará de ejecutar los cuatro scripts en el orden correcto, utilizando las credenciales (`COOKIE`, `API_KEYS`) definidas en tu archivo `.env` principal. No necesitas configurar nada más.

### B. Modo Manual (Standalone)

Útil para ejecuciones aisladas o si no estás usando el lanzador principal.

1.  **Configuración:**
    *   Crea un archivo `config.py` en este mismo directorio (`extras/MASS-EDITION-UNIT3D/`).
    *   Rellena las siguientes variables:
        ```python
        # -- Credenciales del Tracker --
        BASE_URL = "https://tu-tracker.org"
        USERNAME = "TuUsuario"
        # Extrae tu cookie de sesión desde las DevTools del navegador (F12 -> Application -> Cookies)
        COOKIE_VALUE = "tu_cookie_de_sesion"

        # -- Rutas y Metadatos --
        # Ruta a la carpeta que contiene los meta.json de tus subidas
        TMP_ROOT = "/ruta/absoluta/a/tu/tmp"
        # Banner a inyectar en las descripciones
        MSG_NUEVO = "[center][img]https://i.imgur.com/banner.png[/img][/center]"

        # -- APIs para Resurrección de Imágenes --
        IMGBB_API = "tu_api_key_de_imgbb"
        PTSCREENS_API = "tu_api_key_de_ptscreens" # Opcional
        ```

2.  **Ejecución (en orden estricto):**
    *   Define el rango de IDs a procesar como variables de entorno:
        ```bash
        export ID_START=100
        export ID_END=500
        ```
    *   Ejecuta cada script uno por uno:
        ```bash
        python3 01_scraper.py
        python3 02_indexer.py
        python3 03_mass_updater.py
        python3 04_image_resurrector.py
        ```

---

## 🔥 `05_image_regenerator.py` — cuando el host de imágenes se cae

`04_image_resurrector.py` **resube** PNGs que ya estén en `tmp/`. Si `tmp` se ha
vaciado, no puede hacer nada: muere con `No hay imágenes locales`, y sin
`mapeo_maestro.json` ni siquiera sabe qué carpeta mirar.

`05` **genera las capturas de cero** con ffmpeg a partir del fichero original que
sigue sembrándose en el cliente torrent. No depende de `tmp` ni de la secuencia
01-04.

### De dónde sale el mapeo id ↔ fichero

UNIT3D escribe dentro del `.torrent` que sirve un comentario del tipo:

```
This torrent was downloaded from <SITIO>. https://<host>/torrents/<id>
```

qBittorrent lo expone en `torrents_info()` junto a `content_path`. Buscando ese
patrón en los comentarios se obtiene **id del tracker → ruta absoluta**, exacto y
sin *fuzzy matching*. Sustituye a `01_scraper` + `02_indexer` para este caso.

> El patrón se busca contra el host del **sitio**, no el del announce: pueden ser
> distintos (p.ej. announce en `tracker.ejemplo.cc` y sitio en `ejemplo.cc`).

### Qué toca de la descripción

Sólo las etiquetas `[url=…][img=…]…[/img][/url]` que apunten a un host de
`ME_DEAD_HOSTS`. Conserva el ancho original del `[img=N]`, une las nuevas con un
único espacio (los saltos de línea sueltos rompen el render del BBCode) y
reenvía el resto del formulario tal cual: mediainfo, sinopsis, tráiler, banner,
firma y metadatos quedan intactos. Si el número de imágenes muertas no cuadra y
además no son contiguas, **no toca nada** y lo registra.

### Configuración (ninguna credencial nueva)

Reutiliza lo que ya haya configurado: la cookie `ME_*` de 03/04, y de
`RawLoadrr/data/config.py` los `TORRENT_CLIENTS`, los `img_host_N` con sus API
keys y `DEFAULT.screens`.

| Variable | Por defecto | Qué hace |
|---|---|---|
| `ME_DEAD_HOSTS` | `imgbox.com,pixhost.to` | Hosts que disparan la regeneración |
| `ME_REGEN_IMG_HOST` | *(vacío)* | Destino. Vacío = primer `img_host_N` que no esté muerto |
| `ME_REGEN_SCREENS` | *(vacío)* | Nº de capturas. Vacío = `DEFAULT.screens` |
| `ME_REGEN_KEEP_PNG` | `0` | `0` borra los PNG tras subirlos (deja ~cientos de KB en vez de ~13 MB) |
| `ME_REGEN_DRY_RUN` | `0` | `1` enseña el cambio sin escribir en el tracker |
| `ME_REGEN_IMG_SIZE` | `350` | Ancho de reserva si la etiqueta original no traía uno |
| `ME_REGEN_STATE_DIR` | `.` | Dónde viven `mapeo_qbit.json` y `completados_regen.txt` |
| `ME_REGEN_ALL` | `0` | `1` procesa todos los torrents del cliente e ignora `ID_START`/`ID_END` |
| `ME_REGEN_LIMIT` | `0` | Tope por tirada (`0` = sin tope). Para ir en lotes |
| `ME_REGEN_MAX_FALLOS` | `15` | Fallos seguidos tras los que se aborta la tirada |

### Si administras otra instancia (esto le pasa a cualquiera)

El fallo no es del tracker ni tuyo: **`config.py` de RawLoadrr venía con
`img_host_1: 'imgbox'`**, así que toda galería subida con la suite acabó en el mismo
host. El día que ese host cayó, se rompieron a la vez todas las descripciones que
tuvieran imágenes suyas.

Peor aún: **imgbox no lleva credencial**. `Prep.imgbox_upload()` sube de forma anónima
con `pyimgbox`, así que no hay API key que borrar — la única manera de dejar de subir
ahí es sacarlo de la lista `img_host_*`.

Qué hacer, en este orden:

1. **Deja de sangrar.** En tu `config.py` de RawLoadrr, comenta el host caído y sube
   otro al puesto 1. Las plantillas del repo ya vienen así. De paso: si tienes
   `img_host_3: 'pixhos'`, es una errata histórica (`upload_screens` compara con
   `"pixhost"`), ese hueco nunca hizo nada.
2. **Comprueba el alcance** sin tocar nada: `ME_REGEN_DRY_RUN=1`. Te dice cuántos
   torrents tuyos están afectados y te enseña el cambio sin escribir en el tracker.
3. **Repara en lotes.** `ME_REGEN_LIMIT=25` para la primera pasada; míralo en el
   navegador; luego suelta el resto.

⚠️ **Sólo puede arreglar torrents que sigas sembrando en tu cliente.** El id del
tracker se saca del comentario del `.torrent`, que sólo está en el cliente que lo
descargó. Si borraste el fichero, ese torrent no se puede regenerar desde aquí — y
nadie más puede arreglarlo por ti, porque nadie más tiene tu copia. Por el mismo
motivo, cada uploader afectado tiene que pasar esto en su propia máquina.

### El .env se completa solo

Un `.env` que ya existe **no se regenera al reconstruir la imagen** — es un fichero tuyo,
montado dentro del contenedor. Quien viene de una versión anterior se quedaba sin las claves
nuevas: heredaba los valores por defecto del código sin enterarse y, peor, no podía cambiarlos
porque ni siquiera aparecían en el fichero.

Al arrancar, el módulo repasa tu `.env` y **añade sólo lo que falte**, con su comentario y su
valor por defecto:

```
🔧 .env completado con 9 clave(s) nueva(s): ME_REGEN_IMG_HOST, ME_REGEN_SCREENS, …
```

Nunca toca un valor que ya esté escrito (aunque esté vacío), es idempotente —la segunda vez no
hace nada— y escribe in situ, sin sustituir el fichero, porque suele ser un bind-mount de
fichero único y reemplazarlo rompería el inodo dentro del contenedor. Si el `.env` no existe,
lo crea.

### Las banderas se leen en estricto

`1/0`, `true/false`, `yes/no`, `si/no`, `on/off` — y lo que no se reconozca **avisa y se toma
como desactivado**, en vez de decidir en silencio:

```
⚠️  ME_REGEN_DRY_RUN='patata' no se entiende; lo trato como desactivado. Usa 1/0.
```

Antes la regla era "cualquier cosa que no sea `0` es verdadero", así que un `n` o un `no` se
leían como **verdadero** y encerraban la herramienta en simulacro dijeras lo que dijeras.

### Modo de ejecución: usa los flags

`--real` / `--dry-run` **mandan sobre cualquier `.env`**. El modo viajaba sólo en
`ME_REGEN_DRY_RUN`, y un valor viejo en el fichero podía imponerse sobre lo que acababas de
responder: hubo quien confirmó *"sí, edita el tracker"* y se pasó la tirada entera en simulacro
sin escribir nada. Un argumento no lo pisa ningún fichero.

```bash
python3 05_image_regenerator.py --real  --todos   # edita de verdad
python3 05_image_regenerator.py --dry-run --todos # sólo enseña el cambio
```

Si te quedas atascado en simulacro, mira la cabecera — siempre dice en cuál estás:

```
⚙️  Modo    : REAL — se editará el tracker
⚙️  Modo    : SIMULACRO (no se escribe nada)
```

Arreglo directo: lanza con `--real`, o quita `ME_REGEN_DRY_RUN` de tu `.env`.

### Uso

Desde el menú: **Singularity → 3 (UNIT3D Editor) → 3 (Regenerar imágenes desde el
origen)**. Suelto:

```bash
export ID_START=1 ID_END=6000
export ME_REGEN_DRY_RUN=1        # ensayo en seco primero
python3 05_image_regenerator.py
```

Reanudable vía `completados_regen.txt`. De propina repuebla `tmp/` con
`MediaInfo.json` + `meta.json` + `DESCRIPTION.txt`, así que `02_indexer.py` vuelve
a encontrar carpetas y `mapeo_maestro.json` deja de estar vacío.

> ⚠️ Si `img_host_1` sigue siendo el host caído, la cascada de reserva de
> `prep.py` puede volver a caer en él. `05` valida las URLs devueltas y aborta el
> torrent si acaban en un host muerto, pero conviene degradarlo en la config.

---

## 📦 Dependencias

Las dependencias se instalan con el `requirements.txt` principal de `RaW_Suite`.

*   **`requests`**: Para realizar todas las comunicaciones HTTP (GET/PATCH) con la interfaz web del tracker.
*   **`beautifulsoup4`**: Para "parsear" el HTML de las páginas del tracker, esencial en el `scraper` para encontrar los IDs de los torrents y en el `resurrector` para analizar las descripciones.

---

## ⚠️ Disclaimer

*   **Seguridad:** Esta herramienta interactúa directamente con la interfaz web del tracker enviando peticiones `PATCH` autenticadas con tu cookie de sesión. Trata tu cookie como una contraseña.
*   **Uso Responsable:** Los scripts incluyen un retraso variable (`Jitter`) por defecto para simular comportamiento humano y evitar ser bloqueado por protecciones como Cloudflare. No modifiques estos valores a la ligera.
*   **Resiliencia:** Si el proceso se interrumpe (Ctrl+C, error de red), puedes volver a ejecutar el script `03_mass_updater.py` o `04_image_resurrector.py`. Gracias al archivo de control `completados.txt`, continuarán exactamente donde lo dejaron.
