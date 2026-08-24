# syntax=docker/dockerfile:1
############################################################
# Bauen: VapourSynth + ffms2 gegen die FFmpeg-Bibliotheken
############################################################
# CUDA 12.8 ist Pflicht, nicht Geschmackssache: Blackwell-Karten (RTX Pro 2000,
# RTX 50xx) melden Compute Capability sm_120, und CUDA 12.4 kennt davon nichts.
# Ein damit gebautes Image startet zwar, faellt aber bei der ersten Inferenz
# mit "no kernel image is available for execution on the device" um.
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04 AS builder

# Meson legt das Modul nach /usr/local/lib/python3/dist-packages - ohne
# Versionsnummer, weil es gegen die stabile ABI gebaut ist (vapoursynth.abi3.so).
# Die versionierten Pfade stehen nur als Rueckfallebene dahinter.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/usr/local/lib/python3/dist-packages:/usr/local/lib/python3.12/site-packages:/usr/local/lib/python3.12/dist-packages

# Kein "|| true" hier: apt installiert bei einem einzigen unbekannten Paket
# gar nichts, der Fehler wuerde also verschluckt und erst viel spaeter als
# fehlendes git auffallen. libavresample gibt es seit FFmpeg 5 nicht mehr.
RUN apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake libtool pkg-config build-essential \
        git wget ca-certificates \
        python3 python3-dev python3-pip \
        nasm yasm \
        libavcodec-dev libavformat-dev libavutil-dev \
        libswscale-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

# Cython MUSS aus pip kommen, nicht aus apt: das Ubuntu-Paket cython3 legt das
# Programm ausschliesslich als "cython3" ab, der VapourSynth-Build ruft aber
# "cython" auf und bricht sonst mit exit 127 ab. VapourSynth R79 verlangt
# ausserdem mindestens Cython 3.1 und baut mit Meson.
RUN pip3 install --no-cache-dir --break-system-packages \
        "Cython>=3.1.0" meson-python meson ninja \
    && cython --version && meson --version && ninja --version

WORKDIR /opt/src

# zimg: Farbraum- und Skalierungskern von VapourSynth
RUN git clone --depth=1 --branch release-3.0.5 https://github.com/sekrit-twc/zimg.git \
    && cd zimg && ./autogen.sh && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" && make install && ldconfig

# VapourSynth baut seit R7x mit Meson, nicht mehr mit Autotools - im R79-Archiv
# gibt es weder autogen.sh noch configure. In einzelne Schritte zerlegt, damit
# das Log bei einem Fehler direkt die schuldige Zeile nennt.
ARG VAPOURSYNTH_VERSION=R79
RUN wget -q https://github.com/vapoursynth/vapoursynth/archive/refs/tags/${VAPOURSYNTH_VERSION}.tar.gz \
    && tar -zxf ${VAPOURSYNTH_VERSION}.tar.gz
RUN cd vapoursynth-${VAPOURSYNTH_VERSION} \
    && meson setup build --prefix=/usr/local --buildtype=release
RUN cd vapoursynth-${VAPOURSYNTH_VERSION} && ninja -C build
RUN cd vapoursynth-${VAPOURSYNTH_VERSION} && ninja -C build install && ldconfig
# Scheitert der Import trotzdem, listet der Schritt den tatsaechlichen
# Ablageort auf - genau das hat den Pfad oben ueberhaupt erst geklaert.
RUN python3 -c "import vapoursynth; print('VapourSynth im Builder ok:', vapoursynth.core.version_number())" \
    || (echo '--- Suche das installierte Modul ---'; find /usr/local -name "vapoursynth*" -maxdepth 6 | head -20; exit 1)

# ffms2 liefert den Source-Filter. Er reicht die Container-Rotation als
# Frame-Property durch, worauf sich processor.py stuetzt. Auf Version 5.0
# festgenagelt - ein Klon von master macht Builds unvorhersehbar.
RUN git clone --depth=1 --branch 5.0 https://github.com/FFMS/ffms2.git \
    && cd ffms2 && ./autogen.sh && ./configure --prefix=/usr/local --enable-shared \
    && make -j"$(nproc)" && make install \
    && mkdir -p /usr/local/lib/vapoursynth \
    && ln -sf /usr/local/lib/libffms2.so /usr/local/lib/vapoursynth/libffms2.so

