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

if resp.status_code in (200, 201):
    pos_s.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})
else:
    print(f"[-] Ошибка авторизации Posiflora: {resp.status_code}")
    exit()

ms_s = requests.Session()
ms_s.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

print("\n1.5 Получение системного типа цены в МоемСкладе...")
price_type_meta = None
try:
    r_pt = ms_s.get(f"{MOYSKLAD_URL}/context/companysettings/pricetype", timeout=15)
    if r_pt.status_code == 200:
        pts = r_pt.json()
        for pt in pts:
            if pt.get("name") == "Цена продажи":
                price_type_meta = pt.get("meta")
                break
        if not price_type_meta and pts:
            price_type_meta = pts[0].get("meta")
except Exception as e:
    print(f"[!] Ошибка получения типа цены: {e}")

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

print("\n3. Получение текущих остатков склада Лакма Цветы...")
ms_current_stocks = {}
try:
    r_stocks = ms_s.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
    if r_stocks.status_code == 200:
        for row in r_stocks.json():
            if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
                ms_current_stocks[row["assortmentId"]] = float(row.get("stock", 0))
except Exception as e:
    print(f"[!] Ошибка отчета остатков: {e}")

print("\n4. Сканирование существующих позиций в МоемСкладе...")
offset = 0
ms_products = []
ms_ext_codes = set()
ms
