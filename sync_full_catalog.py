import os
import requests
import time
import base64
import re
from dotenv import load_dotenv

load_dotenv()

# --- Настройки ---
POSIFLORA_USER = os.getenv("POSIFLORA_USER", "cvetistroginorf")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS", "19478893")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "d528d07b79305bf9859c706da2cbd62c7777ecf0")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

# Целевые папки в МоемСкладе
FOLDER_VITRINA_ID = "57cbfb03-af7b-11f1-0a80-00ba00393e19"  # Витрина Tilda
FOLDER_AUTH_ID    = "b5104917-b72b-11f1-0a80-00cf000d73af"  # Авторские букеты (Рецепты)
FOLDER_FLOWERS_ID = "a69d7682-b72b-11f1-0a80-0c1e000dacd4"  # Цветы

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

MAX_STOCK_FLOWERS = 35

def norm_name(s):
    clean = re.sub(r'[\(\)\-\_\,\.\"]', ' ', str(s or '')).strip().lower()
    clean = re.sub(r'^букет\s+', '', clean)
    return re.sub(r'\s+', ' ', clean).strip()

# --- Сессии ---
pos_session = requests.Session()
pos_session.trust_env = False
pos_session.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})

resp = pos_session.post(f"{POSIFLORA_URL}/sessions", json={"data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}}, timeout=20)
pos_session.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})

ms_session = requests.Session()
ms_session.trust_env = False
ms_session.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

# --- 1. Сбор существующих товаров из МойСклад ---
print("1. Получение существующих товаров из МойСклад...")
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
        raw_name = str(item.get("name", "")).strip().lower()
        clean = norm_name(raw_name)

        if not default_price_meta and item.get("salePrices"):
            pt_meta = item["salePrices"][0].get("priceType", {}).get("meta")
            if pt_meta: default_price_meta = pt_meta

        info = {
            "id": item["id"],
            "name": item.get("name", "").strip(),
            "href": item["meta"]["href"],
            "has_images": len(item.get("images", {}).get("rows", [])) > 0,
            "folder_href": item.get("productFolder", {}).get("meta", {}).get("href", ""),
            "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0
        }
        ms_goods_by_name[raw_name] = info
        ms_goods_by_name[clean] = info
        ms_goods_by_id[item["id"]] = info

    offset += len(rows)
    if len(rows) < 100: break
print(f"Всего позиций в базе МоегоСклада: {len(ms_goods_by_id)}")

# --- 2. Сбор каталога Posiflora ---
print("\n2. Сбор каталога Posiflora...")
pos_catalog = {}

# 2.1 Актуальная витрина букетов
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/bouquets?sort=-createdAt&page[size]=100&page[number]={page}&include=images,logo", timeout=25)
    if r.status_code != 200: break
    res_data = r.json()
    b_data = res_data.get("data", [])
    if not b_data: break
    
    files_map = {str(inc["id"]): inc.get("attributes", {}).get("file", {}).get("url") for inc in res_data.get("included", []) if inc.get("type") == "files"}
    
    for b in b_data:
        attrs = b.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        completed_at = attrs.get("completedAt")
        
        is_on_showcase = (status == "demonstrated" and not completed_at)
        if not is_on_showcase:
            continue
            
        raw_price = attrs.get("saleAmount") or attrs.get("salePrice") or attrs.get("priceMax") or attrs.get("amount") or 0
        price = float(raw_price) * 100
        if not title: continue

        k_clean = norm_name(title)
        k_raw = title.lower()

        if price <= 0:
            match_ms = ms_goods_by_name.get(k_raw) or ms_goods_by_name.get(k_clean)
            if match_ms: price = match_ms["price"]

        img_urls = []
        logo_rel = b.get("relationships", {}).get("logo", {}).get("data")
        images_rel = b.get("relationships", {}).get("images", {}).get("data", [])
        
        if logo_rel and str(logo_rel.get("id")) in files_map:
            img_urls.append(files_map[str(logo_rel["id"])])
        if isinstance(images_rel, list):
            for img_rel in images_rel:
                fid = str(img_rel.get("id"))
                if fid in files_map and files_map[fid] not in img_urls:
                    img_urls.append(files_map[fid])

        pos_catalog[k_clean] = {
            "title": title, 
            "price": price, 
            "stock": 1.0, 
            "image_urls": img_urls,
            "folder_id": FOLDER_VITRINA_ID,
            "create_if_missing": True
        }
                
    if len(b_data) < 100: break
    page += 1

active_bouquets = [v["title"] for v in pos_catalog.values() if v.get("folder_id") == FOLDER_VITRINA_ID]
print(f"Активных витринных букетов Posiflora: {len(active_bouquets)}")

# 2.2 Рецепты (Авторские букеты)
print("Сбор рецептов (Авторские букеты)...")
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
            url = attrs.get("file") or attrs.get("fileShop") or attrs.get("fileMedium")
            if url: images_map[str(inc["id"])] = url

    for spec in specs:
        attrs = spec.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        if not title or status == "off":
            continue
        
        k_clean = norm_name(title)
        k_raw = title.lower()
        raw_price = attrs.get("minPrice") or attrs.get("maxPrice") or attrs.get("price") or 0
        price = float(raw_price) * 100

        if price <= 0:
            match_ms = ms_goods_by_name.get(k_raw) or ms_goods_by_name.get(k_clean)
            if match_ms: price = match_ms["price"]

        img_urls = []
        spec_imgs = spec.get("relationships", {}).get("images", {}).get("data", [])
        if isinstance(spec_imgs, list):
            for simg in spec_imgs:
                sid = str(simg.get("id"))
                if sid in images_map and images_map[sid] not in img_urls:
                    img_urls.append(images_map[sid])

        if k_clean not in pos_catalog:
            pos_catalog[k_clean] = {
                "title": title,
                "price": price,
                "stock": 5.0, # Всегда в наличии
                "image_urls": img_urls,
                "folder_id": FOLDER_AUTH_ID,
                "create_if_missing": True
            }

    if len(specs) < 100: break
    page += 1

