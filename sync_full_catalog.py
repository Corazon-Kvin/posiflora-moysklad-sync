import os
import requests
import time
import base64
import re
import io
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

# --- Конфигурация ---
POSIFLORA_USER = os.getenv("POSIFLORA_USER", "cvetistroginorf")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS", "19478893")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "d528d07b79305bf9859c706da2cbd62c7777ecf0")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

# Папки в МойСклад
FOLDER_VITRINA_ID = "57cbfb03-af7b-11f1-0a80-00ba00393e19"  # Витрина Tilda
FOLDER_AUTH_ID    = "b5104917-b72b-11f1-0a80-00cf000d73af"  # Авторские букеты (Рецепты)
FOLDER_FLOWERS_ID = "a69d7682-b72b-11f1-0a80-0c1e000dacd4"  # Цветы (стебли)

ALLOWED_FOLDERS = [FOLDER_VITRINA_ID, FOLDER_AUTH_ID, FOLDER_FLOWERS_ID]

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"  # Склад Лакма Цветы
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

MAX_STOCK_FLOWERS = 35

def compress_image(image_bytes, max_dim=700, quality=70):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        return image_bytes

def norm_name(s):
    clean = re.sub(r'[\(\)\-\_\,\.\"]', ' ', str(s or '')).strip().lower()
    clean = re.sub(r'^букет\s+', '', clean)
    return re.sub(r'\s+', ' ', clean).strip()

def get_keys(title):
    t = str(title or '').strip().lower()
    keys = {t, norm_name(t)}
    clean = re.sub(r'[^a-zа-я0-9]', '', t)
    if clean: keys.add(clean)
    digits = re.findall(r'\b\d{3,5}\b', t)
    for d in digits:
        keys.add(d)
        keys.add(f"букет {d}")
        keys.add(re.sub(r'[^0-9]', '', d))
    return keys

# --- Сессии ---
print("1. Авторизация в сервисах...")
pos_session = requests.Session()
pos_session.trust_env = False
pos_session.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})
resp = pos_session.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)
pos_session.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})

ms_session = requests.Session()
ms_session.trust_env = False
ms_session.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

# --- 2. Сбор каталога МойСклад ---
print("2. Сбор ассортимента МойСклад...")
ms_goods_by_name = {}
ms_goods_by_id = {}
default_price_meta = None
offset = 0

while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/assortment?limit=100&offset={offset}&expand=images", timeout=30)
    if res.status_code == 429:
        time.sleep(2.0)
        continue
    rows = res.json().get("rows", [])
    if not rows: break

    for item in rows:
        href = item.get("productFolder", {}).get("meta", {}).get("href", "")
        # Изолируем только цветочные категории
        if not any(f_id in href for f_id in ALLOWED_FOLDERS):
            continue

        raw_name = str(item.get("name", "")).strip().lower()
        clean = norm_name(raw_name)

        if not default_price_meta and item.get("salePrices"):
            pt_meta = item["salePrices"][0].get("priceType", {}).get("meta")
            if pt_meta: default_price_meta = pt_meta

        info = {
            "id": item["id"],
            "name": item.get("name", "").strip(),
            "href": item["meta"]["href"],
            "item_type": item.get("meta", {}).get("type", "product"),
            "has_images": len(item.get("images", {}).get("rows", [])) > 0,
            "folder_href": href,
            "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0
        }
        for k in get_keys(item.get("name")):
            ms_goods_by_name[k] = info
        ms_goods_by_id[item["id"]] = info

    offset += len(rows)
    if len(rows) < 100: break

print(f"Активных позиций в цветочных разделах МойСклад: {len(ms_goods_by_id)}")

# --- 3. Сбор данных из Posiflora ---
print("3. Сбор каталога и фото из Posiflora...")
pos_catalog = {}

# 3.1 Витринные букеты (с поддержкой logo: type=images)
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/bouquets?sort=-createdAt&page[size]=100&page[number]={page}&include=logo", timeout=30)
    if r.status_code != 200: break
    res_data = r.json()
    b_data = res_data.get("data", [])
    if not b_data: break

    images_map = {}
    for inc in res_data.get("included", []):
        if inc.get("type") == "images":
            attrs = inc.get("attributes", {})
            u = attrs.get("fileShop") or attrs.get("fileMedium") or attrs.get("file")
            if u: images_map[str(inc["id"])] = u

    for b in b_data:
        attrs = b.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        completed_at = attrs.get("completedAt")
        
        is_on_showcase = (status == "demonstrated" and not completed_at)
        if not is_on_showcase or not title:
            continue

        raw_price = attrs.get("saleAmount") or attrs.get("salePrice") or attrs.get("priceMax") or attrs.get("amount") or 0
        price = float(raw_price) * 100

        img_url = None
        logo_rel = b.get("relationships", {}).get("logo", {}).get("data")
        if logo_rel and str(logo_rel.get("id")) in images_map:
            img_url = images_map[str(logo_rel["id"])]

        k_clean = norm_name(title)
        pos_catalog[k_clean] = {
            "title": title,
            "price": price,
            "stock": 1.0,
            "image_url": img_url,
            "folder_id": FOLDER_VITRINA_ID,
            "create_if_missing": True
        }

    if len(b_data) < 100 or page >= 5: break
    page += 1

