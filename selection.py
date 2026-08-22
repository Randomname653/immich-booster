"""Auswahl und Priorisierung der Videos, die eine Bearbeitung lohnen.

Der Vorgaenger filterte ueber DEVICE_FILTER auf das Kameramodell. Das ist aus
zwei Gruenden untauglich: In der Praxis tragen nur rund 10 Prozent der Videos
ueberhaupt Kamera-EXIF, und ausgerechnet die neuen Handyaufnahmen (4K, hohe
Bitrate) brauchen die Bearbeitung am wenigsten. Massgeblich ist stattdessen,
wie stark das Material beschaedigt ist.
"""

BUCKET_UPSCALE = "A"   # klein und komprimiert: entstoeren und vergroessern
BUCKET_CLEAN = "B"     # ausreichend gross, aber stark komprimiert: nur entstoeren
BUCKET_BORDER = "C"    # Grenzfall
BUCKET_SKIP = "D"      # gutes Material, nicht anfassen


def healthy_mbit(width, height):
    """Bitrate, die eine H.264-Quelle dieser Groesse bei ~30 fps haben sollte."""
    return max(0.8, (width * height) / 1e6 * 5.5)


def describe(asset):
    """Rohdaten eines Immich-Assets in die Felder uebersetzen, die wir brauchen."""
    ex = asset.get("exifInfo") or {}
    w = asset.get("width") or ex.get("exifImageWidth") or 0
    h = asset.get("height") or ex.get("exifImageHeight") or 0
    # Immich 3.x liefert die Dauer als Millisekunden-Zahl.
    raw = asset.get("duration") or 0
    if isinstance(raw, str):
        parts = raw.split(":")
        try:
            dur = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except (ValueError, IndexError):
            dur = 0.0
    else:
        dur = float(raw) / 1000.0
    size = int(ex.get("fileSizeInByte") or 0)
    mbit = (size * 8 / dur / 1e6) if dur > 0.4 else 0.0
    return {
        "id": asset["id"],
        "name": asset.get("originalFileName") or "",
        "width": w,
        "height": h,
        "short_edge": min(w, h) if (w and h) else 0,
        "duration": dur,
        "size": size,
        "mbit": mbit,
        "favorite": bool(asset.get("isFavorite")),
        "stacked": bool(asset.get("stack")),
        "trashed": bool(asset.get("isTrashed")),
    }


def classify(info, cfg):
    """Einordnen, ob und wie stark ein Video bearbeitet werden soll."""
    w, h, se = info["width"], info["height"], info["short_edge"]
    if not (w and h) or info["duration"] <= 0.4:
        return BUCKET_SKIP, "keine brauchbaren Metadaten"
    if info["trashed"]:
        return BUCKET_SKIP, "im Papierkorb"
    if info["stacked"]:
        return BUCKET_SKIP, "bereits gestapelt"
    if se >= cfg.skip_short_edge:
        return BUCKET_SKIP, f"bereits {se}p"
    if info["mbit"] >= cfg.skip_mbit:
        return BUCKET_SKIP, f"hohe Bitrate ({info['mbit']:.1f} Mbit/s)"

    ratio = info["mbit"] / healthy_mbit(w, h)
    if se < 720:
        return BUCKET_UPSCALE, f"{se}p, {info['mbit']:.1f} Mbit/s"
    if ratio < 0.55:
        return BUCKET_CLEAN, f"stark komprimiert ({ratio:.0%} der ueblichen Bitrate)"
    if ratio < 0.80:
        return BUCKET_BORDER, f"maessig komprimiert ({ratio:.0%})"
    return BUCKET_SKIP, "Qualitaet ausreichend"


def target_size(info, cfg):
    """Zielaufloesung bestimmen: hochskalieren, aber mit Augenmass.

    Ein 176x144-Video auf 4K zu ziehen ergibt keinen Sinn. Wir deckeln auf
    eine sinnvolle kurze Kante und auf einen maximalen Faktor.
    """
    w, h, se = info["width"], info["height"], info["short_edge"]
    if se <= 0:
        return w, h
    factor = min(cfg.max_scale, cfg.target_short_edge / se)
    if factor <= 1.02:
        return w, h
    # Gerade Kantenlaengen, sonst mag der Encoder nicht.
    return (int(w * factor) // 2 * 2, int(h * factor) // 2 * 2)


def priority(info, bucket):
    """Sortierschluessel: je schlechter das Material, desto frueher dran.

    Favoriten kommen zuerst, damit die Videos, die dem Nutzer wichtig sind,
    nicht am Ende einer wochenlangen Warteschlange stehen.
    """
    bucket_rank = {BUCKET_UPSCALE: 0, BUCKET_CLEAN: 1, BUCKET_BORDER: 2}.get(bucket, 9)
    return (0 if info["favorite"] else 1, bucket_rank, info["short_edge"], info["duration"])


def plan(assets, cfg, is_done):
    """Aus allen Assets die Arbeitsliste bauen, beste Kandidaten zuerst."""
    todo, stats = [], {}
    for asset in assets:
        info = describe(asset)
        bucket, reason = classify(info, cfg)
        stats[bucket] = stats.get(bucket, 0) + 1
        if bucket == BUCKET_SKIP:
            continue
        if is_done(info["id"]):
            continue
        if cfg.only_favorites and not info["favorite"]:
            continue
        info["bucket"] = bucket
        info["reason"] = reason
        info["target"] = target_size(info, cfg)
        todo.append(info)
    todo.sort(key=lambda i: priority(i, i["bucket"]))
    return todo, stats
