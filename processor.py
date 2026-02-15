import vapoursynth as vs
core = vs.core
core.max_cache_size = 4000
from vapoursynth import core
import os

try:
    from vsgan import VSGAN
    _vsgan_available = True
except Exception:
    _vsgan_available = False

# Globale Settings
_threads = int(os.environ.get("VS_THREADS", "12"))
_cache_mb = int(os.environ.get("VS_CACHE_MB", "32000"))
core.num_threads = _threads
core.max_cache_size = _cache_mb

def run_boost(input_path, output_path):
    """
    Smarte Pipeline:
    - 4K Input -> Nur Denoising & Cleaning (Kein AI Upscale)
    - < 4K Input -> AI Upscale (x2) auf 4K
    """
    # 1. Video laden (FFMS2 ist der stabilste Source-Filter für MP4/MKV)
    clip = core.lsmas.LibavSMASHSource(source=input_path)
    
    # Metadaten prüfen
    in_w = clip.width
    in_h = clip.height
    is_4k_or_larger = (in_w >= 3840 or in_h >= 2160)

    # 2. Konvertieren zu RGB (float) für Bearbeitung
    clip = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s="709")

    # 3. Entscheidung: AI Upscale oder nur Cleaning?
    
    # Pfad zum Modell (Erwartet RealESRGAN_x2plus.pth für beste Performance/Qualität)
    model_path = os.environ.get("VSGAN_MODEL_PATH", "/models/model.pth")
    
    if is_4k_or_larger:
        # === WEG A: Native 4K Bearbeitung ===
        # Kein Upscale, nur Entrauschen.
        # KNLMeansCL ist ein exzellenter GPU-beschleunigter Denoiser.
        # h=0.6 ist mild (für gute Lichtverhältnisse), bei Konzerten evtl. etwas mehr.
        # Wir lassen es auf einem moderaten Allround-Wert.
        print(f"Input ist bereits 4K ({in_w}x{in_h}). Überspringe AI-Upscale, aktiviere Denoising.")
        clip = core.knlm.KNLMeansCL(clip, d=2, a=2, h=0.8, device_type='gpu')
        
    else:
        # === WEG B: Upscaling für SD/HD Material ===
        print(f"Input ist < 4K ({in_w}x{in_h}). Aktiviere AI-Upscale.")
        
        # Versuche VSGAN mit x2 Modell
        if _vsgan_available and os.path.exists(model_path):
            try:
                vsg = VSGAN(device="cuda")
                vsg.load_model(model_path)
                # Tile-Größe: Wichtig um VRAM nicht zu sprengen. 
                # Blackwell hat viel VRAM, wir können 400-512 nehmen.
                clip = vsg.run(clip, chunk=False, tile=512, overlap=16)
            except Exception as e:
                print(f"WARNUNG: VSGAN fehlgeschlagen ({e}). Fallback auf KNLMeansCL.")
                clip = core.knlm.KNLMeansCL(clip, d=2, a=2, h=1.2, device_type='gpu')
        else:
            print("Kein AI-Modell gefunden oder VSGAN nicht verfügbar. Nutze klassischen Denoiser.")
            clip = core.knlm.KNLMeansCL(clip, d=2, a=2, h=1.2, device_type='gpu')

    # 4. Kontrast/Licht-Verbesserung (Mildes "Pop")
    # Gamma 0.95 macht dunkle Bereiche (Konzerte!) minimal heller, ohne Rauschen zu explodieren
    clip = core.std.Levels(clip, gamma=0.95, min_in=0.0, max_in=1.0, min_out=0.0, max_out=1.0, planes=0)

    # 5. Finale Auflösung sicherstellen (Max 4K)
    # Falls wir z.B. 1440p x2 = 5K haben, skalieren wir sanft auf 4K zurück.
    TARGET_W, TARGET_H = 3840, 2160
    if clip.width > TARGET_W or clip.height > TARGET_H:
        clip = core.resize.Bicubic(clip, width=TARGET_W, height=TARGET_H, format=vs.RGBS)

    # 6. Zurück zu YUV420P10 für den Encoder (NVENC liebt 10-bit Input)
    clip = core.resize.Bicubic(clip, format=vs.YUV420P10, matrix_s="709")
    
    return clip
