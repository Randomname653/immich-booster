############################################################
# Builder stage: CUDA 12.4 devel for FFmpeg + VapourSynth
############################################################
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# FIX: Mirrors, p7zip, clean lists
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && sed -i "s|http://security.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    wget \
    curl \
    build-essential \
    pkg-config \
    nasm \
    yasm \
    cmake \
    meson \
    ninja-build \
    python3 \
    python3-pip \
    python3-dev \
    zlib1g-dev \
    libssl-dev \
    libfreetype6-dev \
    libfontconfig1-dev \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/src

# 1) NVENC headers
RUN git clone --depth=1 https://github.com/FFmpeg/nv-codec-headers.git \
    && make -C nv-codec-headers install

# 2) Build FFmpeg 7.1 (Shared Libs) + FREETYPE fuer Wasserzeichen
RUN git clone --depth=1 --branch release/7.1 https://github.com/FFmpeg/FFmpeg.git ffmpeg \
    && cd ffmpeg \
        && ./configure \
         --prefix=/opt/ffmpeg \
         --enable-shared \
         --disable-static \
            --extra-cflags="-I/usr/local/include -I/usr/local/cuda/include" \
            --extra-ldflags="-L/usr/local/lib -L/usr/local/cuda/lib64" \
         --extra-libs="-lpthread -lm" \
         --bindir=/opt/ffmpeg/bin \
         --enable-gpl \
         --enable-nonfree \
        --enable-cuda-nvcc \
        --enable-libnpp \
        --enable-libfreetype \
        --enable-libfontconfig \
         --disable-doc \
         --disable-debug \
    && make -j"$(nproc)" \
    && make install \
    && strip /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe

# WICHTIG: Pfade setzen
ENV PKG_CONFIG_PATH="/opt/ffmpeg/lib/pkgconfig"
ENV LD_LIBRARY_PATH="/opt/ffmpeg/lib:/usr/local/lib"

# Install Autotools & Upgrade Cython
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    autoconf automake libtool \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir "Cython>=3.0.0"

# 3) Build zimg 3.0.5 manually
RUN git clone --depth=1 --branch release-3.0.5 https://github.com/sekrit-twc/zimg.git \
    && cd zimg \
    && ./autogen.sh \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install

# 4) Build VapourSynth R70
ARG VAPOURSYNTH_VERSION=R70
RUN wget https://github.com/vapoursynth/vapoursynth/archive/refs/tags/${VAPOURSYNTH_VERSION}.tar.gz \
    && tar -zxvf ${VAPOURSYNTH_VERSION}.tar.gz \
    && cd vapoursynth-${VAPOURSYNTH_VERSION} \
    && ./autogen.sh \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && cd .. \
    && rm -rf vapoursynth-${VAPOURSYNTH_VERSION} ${VAPOURSYNTH_VERSION}.tar.gz

ENV PYTHONPATH=/usr/local/lib/python3.10/site-packages

# 5) Build FFMS2
RUN git clone --depth=1 https://github.com/FFMS/ffms2.git \
    && cd ffms2 \
    && ./autogen.sh \
    && ./configure --prefix=/usr/local \
        PKG_CONFIG_PATH="/opt/ffmpeg/lib/pkgconfig" \
        CFLAGS="-I/opt/ffmpeg/include" \
        CXXFLAGS="-I/opt/ffmpeg/include" \
        LDFLAGS="-L/opt/ffmpeg/lib" \
    && make -j"$(nproc)" \
    && make install \
    && mkdir -p /usr/local/lib/vapoursynth \
    && ln -s /usr/local/lib/libffms2.so /usr/local/lib/vapoursynth/libffms2.so

# 6) Install OTHER Plugins via vsrepo
RUN wget -O /usr/local/bin/vsrepo.py https://raw.githubusercontent.com/vapoursynth/vsrepo/master/vsrepo.py \
    && chmod +x /usr/local/bin/vsrepo.py \
    && python3 /usr/local/bin/vsrepo.py update \
    && (python3 /usr/local/bin/vsrepo.py install knlmeanscl fmtconv || true)

############################################################
# Runtime stage
############################################################
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV VAPOURSYNTH_PLUGIN_PATH=/usr/local/lib/vapoursynth
ENV PYTHONPATH=/app:/usr/local/lib/python3.10/site-packages
ENV LD_LIBRARY_PATH="/opt/ffmpeg/lib:/usr/local/lib:$LD_LIBRARY_PATH"

# FIX: libatomic1 und libpython3.10 hinzufuegen
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && sed -i "s|http://security.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --fix-missing \
    && apt-get install -y --no-install-recommends libexpat1 libpython3.10 libatomic1 \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    libimage-exiftool-perl \
    ocl-icd-libopencl1 \
    python3 \
    python3-pip \
    libfreetype6 \
    libfontconfig1 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# FIX: OpenCL Vendor File erstellen (WICHTIG fuer KNLMeansCL)
RUN mkdir -p /etc/OpenCL/vendors && \
    echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

RUN ldconfig

# Optional: TensorRT Runtime
ARG ENABLE_TRT=0
RUN if [ "$ENABLE_TRT" = "1" ]; then \
      rm -rf /var/lib/apt/lists/* && \
      apt-get update --fix-missing && apt-get install -y --no-install-recommends gnupg ca-certificates wget && \
      wget -qO /usr/share/keyrings/nvidia-cuda-archive-keyring.gpg https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/3bf863cc.pub && \
      echo "deb [signed-by=/usr/share/keyrings/nvidia-cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64 /" > /etc/apt/sources.list.d/nvidia-cuda.list && \
      apt-get update --fix-missing && apt-get install -y --no-install-recommends libnvinfer8 libnvonnxparsers8 libnvparsers8 && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Copy Artifacts
COPY --from=builder /opt/ffmpeg /opt/ffmpeg
ENV PATH="/opt/ffmpeg/bin:${PATH}"
COPY --from=builder /usr/local/ /usr/local/
RUN ldconfig

# App Setup
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision

COPY . /app

# Finaler Healthcheck-Fix (wirklich minimalistisch)
RUN echo "import sys; import vapoursynth as vs; sys.exit(0 if hasattr(vs.core, \"knlm\") else 1)" > healthcheck.py

CMD ["python3", "main.py"]
