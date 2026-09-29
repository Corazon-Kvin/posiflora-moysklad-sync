import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()

# --- Настройки ---
POSIFLORA_USER = os.getenv("POSIFLORA_USER", "cvetistroginorf")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS", "19478893")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "d528d07b79305bf9859c706da2cbd62c7777ecf0")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

TARGET_FOLDER_ID = "d1bd2ba3-af7a-11f1-0a80-02f300382021"
TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

FOLDER_HREF = f"{MOYSKLAD_URL}/entity/productfolder/{TARGET_FOLDER_ID}"
STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

MAX_STOCK_FLOWERS = 35

# --- Авторизация ---
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

# --- 1. Сбор товаров из МоегоСклада ---
print("1. Получение товаров из МойСклад...")
ms_goods_by_name = {}
ms_goods_by_id = {}
offset = 0
while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}", timeout=30)
    if res.status_code == 429:
        time.sleep(2.0)
        continue
    rows = res.json().get("rows", [])
    if not rows: break
    for item in rows:
        clean_name = item["name"].strip().lower()
        info = {
            "id": item["id"],
            "name": item["name"].strip(),
            "href": item["meta"]["href"],
            "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0
        }
        ms_goods_by_name[clean_name] = info
        ms_goods_by_id[item["id"]] = info
    offset += len(rows)
    if len(rows) < 100: break
print(f"Всего товаров в МоемСкладе: {len(ms_goods_by_name)}")

# --- 2. Сбор каталога из Posiflora ---
print("\n2. Сбор каталога из Posiflora...")
pos_catalog = {}

# Букеты витрины (сортируем по свежести sort=-createdAt, ищем статус demonstrated)
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/bouquets?sort=-createdAt&page[size]=100&page[number]={page}&include=logo", timeout=25)
    if r.status_code != 200:
        break
    res_data = r.json()
    b_data = res_data.get("data", [])
    if not b_data: 
        break
    
    logos = {str(inc["id"]): inc.get("attributes", {}).get("file", {}).get("url") for inc in res_data.get("included", []) if inc.get("type") == "files"}
    
    for b in b_data:
        attrs = b.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        completed_at = attrs.get("completedAt")
        
        # Витринный букет: статус demonstrated и не закрыт/не продан
        is_on_showcase = (status == "demonstrated" and not completed_at)
        
        raw_price = attrs.get("saleAmount") or attrs.get("amount") or attrs.get("trueSaleAmount") or attrs.get("price") or 0
        price = float(raw_price) * 100
        
        if not title:
            continue
            
        if price <= 0 and title.lower() in ms_goods_by_name:
            price = ms_goods_by_name[title.lower()]["price"]
            
        if price <= 0:
            continue

        logo_rel = b.get("relationships", {}).get("logo", {}).get("data")
        logo_id = str(logo_rel.get("id")) if logo_rel else None

        k = title.lower()
        if k not in pos_catalog:
            pos_catalog[k] = {
                "title": title, 
                "price": price, 
                "stock": 1.0 if is_on_showcase else 0.0,
                "image_url": logos.get(logo_id)
            }
        else:
            if is_on_showcase:
                pos_catalog[k]["stock"] = 1.0
                
    if len(b_data) < 100:
        break
    # Ограничиваемся первыми 200 последними созданными букетами для максимальной скорости
    if page >= 2:
        break
    page += 1

active_bouquets = [v["title"] for v in pos_catalog.values() if v["stock"] > 0]
print(f"Активных витринных букетов Posiflora: {len(active_bouquets)}")

