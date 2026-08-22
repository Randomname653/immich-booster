"""Immich Video Booster - Hauptschleife.

Holt Videos aus Immich, restauriert sie temporal, laedt sie zurueck und
stapelt sie mit dem Original. Das Original bleibt dabei immer erhalten.
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, time as dtime

# Eigenes Verzeichnis in den Suchpfad: ein eingebettetes Python (portables
# Setup) laeuft isoliert und ignoriert PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import encode
import selection
# processor.py wird NICHT importiert: vspipe laedt es als eigenes Skript in
# einem eigenen Prozess. Ein Import hier wuerde VapourSynth unnoetig in den
# Hauptprozess ziehen und bei gesetztem VS_SOURCE sogar eine Kette aufbauen.
from immich import Immich, ImmichError

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("booster")


def _flag(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _clock(name, default):
    raw = os.environ.get(name, default).strip()
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except ValueError:
        hh, mm = default.split(":")
        return dtime(int(hh), int(mm))


class Config:
    def __init__(self):
        self.url = os.environ.get("IMMICH_URL", "http://localhost:2283")
        self.key = os.environ.get("IMMICH_API_KEY", "")
        self.temp = os.environ.get("TEMP_DIR", "/app/temp")
        self.db_path = os.environ.get("DB_PATH", "/app/config/processed.db")

        # Auswahl
        self.skip_short_edge = _num("SKIP_IF_SHORT_EDGE_GTE", 2160, int)
        self.skip_mbit = _num("SKIP_IF_MBIT_GTE", 45, float)
        self.target_short_edge = _num("TARGET_SHORT_EDGE", 1080, int)
        self.max_scale = _num("MAX_SCALE", 4, float)
        self.only_favorites = _flag("ONLY_FAVORITES")

        # Verarbeitung
        self.model = _num("VS_MODEL", 6, int)
        self.length = _num("VS_LENGTH", 15, int)
        self.tile = _num("VS_TILE", 0, int)
        self.cpu_cache = _flag("VS_CPU_CACHE")
        self.restore = _flag("RESTORE", "1")

        # Werkzeuge
        self.ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
        self.ffprobe = os.environ.get("FFPROBE", "ffprobe")
        self.vspipe = os.environ.get("VSPIPE", "vspipe")
        self.exiftool = os.environ.get("EXIFTOOL", "exiftool")
        self.script = os.environ.get("VS_SCRIPT", os.path.join(os.path.dirname(__file__), "processor.py"))
        self.font_file = os.environ.get("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

        # Encoder
        self.encoder = os.environ.get("ENCODER", "hevc_nvenc")
        self.preset = os.environ.get("PRESET", "p6")
        self.cq = _num("CQ", 22, int)
        self.job_timeout = _num("JOB_TIMEOUT", 7200, int)

        # Kennzeichnung
        self.watermark = _flag("WATERMARK_ENABLED", "1")
        self.watermark_text = os.environ.get("WATERMARK_TEXT", "AI")
        self.watermark_alpha = _num("WATERMARK_ALPHA", 0.35, float)
        self.tag_name = os.environ.get("TAG_NAME", "AI-enhanced")

        # Betrieb
        self.dry_run = _flag("DRY_RUN", "1")
        self.night_start = _clock("NIGHT_START", "01:15")
        self.night_end = _clock("NIGHT_END", "06:15")
        self.always_on = _flag("IGNORE_TIME_WINDOW")
        self.max_per_run = _num("MAX_PER_RUN", 0, int)
        self.idle_sleep = _num("IDLE_SLEEP", 300, int)

    def in_window(self):
        if self.always_on:
            return True
        now = datetime.now().time()
        if self.night_start < self.night_end:
            return self.night_start <= now <= self.night_end
        return now >= self.night_start or now <= self.night_end


class Ledger:
    """Merkt sich, was erledigt ist - und was dauerhaft scheitert.

    Ohne die Fehlerzaehlung wuerde ein einzelnes kaputtes Video die Schleife
    endlos blockieren: Prozess stirbt, Container startet neu, dasselbe Video
    ist wieder das naechste.
    """

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS processed ("
            " asset_id TEXT PRIMARY KEY, new_id TEXT, at TIMESTAMP,"
            " src_bytes INTEGER, out_bytes INTEGER, seconds REAL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS failed ("
            " asset_id TEXT PRIMARY KEY, tries INTEGER, last TEXT, at TIMESTAMP)"
        )
        self.conn.commit()

    def done(self, asset_id, max_tries=3):
        cur = self.conn.execute("SELECT 1 FROM processed WHERE asset_id=?", (asset_id,))
        if cur.fetchone():
            return True
        cur = self.conn.execute("SELECT tries FROM failed WHERE asset_id=?", (asset_id,))
        row = cur.fetchone()
        return bool(row and row[0] >= max_tries)

    def mark_done(self, asset_id, new_id, src_bytes, out_bytes, seconds):
        self.conn.execute(
            "INSERT OR REPLACE INTO processed VALUES (?,?,?,?,?,?)",
            (asset_id, new_id, datetime.now(), src_bytes, out_bytes, seconds),
        )
        self.conn.execute("DELETE FROM failed WHERE asset_id=?", (asset_id,))
        self.conn.commit()

    def mark_failed(self, asset_id, message):
        cur = self.conn.execute("SELECT tries FROM failed WHERE asset_id=?", (asset_id,))
        row = cur.fetchone()
        tries = (row[0] if row else 0) + 1
        self.conn.execute(
            "INSERT OR REPLACE INTO failed VALUES (?,?,?,?)",
            (asset_id, tries, str(message)[:500], datetime.now()),
        )
        self.conn.commit()
        return tries

    def stats(self):
        p = self.conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        f = self.conn.execute("SELECT COUNT(*) FROM failed").fetchone()[0]
        return p, f


def process_one(api, cfg, ledger, info):
    """Ein Video verarbeiten. Wirft nur, wenn wirklich nichts zu retten ist."""
    started = time.time()
    base = os.path.splitext(os.path.basename(info["name"] or info["id"]))[0]
    src = os.path.join(cfg.temp, f"{info['id']}_src")
    dst = os.path.join(cfg.temp, f"{base}_boosted.mp4")

    tw, th = info["target"]
    log.info("  %s  %dx%d -> %dx%d  (%s: %s)",
             info["name"][:48], info["width"], info["height"], tw, th,
             info["bucket"], info["reason"])

    try:
        api.download_original(info["id"], src)

        env = {
            "VS_SOURCE": src,
            "VS_TARGET_W": tw,
            "VS_TARGET_H": th,
            "VS_MODEL": cfg.model,
            "VS_LENGTH": cfg.length,
            "VS_TILE": cfg.tile,
            "VS_CPU_CACHE": "1" if cfg.cpu_cache else "0",
            "VS_RESTORE": "1" if cfg.restore else "0",
        }
        encode.run_pipeline(src, dst, cfg.script, env, cfg)

        ok, why = encode.verify(src, dst, cfg)
        if not ok:
            raise encode.EncodeError(f"Qualitaetspruefung: {why}")

        encode.clone_metadata(src, dst, cfg)

        src_bytes = os.path.getsize(src)
        out_bytes = os.path.getsize(dst)
        seconds = time.time() - started

        if cfg.dry_run:
            log.info("    [Probelauf] fertig in %.0fs, %.1f -> %.1f MB, nichts hochgeladen",
                     seconds, src_bytes / 1e6, out_bytes / 1e6)
            return None

        new_id, _ = api.upload(dst, {"id": info["id"], "fileCreatedAt": info["_created"],
                                     "fileModifiedAt": info["_modified"],
                                     "deviceAssetId": info.get("_device_asset"),
                                     "deviceId": info.get("_device")})
        try:
            api.stack(info["id"], new_id)
        except ImmichError as exc:
            # Nicht gestapelt heisst: loses Duplikat in der Bibliothek. Das
            # muss sichtbar sein, nicht still durchgehen.
            log.error("    Stapeln fehlgeschlagen (%s) - %s liegt lose in der Bibliothek",
                      exc, new_id)
        if cfg.tag_name:
            try:
                api.add_tag([new_id], cfg.tag_name)
            except ImmichError as exc:
                log.warning("    Tag nicht gesetzt: %s", exc)

        log.info("    fertig in %.0fs, %.1f -> %.1f MB, gestapelt",
                 seconds, src_bytes / 1e6, out_bytes / 1e6)
        ledger.mark_done(info["id"], new_id, src_bytes, out_bytes, seconds)
        return new_id
    finally:
        for path in (src, dst, src + ".part"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def run_local(paths):
    """Eine oder mehrere lokale Dateien verarbeiten, ohne Immich.

    Zum Pruefen der Bildkette und zum Messen des Durchsatzes, bevor der
    Dienst auf die Bibliothek losgelassen wird.
    """
    cfg = Config()
    os.makedirs(cfg.temp, exist_ok=True)
    rc = 0
    for path in paths:
        if not os.path.exists(path):
            log.error("nicht gefunden: %s", path)
            rc = 1
            continue
        info = encode.probe(path, cfg.ffprobe)
        vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
        dur = float(info.get("format", {}).get("duration") or 0)
        size = int(info.get("format", {}).get("size") or 0)
        item = {
            "id": os.path.basename(path), "name": os.path.basename(path),
            "width": w, "height": h, "short_edge": min(w, h) if w and h else 0,
            "duration": dur, "size": size,
            "mbit": (size * 8 / dur / 1e6) if dur > 0.4 else 0.0,
            "favorite": False, "stacked": False, "trashed": False,
        }
        bucket, reason = selection.classify(item, cfg)
        tw, th = selection.target_size(item, cfg)
        out = os.path.join(cfg.temp, os.path.splitext(os.path.basename(path))[0] + "_boosted.mp4")
        log.info("%s  %dx%d %.1f Mbit/s  -> Einstufung %s (%s)",
                 item["name"], w, h, item["mbit"], bucket, reason)
        if bucket == selection.BUCKET_SKIP:
            log.info("    wuerde uebersprungen - verarbeite trotzdem, da ausdruecklich angefordert")
        log.info("    Ziel: %dx%d", tw, th)

        started = time.time()
        env = {
            "VS_SOURCE": path, "VS_TARGET_W": tw, "VS_TARGET_H": th,
            "VS_MODEL": cfg.model, "VS_LENGTH": cfg.length, "VS_TILE": cfg.tile,
            "VS_CPU_CACHE": "1" if cfg.cpu_cache else "0",
            "VS_RESTORE": "1" if cfg.restore else "0",
        }
        try:
            stats = encode.run_pipeline(path, out, cfg.script, env, cfg)
            seconds = time.time() - started
            ok, why = encode.verify(path, out, cfg)
            encode.clone_metadata(path, out, cfg)
            fps = ""
            for token in (stats or "").split():
                if token.endswith("fps)"):
                    fps = token.strip("()fps)")
            log.info("    %s in %.0fs%s  ->  %.1f MB   Pruefung: %s",
                     "fertig" if ok else "MIT BEANSTANDUNG", seconds,
                     f" ({fps} fps)" if fps else "",
                     os.path.getsize(out) / 1e6 if os.path.exists(out) else 0, why)
            log.info("    Ergebnis: %s", out)
            if not ok:
                rc = 1
        except Exception as exc:
            log.error("    fehlgeschlagen: %s", str(exc)[:600])
            rc = 1
    return rc


def main():
    cfg = Config()
    if not cfg.key:
        log.error("IMMICH_API_KEY fehlt - ohne Schluessel geht nichts.")
        return 1
    os.makedirs(cfg.temp, exist_ok=True)

    api = Immich(cfg.url, cfg.key)
    try:
        version, who = api.ping()
        log.info("Immich %s erreichbar, angemeldet als %s", version, who)
    except ImmichError as exc:
        log.error("Immich nicht erreichbar: %s", exc)
        return 1

    ledger = Ledger(cfg.db_path)
    done, failed = ledger.stats()
    log.info("Bisher bearbeitet: %d   dauerhaft fehlgeschlagen: %d", done, failed)
    if cfg.dry_run:
        log.info("PROBELAUF aktiv - es wird nichts hochgeladen (DRY_RUN=false zum Scharfschalten)")

    while True:
        if not cfg.in_window():
            time.sleep(60)
            continue

        try:
            assets = list(api.iter_videos())
        except ImmichError as exc:
            log.error("Abruf fehlgeschlagen: %s", exc)
            time.sleep(cfg.idle_sleep)
            continue

        todo, stats = selection.plan(assets, cfg, ledger.done)
        log.info("Einstufung: %s   |   offen: %d",
                 ", ".join(f"{k}={v}" for k, v in sorted(stats.items())), len(todo))

        if not todo:
            log.info("Nichts zu tun. Naechste Pruefung in %ds.", cfg.idle_sleep)
            time.sleep(cfg.idle_sleep)
            continue

        count = 0
        for info in todo:
            if not cfg.in_window():
                log.info("Zeitfenster zu Ende - Rest beim naechsten Mal.")
                break
            if cfg.max_per_run and count >= cfg.max_per_run:
                log.info("Obergrenze von %d Videos erreicht.", cfg.max_per_run)
                break

            # Felder, die erst beim Upload gebraucht werden, aus dem Rohasset holen.
            raw = next((a for a in assets if a["id"] == info["id"]), {})
            info["_created"] = raw.get("fileCreatedAt")
            info["_modified"] = raw.get("fileModifiedAt")
            info["_device_asset"] = raw.get("deviceAssetId")
            info["_device"] = raw.get("deviceId")

            try:
                process_one(api, cfg, ledger, info)
                count += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # ein kaputtes Video darf den Dienst nicht beenden
                tries = ledger.mark_failed(info["id"], exc)
                log.error("    fehlgeschlagen (Versuch %d): %s", tries, str(exc)[:300])

        if cfg.dry_run and count:
            log.info("Probelauf beendet: %d Videos gerechnet.", count)
            return 0
        time.sleep(5)


if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        if args and args[0] in ("--file", "-f"):
            sys.exit(run_local(args[1:]))
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        log.info("Abbruch durch Benutzer.")
        sys.exit(130)
