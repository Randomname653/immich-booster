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

# DEBUG CONFIG
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
DEBUG_LIMIT = 3

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
    if DEBUG_MODE: return True # Debug ignoriert Zeitfenster
    
    now = datetime.now().time()
    if START_TIME < END_TIME:
        return START_TIME <= now <= END_TIME
    return now >= START_TIME or now <= END_TIME

def clean_filename(filename):
    name, ext = os.path.splitext(filename)
    clean_name = re.sub(r'\+\d+$', '', name)
    return f"{clean_name}_boosted{ext}"

def process_video(asset):
    asset_id = asset['id']
    original_filename = asset['originalFileName']
    
    if not os.path.splitext(original_filename)[1]:
        original_filename += ".mp4"

    local_input_path = os.path.join(TEMP_DIR, f"input_{asset_id}.mp4")
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
        print(f"❌ Netzwerkfehler: {e}")
        return False

    os.environ['VS_SOURCE'] = local_input_path
    
    wm = ""
    if WATERMARK_ENABLED:
        wm = f"-vf \"drawtext=text='{WATERMARK_TEXT}':fontcolor=white@{WATERMARK_ALPHA}:fontsize=h/60:x=w-tw-20:y=h-th-20\""

    cmd = (
        f"vspipe -c y4m processor_wrapper.py - | "
        f"ffmpeg -y -i pipe: -i \"{local_input_path}\" {wm} "
        f"-c:v hevc_nvenc -preset p6 -cq 20 -map 0:v:0 -map 1:a? -c:a copy \"{local_output_path}\""
    )
    
    try:
        print("🚀 Boosting Video & Audio...")
        subprocess.run(cmd, shell=True, check=True)
        subprocess.run(["exiftool", "-TagsFromFile", local_input_path, "-all:all", "-FileModifyDate", "-overwrite_original", local_output_path], check=True)
        
        print("⬆️ Uploading...")
        with open(local_output_path, 'rb') as f:
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
                
                print("📚 Stacking...")
                stack_payload = {"assetIds": [new_id, asset_id]}
                requests.post(f"{IMMICH_URL}/stacks", json=stack_payload, headers={"x-api-key": API_KEY})
                return True
            else:
                print(f"❌ Upload failed: {r.text}")
                
    except Exception as e:
        print(f"❌ Fehler bei Verarbeitung: {e}")
    finally:
        if os.path.exists(local_input_path): os.remove(local_input_path)
        if os.path.exists(local_output_path): os.remove(local_output_path)
    
    return False

def main():
    conn = init_db()
    print("🤖 Booster online.")
    if DEBUG_MODE:
        print(f"🐞 DEBUG MODE AKTIV! Bearbeite max. {DEBUG_LIMIT} Videos sofort.")
    else:
        print("🌙 Nachtschicht-Modus: Warte auf Zeitfenster...")

    debug_counter = 0
    
    while True:
        if not is_within_time_window():
            if int(time.time()) % 600 == 0:
                print(f"zzz... {datetime.now().strftime('%H:%M')}", flush=True)
            time.sleep(60)
            continue
            
        print("\n🔍 Suche neue Videos...", flush=True)
        try:
            resp = requests.post(f"{IMMICH_URL}/search/metadata", json={"type": "VIDEO"}, headers={"x-api-key": API_KEY})
            if resp.status_code != 200:
                time.sleep(60); continue
                
            assets = resp.json().get('assets', {}).get('items', [])
            assets.sort(key=lambda x: x['fileCreatedAt'], reverse=True)

            work_done = False
            for asset in assets:
                # Zeitfenster Check (nur wenn kein Debug)
                if not DEBUG_MODE and not is_within_time_window(): break
                
                # Debug Limit Check
                if DEBUG_MODE and debug_counter >= DEBUG_LIMIT:
                    print(f"🛑 DEBUG LIMIT ({DEBUG_LIMIT}) ERREICHT. Stoppe Arbeit.")
                    # Wir schlafen hier ewig, damit der Container nicht neustartet und den Zähler resettet
                    time.sleep(86400) 
                    break

                if is_processed(conn, asset['id']): continue
                
                filename = asset.get('originalFileName', '')
                path = asset.get('originalPath', '')
                
                # SKIP LOOP
                if '_boosted' in filename or '_boosted' in path:
                    mark_processed(conn, asset['id'])
                    continue
                
                # Device Filter
                model = asset.get('deviceInfo', {}).get('model', '')
                if DEVICE_FILTER and DEVICE_FILTER not in model:
                    continue

                print(f"🔨 Gefunden: {filename} ({model})")
                if process_video(asset):
                    mark_processed(conn, asset['id'])
                    work_done = True
                    
                    if DEBUG_MODE:
                        debug_counter += 1
                        print(f"🐞 Debug Status: {debug_counter}/{DEBUG_LIMIT} erledigt.")
                    
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
