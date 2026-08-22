"""Encoding und Qualitaetskontrolle.

vspipe liefert das bearbeitete Bild, ffmpeg kodiert es und legt Ton sowie
Metadaten des Originals dazu. Die Prozesse werden ueber eine Pipe verbunden,
ohne Shell - Dateinamen aus Immich koennen Anfuehrungszeichen und Semikolons
enthalten und wuerden ein zusammengebautes Kommando zerlegen.
"""

import json
import logging
import os
import subprocess

log = logging.getLogger("encode")

MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


class EncodeError(RuntimeError):
    pass


def probe(path, ffprobe="ffprobe"):
    """Streams und Format einer Datei auslesen."""
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format",
           "-of", "json", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise EncodeError(f"ffprobe fehlgeschlagen: {out.stderr[:300]}")
    return json.loads(out.stdout or "{}")


def audio_args(path, ffprobe="ffprobe"):
    """Passende Tonbehandlung waehlen.

    Alte 3GP-Aufnahmen tragen AMR-NB, das ein MP4-Container nicht aufnehmen
    kann. Ein pauschales "-c:a copy" laesst ffmpeg dort ohne Ausgabedatei
    abbrechen. Solche Tonspuren werden deshalb nach AAC umkodiert.
    """
    try:
        info = probe(path, ffprobe)
    except EncodeError:
        return ["-map", "1:a?", "-c:a", "aac", "-b:a", "160k"], "unbekannt -> AAC"
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return ["-an"], "kein Ton"
    codec = (audio[0].get("codec_name") or "").lower()
    if codec in MP4_SAFE_AUDIO:
        return ["-map", "1:a?", "-c:a", "copy"], f"{codec} kopiert"
    return ["-map", "1:a?", "-c:a", "aac", "-b:a", "160k"], f"{codec} -> AAC"


def watermark_filter(cfg):
    """Kennzeichnung fuer KI-bearbeitetes Material.

    Bewusst dauerhaft eingebrannt: das Original bleibt als gestapeltes Asset
    erhalten, die bearbeitete Fassung soll als solche erkennbar sein. Der
    dunkle Rand sorgt dafuer, dass die Kennzeichnung auch auf hellem Grund
    sichtbar bleibt - ohne ihn verschwindet sie im weissen Bildbereich.
    """
    if not cfg.watermark:
        return []
    text = cfg.watermark_text.replace("\\", "").replace(":", "").replace("'", "")
    parts = [
        f"text='{text}'",
        "fontsize=h/45",
        f"fontcolor=white@{cfg.watermark_alpha}",
        "borderw=2",
        "bordercolor=black@0.3",
        "x=w-tw-h/50",
        "y=h-th-h/50",
    ]
    if cfg.font_file and os.path.exists(cfg.font_file):
        escaped = cfg.font_file.replace("\\", "/").replace(":", "\\:")
        parts.insert(0, f"fontfile='{escaped}'")
    return ["-filter:v:0", "drawtext=" + ":".join(parts)]


def run_pipeline(source, dest, script, env, cfg):
    """vspipe -> ffmpeg ausfuehren und die Ausgabedatei erzeugen."""
    amap, adesc = audio_args(source, cfg.ffprobe)
    log.info("    Ton: %s", adesc)

    vspipe_cmd = [cfg.vspipe, "-c", "y4m", script, "-"]
    ffmpeg_cmd = [
        cfg.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:", "-i", source,
        *watermark_filter(cfg),
        "-c:v", cfg.encoder, "-preset", cfg.preset, "-cq", str(cfg.cq),
        "-pix_fmt", "p010le",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map", "0:v:0", *amap,
        "-movflags", "+faststart",
        dest,
    ]

    penv = dict(os.environ)
    penv.update({k: str(v) for k, v in env.items()})

    vs_proc = subprocess.Popen(vspipe_cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=penv)
    ff_proc = subprocess.Popen(ffmpeg_cmd, stdin=vs_proc.stdout,
                               stderr=subprocess.PIPE)
    # Damit vspipe ein SIGPIPE bekommt, wenn ffmpeg vorzeitig aussteigt.
    vs_proc.stdout.close()

    ff_err = ff_proc.communicate(timeout=cfg.job_timeout)[1]
    vs_err = vs_proc.stderr.read()
    vs_proc.wait(timeout=60)

    if ff_proc.returncode != 0 or not os.path.exists(dest):
        raise EncodeError(
            "ffmpeg: " + (ff_err or b"").decode("utf8", "replace")[:400]
            + " | vspipe: " + (vs_err or b"").decode("utf8", "replace")[-400:]
        )
    return (vs_err or b"").decode("utf8", "replace")


def verify(source, dest, cfg):
    """Pruefen, ob das Ergebnis brauchbar ist.

    Diese Kontrolle ist nicht optional: Beim Test lieferten zwei von drei
    Inferenz-Backends stillschweigend schwarze oder NaN-Bilder, ohne dass
    irgendein Prozess einen Fehler meldete. Ohne Pruefung landet so etwas
    unbemerkt in der Bibliothek.
    """
    if not os.path.exists(dest) or os.path.getsize(dest) < 10_000:
        return False, "Ausgabedatei fehlt oder ist winzig"

    src_info = probe(source, cfg.ffprobe)
    dst_info = probe(dest, cfg.ffprobe)

    def video_stream(info):
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                return s
        return {}

    sv, dv = video_stream(src_info), video_stream(dst_info)
    if not dv:
        return False, "kein Videostream in der Ausgabe"

    src_dur = float(src_info.get("format", {}).get("duration") or 0)
    dst_dur = float(dst_info.get("format", {}).get("duration") or 0)
    if src_dur > 1 and abs(dst_dur - src_dur) > max(1.0, src_dur * 0.05):
        return False, f"Laufzeit weicht ab ({dst_dur:.1f}s statt {src_dur:.1f}s)"

    # Mittlere Helligkeit ueber Stichproben: faengt komplett schwarze Ergebnisse.
    cmd = [cfg.ffmpeg, "-v", "error", "-i", dest,
           "-vf", "select='not(mod(n\\,37))',signalstats,metadata=print",
           "-frames:v", "12", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    values = [float(line.split("=")[1]) for line in out.stderr.splitlines()
              if "lavfi.signalstats.YAVG" in line and "=" in line]
    if values:
        avg = sum(values) / len(values)
        if avg < 3.0:
            return False, f"Ausgabe praktisch schwarz (YAVG {avg:.1f})"
        if any(v != v for v in values):  # NaN
            return False, "NaN im Bildsignal"
    return True, "ok"


def clone_metadata(source, dest, cfg):
    """Aufnahmedatum, GPS und Kameradaten vom Original uebernehmen."""
    cmd = [cfg.exiftool, "-TagsFromFile", source, "-all:all",
           "-XMP:Description=AI enhanced (immich-booster)",
           "-overwrite_original", "-q", "-q", dest]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        log.warning("    exiftool: %s", (res.stderr or "")[:200])
        return False
    return True
