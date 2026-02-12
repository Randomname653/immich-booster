import vapoursynth as vs
import os
from processor import run_boost

core = vs.core
source_path = os.environ.get('VS_SOURCE')

if not source_path:
    raise ValueError("Keine Source angegeben")

# Verwende die echte Pipeline aus processor.py
clip = run_boost(source_path, None)
clip.set_output()
