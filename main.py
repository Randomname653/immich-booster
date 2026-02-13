import requests
import os
import subprocess
import shutil
import time
import sqlite3
import re
from datetime import datetime, time as dtime

# KONFIGURATION
IMMICH_URL = os.getenv("IMMICH_URL", "http://localhost:2283/api")
API_KEY = os.getenv("IMMICH_API_KEY", "")
TEMP_DIR = os.getenv("TEMP_DIR", "/app/temp")
DEVICE_FILTER = os.getenv("DEVICE_FILTER", "") 
DB_PATH = "/app/config/processed.db"

# ZEITFENSTER (01:15 - 06:15)
START_TIME = dtime(1, 15)
END_TIME = dtime(6, 15)

# Watermark
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "ARCHIVE PROOF | INTERNAL")
WATERMARK_ALPHA = float(os.getenv("WATERMARK_ALPHA", "0.15"))
WATERMARK_ENABLED = os.getenv("WATERMARK_ENABLED", "true").lower() in ("1", "true", "yes")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS processed_videos
                 (asset_id TEXT PRIMARY KEY, processed_at TIMESTAMP)''')
    conn.commit()
    return conn

def is_processed(conn, asset_id):
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_videos WHERE asset_id = ?", (asset_id,))
    return c.fetchone() is not None

def mark_processed(conn, asset_id):
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO processed_videos (asset_id, processed_at) VALUES (?, ?)", 
              (asset_id, datetime.now()))
    conn.commit()

def is_within_time_window():
    now = datetime.now().time()
    if START_TIME < END_TIME:
        return START_TIME <= now <= END_TIME
    return now >= START_TIME or now <= END_TIME

def clean_filename(filename):
    """Entfernt Immich Duplikat-Zaehler wie '+1', '+55' aus dem Namen"""
    name, ext = os.path.splitext(filename)
    # Entfernt "+Zahl" am Ende des Namens
    clean_name = re.sub(r'\+\d+$', '', name)
    return f"{clean_name}_boosted{ext}"

def process_video(asset):
    asset_id = asset['id']
    original_filename = asset['originalFileName']
    
    # Sicherstellen, dass wir eine Extension haben
    if not os.path.splitext(original_filename)[1]:
        original_filename += ".mp4" # Fallback

    local_input_path = os.path.join(TEMP_DIR, f"input_{asset_id}.mp4")
    
    # Sauberer Ausgabename ohne +1 etc.
    output_filename = clean_filename(original_filename)
    local_output_path = os.path.join(TEMP_DIR, output_filename)

    print(f"📥 Downloading {original_filename}...")
    url = f"{IMMICH_URL}/assets/{asset_id}/original"
    try:
        with requests.get(url, headers={"x-api-key": API_KEY}, stream=True, timeout=120) as r:
            if r.status_code != 200:
                print(f"❌ Download fehlgeschlagen: {r.status_code}")
                return False
            with open(local_input_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
    except Exception as e:
        print(f"❌ Netzwerkfehler beim Download: {e}")
        return False

    os.environ['VS_SOURCE'] = local_input_path
    
    # Wasserzeichen Filter
    wm = ""
    if WATERMARK_ENABLED:
        wm = f"-vf \"drawtext=text='{WATERMARK_TEXT}':fontcolor=white@{WATERMARK_ALPHA}:fontsize=h/60:x=w-tw-20:y=h-th-20\""

    # AUDIO & VIDEO PROCESSING
    # -map 0:v:0 -> Video von Pipe (VapourSynth)
    # -map 1:a?  -> Audio von Originaldatei (falls vorhanden)
    cmd = (
        f"vspipe -c y4m processor_wrapper.py - | "
        f"ffmpeg -y -i pipe: -i \"{local_input_path}\" {wm} "
        f"-c:v hevc_nvenc -preset p6 -cq 20 -map 0:v:0 -map 1:a? -c:a copy \"{local_output_path}\""
    )
    
    try:
        print("🚀 Boosting Video & Audio...")
        subprocess.run(cmd, shell=True, check=True)
        
        # Metadaten kopieren
        subprocess.run(["exiftool", "-TagsFromFile", local_input_path, "-all:all", "-FileModifyDate", "-overwrite_original", local_output_path], check=True)
        
        # Upload
        print("⬆️ Uploading...")
        with open(local_output_path, 'rb') as f:
            # DeviceAssetID eindeutig machen
            device_asset_id = f"{asset['deviceAssetId']}-boosted-{int(time.time())}"
            
            data = {
                'deviceAssetId': device_asset_id,
                'deviceId': asset['deviceId'],
                'fileCreatedAt': asset['fileCreatedAt'],
                'fileModifiedAt': asset['fileModifiedAt'],
                'isFavorite': str(asset['isFavorite']).lower(),
                'duration': asset.get('duration', '0:00:00')
            }
            
            r = requests.post(f"{IMMICH_URL}/assets", headers={"x-api-key": API_KEY}, files={'assetData': f}, data=data)
            
            if r.status_code in [200, 201]:
                new_id = r.json()['id']
                print(f"✅ Upload success! New ID: {new_id}")
                
                # STACKING (Correct endpoint: POST /stacks)
                print("📚 Stacking...")
                stack_payload = {
                    "assetIds": [new_id, asset_id] # [Primary (Boosted), Secondary (Original)]
                }
                s = requests.post(f"{IMMICH_URL}/stacks", json=stack_payload, headers={"x-api-key": API_KEY})
                if s.status_code in [200, 201]:
                     print("✅ Stack created.")
                else:
                     print(f"⚠️ Stacking failed: {s.text}")

                return True
            else:
                print(f"❌ Upload failed: {r.text}")
                
    except Exception as e:
        print(f"❌ Fehler bei Verarbeitung: {e}")
    finally:
        # Cleanup
        if os.path.exists(local_input_path): os.remove(local_input_path)
        if os.path.exists(local_output_path): os.remove(local_output_path)
    
    return False

def main():
    conn = init_db()
    print("🤖 Booster online. Warte auf Nachtschicht...")
    
    while True:
        if not is_within_time_window():
            # Kurzer Log alle 10 min damit man sieht dass er lebt
            if int(time.time()) % 600 == 0:
                print(f"zzz... {datetime.now().strftime('%H:%M')}", flush=True)
            time.sleep(60)
            continue
            
        print("\n🔍 Suche neue Videos...", flush=True)
        try:
            resp = requests.post(f"{IMMICH_URL}/search/metadata", json={"type": "VIDEO"}, headers={"x-api-key": API_KEY})
            if resp.status_code != 200:
                print(f"API Fehler: {resp.status_code}")
                time.sleep(60)
                continue
                
            assets = resp.json().get('assets', {}).get('items', [])
            # Sortieren: Neueste zuerst
            assets.sort(key=lambda x: x['fileCreatedAt'], reverse=True)

            work_done = False
            for asset in assets:
                if not is_within_time_window(): break
                
                # 1. Check: Schon bearbeitet?
                if is_processed(conn, asset['id']):
                    continue
                
                # 2. Check: Ist das file selbst schon ein Boost? (Namens-Check)
                filename = asset.get('originalFileName', '')
                path = asset.get('originalPath', '')
                
                # WICHTIG: Überspringe alles was '_boosted' heißt
                if '_boosted' in filename or '_boosted' in path:
                    # Wir markieren es als processed in der DB, damit wir es nicht jedes Mal prüfen müssen
                    mark_processed(conn, asset['id'])
                    continue
                
                # 3. Check: Device Filter
                model = asset.get('deviceInfo', {}).get('model', '')
                if DEVICE_FILTER and DEVICE_FILTER not in model:
                    continue

                # ALLES OK -> PROCESS
                print(f"🔨 Gefunden: {filename} ({model})")
                if process_video(asset):
                    mark_processed(conn, asset['id'])
                    work_done = True
                    # Kurze Pause damit Server atmen kann
                    time.sleep(5)
            
            if not work_done:
                print("😴 Keine Arbeit. Warte 5 Minuten...")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Main Loop Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
    main()
