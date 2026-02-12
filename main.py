import requests
import os
import subprocess
import shutil
import time
import sqlite3
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

def process_video(asset):
    asset_id = asset['id']
    filename = asset['originalPath'].split('/')[-1]
    original_path = os.path.join(TEMP_DIR, filename)
    boosted_path = os.path.join(TEMP_DIR, os.path.splitext(filename)[0] + "_boosted.mp4")

    print(f"📥 Downloading {filename}...")
    url = f"{IMMICH_URL}/assets/{asset_id}/original"
    with requests.get(url, headers={"x-api-key": API_KEY}, stream=True) as r:
        if r.status_code != 200: return False
        with open(original_path, 'wb') as f: shutil.copyfileobj(r.raw, f)

    os.environ['VS_SOURCE'] = original_path
    
    # Wasserzeichen Filter
    wm = ""
    if WATERMARK_ENABLED:
        wm = f"-vf \"drawtext=text='{WATERMARK_TEXT}':fontcolor=white@{WATERMARK_ALPHA}:fontsize=h/60:x=w-tw-20:y=h-th-20\""

    # AUDIO FIX: Nimmt Video von vspipe und Audio von Originaldatei
    cmd = (
        f"vspipe -c y4m processor_wrapper.py - | "
        f"ffmpeg -y -i pipe: -i \"{original_path}\" {wm} "
        f"-c:v hevc_nvenc -preset p6 -cq 20 -map 0:v:0 -map 1:a? -c:a copy \"{boosted_path}\""
    )
    
    try:
        print("🚀 Boosting Video & Audio...")
        subprocess.run(cmd, shell=True, check=True)
        subprocess.run(["exiftool", "-TagsFromFile", original_path, "-all:all", "-FileModifyDate", "-overwrite_original", boosted_path], check=True)
        
        with open(boosted_path, 'rb') as f:
            data = {'deviceAssetId': f"{asset['deviceAssetId']}-boosted-{int(time.time())}",
                    'deviceId': asset['deviceId'], 'fileCreatedAt': asset['fileCreatedAt'],
                    'fileModifiedAt': asset['fileModifiedAt'], 'isFavorite': str(asset['isFavorite']).lower()}
            r = requests.post(f"{IMMICH_URL}/assets", headers={"x-api-key": API_KEY}, files={'assetData': f}, data=data)
            
            if r.status_code in [200, 201]:
                new_id = r.json()['id']
                print(f"✅ Uploaded! Stacking {new_id}...")
                requests.post(f"{IMMICH_URL}/assets/{asset_id}/stack", json={"childIds": [new_id]}, headers={"x-api-key": API_KEY})
                return True
    finally:
        for p in [original_path, boosted_path]:
            if os.path.exists(p): os.remove(p)
    return False

def main():
    conn = init_db()
    print("🤖 Booster online. Warte auf Nachtschicht...")
    while True:
        if not is_within_time_window():
            print(f"zzz... {datetime.now().strftime('%H:%M:%S')}", end="\r")
            time.sleep(60); continue
            
        print("\n🔍 Suche neue Videos...")
        resp = requests.post(f"{IMMICH_URL}/search/metadata", json={"type": "VIDEO"}, headers={"x-api-key": API_KEY})
        assets = resp.json().get('assets', {}).get('items', [])
        assets.sort(key=lambda x: x['fileCreatedAt'], reverse=True)

        for asset in assets:
            if not is_within_time_window(): break
            if is_processed(conn, asset['id']) or "_boosted" in asset['originalPath']: continue
            if process_video(asset):
                mark_processed(conn, asset['id'])
                # Zum Testen: Nur eins. Fuer Dauerbetrieb: das "break" spaeter entfernen.
                break 
        time.sleep(300)

if __name__ == "__main__":
    main()
