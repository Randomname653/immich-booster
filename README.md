# Immich Booster

Restauriert alte, stark komprimierte Videos in einer Immich-Bibliothek und legt
das Ergebnis als gestapeltes Asset neben das Original. **Das Original bleibt
immer erhalten** und wird nie ersetzt.

Läuft als Container auf einem NAS mit NVIDIA-GPU, arbeitet standardmäßig nachts.

## Was es tut

1. Holt alle Videos über die Immich-API (über **alle** Ergebnisseiten)
2. Stuft jedes Video danach ein, wie stark es beschädigt ist
3. Restauriert die lohnenden mit **BasicVSR++**, skaliert sie hoch und kodiert mit NVENC
4. Prüft das Ergebnis, überträgt die Metadaten, brennt eine KI-Kennzeichnung ein
5. Lädt hoch, stapelt mit dem Original und setzt einen Immich-Tag

## Das Bildverfahren

Verwendet wird **BasicVSR++, Modell „NTIRE 2021 Quality Enhancement of
Compressed Video, Track 3"**. Das Modell arbeitet *temporal*: Es zieht seine
Information aus den Nachbarframes, statt Details zu erfinden. Bei bewegtem Bild
liefert jedes Nachbarframe echte Sub-Pixel-Information über dieselbe Stelle —
so entstehen reale Details statt halluzinierter. Als Nebeneffekt entfällt das
Flackern, das Einzelbild-Modelle im Video erzeugen.

Anschließend wird klassisch mit Lanczos vergrößert.

**Bewusst kein KI-Upscaler.** Getestet wurden SwinIR realSR (x2/x4, GAN und
PSNR) und DPIR. Beide liefern auf Standbildern mehr Schärfe, erzeugen aber
einen glattgebügelten Plastik-Eindruck — Haut wirkt wie retuschiert, Kanten
wie gezeichnet. Im direkten Vergleich schnitten sie schlechter ab als das
unbearbeitete Original.

### Warum das Ergebnis geprüft wird

Beim Testen lieferten zwei von drei Inferenz-Backends **stillschweigend
falsche Bilder**: ONNX Runtime CUDA gab reine Nullen zurück, TensorRT mit fp16
lieferte NaN — beide ohne jede Fehlermeldung, mit plausibel aussehenden
Bildraten. Ohne die Ausgabekontrolle in `encode.verify()` hätte ein
Nachtdurchlauf tausende schwarze Videos erzeugt und ordentlich in die
Bibliothek gestapelt. Die Prüfung ist deshalb nicht abschaltbar.

## Voraussetzungen

- Docker mit NVIDIA Container Toolkit
- NVIDIA-GPU; bei **Blackwell** (RTX Pro 2000, RTX 50xx) zwingend ein Treiber
  für CUDA 12.8+. Das Image ist entsprechend gebaut — mit CUDA 12.4 scheitert
  die Inferenz auf diesen Karten mit *„no kernel image is available"*.
- Immich 2.x/3.x (getestet gegen 3.1.0)

## Einrichtung

```bash
cp .env.example .env
# IMMICH_URL und IMMICH_API_KEY eintragen
docker compose up -d --build
docker compose logs -f
```

`DRY_RUN=true` ist Voreinstellung: Es wird alles gerechnet, aber nichts
hochgeladen. Erst wenn die Ergebnisse überzeugen, auf `false` stellen.

### Einzelne Datei prüfen, ohne Immich

```bash
docker compose run --rm immich-booster python3 main.py --file /app/temp/probe.mp4
```

## Auswahl der Videos

Es gibt **keinen Gerätefilter**. In der Praxis tragen nur rund 10 % der Videos
überhaupt Kamera-EXIF, und ausgerechnet die neuen Handyaufnahmen brauchen die
Bearbeitung am wenigsten. Maßgeblich ist stattdessen der Zustand des Materials:

| Klasse | Bedeutung | Behandlung |
|---|---|---|
| **A** | kurze Kante unter 720p | restaurieren und vergrößern |
| **B** | groß genug, aber stark komprimiert | restaurieren |
| **C** | mäßig komprimiert | leichte Aufbereitung |
| **D** | Qualität ausreichend | unangetastet |

Favoriten werden vorgezogen. Videos über `SKIP_IF_SHORT_EDGE_GTE` oder
`SKIP_IF_MBIT_GTE` werden nie angefasst — ein Neukodieren wäre dort ein
Verlustgeschäft.

## Rechenzeit

Gemessen auf einer RTX 4090, über vollständige Sequenzen:

| Quellgröße | Bilder/s | MPix/s |
|---|---|---|
| 176×144 | 22,5 | 0,57 |
| 720×720 | 9,5 | 4,9 |
| 1280×720 | 5,8 | 5,3 |

Kleine Bilder lasten die GPU schlecht aus, der Durchsatz steigt mit der
Auflösung. Für rund 18 Stunden Quellmaterial ergibt das etwa 30 Stunden auf
einer 4090. Eine kleinere Karte braucht entsprechend länger.

Bei knappem VRAM: `VS_LENGTH` senken, `VS_TILE=512` setzen oder
`VS_CPU_CACHE=true` (kostet Tempo).

## Kennzeichnung

KI-bearbeitete Videos werden dauerhaft gekennzeichnet — sichtbar durch ein
eingebranntes Wasserzeichen unten rechts und maschinenlesbar durch einen
XMP-Eintrag sowie den Immich-Tag `AI-enhanced`. Der dunkle Rand am Schriftzug
sorgt dafür, dass die Kennzeichnung auch auf hellem Grund sichtbar bleibt.

## Aufbau

| Datei | Zweck |
|---|---|
| `main.py` | Ablaufsteuerung, Zeitfenster, Fehlerbehandlung |
| `immich.py` | API-Client (Pagination, Upload, Stapeln, Tags) |
| `selection.py` | Einstufung und Priorisierung |
| `processor.py` | VapourSynth-Kette, wird von `vspipe` geladen |
| `encode.py` | ffmpeg-Aufruf, Ergebnisprüfung, Metadaten |
| `healthcheck.py` | Bereitschaftsprüfung des Containers |

## Fehlerverhalten

Ein Video, das nicht verarbeitet werden kann, beendet den Dienst nicht. Der
Fehler wird gezählt; nach drei Versuchen wird das Video übersprungen. Ohne
diese Zählung blockiert eine einzige defekte Datei den gesamten Ablauf, weil
`restart: unless-stopped` den Container neu startet und dasselbe Video wieder
an der Reihe wäre.

Bekannte Fälle, die abgefangen sind:

- **AMR-Ton** in alten 3GP-Dateien — MP4 kann ihn nicht aufnehmen, er wird zu
  AAC umkodiert. Ein pauschales `-c:a copy` lässt ffmpeg hier ohne Ausgabedatei
  abbrechen.
- **SD-Farbmatrix** — Material unter 720p folgt BT.601, nicht BT.709. Eine
  pauschale 709-Annahme verschiebt die Farben sichtbar.
- **Container-Rotation** — wird aus den Frame-Properties übernommen, damit
  Hochkantvideos nicht quer landen.

## Lizenz

MIT