# Номенклатура цветов
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/inventory-items?page[size]=100&page[number]={page}", timeout=25)
    if r.status_code != 200:
        break
    i_data = r.json().get("data", [])
    if not i_data: 
        break
    for it in i_data:
        attrs = it.get("attributes", {})
        title = attrs.get("title", "").strip()
        if not title: 
            continue
        qty = float(attrs.get("qty") or attrs.get("quantity") or attrs.get("balance") or attrs.get("currentQty") or 0)
        raw_price = attrs.get("saleAmount") or attrs.get("salePrice") or attrs.get("price") or attrs.get("costPrice") or 0
        price = float(raw_price) * 100

        if price <= 0 and title.lower() in ms_goods_by_name:
            price = ms_goods_by_name[title.lower()]["price"]

        if price <= 0 and qty <= 0:
            continue
        
        capped_qty = min(qty, float(MAX_STOCK_FLOWERS)) if qty > 0 else 0.0
        if title.lower() not in pos_catalog:
            pos_catalog[title.lower()] = {"title": title, "price": price, "stock": capped_qty, "image_url": None}
        elif qty > 0:
            pos_catalog[title.lower()]["stock"] = capped_qty
    if len(i_data) < 100:
        break
    page += 1

print(f"Всего актуальных позиций (букеты + цветы): {len(pos_catalog)}")

# --- 3. Создание новинок в папке «Цветы лакма» ---
print("\n3. Создание недостающих товаров...")
created_count = 0
for key, item_data in pos_catalog.items():
    if key not in ms_goods_by_name:
        payload = {
            "name": item_data["title"],
            "productFolder": {"meta": {"href": FOLDER_HREF, "type": "productfolder", "mediaType": "application/json"}},
            "salePrices": [{"value": item_data["price"], "priceType": {"name": "Цена продажи"}}] if item_data["price"] > 0 else []
        }
        res_create = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        if res_create.status_code in (200, 201):
            created = res_create.json()
            info = {
                "id": created["id"], 
                "name": created["name"],
                "href": created["meta"]["href"], 
                "price": item_data["price"]
            }
            ms_goods_by_name[key] = info
            ms_goods_by_id[created["id"]] = info
            created_count += 1
            print(f"  [+] Создан: {item_data['title']}")
            
            if item_data["image_url"]:
                try:
                    import base64
                    img_data = requests.get(item_data["image_url"], timeout=15).content
                    ms_session.post(f"{MOYSKLAD_URL}/entity/product/{created['id']}/images", json={"filename": "photo.jpg", "content": base64.b64encode(img_data).decode("utf-8")}, timeout=15)
                except: 
                    pass
        elif res_create.status_code == 429:
            time.sleep(2.0)
print(f"Создано новинок: {created_count}")

# --- 4. Калибровка остатков на Склад Лакма Цветы ---
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
    if not matched: 
        continue
    target_stock = item_data["stock"]
    cur_stock = stocks_by_store.get((matched["id"], TARGET_STORE_ID), 0.0)
    
    if cur_stock < target_stock and target_stock > 0:
        enter_positions.append({
            "quantity": round(target_stock - cur_stock, 2), 
            "price": matched["price"], 
            "assortment": {"meta": {"href": matched["href"], "type": "product", "mediaType": "application/json"}}
        })

# Списание лишних/проданных букетов
for (a_id, s_id), cur_stock in stocks_by_store.items():
    if s_id != TARGET_STORE_ID or cur_stock <= 0: 
        continue
    good = ms_goods_by_id.get(a_id)
    if not good: 
        continue
    target = pos_catalog.get(good.get("name", "").lower(), {}).get("stock", 0.0)
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
            "description": "Автовыравнивание (Posiflora витрина)", 
            "positions": enter_positions[i:i+1000]
        }, timeout=60)
    print(f"  [+] Оприходовано: {len(enter_positions)} поз. на Склад Лакма Цветы")

if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        ms_session.post(f"{MOYSKLAD_URL}/entity/loss", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}}, 
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}}, 
            "applicable": True, 
            "description": "Автосписание проданных/снятых позиций", 
            "positions": loss_positions[i:i+1000]
        }, timeout=60)
    print(f"  [-] Списано: {len(loss_positions)} поз.")

print("\nСинхронизация полностью завершена!")
