# Immich Booster - Video Enhancement Pipeline

Ein vollautomatisierter Container für KI-gestützter Videoverbesserung mit NVIDIA Blackwell (CUDA 12.4), VapourSynth und nativer Immich-Integration.

## Features

✅ **Automatische Videoverarbeitung** via Immich API
✅ **GPU-beschleunigt** mit NVIDIA NVENC (HEVC)
✅ **VapourSynth Pipeline** für professionelle Verarbeitung
✅ **Metadaten-Kloning** via ExifTool (GPS, Zeitstempel, Kamera-Modell)
✅ **Automatisches Stacking** im Original-Album
✅ **CUDA 12.4** für maximale Performance

## Pipeline-Schritte

1. **Fetch**: Video aus Immich downloaden
2. **Process**: VapourSynth Pipeline (Denoise + Light Enhancement) auf GPU
3. **Tag**: Dateiname mit `_boosted` versehen
4. **Meta-Clone**: ExifTool kopiert alle Metadaten vom Original
5. **Upload & Stack**: Re-Upload und via API mit Original "stacken"

## Setup

### Voraussetzungen

- Docker mit NVIDIA GPU Support
- NVIDIA GPU (Blackwell oder ähnlich)
- Immich API-Key
- Netzwerk-Zugang zu Immich-Instance

### Installation (Standard Docker)

```bash
# 1. .env konfigurieren
cp .env.example .env
# Bearbeite .env mit deinen Settings

# 2. Docker Image bauen
docker build -t immich-booster .

# 3. Container starten
docker run --gpus all \
  --env-file .env \
  -v $(pwd)/temp:/app/temp \
  -v $(pwd)/models:/models \
  immich-booster
```

### Mit Docker Compose

```bash
# Annahme: Immich läuft bereits in Docker Netzwerk "immich-network"
docker-compose up -d

### Build & Push auf GitHub (GHCR)

Automatischer Docker-Build via GitHub Actions ist eingerichtet:

1. Repository zu GitHub pushen (Branch `main`/`master`).
2. Workflow `.github/workflows/docker-publish.yml` baut und pusht nach GHCR:
  - Image: `ghcr.io/Randomname653/immich-booster:latest`
  - Zusätzliche Tags: Branch, Tag (`vX.Y.Z`), Commit-SHA

Pull auf TrueNAS:
```
docker pull ghcr.io/Randomname653/immich-booster:latest
```

Compose-Referenz (TrueNAS):
```
image: ghcr.io/Randomname653/immich-booster:latest
```

### TrueNAS SCALE (empfohlen)

1. Apps → Einstellungen → GPU-Unterstützung aktivieren (NVIDIA Treiber installieren, TrueNAS ggf. neu starten).
2. Apps → Docker Compose → Neues Compose. Inhalt aus `docker-compose.yml` einfügen.
3. Volumes auf einen Dataset mappen, z. B. `/mnt/tank/immich-booster/temp` → `/app/temp`, `/mnt/tank/immich-booster/models` → `/models`.
4. `.env` Werte im Compose-UI setzen oder `.env` Datei als Secret/Config referenzieren:
  - `IMMICH_URL=http://<immich-host>:2283/api`
  - `IMMICH_API_KEY=<dein_api_key>`
  - Optional: `DEVICE_FILTER=Pixel`, `TEMP_DIR=/app/temp`
5. Ressourcen → GPU: eine oder mehrere GPUs zuweisen.
6. App starten und Logs prüfen.

Smoke-Test (GPU):
```
docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi
```
```

## Healthcheck

Der Container bringt einen Healthcheck mit, der regelmäßig prüft:

- VapourSynth und Plugins: `lsmas`, `knlm`, `fmtc`
- `vspipe` Verfügbarkeit
- FFmpeg Encoder: `hevc_nvenc`/`h264_nvenc`
- ExifTool Installation

Manueller Test:

```
docker run --rm immich-booster:latest python /app/healthcheck.py
```

Status in Docker/Compose ist als `healthy`/`unhealthy` sichtbar.

## Compose-Beispiel

Beispiel `docker-compose.example.yml` (Werte ersetzen):

```yaml
version: '3.8'
services:
  immich-booster:
    image: ghcr.io/Randomname653/immich-booster:latest # oder: immich-booster:latest (lokal gebaut)
    container_name: immich-booster
    runtime: nvidia
    environment:
      - TZ=Europe/Berlin
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility,video
      - IMMICH_URL=http://192.168.1.10:2283/api   # <- anpassen
      - IMMICH_API_KEY=YOUR_API_KEY_HERE          # <- anpassen
      - TEMP_DIR=/app/temp                        # optional
      - DEVICE_FILTER=Pixel                       # optional
    volumes:
      - /mnt/tank/immich-booster/temp:/app/temp:rw
      - /mnt/tank/immich-booster/models:/models:rw
      - /mnt/tank/immich-booster/config:/app/config:rw
    restart: unless-stopped
```

## Konfiguration

Bearbeite `.env`:

```env
IMMICH_URL=http://192.168.1.X:2283/api      # Deine Immich URL
IMMICH_API_KEY=abc123...                      # Dein API-Key
DEVICE_FILTER=Pixel                           # Nur diese Geräte prozessieren
```

## Nächste Schritte

### 1. TensorRT Engine für Blackwell

```bash
# BasicVSR++ ONNX Model herunterladen
wget https://download.openmmlab.com/mmagic/basicvsr_plusplus/basicvsr_plusplus_w7_8x4d64e64_600k_reds.pth

# Mit trtexec in Engine-Datei konvertieren (NVIDIA Tools)
trtexec --onnx=basicvsr_pp.onnx --saveEngine=basicvsr_pp_blackwell.engine --best
```

### 2. VapourSynth Filter aktivieren

Ersetze den `KNLMeansCL` Dummy in `processor.py` mit echtem BasicVSR++ über vs-mlrt:

```python
clip = core.trt.Model(clip, engine_path="/models/basicvsr_pp_blackwell.engine")
```

### 3. Logging & Monitoring

Die Logs werden im Container ausgegeben. Für Persistierung:

```bash
docker logs immich-booster -f
```

## Troubleshooting

### "No GPU found"
```bash
# GPU Support prüfen
docker run --gpus all nvidia/cuda:12.4-runtime-ubuntu22.04 nvidia-smi
```

### "vspipe command not found"
Die Pipeline fällt automatisch auf reines FFmpeg (NVENC) zurück. Für VapourSynth stelle sicher, dass `vspipe` und benötigte Plugins verfügbar sind. In Compose/Container prüfen:
```
which vspipe && vspipe --version
```

### "ExifTool Error"
```bash
# Manuell im Container testen
docker exec immich-booster exiftool -ver
```

## API Endpoints

### Asset Image URL Structure
```
http://immich-instance/api/download/asset/{assetId}
```

### Stack API (Immich v1.90+)
```
POST /api/asset/stack/parent
{
  "parentAssetId": "uuid",
  "childAssetIds": ["uuid1", "uuid2"]
}
```

## Performance-Tipps

- **CUDA Memory**: `core.max_cache_size = 20000` für 4K
- **Threads**: `core.num_threads = 16` für Blackwell
- **Preset**: `hevc_nvenc -preset slow` für beste Qualität
- **CRF**: `-cq 20` für visuelle Qualität

## License

MIT License - Siehe LICENSE Datei

## Support

Für Fragen oder Issues: [GitHub Issues](https://github.com/video-boost/immich-booster/issues)