auth_bouquets = [v["title"] for v in pos_catalog.values() if v.get("folder_id") == FOLDER_AUTH_ID]
print(f"Всего авторских букетов (рецептов): {len(auth_bouquets)}")

# 2.3 Номенклатура цветов через /catalog
print("Сбор остатков цветов через /catalog...")
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
        k_raw = title.lower()

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
        if item_price <= 0:
            match_ms = ms_goods_by_name.get(k_raw) or ms_goods_by_name.get(k_clean)
            if match_ms: item_price = match_ms["price"]

        capped_qty = min(item_stock, float(MAX_STOCK_FLOWERS)) if item_stock > 0 else 0.0

        if k_clean not in pos_catalog:
            pos_catalog[k_clean] = {
                "title": title, 
                "price": item_price, 
                "stock": capped_qty, 
                "image_urls": [], 
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

print(f"Всего позиций в сводном каталоге: {len(pos_catalog)}")

# --- 3. Создание новинок и догрузка отсутствующих фото ---
print("\n3. Создание недостающих товаров и проверка фото...")
created_count = 0
for key, item_data in pos_catalog.items():
    raw_key = item_data["title"].lower()
    matched = ms_goods_by_name.get(key) or ms_goods_by_name.get(raw_key)

    # 3.1 Создание карточки, если её нет
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
        
        res_create = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        
        if res_create.status_code in (200, 201):
            created = res_create.json()
            created_id = created["id"]
            matched = {"id": created_id, "name": created["name"], "href": created["meta"]["href"], "has_images": False, "price": item_data["price"]}
            ms_goods_by_name[key] = matched
            ms_goods_by_name[raw_key] = matched
            ms_goods_by_id[created_id] = matched
            created_count += 1
            folder_label = "Витрина Tilda" if item_data['folder_id'] == FOLDER_VITRINA_ID else "Авторские букеты"
            print(f"  [+] Создан в [{folder_label}]: {item_data['title']} ({item_data['price']/100:.0f} руб.)")
        elif res_create.status_code == 429:
            time.sleep(2.0)

    # 3.2 Докачиваем фото, если у товара в МоемСкладе их до сих пор нет
    if matched and not matched.get("has_images") and item_data.get("image_urls"):
        for idx, img_url in enumerate(item_data["image_urls"]):
            try:
                img_data = requests.get(img_url, timeout=15).content
                ms_session.post(
                    f"{MOYSKLAD_URL}/entity/product/{matched['id']}/images", 
                    json={"filename": f"photo_{idx}.jpg", "content": base64.b64encode(img_data).decode("utf-8")}, 
                    timeout=15
                )
                matched["has_images"] = True
            except Exception:
                pass

print(f"Создано новинок: {created_count}")

# --- 4. Выравнивание остатков ---
print("\n4. Корректировка остатков...")
stocks_by_store = {}
r_stocks = ms_session.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45).json()
for row in r_stocks:
    if row.get("assortmentId") and row.get("storeId"):
        stocks_by_store[(row["assortmentId"], row["storeId"])] = float(row.get("stock", 0))

enter_positions = []
loss_positions = []

for key, item_data in pos_catalog.items():
    raw_key = item_data["title"].lower()
    matched = ms_goods_by_name.get(key) or ms_goods_by_name.get(raw_key)
    if not matched: continue
    target_stock = item_data["stock"]
    cur_stock = stocks_by_store.get((matched["id"], TARGET_STORE_ID), 0.0)
    
    if cur_stock < target_stock and target_stock > 0:
        enter_positions.append({
            "quantity": round(target_stock - cur_stock, 2), 
            "price": matched["price"], 
            "assortment": {"meta": {"href": matched["href"], "type": "product", "mediaType": "application/json"}}
        })

for (a_id, s_id), cur_stock in stocks_by_store.items():
    if s_id != TARGET_STORE_ID or cur_stock <= 0: continue
    good = ms_goods_by_id.get(a_id)
    if not good: continue
    good_name = good.get("name", "")
    target = pos_catalog.get(norm_name(good_name), {}).get("stock")
    if target is None:
        target = pos_catalog.get(good_name.lower(), {}).get("stock", 0.0)
        
    if cur_stock > target:
        loss_positions.append({
            "quantity": round(cur_stock - target, 2), 
            "assortment": {"meta": {"href": good["href"], "type": "product", "mediaType": "application/json"}}
        })

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json={"organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}}, "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}}, "applicable": True, "description": "Автовыравнивание (Posiflora -> МойСклад)", "positions": enter_positions[i:i+1000]}, timeout=60)
    print(f"  [+] Оприходовано: {len(enter_positions)} поз.")

if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/loss", json={"organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}}, "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}}, "applicable": True, "description": "Автосписание лишних остатков", "positions": loss_positions[i:i+1000]}, timeout=60)
    print(f"  [-] Списано: {len(loss_positions)} поз.")

print("\nСинхронизация полностью завершена!")
