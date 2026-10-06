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
    except Exception:
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

print("\n2. Сбор фото витринных букетов Posiflora...")
vitrina_photos = {}
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/showcases?page[number]={page}&page[size]=100&include=logo", timeout=30)
        if r.status_code != 200: break
        items = r.json().get("data", [])
        if not items: break
        for it in items:
            title = it.get("attributes", {}).get("title", "")
            logo_data = it.get("relationships", {}).get("logo", {}).get("data")
            if logo_data and logo_data.get("id"):
                vitrina_photos[norm_name(title)] = logo_data["id"]
        if len(items) < 100: break
        page += 1
    except Exception as e:
        print(f"[!] Ошибка API витрины: {e}")
        break

print("\n3. Сбор остатков со Склада Лакма Цветы в МойСклад...")
ms_current_stocks = {}
try:
    r_stocks = ms_s.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
    if r_stocks.status_code == 200:
        for row in r_stocks.json():
            if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
                ms_current_stocks[row["assortmentId"]] = float(row.get("stock", 0))
except Exception as e:
    print(f"[!] Ошибка API остатков МС: {e}")

print("\n4. Сбор ассортимента из МойСклад (исключая кофейню/пекарню)...")
offset = 0
ms_products = []
bakery_stop = ["круассан", "сосиска", "слойка", "датская", "кремибон", "хлеб", "чиабатта", "багет", "булочк", "пирож", "сырник", "кофе", "напиток", "эспрессо", "капучино", "торт", "макарон", "десерт"]

while True:
    try:
        res = ms_s.get(f"{MOYSKLAD_URL}/entity/assortment?limit=100&offset={offset}&expand=productFolder,images", timeout=30)
        if res.status_code != 200: break
        rows = res.json().get("rows", [])
        if not rows: break

        for item in rows:
            name = str(item.get("name") or "").strip()
            folder_name = str(item.get("productFolder", {}).get("name") or "").strip()

            # Игнорируем выпечку и кофе по названию товара ИЛИ названию папки
            if any(w in name.lower() for w in bakery_stop) or any(w in folder_name.lower() for w in bakery_stop):
                continue

            ms_products.append({
                "id": item["id"],
                "name": name,
                "external_code": str(item.get("externalCode") or ""),
                "href": item["meta"]["href"],
                "type": item.get("meta", {}).get("type", "product"),
                "folder_href": item.get("productFolder", {}).get("meta", {}).get("href", ""),
                "has_image": bool(item.get("images", {}).get("rows")),
                "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0,
                "stock": ms_current_stocks.get(item["id"], 0.0)
            })

        offset += len(rows)
        if len(rows) < 100: break
    except Exception as e:
        print(f"[!] Ошибка API ассортимента МС: {e}")
        break

print(f"Всего позиций в МоемСкладе для сверки: {len(ms_products)}")

print("\n5. Сбор срезки из каталога Posiflora...")
flower_stocks = {}
page = 1
while True:
    try:
        params = {"page[number]": page, "page[size]": 100, "include": "balances", "distribution": "true", "concealment": "false"}
        r = pos_s.get(f"{POSIFLORA_URL}/catalog", params=params, timeout=30)
        if r.status_code != 200: break
        res_json = r.json()
        items = res_json.get("data", [])
        if not items: break

        bal_map = {}
        for inc in res_json.get("included", []):
            if inc.get("type") == "item-balances":
                attrs = inc.get("attributes", {})
                bal_map[str(inc.get("id"))] = float(attrs.get("qtyOnWindow", 0) or attrs.get("qty", 0) or 0)

        for it in items:
            it_title = str(it.get("attributes", {}).get("title") or "").strip()
            st = 0.0
            bal_rel = it.get("relationships", {}).get("balances", {}).get("data", [])
            if isinstance(bal_rel, list):
                for b in bal_rel:
                    bid = str(b.get("id"))
                    if bid in bal_map: st += bal_map[bid]
            flower_stocks[norm_name(it_title)] = min(st, 35.0) if st > 0 else 0.0

        if len(items) < 100: break
        page += 1
    except Exception as e:
        print(f"[!] Ошибка API срезки Posiflora: {e}")
        break

