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

# Папки в МойСклад
FOLDER_VITRINA_ID = "57cbfb03-af7b-11f1-0a80-00ba00393e19"  # Цветы лакма/Витрина Tilda
FOLDER_AUTH_ID    = "b5104917-b72b-11f1-0a80-00cf000d73af"  # Цветы лакма/Каталог/Авторские букеты
FOLDER_FLOWERS_ID = "a69d7682-b72b-11f1-0a80-0c1e000dacd4"  # Цветы лакма/Каталог/Цветы

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"    # Склад Лакма Цветы
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

MAX_STOCK_FLOWERS = 35

def norm_name(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip().lower()

# --- Сессии ---
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

# --- 1. Сбор существующих товаров из МойСклад ---
print("1. Получение существующих товаров из МойСклад...")
ms_goods_by_name = {}
ms_goods_by_id = {}
default_price_meta = None
offset = 0

while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/assortment?limit=100&offset={offset}", timeout=30)
    if res.status_code == 429:
        time.sleep(2.0)
        continue
    rows = res.json().get("rows", [])
    if not rows: break
    for item in rows:
        clean = norm_name(item.get("name"))
        if not default_price_meta and item.get("salePrices"):
            pt_meta = item["salePrices"][0].get("priceType", {}).get("meta")
            if pt_meta:
                default_price_meta = pt_meta

        info = {
            "id": item["id"],
            "name": item.get("name", "").strip(),
            "href": item["meta"]["href"],
            "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0
        }
        ms_goods_by_name[clean] = info
        ms_goods_by_id[item["id"]] = info
    offset += len(rows)
    if len(rows) < 100: break
print(f"Всего позиций в базе МоегоСклада: {len(ms_goods_by_name)}")

# --- 2. Сбор каталога Posiflora ---
print("\n2. Сбор каталога Posiflora...")
pos_catalog = {}

# 2.1 Актуальная витрина букетов (папка «Витрина Tilda»)
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
        raw_price = attrs.get("saleAmount") or attrs.get("salePrice") or attrs.get("priceMax") or attrs.get("amount") or 0
        price = float(raw_price) * 100
        
        if not title: continue
        k = norm_name(title)
        if price <= 0 and k in ms_goods_by_name:
            price = ms_goods_by_name[k]["price"]

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

        if k not in pos_catalog:
            pos_catalog[k] = {
                "title": title, 
                "price": price, 
                "stock": 1.0 if is_on_showcase else 0.0, 
                "image_urls": img_urls,
                "folder_id": FOLDER_VITRINA_ID,
                "create_if_missing": True
            }
        else:
            if is_on_showcase:
                pos_catalog[k]["stock"] = 1.0
                
    if len(b_data) < 100: break
    page += 1

active_bouquets = [v["title"] for v in pos_catalog.values() if v.get("folder_id") == FOLDER_VITRINA_ID and v["stock"] > 0]
print(f"Активных витринных букетов Posiflora: {len(active_bouquets)}")

# 2.2 Авторские букеты / Рецепты (папка «Авторские букеты»)
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
            if url:
                images_map[str(inc["id"])] = url

    for spec in specs:
        attrs = spec.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        if not title or status == "off":
            continue
        
        k = norm_name(title)
        raw_price = attrs.get("minPrice") or attrs.get("maxPrice") or attrs.get("price") or 0
        price = float(raw_price) * 100

        if price <= 0 and k in ms_goods_by_name:
            price = ms_goods_by_name[k]["price"]

        img_urls = []
        spec_imgs = spec.get("relationships", {}).get("images", {}).get("data", [])
        if isinstance(spec_imgs, list):
            for simg in spec_imgs:
                sid = str(simg.get("id"))
                if sid in images_map and images_map[sid] not in img_urls:
                    img_urls.append(images_map[sid])

        # Авторские букеты всегда в наличии (остаток 5)
        if k not in pos_catalog:
            pos_catalog[k] = {
                "title": title,
                "price": price,
                "stock": 5.0,
                "image_urls": img_urls,
                "folder_id": FOLDER_AUTH_ID,
                "create_if_missing": True
            }

    if len(specs) < 100: break
    page += 1

auth_bouquets = [v["title"] for v in pos_catalog.values() if v.get("folder_id") == FOLDER_AUTH_ID]
print(f"Всего авторских букетов (рецептов): {len(auth_bouquets)}")

# 2.3 Номенклатура цветов и складские остатки через /catalog
print("Сбор остатков цветов через /catalog...")
page = 1
while True:
    params = {
        "page[number]": page,
        "page[size]": 100,
        "include": "category,prices,balances",
        "sort": "title",
        "distribution": "true",
        "concealment": "false"
    }
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
            if stock > 0:
                balances_map[inc_id] = stock
        elif inc_type == "prices":
            price_val = float(attrs.get("price") or attrs.get("value") or 0) * 100
            if price_val > 0:
                prices_map[inc_id] = price_val

    for item in items:
        attrs = item.get("attributes", {})
        title = attrs.get("title", "").strip()
        if not title: continue
        k = norm_name(title)

        bal_rel = item.get("relationships", {}).get("balances", {}).get("data", [])
        prices_rel = item.get("relationships", {}).get("prices", {}).get("data", [])
        
        item_stock = 0.0
        item_price = 0.0

        if isinstance(bal_rel, list):
            for b in bal_rel:
                bid = str(b.get("id", ""))
                if bid in balances_map:
                    item_stock += balances_map[bid]
        elif isinstance(bal_rel, dict):
            bid = str(bal_rel.get("id", ""))
            if bid in balances_map:
                item_stock += balances_map[bid]

        if isinstance(prices_rel, list):
            for p in prices_rel:
                pid = str(p.get("id", ""))
                if pid in prices_map and item_price <= 0:
                    item_price = prices_map[pid]
        elif isinstance(prices_rel, dict):
            pid = str(prices_rel.get("id", ""))
            if pid in prices_map:
                item_price = prices_map[pid]

        if item_price <= 0:
            raw_p = attrs.get("priceMax") or attrs.get("salePrice") or attrs.get("price") or 0
            item_price = float(raw_p) * 100
        if item_price <= 0 and k in ms_goods_by_name:
            item_price = ms_goods_by_name[k]["price"]

        capped_qty = min(item_stock, float(MAX_STOCK_FLOWERS)) if item_stock > 0 else 0.0

        if k not in pos_catalog:
            pos_catalog[k] = {
                "title": title, 
                "price": item_price, 
                "stock": capped_qty, 
                "image_urls": [], 
                "folder_id": FOLDER_FLOWERS_ID,
                "create_if_missing": False  # Цветы не дублируем, только обновляем остатки
            }
        else:
            if capped_qty > 0:
                pos_catalog[k]["stock"] = capped_qty
            if item_price > 0 and pos_catalog[k]["price"] <= 0:
                pos_catalog[k]["price"] = item_price

    if len(items) < 100: break
    page += 1

print(f"Всего позиций в сводном каталоге: {len(pos_catalog)}")

# --- 3. Создание новинок (Букеты витрины и Авторские букеты) ---
print("\n3. Создание недостающих товаров в нужные папки...")
created_count = 0
for key, item_data in pos_catalog.items():
    if not item_data.get("create_if_missing"):
        continue

    if key not in ms_goods_by_name:
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
            ms_goods_by_name[key] = {"id": created_id, "name": created["name"], "href": created["meta"]["href"], "price": item_data["price"]}
            ms_goods_by_id[created_id] = ms_goods_by_name[key]
            created_count += 1
            folder_label = "Витрина Tilda" if item_data['folder_id'] == FOLDER_VITRINA_ID else "Авторские букеты"
            print(f"  [+] Создан в [{folder_label}]: {item_data['title']} ({item_data['price']/100:.0f} руб.)")
            
            # Загрузка всех фотографий галереи
            for idx, img_url in enumerate(item_data.get("image_urls", [])):
                try:
                    img_data = requests.get(img_url, timeout=15).content
                    ms_session.post(
                        f"{MOYSKLAD_URL}/entity/product/{created_id}/images", 
                        json={"filename": f"photo_{idx}.jpg", "content": base64.b64encode(img_data).decode("utf-8")}, 
                        timeout=15
                    )
                except Exception:
                    pass
        elif res_create.status_code == 429:
            time.sleep(2.0)
        else:
            print(f"  [!] Ошибка создания '{item_data['title']}': {res_create.status_code} - {res_create.text}")

print(f"Создано новинок: {created_count}")

# --- 4. Выравнивание остатков на «Склад Лакма Цветы» ---
print("\n4. Корректировка остатков...")
stocks_by_store = {}
r_stocks = ms_session.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45).json()
for row in r_stocks:
    if row.get("assortmentId") and row.get("storeId"):
        stocks_by_store[(row["assortmentId"], row["storeId"])] = float(row.get("stock", 0))

enter_positions = []
loss_positions = []

for key, item_data in pos_catalog.items():
    matched = ms_goods_by_name.get(key)
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
    target = pos_catalog.get(norm_name(good.get("name", "")), {}).get("stock", 0.0)
    if cur_stock > target:
        loss_positions.append({
            "quantity": round(cur_stock - target, 2), 
            "assortment": {"meta": {"href": good["href"], "type": "product", "mediaType": "application/json"}}
        })

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}}, 
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}}, 
            "applicable": True, 
            "description": "Автовыравнивание (Posiflora -> МойСклад)", 
            "positions": enter_positions[i:i+1000]
        }, timeout=60)
    print(f"  [+] Оприходовано: {len(enter_positions)} поз. на Склад Лакма Цветы")

if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/loss", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}}, 
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}}, 
            "applicable": True, 
            "description": "Автосписание лишних остатков", 
            "positions": loss_positions[i:i+1000]
        }, timeout=60)
    print(f"  [-] Списано: {len(loss_positions)} поз.")

print("\nСинхронизация полностью завершена!")