############################################################
# Laufzeit
############################################################
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

# Gleicher Suchpfad wie im Builder: ohne ihn findet "import vapoursynth"
# nichts, obwohl die Dateien im Image liegen.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VAPOURSYNTH_PLUGIN_PATH=/usr/local/lib/vapoursynth \
    LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONPATH=/app:/usr/local/lib/python3/dist-packages:/usr/local/lib/python3.12/site-packages:/usr/local/lib/python3.12/dist-packages

# ffmpeg zieht die passenden av-Bibliotheken als Abhaengigkeit nach; sie hier
# einzeln mit Versionsnummer aufzuzaehlen bricht nur bei jedem Ubuntu-Update.
# libpython3.12t64 ist Pflicht: vspipe bettet einen Python-Interpreter ein und
# braucht dessen Shared Library. Fehlt sie, startet der Container anstandslos
# und scheitert erst beim ersten Video mit "Failed to initialize VSScript.
# Python executable and library path could not be determined".
# In Ubuntu 24.04 traegt das Paket wegen der 64-Bit-time_t-Umstellung das
# Suffix t64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip libpython3.12t64 \
        ffmpeg \
        libimage-exiftool-perl \
        fonts-dejavu-core \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/ /usr/local/
RUN ldconfig

# Die Kette einmal echt durchlaufen lassen. Ein blosses "import vapoursynth"
# beweist nichts: es gelingt auch dann, wenn vspipe seinen eingebetteten
# Interpreter nicht hochbekommt - genau der Fall, der sonst erst beim ersten
# Video auffaellt.
COPY <<'PROBE' /tmp/probe.vpy
import vapoursynth as vs
vs.core.std.LoadPlugin("/usr/local/lib/vapoursynth/libffms2.so")
assert hasattr(vs.core, "ffms2"), "ffms2 laedt nicht"
vs.core.std.BlankClip(width=64, height=64, length=3).set_output()
PROBE
RUN vspipe -c y4m /tmp/probe.vpy - > /dev/null \
    && echo "vspipe, VSScript und ffms2 arbeiten" \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q hevc_nvenc \
    && echo "hevc_nvenc vorhanden" \
    && rm -f /tmp/probe.vpy

# PyTorch mit cu128: Voraussetzung dafuer, dass BasicVSR++ auf Blackwell laeuft.
RUN pip3 install --no-cache-dir --break-system-packages \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch torchvision

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Modelle beim Bauen holen, damit der erste Lauf nachts nicht am Netz haengt.
# Schlaegt der Download fehl, bricht der Build nicht ab - processor.py laedt sie
# dann zur Laufzeit nach. Die Zahl am Ende gehoert aber ins Log, sonst bleibt
# unklar, ob der Schritt etwas bewirkt hat.
RUN (python3 -m vsbasicvsrpp || echo "WARNUNG: Vorab-Download fehlgeschlagen") \
    && python3 - <<'PY'
import os, vsbasicvsrpp
base = os.path.dirname(vsbasicvsrpp.__file__)
found = [os.path.join(r, f) for r, _d, fs in os.walk(base) for f in fs if f.endswith(".pth")]
print(f"Modelldateien im Image: {len(found)}")
for f in sorted(found):
    print("   ", os.path.relpath(f, base), f"{os.path.getsize(f)/1e6:.0f} MB")
PY

COPY *.py /app/

ENV TEMP_DIR=/app/temp \
    DB_PATH=/app/config/processed.db \
    VS_SCRIPT=/app/processor.py \
    FONT_FILE=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

RUN mkdir -p /app/temp /app/config

HEALTHCHECK --interval=5m --timeout=60s --start-period=2m --retries=3 \
    CMD python3 /app/healthcheck.py || exit 1

CMD ["python3", "main.py"]
