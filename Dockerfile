FROM python:3.11-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LD_LIBRARY_PATH=/usr/local/lib

# 1. Arsenal de construcción y dependencias de MakeMKV
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    nasm \
    git \
    # Binarios multimedia
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    tor \
    pciutils \
    # Dependencias MakeMKV
    libssl-dev \
    libexpat1-dev \
    libgl1-mesa-dev \
    qtbase5-dev \
    zlib1g-dev \
    libavcodec-dev \
    libavutil-dev \
    libavformat-dev \
    libswresample-dev \
    libc6-dev \
    # Herramientas de vida
    nano \
    htop \
    curl \
    python3-dev \
    && apt-get clean

# 2. Instalación de MakeMKV (Usando archivos locales proporcionados)
#COPY extras/makemkv-install /tmp/makemkv-install
#WORKDIR /tmp/makemkv-install
#RUN MAKEMKV_VERSION=1.18.3 && \
#    # Build OSS
#    cd makemkv-oss-${MAKEMKV_VERSION} && \
#    ./configure && \
#    make -j$(nproc) && \
#    make install && \
#    cd .. && \
#    # Install BIN
#    cd makemkv-bin-${MAKEMKV_VERSION} && \
#    mkdir -p tmp && \
#    echo "yes" | make install && \
#    cd .. && \
#    rm -rf /tmp/makemkv-install

# 3. Actualizamos herramientas de Python
RUN pip install --no-cache-dir --upgrade pip setuptools wheel cython

WORKDIR /src

# --- 4. FORJA DE ZIMG (Virtud y Excelencia) ---
RUN git clone --recursive https://github.com/sekrit-twc/zimg.git /tmp/zimg && \
    cd /tmp/zimg && \
    ./autogen.sh && \
    ./configure --prefix=/usr/local --enable-simd && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    rm -rf /tmp/zimg

# 5. FORJA DE VapourSynth (RELEASE ESTABLE R73)
RUN git clone -b R73 https://github.com/vapoursynth/vapoursynth.git && \
    cd vapoursynth && \
    ./autogen.sh && \
    ./configure && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    cd .. && rm -rf vapoursynth

# --- 6. HERRAMIENTAS DE CONSTRUCCIÓN Y MÚSCULOS ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    meson \
    ninja-build \
    pkg-config \
    libxxhash-dev \
    libavformat-dev \
    libavcodec-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libass-dev && \
    rm -rf /var/lib/apt/lists/*

# --- 7. FORJA DE L-SMASH (Librería Base - El Yunque) ---
# Necesaria para que el plugin tenga donde apoyarse
RUN git clone https://github.com/l-smash/l-smash.git /tmp/l-smash && \
    cd /tmp/l-smash && \
    ./configure --prefix=/usr --enable-shared && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    rm -rf /tmp/l-smash

# --- 12. FORJA DE L-SMASH WORKS (Fijación de Binario) ---
RUN git clone https://github.com/oatssss/L-SMASH-Works.git /tmp/lsmas-plugin && \
    cd /tmp/lsmas-plugin && \
    # Parche de flags para FFmpeg 5/6
    sed -i '1s/^/#define AV_FRAME_FLAG_INTERLACED (1 << 0)\n#define AV_FRAME_FLAG_TOP_FIELD_FIRST (1 << 1)\n/' common/video_output.h && \
    cd VapourSynth && \
    sed -i '1s/^/#include <strings.h>\n/' video_output.c && \
    ./configure --prefix=/usr/local --extra-cflags="-I/usr/local/include" --extra-ldflags="-L/usr/local/lib" && \
    make -j$(nproc) && \
    mkdir -p /usr/local/lib/vapoursynth && \
    # Usamos comodín para pillar 'libvslsmashsource.so.942' y renombrarlo correctamente
    cp libvslsmashsource.so* /usr/local/lib/vapoursynth/vslsmashsource.so && \
    ldconfig && \
    rm -rf /tmp/lsmas-plugin

# 1. Habilitamos contrib, non-free y non-free-firmware (Formato nuevo y viejo)
#RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
#        sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources; \
#    else \
#        sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
#    fi && \
#    apt-get update && apt-get install -y \
#    mesa-va-drivers \
#    intel-media-va-driver-non-free \
#    libva-drm2 \
#    vainfo \
#    && rm -rf /var/lib/apt/lists/*

# Variable de entorno de seguridad (luego el Agente la puede pisar)
ENV MOZ_X11_EGL=1

# --- 9. ENTORNO DE TRABAJO ---
WORKDIR /app

# 9. Instalación de librerías de la Suite
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Ajustamos la propiedad de la carpeta de la app para nuestro usuario 1000
RUN chown -R 1000:1000 /app

# 10. Despliegue
COPY --chown=1000:1000 . .
RUN mkdir -p logs tmp core/templates && chown -R 1000:1000 /app

# --- 11. AESTHETICS + HARDENING (v3.0.0) ---
# tini = proper PID-1, reaps zombies and forwards signals cleanly.
# useradd = makes the runtime UID 1000 a real account so tools like git,
# less, and the shell prompt stop emitting `cannot find name for user ID
# 1000`. The compose still pins `user: "1000:1000"`; this is belt-and-
# suspenders for anyone running the image outside compose.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 singularity \
    && useradd -u 1000 -g 1000 -m -s /bin/bash -d /home/singularity \
       -c "Singularity Suite runtime" singularity \
    && chown -R 1000:1000 /home/singularity

# OCI image metadata — kills the "noname image" critique.
LABEL org.opencontainers.image.title="Singularity Suite" \
      org.opencontainers.image.description="ARR-stack media management toolkit — CSI, RawLoadrr, MKVerything, Mass Editor" \
      org.opencontainers.image.source="https://codeberg.org/RawSmoke/Singularity" \
      org.opencontainers.image.url="https://codeberg.org/RawSmoke/Singularity" \
      org.opencontainers.image.documentation="https://codeberg.org/RawSmoke/Singularity" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.authors="RawSmoke" \
      org.opencontainers.image.vendor="RawSmoke" \
      org.opencontainers.image.base.name="python:3.11-bookworm" \
      org.opencontainers.image.version="3.0.1"

# Dashboard port. EXPOSE doesn't publish on host-net mode, but it
# self-documents the listening service for `docker inspect`/`docker ps`.
EXPOSE 8002

# Explicit > implicit. SIGTERM lets python3 / tini close gracefully.
STOPSIGNAL SIGTERM

# Liveness probe — accepts 2xx/3xx (dashboard redirects "/" → /login).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsSL http://127.0.0.1:8002/ -o /dev/null || exit 1

# Drop privileges as the final image layer. Standalone `docker run` of
# this image now lands in a UID-1000 shell, not root.
USER 1000:1000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "singularity.py"]
