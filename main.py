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

# DEBUG
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
DEBUG_LIMIT = 3

# ZEITFENSTER
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
    if DEBUG_MODE: return True
    now = datetime.now().time()
    if START_TIME < END_TIME:
        return START_TIME <= now <= END_TIME
    return now >= START_TIME or now <= END_TIME

def clean_filename(filename):
    name, ext = os.path.splitext(filename)
    clean_name = re.sub(r'\+\d+$', '', name)
    return f"{clean_name}_boosted{ext}"

def get_asset_info(asset_id):
    """Holt Details zu einem Asset"""
    try:
        r = requests.get(f"{IMMICH_URL}/assets/{asset_id}", headers={"x-api-key": API_KEY})
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def get_best_source_and_parent(initial_asset):
    """
    Analysiert den Stack eines Assets.
    Return: (best_source_asset, stack_parent_id)
    - best_source_asset: Das groesste File im Stack (vermutlich das echte Original)
    - stack_parent_id: Die ID des Haupt-Elements fuer das Stacking
    """
    
    # 1. Ist das Asset Teil eines Stacks?
    parent_id = initial_asset.get('stackParentId') # Wenn es ein Kind ist
    
    # Wenn kein Parent, ist es vielleicht selbst der Parent oder Single?
    # Wir behandeln es erst mal als Single/Parent.
    primary_id = parent_id if parent_id else initial_asset['id']
    
    # 2. Wenn nicht gestackt (oder wir wissen es nicht genau), pruefen wir via API
    # Wir holen den "Stack" via Parent ID (oder eigener ID)
    # Da Immich API etwas tricky ist, holen wir erst mal das Primary Asset komplett
    primary_asset = get_asset_info(primary_id)
    
    if not primary_asset:
        return initial_asset, initial_asset['id']

    # Sammle alle Kandidaten (Parent + Kinder)
    candidates = [primary_asset]
    
    # Hat der Parent Kinder? (Im 'stack' Objekt)
    if 'stack' in primary_asset and primary_asset['stack']:
        # Wir müssen die IDs der Kinder holen. Leider liefert /assets/{id} nicht immer die Kinder-Details direkt.
        # Wir verlassen uns auf das, was da ist.
        pass
    
    # Wir suchen nach dem groessten File ("Source of Truth")
    # Achtung: Wir haben hier evtl. nicht alle Kinder, wenn die API sie nicht listet.
    # Aber wir vergleichen zumindest das, was wir haben.
    
    # Einfache Heuristik:
    # Wenn wir "Google Boost" vs "Original" haben, wollen wir das Original.
    # Das Original ist meistens groesser.
    
    best_candidate = primary_asset
    max_size = int(primary_asset.get('exifInfo', {}).get('fileSizeInByte', 0) or 0)
    
    # Falls das Search-Result Kinder hatte (manchmal sind die in search results flattened)
    # Wir nehmen vereinfacht an: Das Input-Asset oder dessen Parent sind die Hauptkandidaten.
    
    # Check: Ist das Input-Asset evtl. groesser als der Parent? (Sollte nicht sein, aber sicher ist sicher)
    input_size = int(initial_asset.get('exifInfo', {}).get('fileSizeInByte', 0) or 0)
    
    if input_size > max_size:
        best_candidate = initial_asset
        max_size = input_size

    print(f"🔎 Stack-Check: Primary={primary_id}, Source={best_candidate['id']} ({max_size/1024/1024:.2f} MB)")
    
    return best_candidate, primary_id

