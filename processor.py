"""VapourSynth-Pipeline: temporale Restauration und Skalierung.

Wird von vspipe geladen und liest seine Parameter aus der Umgebung, weil
vspipe keine eigenen Argumente an das Skript durchreicht.

Verfahren: BasicVSR++ "NTIRE 2021 Quality Enhancement of Compressed Video,
Track 3". Das Modell arbeitet temporal, zieht seine Information also aus den
Nachbarframes statt Details zu erfinden. Anschliessend wird klassisch mit
Lanczos vergroessert. Bewusst KEIN KI-Upscaler: die getesteten Kandidaten
(SwinIR, DPIR) erzeugen einen glattgebuegelten Plastik-Eindruck, der schlechter
aussieht als das Original.
"""

import os

import vapoursynth as vs

core = vs.core

_ROOT = os.environ.get("VS_PLUGIN_ROOT", "")


# Uebliche Ablageorte des Source-Filters. Auf den Autoload-Mechanismus allein
# ist kein Verlass: er haengt an Umgebungsvariablen und einer Konfigurationsdatei,
# und faellt er aus, meldet VapourSynth lediglich ein unbekanntes Attribut.
_FFMS2_CANDIDATES = (
    "/usr/local/lib/libffms2.so",
    "/usr/local/lib/vapoursynth/libffms2.so",
    "/usr/lib/vapoursynth/libffms2.so",
    "/usr/lib/x86_64-linux-gnu/vapoursynth/libffms2.so",
)


def _plugin_dir():
    """Verzeichnis, aus dem VapourSynth selbsttaetig laedt.

    Es liegt neben dem Python-Modul, nicht unter /usr/local/lib/vapoursynth -
    fragen ist zuverlaessiger als raten.
    """
    try:
        import vapoursynth
        base = os.path.dirname(vapoursynth.__file__)
        candidate = os.path.join(base, "plugins")
        if os.path.isdir(candidate):
            return candidate
    except Exception:
        pass
    return None


def _load_plugins():
    """Source-Filter sicherstellen, notfalls ueber den Dateipfad."""
    if _ROOT:
        # Portables Windows-Setup: Backend-Bibliotheken in den DLL-Suchpfad.
        for sub in ("vsmlrt/vsort", "vsmlrt/vsmlrt-cuda", "vsmlrt"):
            path = os.path.join(_ROOT, *sub.split("/"))
            if os.path.isdir(path):
                try:
                    os.add_dll_directory(path)
                except (AttributeError, OSError):
                    pass
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")

    if hasattr(core, "ffms2"):
        return

    plugin_dir = _plugin_dir()
    candidates = [os.environ.get("VS_FFMS2")]
    if plugin_dir:
        candidates.append(os.path.join(plugin_dir, "libffms2.so"))
    candidates += list(_FFMS2_CANDIDATES)

    tried = []
    for path in candidates:
        if not path:
            continue
        tried.append(path)
        if os.path.exists(path):
            core.std.LoadPlugin(path)
            if hasattr(core, "ffms2"):
                return
    raise RuntimeError(
        "Source-Filter ffms2 nicht gefunden. Gesucht in:\n  " + "\n  ".join(tried)
        + "\nEntweder VS_FFMS2 auf die Bibliothek zeigen lassen oder "
          "VAPOURSYNTH_PLUGIN_PATH pruefen."
    )


def _input_matrix(clip):
    """Farbmatrix der Quelle bestimmen.

    Standard-definition-Material folgt BT.601, HD und groesser BT.709. Der
    Vorgaenger nahm pauschal 709 an und verschob damit bei jedem SD-Video die
    Farben. Traegt der Clip die Matrix selbst im Frame, hat diese Vorrang.
    """
    try:
        props = clip.get_frame(0).props
        matrix = props.get("_Matrix")
        # 2 = "unspecified", damit koennen wir nichts anfangen.
        if matrix is not None and matrix != 2:
            return None  # bereits getaggt, resize uebernimmt es selbst
    except Exception:
        pass
    return "470bg" if min(clip.width, clip.height) < 720 else "709"


def _apply_rotation(clip):
    """Container-Rotation anwenden, die ffms2 als Frame-Property meldet."""
    try:
        rot = int(clip.get_frame(0).props.get("_Rotation", 0) or 0)
    except Exception:
        return clip
    rot %= 360
    if rot == 90:
        return core.std.Transpose(core.std.FlipVertical(clip))
    if rot == 180:
        return core.std.FlipVertical(core.std.FlipHorizontal(clip))
    if rot == 270:
        return core.std.FlipVertical(core.std.Transpose(clip))
    return clip


def build(source, target_w=0, target_h=0, restore=True, model=6,
          length=15, tile=0, cpu_cache=False):
    """Verarbeitungskette aufbauen und den fertigen Clip zurueckgeben."""
    _load_plugins()

    clip = core.ffms2.Source(source=source, cache=False)
    clip = _apply_rotation(clip)

    matrix_in = _input_matrix(clip)
    if matrix_in:
        rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s=matrix_in)
    else:
        rgb = core.resize.Bicubic(clip, format=vs.RGBS)

    if restore:
        from vsbasicvsrpp import basicvsrpp
        rgb = basicvsrpp(
            rgb,
            model=model,
            length=length,
            tile=[tile, tile] if tile else [0, 0],
            tile_pad=16,
            cpu_cache=cpu_cache,
            # Rueckfallebene: konnte das Modell beim Bauen des Images nicht
            # mitgeliefert werden, wird es beim ersten Lauf nachgeladen. Das
            # dauert einmalig, ist aber besser als ein Abbruch.
            auto_download=True,
        )

    if target_w and target_h and (target_w != rgb.width or target_h != rgb.height):
        rgb = core.resize.Lanczos(rgb, target_w, target_h, filter_param_a=3)

    # 10 bit, weil NVENC damit sauberer quantisiert und Banding vermeidet.
    return core.resize.Bicubic(rgb, format=vs.YUV420P10, matrix_s="709")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# vspipe laedt dieses Modul als Skript: Ausgabe direkt setzen.
if "VS_SOURCE" in os.environ:
    build(
        source=os.environ["VS_SOURCE"],
        target_w=_env_int("VS_TARGET_W", 0),
        target_h=_env_int("VS_TARGET_H", 0),
        restore=os.environ.get("VS_RESTORE", "1") not in ("0", "false", "no"),
        model=_env_int("VS_MODEL", 6),
        length=_env_int("VS_LENGTH", 15),
        tile=_env_int("VS_TILE", 0),
        cpu_cache=os.environ.get("VS_CPU_CACHE", "0") in ("1", "true", "yes"),
    ).set_output()
