import os
import io
import re
import time
import base64
import requests
from PIL import Image

# === Переменные окружения и ключи ===
POSIFLORA_USER = os.getenv("POSIFLORA_USER", "cvetistroginorf")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS", "19478893")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"
POSIFLORA_STORE_ID = "cde3de25-51e3-4193-a164-84cdebd01e94"

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "d528d07b79305bf9859c706da2cbd62c7777ecf0")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"  # Склад Лакма Цветы
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

FOLDER_VITRINA_ID = "57cbfb03-af7b-11f1-0a80-00ba00393e19"
FOLDER_AUTH_ID    = "b5104917-b72b-11f1-0a80-00cf000d73af"

def norm_name(s):
    clean = re.sub(r'[\(\)\-\_\,\.\"]', ' ', str(s or '')).strip().lower()
    clean = re.sub(r'^букет\s+', '', clean)
    return re.sub(r'\s+', ' ', clean).strip()

def compress_image(image_bytes, max_dim=1200, quality=85):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            if w > h:
                new_w = max_dim
                new_h = int(h * (max_dim / w))
            else:
                new_h = max_dim
                new_w = int(w * (max_dim / h))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"    [!] Ошибка сжатия фото: {e}")
        return image_bytes

print("1. Подключение к сессиям API...")
pos_s = requests.Session()
pos_s.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})
resp = pos_s.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)
pos_s.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})

ms_s = requests.Session()
ms_s.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

# 2. Построение дерева всех цветочных групп МойСклад
print("\n2. Сканирование папок МойСклад...")
all_folders = {}
root_flower_ids = set()
offset = 0

while True:
    r_f = ms_s.get(f"{MOYSKLAD_URL}/entity/productfolder?limit=100&offset={offset}", timeout=30).json()
    f_rows = r_f.get("rows", [])
    if not f_rows: break
    for f in f_rows:
        fid = f["id"]
        p_href = f.get("productFolder", {}).get("meta", {}).get("href", "")
        parent_id = p_href.split("/")[-1] if p_href else None
        name = f.get("name", "").strip()
        all_folders[fid] = {"name": name, "parent_id": parent_id}
        
        # Расширенный список папок, чтобы не пропустить самостоятельные категории вне "Цветы лакма"
        if any(w in name.lower() for w in ["цветы лакма", "витрина tilda", "авторские букеты", "цветы", "декор", "свечи", "вазы", "подсвечники", "шары", "открытки", "коробки", "упаковка", "игрушки", "сухоцветы"]):
            root_flower_ids.add(fid)
    offset += len(f_rows)
    if len(f_rows) < 100: break

flower_folder_ids = set(root_flower_ids)
changed = True
while changed:
    changed = False
    for fid, finfo in all_folders.items():
        if fid not in flower_folder_ids and finfo["parent_id"] in flower_folder_ids:
            flower_folder_ids.add(fid)
            changed = True

# 3. Фото витринных букетов из Posiflora
print("\n3. Сбор фото витринных букетов Posiflora...")
vitrina_photos = {}
page = 1
while True:
    r = pos_s.get(f"{POSIFLORA_URL}/showcases?page[number]={page}&page[size]=100&include=logo", timeout=30)
    if r.status_code != 200: break
    d = r.json()
    items = d.get("
