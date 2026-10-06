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
    except Exception:
        return image_bytes

print("1. Подключение к API...")
pos_s = requests.Session()
pos_s.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})
resp = pos_s.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)
pos_s.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})

ms_s = requests.Session()
ms_s.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

# 2. Сканирование существующих папок в МоемСкладе
print("\n2. Сканирование структуры папок МоегоСклада...")
folder_map = {}
default_flower_folder = None
default_decor_folder = None

offset = 0
while True:
    try:
        rf = ms_s.get(f"{MOYSKLAD_URL}/entity/productfolder?limit=100&offset={offset}", timeout=30).json()
        f_rows = rf.get("rows", [])
        if not f_rows:
            break
        for f in f_rows:
            f_name = f.get("name", "").strip().lower()
            f_href = f["meta"]["href"]
            folder_map[f_name] = f_href
            if "витрина tilda" in f_name or "витрина тильда" in f_name:
                folder_map["витрина"] = f_href
            elif "авторские букеты" in f_name:
                folder_map["авторские"] = f_href
            elif f_name in ("цветы", "цветы (стебли)"):
                default_flower_folder = f_href
            elif f_name == "декор":
                default_decor_folder = f_href
        offset += len(f_rows)
        if len(f_rows) < 100:
            break
    except Exception as e:
        print(f"[!] Ошибка сканирования папок: {e}")
        break

def get_folder_href_for_item(name, category_name=""):
    search_text = f"{name} {category_name}".lower()
    if "витрин" in search_text:
        return folder_map.get("витрина")
    if "свеч" in search_text:
        return folder_map.get("свечи", default_decor_folder)
    if "ваз" in search_text:
        return folder_map.get("вазы", default_decor_folder)
    if "подсвечник" in search_text:
        return folder_map.get("подсвечники", default_decor_folder)
    if "шар" in search_text:
        return folder_map.get("шары", default_decor_folder)
    if "открытк" in search_text or "конверт" in search_text:
        return folder_map.get("открытки", default_decor_folder)
    if "коробк" in search_text:
        return folder_map.get("подарочные коробки", default_decor_folder)
    if "игрушк" in search_text:
        return folder_map.get("игрушки", default_decor_folder)
    if "лент" in search_text or "упаковк" in search_text or "пленк" in search_text:
        return folder_map.get("упаковка", default_decor_folder)
    return default_flower_folder or default_decor_folder

# 3. Сбор витринных букетов Posiflora (/showcases)
print("\n3. Сбор витринных букетов из Posiflora...")
pos_showcase = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/showcases?page[number]={page}&page[size]=100&include=logo,prices", timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        items = d.get("data", [])
        if not items:
            break

        inc_map = {}
        for inc in d.get("included", []):
            inc_map[(inc.get("type"), str(inc.get("id")))] = inc

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            it_id = str(it.get("id"))
            price_val = 0
            for pr_rel in it.get("relationships", {}).get("prices", {}).get("data", []):
                pr_obj = inc_map.get(("prices", str(pr_rel.get("id"))))
                if pr_obj:
                    price_val = float(pr_obj.get("attributes", {}).get("value", 0))
                    break
            logo_id = None
            logo_rel = it.get("relationships", {}).get("logo", {}).get("data")
            if logo_rel and logo_rel.get("id"):
                logo_id = str(logo_rel["id"])
            pos_showcase.append({"id": it_id, "title": title, "price": price_val, "logo_id": logo_id})
        if len(items) < 100:
            break
        page += 1
    except Exception:
        break

# 4. Сбор авторских букетов Posiflora (/recipes)
print("\n4. Сбор рецептов авторских букетов из Posiflora...")
pos_recipes = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/recipes?page[number]={page}&page[size]=100&include=logo,prices", timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        items = d.get("data", [])
        if not items:
            break

        inc_map = {}
        for inc in d.get("included", []):
            inc_map[(inc.get("type"), str(inc.get("id")))] = inc

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            it_id = str(it.get("id"))
            price_val = 0
            for pr_rel in it.get("relationships", {}).