# 3.2 Авторские букеты (Рецепты)
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/specifications?page[size]=100&page[number]={page}&include=images", timeout=30)
    if r.status_code != 200: break
    res_data = r.json()
    specs = res_data.get("data", [])
    if not specs: break

    images_map = {}
    for inc in res_data.get("included", []):
        if inc.get("type") == "images":
            attrs = inc.get("attributes", {})
            u = attrs.get("fileShop") or attrs.get("fileMedium") or attrs.get("file")
            if u: images_map[str(inc["id"])] = u

    for spec in specs:
        attrs = spec.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        if not title or status == "off":
            continue

        k_clean = norm_name(title)
        raw_price = attrs.get("minPrice") or attrs.get("maxPrice") or attrs.get("price") or 0
        price = float(raw_price) * 100

        img_url = None
        spec_imgs = spec.get("relationships", {}).get("images", {}).get("data", [])
        if isinstance(spec_imgs, list):
            for simg in spec_imgs:
                sid = str(simg.get("id"))
                if sid in images_map:
                    img_url = images_map[sid]
                    break

        if k_clean not in pos_catalog:
            pos_catalog[k_clean] = {
                "title": title,
                "price": price,
                "stock": 5.0,
                "image_url": img_url,
                "folder_id": FOLDER_AUTH_ID,
                "create_if_missing": True
            }

    if len(specs) < 100: break
    page += 1

# 3.3 Цветы (срезка)
page = 1
while True:
    params = {"page[number]": page, "page[size]": 100, "include": "category,prices,balances", "sort": "title", "distribution": "true", "concealment": "false"}
    r = pos_session.get(f"{POSIFLORA_URL}/catalog", params=params, timeout=30)
    if r.status_code != 200: break
    res_json = r.json()
    items = res_json.get("data", [])
    if not items: break

    balances_map = {}
    prices_map = {}
    for inc in res_json.get("included", []):
        inc_type = inc.get("type")
        inc_id = str(inc.get("id", ""))
        attrs = inc.get("attributes", {})
        if inc_type == "item-balances":
            stock = float(attrs.get("qtyOnWindow", 0) or attrs.get("qty", 0) or 0)
            if stock > 0: balances_map[inc_id] = stock
        elif inc_type == "prices":
            price_val = float(attrs.get("price") or attrs.get("value") or 0) * 100
            if price_val > 0: prices_map[inc_id] = price_val

    for item in items:
        attrs = item.get("attributes", {})
        title = attrs.get("title", "").strip()
        if not title: continue
        k_clean = norm_name(title)

        bal_rel = item.get("relationships", {}).get("balances", {}).get("data", [])
        prices_rel = item.get("relationships", {}).get("prices", {}).get("data", [])
        
        item_stock = 0.0
        item_price = 0.0

        if isinstance(bal_rel, list):
            for b in bal_rel:
                bid = str(b.get("id", ""))
                if bid in balances_map: item_stock += balances_map[bid]
        elif isinstance(bal_rel, dict):
            bid = str(bal_rel.get("id", ""))
            if bid in balances_map: item_stock += balances_map[bid]

        if isinstance(prices_rel, list):
            for p in prices_rel:
                pid = str(p.get("id", ""))
                if pid in prices_map and item_price <= 0: item_price = prices_map[pid]
        elif isinstance(prices_rel, dict):
            pid = str(prices_rel.get("id", ""))
            if pid in prices_map: item_price = prices_map[pid]

        if item_price <= 0:
            raw_p = attrs.get("priceMax") or attrs.get("salePrice") or attrs.get("price") or 0
            item_price = float(raw_p) * 100

        capped_qty = min(item_stock, float(MAX_STOCK_FLOWERS)) if item_stock > 0 else 0.0

        if k_clean not in pos_catalog:
            pos_catalog[k_clean] = {
                "title": title,
                "price": item_price,
                "stock": capped_qty,
                "image_url": None,
                "folder_id": FOLDER_FLOWERS_ID,
                "create_if_missing": False
            }
        else:
            if capped_qty > 0:
                pos_catalog[k_clean]["stock"] = capped_qty
            if item_price > 0 and pos_catalog[k_clean]["price"] <= 0:
                pos_catalog[k_clean]["price"] = item_price

    if len(items) < 100: break
    page += 1

