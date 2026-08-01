# 🪶 Singularity Lite

### La suite Singularity para hardware modesto.

[![Docker](https://img.shields.io/badge/Docker-build_it_yourself-blue.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![Arch](https://img.shields.io/badge/arch-amd64_·_arm64_·_armhf-brightgreen.svg)]()
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://opensource.org/licenses/AGPL-3.0)

Una Raspberry Pi 4, un NAS, un mini-PC de segunda mano, una tostadora con Docker.
Sube, ordena y edita en tu tracker UNIT3D. **No transcodifica.**

> Esto es una variante de [Singularity](https://codeberg.org/RawSmoke/Singularity),
> no un sustituto. Si tienes un escritorio en condiciones, usa la suite completa:
> hace todo esto **y además** procesa vídeo.

---

## Por qué existe

La imagen completa de Singularity está pensada para un escritorio. Compila zimg,
VapourSynth, l-smash y L-SMASH-Works desde fuente, instala MakeMKV y Google Chrome,
y mete los drivers VAAPI de Intel. En una máquina ARM eso no es que vaya lento: es
que **no construye**.

| Lo que trae la imagen completa | Qué pasa en ARM |
|---|---|
| `intel-media-va-driver-non-free` | No existe el paquete para arm64. El build muere ahí. |
| Google Chrome | Google no publica Chrome para ARM64 Linux. Y el Chromium de ARM64 no lleva Widevine, así que Recordrr no podría funcionar igualmente. |
| MakeMKV (`makemkv-bin`) | Son binarios x86_64. Instalan, pero no ejecutan. |
| zimg + VapourSynth + l-smash | Compilan, pero son horas de build y un OOM casi seguro con `make -j$(nproc)` en 2-4 GB. |

Lite quita todo eso. Lo que queda es Python puro más cuatro binarios de repo, y
construye en cualquier arquitectura sin un solo `git clone` de dependencias.

**No se publica imagen a propósito.** La construyes tú, que es justamente lo que
hace que funcione en tu arquitectura sin manifiestos multi-arch ni emulación.

---

## Qué hace y qué no

**Sí:**

- **RawLoadrr** — el uploader completo, con sus módulos de tracker.
- **UNIT3D Mass Edition** — el pipeline 01-04: scraping, indexado, edición masiva
  de descripciones y resurrección de imágenes.
- **CSI** — cruza tu cliente, tu biblioteca y el tracker para decirte qué te falta
  por subir.
- **Triaje MKV** — separa HEVC de H264. Solo lee codecs con `ffprobe`, es barato.
- **MKVerything · Auditoría de campo** — recorre la biblioteca y saca informe de
  archivos rotos, legacy y sospechosos. Con modo rápido (solo cabeceras) y modo
  profundo (decodifica). **Elige rápido**: el profundo son horas en una Pi.
- **Dashboard web** en el puerto 8002.

**No:**

- Rescatar o reparar MKVs, convertir legacy, extraer ISOs, God/Goddess Mode.
- **Recordrr** (captura por navegador). Imposible en ARM, no es una decisión.
- Chaos Maker.

Auditar es barato porque solo lee metadatos. Reconstruir no. Las listas que genera
la auditoría son portables: llévatelas a la suite completa en una máquina capaz.

---

## Instalación

### Requisitos

- Docker + Docker Compose v2 (`docker compose`, con espacio, no `docker-compose`)
- `git` y `make`
- ~1,5 GB libres para la imagen
- Cualquier arquitectura: amd64, arm64, armhf

Comprueba que los tienes:

```bash
docker --version && docker compose version && make --version | head -1
```

Si tu usuario no está en el grupo `docker`, o lo añades
(`sudo usermod -aG docker $USER`, y vuelve a entrar), o antepón `sudo` a todo.

---

### Paso 1 — Clonar y preparar

```bash
git clone https://github.com/RawSmokeTerribilus/Singularity_Lite.git
cd Singularity_Lite
make install
```

`make install` crea la estructura de `work_data/`, copia las plantillas a `config/`
y **crea los archivos que el compose va a montar**. No te lo saltes: es lo que evita
el fallo del que habla la sección "Archivos que se convierten en directorios".

Al terminar deberías tener:

```
./.env                      <- config de docker compose
./config/.env               <- config de la aplicación
./config/config.py          <- RawLoadrr (trackers, cliente torrent)
./config/singularity_config.py
./config/mass_config.py
./work_data/...             <- persistencia (logs, tmp, estado del mass editor)
```

---

### Paso 2 — Editar `./.env`  (lo lee **docker compose**)

```bash
nano .env
```

```ini
PUID=1000                  # pon aquí el resultado de:  id -u
PGID=1000                  # pon aquí el resultado de:  id -g
TZ=Europe/Madrid
MEDIA_ROOT=/mnt/media      # OBLIGATORIO — la raíz de tu biblioteca
```

**`MEDIA_ROOT` es obligatorio**: el compose se niega a arrancar sin él. Se monta
**en la misma ruta dentro y fuera** del contenedor, así que las rutas que teclees en
el TUI y las que ya conocen tu Sonarr/Radarr son las mismas. Ejemplos:
`/mnt/media`, `/volume1/data` (Synology), `/mnt/user/data` (Unraid), `/srv/storage`.

`PUID`/`PGID` deben poder **escribir** en `./config` y `./work_data`, o la
aplicación no podrá guardar su propia configuración.

---

### Paso 3 — Editar `./config/.env`  (lo lee **la aplicación**)

```bash
nano config/.env
```

Lo mínimo para empezar a subir:

```ini
TRACKER_BASE_URL=https://tu-tracker.net
TRACKER_ABBREV=NOBS
TRACKER_USERNAME=TuUsuario
TRACKER_COOKIE_NAME=nombre_de_la_cookie_de_sesion
TRACKER_COOKIE_VALUE=            # <- pégala del navegador (F12 > Application > Cookies)
CUSTOM_USER_AGENT=undici

ME_TRACKER_URL=https://tu-tracker.net
ME_TRACKER_USERNAME=TuUsuario
ME_TRACKER_COOKIE=               # <- la misma cookie
ME_CUSTOM_USER_AGENT=undici      # <- IGUAL que CUSTOM_USER_AGENT. Ver más abajo.

TMDB_API_KEY=
IMGBB_API_KEY=
```

> **¿Por qué dos `.env`?** Docker Compose solo interpola `${...}` desde el `.env` del
> directorio del proyecto. Lo que pones en `env_file:` llega al contenedor, pero
> compose no lo ve. De ahí los dos archivos. No son intercambiables.

Los otros tres (`config.py`, `singularity_config.py`, `mass_config.py`) ya están
creados desde plantilla y leen del `.env`. Normalmente **no se tocan**.

---

### Paso 4 — Construir

```bash
make build
```

Aquí no se compila nada: es `apt install` + `pip install`. Aun así, en una Raspberry
Pi cuenta con **10-25 minutos** la primera vez, sobre todo si pip tiene que construir
alguna rueda para tu arquitectura. Las siguientes veces va con caché.

Si pip falla construyendo alguna dependencia (típico en armhf o en NAS viejos),
descomenta el bloque `build-essential python3-dev` del `Dockerfile` y repite.

---

### Paso 5 — Arrancar

```bash
make up          # levanta el contenedor y el dashboard
make attach      # entra al TUI
```

Comprueba que está vivo:

```bash
docker ps                    # singularity_core debería estar "healthy"
make logs                    # logs en vivo
```

El dashboard escucha en el **puerto 8002** de la máquina (`network_mode: host`).

---

### Paso 6 — Primera prueba

1. `make attach`
2. Opción **[2] RawLoadrr** → sube algo pequeño y conocido.
3. Opción **[3] UNIT3D Ed.** → prueba el scraper: debería listar tus IDs.

Si el scraper devuelve 403 o 0 IDs, ve directo a la sección siguiente: son los dos
fallos que se llevan el 90% de los sustos.

---

### Si algo falla, manda esto

```bash
docker compose config          # ¿el compose es válido?
make check                     # ¿hay archivos convertidos en directorios?
docker ps -a | grep singularity
make logs | tail -50
docker exec singularity_core sh -c 'which ffmpeg ffprobe mediainfo mkvmerge'
```

---

## Dos cosas que te van a morder

### 1. El User-Agent

Si el scraper devuelve **403 en la primera petición**, casi seguro es esto:

```ini
CUSTOM_USER_AGENT=undici
ME_CUSTOM_USER_AGENT=undici     # <-- LOS DOS IGUALES
```

Un UA de navegador aquí choca con el WAF de un tracker que espera el UA del
cliente. No es Cloudflare, no es tu cookie, no es fingerprinting TLS. Es esta
línea. Las plantillas ya vienen bien; si lo cambias, cámbialo en los dos sitios.

Si en vez de 403 te salen **0 IDs** y "página sin torrents en el DOM", entonces sí
es la cookie: caducó y el tracker te está sirviendo la página de login, que no
tiene torrents. Sácala otra vez del navegador.

> Al pegar la cookie en el TUI no se ve nada. Es normal, es un campo enmascarado.
> `Ctrl+Shift+V` y Enter.

### 2. Archivos que se convierten en directorios

`docker-compose.yml` monta **archivos sueltos**. Si el archivo no existe en el host
cuando arranca el contenedor, **Docker crea un directorio en su lugar**. No da
error. El fallo aparece mucho después y muy lejos:

- los cuatro scripts de mass-edition mueren al importar `config`
- `ids.txt` revienta con `IsADirectoryError`
- borras la caché en el host y no cambia nada, porque el contenedor está leyendo
  su propia copia interna

Por eso **`make install` va antes del primer `make up`**. Si sospechas:

```bash
make check
```

Y dentro del contenedor, `core/preflight.py` vuelve a comprobarlo al arrancar y te
dice exactamente qué borrar.

---

## Comandos

| Comando | Qué hace |
|---|---|
| `make install` | Crea estructura, plantillas y archivos de persistencia |
| `make build` | Construye la imagen para tu arquitectura |
| `make up` / `make down` | Arranca / para |
| `make attach` | Entra al TUI |
| `make shell` | Shell dentro del contenedor |
| `make logs` | Logs en vivo |
| `make check` | Diagnostica el fallo archivo-vs-directorio |

---

## Rendimiento real

Lo que de verdad cuesta en hardware modesto:

- **Hashear el torrent.** `torf` hashea toda la carga en Python. Es la operación
  más pesada, de largo. Si el `.torrent` ya existe en tu cliente, RawLoadrr lo
  reutiliza y se salta el hasheo entero — merece la pena tener el cliente
  configurado.
- **Los screenshots.** Un proceso `ffmpeg` por captura, con seek por entrada: no
  decodifica el archivo entero, solo salta y saca un fotograma. Asumible.
  Bájalos en `config/config.py` si vas justo.
- **La auditoría en modo profundo.** Decodifica cada archivo entero. Horas.
  Usa el modo rápido salvo que estés cazando corrupción concreta.

Nada de esto transcodifica. Si necesitas transcodificar, usa un Tdarr remoto o la
suite completa en otra máquina.

---

## Licencia

AGPL-3.0-or-later, igual que Singularity.
