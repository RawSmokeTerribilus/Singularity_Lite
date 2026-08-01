# Singularity Lite — arch-neutral build for weak hardware.
#
# Builds as-is on amd64, arm64 and armhf: Raspberry Pi, NAS boxes, old thin
# clients. There is no published image for this variant on purpose — you build
# it yourself, which is precisely why none of the source forges the full image
# carries need to exist here.
#
# Deliberately ABSENT compared to the full Singularity image, and why:
#   - MakeMKV        : makemkv-bin ships x86_64-only binaries. Cannot run on ARM.
#   - zimg           : only needed by VapourSynth.
#   - VapourSynth    : only reached by RawLoadrr's opt-in --vapoursynth screenshot
#                      mode. The default ffmpeg screenshot path is used instead.
#   - l-smash / L-SMASH-Works : VapourSynth source plugins.
#   - Google Chrome  : no ARM64 Linux build exists, and ARM64 Chromium carries no
#                      Widevine CDM, so Recordrr could never work here anyway.
#   - VAAPI drivers  : intel-media-va-driver-non-free has no arm64 package (this
#                      is the line that killed the first Pi build attempt), and a
#                      Pi exposes no VAAPI encoder regardless.
#
# Net effect: no compiler toolchain, no source builds, no non-free repos. The
# build is a package install and a pip install.

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# --- 1. RUNTIME BINARIES ---
# The complete set. Every one of these is invoked by code that survives in Lite:
#   ffmpeg/ffprobe : RawLoadrr screenshots (src/prep.py), verifier health checks
#   mediainfo      : the CLI (verifier, CSI) AND libmediainfo for the pymediainfo
#                    binding that RawLoadrr imports at module scope
#   mkvtoolnix     : mkvmerge -J, the verifier's structural check
#   procps         : `ps`, used by core/status_manager for live cpu/rss metrics
#   tini           : proper PID 1 — reaps zombies, forwards signals
#   curl           : the HEALTHCHECK below
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mediainfo \
    mkvtoolnix \
    procps \
    tini \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Optional extras, left out by default — uncomment if you need them:
#   mono-runtime : BDInfo, only for raw BDMV folder uploads
#   mktorrent    : only if you switch torrent_creation off the default (torf)
#   build-essential python3-dev : only if pip has to build a wheel from source
#     for your architecture. Most arm64 wheels exist; armhf/NAS users may need it.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     mono-runtime mktorrent build-essential python3-dev \
#     && rm -rf /var/lib/apt/lists/*

# --- 2. PYTHON DEPS ---
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 3. APP ---
COPY --chown=1000:1000 . .
RUN mkdir -p logs tmp core/templates && chown -R 1000:1000 /app

# --- 4. RUNTIME USER ---
# Makes UID 1000 a real account so git/less/the shell stop emitting
# "cannot find name for user ID 1000". Compose also pins user: "1000:1000";
# this is belt-and-suspenders for a bare `docker run`.
RUN groupadd -g 1000 singularity \
    && useradd -u 1000 -g 1000 -m -s /bin/bash -d /home/singularity \
       -c "Singularity Lite runtime" singularity \
    && chown -R 1000:1000 /home/singularity

LABEL org.opencontainers.image.title="Singularity Lite" \
      org.opencontainers.image.description="Singularity suite for low-power hardware — uploader, tracker mass editor, CSI, dashboard. No transcoding, no disc ripping, no browser capture." \
      org.opencontainers.image.source="https://github.com/RawSmokeTerribilus/Singularity_Lite" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.authors="RawSmoke" \
      org.opencontainers.image.vendor="RawSmoke" \
      org.opencontainers.image.base.name="python:3.11-slim-bookworm"

# Dashboard port. EXPOSE doesn't publish under host networking, but it
# self-documents the listening service for `docker inspect` / `docker ps`.
EXPOSE 8002

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsSL http://127.0.0.1:8002/ -o /dev/null || exit 1

USER 1000:1000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "singularity.py"]