def process_video(asset):
    # Schritt 1: Das RICHTIGE Original finden
    source_asset, stack_parent_id = get_best_source_and_parent(asset)
    
    asset_id = source_asset['id']
    original_filename = source_asset['originalFileName']
    
    # Dateinamen-Check
    if '_boosted' in original_filename:
        print(f"⏭️ Überspringe bereits geboostetes File: {original_filename}")
        return False

    if not os.path.splitext(original_filename)[1]:
        original_filename += ".mp4"

    local_input_path = os.path.join(TEMP_DIR, f"input_{asset_id}.mp4")
    output_filename = clean_filename(original_filename)
    local_output_path = os.path.join(TEMP_DIR, output_filename)

    print(f"📥 Downloading Source: {original_filename}...")
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
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        wm = f"-vf \"drawtext=fontfile='{font_path}':text='{WATERMARK_TEXT}':fontcolor=white@{WATERMARK_ALPHA}:fontsize=h/60:x=w-tw-20:y=h-th-20\""

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
            device_asset_id = f"{source_asset['deviceAssetId']}-boosted-{int(time.time())}"
            data = {
                'deviceAssetId': device_asset_id,
                'deviceId': source_asset['deviceId'],
                'fileCreatedAt': source_asset['fileCreatedAt'],
                'fileModifiedAt': source_asset['fileModifiedAt'],
                'isFavorite': str(source_asset['isFavorite']).lower(),
                'duration': source_asset.get('duration', '0:00:00')
            }
            r = requests.post(f"{IMMICH_URL}/assets", headers={"x-api-key": API_KEY}, files={'assetData': f}, data=data)
            
            if r.status_code in [200, 201]:
                new_id = r.json()['id']
                print(f"✅ Upload success! New ID: {new_id}")
                
                print(f"📚 Stacking zu Parent {stack_parent_id}...")
                # WICHTIG: Wir stacken den Parent (Alt) mit dem Neuen. Immich merged das dann.
                stack_payload = {
                    "assetIds": [stack_parent_id, new_id] 
                }
                s = requests.post(f"{IMMICH_URL}/stacks", json=stack_payload, headers={"x-api-key": API_KEY})
                if s.status_code in [200, 201]:
                    print("✅ Stack merged.")
                else:
                    print(f"⚠️ Stacking Meldung: {s.text}") # Kann auch failen wenn schon gestackt, ist oft ok.

                return True
            else:
                print(f"❌ Upload failed: {r.text}")
                
    except Exception as e:
        print(f"❌ Processing Fehler: {e}")
    finally:
        if os.path.exists(local_input_path): os.remove(local_input_path)
        if os.path.exists(local_output_path): os.remove(local_output_path)
    
    return False

def main():
    conn = init_db()
    print("🤖 Booster online.")
    if DEBUG_MODE:
        print(f"🐞 DEBUG MODE: Max {DEBUG_LIMIT} Videos.")

    debug_counter = 0
    
    while True:
        if not is_within_time_window():
            if int(time.time()) % 600 == 0:
                print(f"zzz... {datetime.now().strftime('%H:%M')}", flush=True)
            time.sleep(60); continue
            
        print("\n🔍 Suche...", flush=True)
        try:
            resp = requests.post(f"{IMMICH_URL}/search/metadata", json={"type": "VIDEO"}, headers={"x-api-key": API_KEY})
            assets = resp.json().get('assets', {}).get('items', [])
            assets.sort(key=lambda x: x['fileCreatedAt'], reverse=True)

            work_done = False
            for asset in assets:
                if not DEBUG_MODE and not is_within_time_window(): break
                if DEBUG_MODE and debug_counter >= DEBUG_LIMIT:
                    print("🛑 Debug Limit erreicht."); time.sleep(86400); break

                # Grober Check vorab
                if is_processed(conn, asset['id']): continue
                if '_boosted' in asset.get('originalFileName', ''): 
                    mark_processed(conn, asset['id'])
                    continue
                
                # Device Filter
                if DEVICE_FILTER and DEVICE_FILTER not in asset.get('deviceInfo', {}).get('model', ''):
                    continue

                # Jetzt gehts los
                if process_video(asset):
                    mark_processed(conn, asset['id'])
                    # Auch den Parent markieren, damit wir den Stack nicht nochmal anfassen
                    if asset.get('stackParentId'):
                        mark_processed(conn, asset.get('stackParentId'))
                    
                    work_done = True
                    if DEBUG_MODE: debug_counter += 1
                    time.sleep(5)
            
            if not work_done:
                print("😴 Warte 5 min...")
                time.sleep(300)

        except Exception as e:
            print(f"❌ Error: {e}"); time.sleep(60)

if __name__ == "__main__":
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    main()
