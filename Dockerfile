############################################################
# Builder stage: CUDA 12.4 devel for FFmpeg + VapourSynth
############################################################
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# WICHTIG: autoconf, automake, libtool sind fuer autogen.sh zwingend erforderlich
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && sed -i "s|http://security.ubuntu.com/ubuntu/|http://us.archive.ubuntu.com/ubuntu/|g" /etc/apt/sources.list \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    ca-certificates git wget curl build-essential pkg-config nasm yasm cmake meson ninja-build \
    python3 python3-pip python3-dev zlib1g-dev libssl-dev libfreetype6-dev libfontconfig1-dev p7zip-full \
    autoconf automake libtool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/src

# 1) NVENC headers
RUN git clone --depth=1 https://github.com/FFmpeg/nv-codec-headers.git && make -C nv-codec-headers install

# 2) Build FFmpeg 7.1
RUN git clone --depth=1 --branch release/7.1 https://github.com/FFmpeg/FFmpeg.git ffmpeg \
    && cd ffmpeg \
        && ./configure --prefix=/opt/ffmpeg --enable-shared --disable-static \
            --extra-cflags="-I/usr/local/include -I/usr/local/cuda/include" \
            --extra-ldflags="-L/usr/local/lib -L/usr/local/cuda/lib64" \
            --extra-libs="-lpthread -lm" --bindir=/opt/ffmpeg/bin \
            --enable-gpl --enable-nonfree --enable-cuda-nvcc --enable-libnpp \
            --enable-libfreetype --enable-libfontconfig --disable-doc --disable-debug \
    && make -j"$(nproc)" && make install

# 3) Build zimg, VapourSynth, FFMS2
RUN pip3 install --no-cache-dir "Cython>=3.0.0"
RUN git clone --depth=1 --branch release-3.0.5 https://github.com/sekrit-twc/zimg.git && cd zimg && ./autogen.sh && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install
RUN wget https://github.com/vapoursynth/vapoursynth/archive/refs/tags/R70.tar.gz && tar -zxvf R70.tar.gz && cd vapoursynth-R70 && ./autogen.sh && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install && ldconfig
RUN git clone --depth=1 https://github.com/FFMS/ffms2.git && cd ffms2 && ./autogen.sh && ./configure --prefix=/usr/local PKG_CONFIG_PATH="/opt/ffmpeg/lib/pkgconfig" CFLAGS="-I/opt/ffmpeg/include" CXXFLAGS="-I/opt/ffmpeg/include" LDFLAGS="-L/opt/ffmpeg/lib" && make -j"$(nproc)" && make install \
    && mkdir -p /usr/local/lib/vapoursynth && ln -s /usr/local/lib/libffms2.so /usr/local/lib/vapoursynth/libffms2.so

RUN wget -O /usr/local/bin/vsrepo.py https://raw.githubusercontent.com/vapoursynth/vsrepo/master/vsrepo.py && chmod +x /usr/local/bin/vsrepo.py && python3 /usr/local/bin/vsrepo.py update && (python3 /usr/local/bin/vsrepo.py install knlmeanscl fmtconv || true)

############################################################
# Runtime stage
############################################################
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV VAPOURSYNTH_PLUGIN_PATH=/usr/local/lib/vapoursynth
ENV PYTHONPATH=/app:/usr/local/lib/python3.10/site-packages
ENV LD_LIBRARY_PATH="/opt/ffmpeg/lib:/usr/local/lib:$LD_LIBRARY_PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 libpython3.10 libatomic1 ca-certificates libimage-exiftool-perl ocl-icd-libopencl1 \
    python3 python3-pip libfreetype6 libfontconfig1 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/OpenCL/vendors && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

COPY --from=builder /opt/ffmpeg /opt/ffmpeg
ENV PATH="/opt/ffmpeg/bin:${PATH}"
COPY --from=builder /usr/local/ /usr/local/
RUN ldconfig

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision

COPY . .

# Healthcheck
RUN echo "import sys\nimport vapoursynth as vs\ntry:\n    if hasattr(vs.core, \"knlm\"):\n        sys.exit(0)\n    sys.exit(1)\nexcept:\n    sys.exit(1)" > healthcheck.py

CMD ["python3", "main.py"]