print(f"Всего позиций в объединенном каталоге Posiflora: {len(pos_catalog)}")

# --- 4. Синхронизация товаров и загрузка фото со сжатием ---
print("4. Создание новинок и обновление фото...")
created_count = 0
uploaded_photos = 0

for key, item_data in pos_catalog.items():
    matched = None
    for k in get_keys(item_data["title"]):
        if k in ms_goods_by_name:
            matched = ms_goods_by_name[k]
            break

    # Создание, если новинка
    if not matched and item_data.get("create_if_missing"):
        sale_prices = []
        if item_data["price"] > 0:
            if default_price_meta:
                sale_prices.append({"value": item_data["price"], "priceType": {"meta": default_price_meta}})
            else:
                sale_prices.append({"value": item_data["price"]})

        folder_href = f"{MOYSKLAD_URL}/entity/productfolder/{item_data['folder_id']}"
        payload = {
            "name": item_data["title"],
            "productFolder": {"meta": {"href": folder_href, "type": "productfolder", "mediaType": "application/json"}},
            "salePrices": sale_prices
        }
        res_c = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        if res_c.status_code in (200, 201):
            created = res_c.json()
            matched = {
                "id": created["id"],
                "name": created["name"],
                "href": created["meta"]["href"],
                "item_type": "product",
                "has_images": False,
                "price": item_data["price"]
            }
            for k in get_keys(created["name"]):
                ms_goods_by_name[k] = matched
            ms_goods_by_id[created["id"]] = matched
            created_count += 1
            print(f"  [+] Создан товар: {item_data['title']}")

    # Загрузка фото со сжатием, если у товара его еще нет
    if matched and not matched.get("has_images") and item_data.get("image_url"):
        try:
            raw_b = requests.get(item_data["image_url"], timeout=15).content
            compressed = compress_image(raw_b)
            r_post = ms_session.post(
                f"{MOYSKLAD_URL}/entity/{matched['item_type']}/{matched['id']}/images",
                json={"filename": "photo_web.jpg", "content": base64.b64encode(compressed).decode("utf-8")},
                timeout=15
            )
            if r_post.status_code in (200, 201):
                matched["has_images"] = True
                uploaded_photos += 1
                print(f"  [+] Добавлено сжатое фото: {matched['name']}")
        except Exception as e:
            print(f"      Ошибка загрузки фото {matched['name']}: {e}")

print(f"Итог: создано товаров: {created_count}, загружено фото: {uploaded_photos}")

# --- 5. Выравнивание остатков на складе Лакма Цветы ---
print("5. Автовыравнивание остатков...")
stocks_by_store = {}
r_stocks = ms_session.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45).json()
for row in r_stocks:
    if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
        stocks_by_store[row["assortmentId"]] = float(row.get("stock", 0))

enter_positions = []
loss_positions = []

for key, item_data in pos_catalog.items():
    matched = None
    for k in get_keys(item_data["title"]):
        if k in ms_goods_by_name:
            matched = ms_goods_by_name[k]
            break
    if not matched: continue

    target_stock = item_data["stock"]
    cur_stock = stocks_by_store.get(matched["id"], 0.0)

    if cur_stock < target_stock and target_stock > 0:
        enter_positions.append({
            "quantity": round(target_stock - cur_stock, 2),
            "price": matched["price"],
            "assortment": {"meta": {"href": matched["href"], "type": matched["item_type"], "mediaType": "application/json"}}
        })

# Списание отсутствующих на витрине букетов
for p_id, cur_stock in stocks_by_store.items():
    if cur_stock <= 0: continue
    good = ms_goods_by_id.get(p_id)
    if not good: continue
    folder_href = good.get("folder_href", "")

    # Списываем только витринные, если они сняты с витрины Posiflora
    if FOLDER_VITRINA_ID in folder_href:
        matched_target = None
        for k in get_keys(good["name"]):
            if k in pos_catalog and pos_catalog[k].get("folder_id") == FOLDER_VITRINA_ID:
                matched_target = pos_catalog[k]["stock"]
                break
        if matched_target is None:
            loss_positions.append({
                "quantity": cur_stock,
                "assortment": {"meta": {"href": good["href"], "type": good["item_type"], "mediaType": "application/json"}}
            })

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Автовыравнивание Posiflora -> МойСклад",
            "positions": enter_positions[i:i+1000]
        }, timeout=60)
    print(f"  [+] Оприходовано: {len(enter_positions)} поз.")

if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/loss", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Автосписание проданных букетов витрины",
            "positions": loss_positions[i:i+1000]
        }, timeout=60)
    print(f"  [-] Списано проданных с витрины: {len(loss_positions)} поз.")

print("\nПолная синхронизация завершена успешно!")
