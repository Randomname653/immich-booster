"""Bereitschaftspruefung des Containers.

Prueft alles, was die Pipeline zur Laufzeit braucht. Bewusst mit Ausgabe: bei
"unhealthy" soll aus `docker logs` hervorgehen, welcher Teil fehlt, statt nur
einen Rueckgabewert zu sehen.
"""

import os
import shutil
import subprocess
import sys


def check(name, fn):
    try:
        detail = fn()
    except Exception as exc:
        print(f"FEHLT   {name}: {str(exc)[:160]}")
        return False
    print(f"ok      {name}{f' ({detail})' if detail else ''}")
    return True


def _vapoursynth():
    import vapoursynth as vs
    if not hasattr(vs.core, "ffms2"):
        raise RuntimeError("Source-Filter ffms2 nicht geladen")
    return f"Core R{vs.core.version_number()}"


def _torch_cuda():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA nicht verfuegbar - GPU im Container durchgereicht?")
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    # sm_120 (Blackwell) braucht ein cu128-Build; mit aelteren Raedern schlaegt
    # erst die Inferenz fehl, nicht der Import.
    if cap[0] >= 12 and not torch.version.cuda.startswith(("12.8", "12.9", "13")):
        raise RuntimeError(f"{name} ist sm_{cap[0]}{cap[1]}, aber torch fuer CUDA {torch.version.cuda}")
    return f"{name}, sm_{cap[0]}{cap[1]}, torch/CUDA {torch.version.cuda}"


def _basicvsrpp():
    import vsbasicvsrpp
    base = os.path.dirname(vsbasicvsrpp.__file__)
    models = [f for f in os.listdir(base) if f.endswith(".pth")]
    if not models:
        raise RuntimeError("keine Modelldateien vorhanden")
    return f"{len(models)} Modelle"


def _nvenc():
    ff = os.environ.get("FFMPEG", "ffmpeg")
    out = subprocess.run([ff, "-hide_banner", "-encoders"],
                         capture_output=True, text=True, timeout=60).stdout
    if "hevc_nvenc" not in out:
        raise RuntimeError("hevc_nvenc fehlt")
    return "hevc_nvenc"


def _exiftool():
    exe = os.environ.get("EXIFTOOL", "exiftool")
    if not shutil.which(exe):
        raise RuntimeError("nicht im Pfad")
    out = subprocess.run([exe, "-ver"], capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def _writable():
    for path in (os.environ.get("TEMP_DIR", "/app/temp"),
                 os.path.dirname(os.environ.get("DB_PATH", "/app/config/processed.db"))):
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
    return "temp + config beschreibbar"


if __name__ == "__main__":
    results = [
        check("VapourSynth + ffms2", _vapoursynth),
        check("PyTorch / CUDA", _torch_cuda),
        check("BasicVSR++ Modelle", _basicvsrpp),
        check("FFmpeg NVENC", _nvenc),
        check("ExifTool", _exiftool),
        check("Schreibrechte", _writable),
    ]
    sys.exit(0 if all(results) else 1)