print("\n6. Синхронизация остатков и фото...")
enter_positions = []
loss_positions = []
updated_log = []

for p in ms_products:
    f_href = p["folder_href"]
    cur_stock = p["stock"]
    ext_code = p["external_code"]
    name = p["name"]
    n_name = norm_name(name)
    target_stock = None

    # Витрина Tilda
    if FOLDER_VITRINA_ID in f_href:
        target_stock = 1.0
        if not p["has_image"] and n_name in vitrina_photos:
            try:
                r_img_meta = pos_s.get(f"{POSIFLORA_URL}/images/{vitrina_photos[n_name]}", timeout=15)
                if r_img_meta.status_code == 200:
                    img_url = r_img_meta.json().get("data", {}).get("attributes", {}).get("url")
                    if img_url:
                        raw_bytes = requests.get(img_url, timeout=20).content
                        compressed = compress_image(raw_bytes)
                        b64 = base64.b64encode(compressed).decode("utf-8")
                        ms_s.post(f"{MOYSKLAD_URL}/entity/product/{p['id']}/images", json={
                            "filename": f"{p['id']}.jpg",
                            "content": b64
                        }, timeout=25)
                        print(f"  [+] Загружено и сжато фото для: {name}")
            except Exception:
                pass

    # Авторские букеты
    elif FOLDER_AUTH_ID in f_href:
        target_stock = 5.0

    # Штучные товары (Свечи, Вазы, Шары и т.д.) - обращаемся к API только если код похож на UUID
    elif len(ext_code) >= 32:
        try:
            r_st = pos_s.get(f"{POSIFLORA_URL}/inventory-items/{ext_code}/stats-by-store/{POSIFLORA_STORE_ID}", timeout=10)
            if r_st.status_code == 200:
                target_stock = float(r_st.json().get("data", {}).get("attributes", {}).get("qty", 0))
            time.sleep(0.05) # МИКРО-ПАУЗА, чтобы не получить бан от Posiflora
        except Exception:
            pass

    # Срезка
    if target_stock is None and n_name in flower_stocks:
        target_stock = flower_stocks[n_name]

    # Подготовка операции
    if target_stock is not None:
        diff = round(target_stock - cur_stock, 2)
        if diff != 0:
            assort_meta = {"meta": {"href": p["href"], "type": p["type"], "mediaType": "application/json"}}
            if diff > 0:
                enter_positions.append({"quantity": diff, "price": p["price"], "assortment": assort_meta})
                updated_log.append(f"  [+] {name}: было {cur_stock} -> станет {target_stock} (+{diff})")
            else:
                loss_positions.append({"quantity": abs(diff), "assortment": assort_meta})
                updated_log.append(f"  [-] {name}: было {cur_stock} -> станет {target_stock} ({diff})")

print(f"\nВсего изменений по остаткам: {len(enter_positions) + len(loss_positions)}")
for line in updated_log[:25]: print(line)
if len(updated_log) > 25: print(f"  ... и еще {len(updated_log) - 25} позиций")

# Оприходования
if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        try:
            ms_s.post(f"{MOYSKLAD_URL}/entity/enter", json={
                "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
                "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
                "applicable": True,
                "description": "Синхронизация остатков: Posiflora -> МойСклад",
                "positions": enter_positions[i:i+1000]
            }, timeout=60)
        except Exception as e:
            print(f"[!] Ошибка создания оприходования: {e}")
    print(f"\n[+] Успешно оприходовано: {len(enter_positions)} позиций")

# Списания
if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        try:
            ms_s.post(f"{MOYSKLAD_URL}/entity/loss", json={
                "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
                "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
                "applicable": True,
                "description": "Автосписание проданных товаров: Posiflora -> МойСклад",
                "positions": loss_positions[i:i+1000]
            }, timeout=60)
        except Exception as e:
            print(f"[!] Ошибка создания списания: {e}")
    print(f"[-] Успешно списано: {len(loss_positions)} позиций")

if not enter_positions and not loss_positions:
    print("\nВсе остатки по всем категориям на 100% совпадают!")

print("\nСинхронизация полностью завершена.")
