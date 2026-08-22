# syntax=docker/dockerfile:1
############################################################
# Bauen: VapourSynth + ffms2 gegen die FFmpeg-Bibliotheken
############################################################
# CUDA 12.8 ist Pflicht, nicht Geschmackssache: Blackwell-Karten (RTX Pro 2000,
# RTX 50xx) melden Compute Capability sm_120, und CUDA 12.4 kennt davon nichts.
# Ein damit gebautes Image startet zwar, faellt aber bei der ersten Inferenz
# mit "no kernel image is available for execution on the device" um.
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config build-essential \
        git wget ca-certificates cython3 \
        python3 python3-dev python3-pip \
        nasm yasm \
        libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
        libswresample-dev libavresample-dev || true \
    && apt-get install -y --no-install-recommends \
        libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/src

# zimg: Farbraum- und Skalierungskern von VapourSynth
RUN git clone --depth=1 --branch release-3.0.5 https://github.com/sekrit-twc/zimg.git \
    && cd zimg && ./autogen.sh && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" && make install

ARG VAPOURSYNTH_VERSION=R79
RUN wget -q https://github.com/vapoursynth/vapoursynth/archive/refs/tags/${VAPOURSYNTH_VERSION}.tar.gz \
    && tar -zxf ${VAPOURSYNTH_VERSION}.tar.gz \
    && cd vapoursynth-${VAPOURSYNTH_VERSION} \
    && ./autogen.sh && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" && make install && ldconfig

# ffms2 liefert den Source-Filter. Er reicht die Container-Rotation als
# Frame-Property durch, worauf sich processor.py stuetzt.
RUN git clone --depth=1 https://github.com/FFMS/ffms2.git \
    && cd ffms2 && ./autogen.sh && ./configure --prefix=/usr/local --enable-shared \
    && make -j"$(nproc)" && make install \
    && mkdir -p /usr/local/lib/vapoursynth \
    && ln -sf /usr/local/lib/libffms2.so /usr/local/lib/vapoursynth/libffms2.so

############################################################
# Laufzeit
############################################################
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VAPOURSYNTH_PLUGIN_PATH=/usr/local/lib/vapoursynth \
    LD_LIBRARY_PATH=/usr/local/lib

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ffmpeg \
        libimage-exiftool-perl \
        libavcodec60 libavformat60 libavutil58 libswscale7 libswresample4 \
        fonts-dejavu-core \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/ /usr/local/
RUN ldconfig

# PyTorch mit cu128: Voraussetzung dafuer, dass BasicVSR++ auf Blackwell laeuft.
RUN pip3 install --no-cache-dir --break-system-packages \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch torchvision

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Modelle beim Bauen holen, damit der erste Lauf nachts nicht am Netz haengt.
RUN python3 -c "import vsbasicvsrpp, os; os.makedirs('/models', exist_ok=True)" \
    && python3 -m vsbasicvsrpp || true

COPY *.py /app/

ENV TEMP_DIR=/app/temp \
    DB_PATH=/app/config/processed.db \
    VS_SCRIPT=/app/processor.py \
    FONT_FILE=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

RUN mkdir -p /app/temp /app/config

HEALTHCHECK --interval=5m --timeout=60s --start-period=2m --retries=3 \
    CMD python3 /app/healthcheck.py || exit 1

CMD ["python3", "main.py"]
