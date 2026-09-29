import os
import requests
import time

# --- НАСТРОЙКИ POSIFLORA ---
POSIFLORA_USER = os.getenv("POSIFLORA_USER", "cvetistroginorf")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS", "19478893")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"
STORE_ID = "cde3de25-51e3-4193-a164-84cdebd01e94"

# --- НАСТРОЙКИ МОЙСКЛАД ---
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "d528d07b79305bf9859c706da2cbd62c7777ecf0")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"
STORE_HREF = f"https://api.moysklad.ru/api/remap/1.2/entity/store/{TARGET_STORE_ID}"
ORG_HREF = "https://api.moysklad.ru/api/remap/1.2/entity/organization/b34f89b3-c981-11ee-0a80-07770038ef19"

MAX_STOCK_FLOWERS = 35

# --- 1. АВТОРИЗАЦИЯ ---
pos_session = requests.Session()
pos_session.trust_env = False
pos_session.headers.update({
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
})

resp = pos_session.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)
token = resp.json()["data"]["attributes"]["accessToken"]
pos_session.headers.update({"Authorization": f"Bearer {token}"})

ms_session = requests.Session()
ms_session.trust_env = False
ms_session.headers.update({
    "Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}",
    "Content-Type": "application/json"
})

# --- 2. СБОР СУЩЕСТВУЮЩИХ ТОВАРОВ ИЗ МОЙСКЛАД ---
print("1. Загрузка справочника товаров из МоегоСклада...")
ms_goods_by_name = {}
offset = 0
while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}", timeout=30)
    if res.status_code == 429:
        time.sleep(2.0)
        continue
    data = res.json()
    rows = data.get("rows", [])
    if not rows:
        break
    for item in rows:
        name_clean = item["name"].strip().lower()
        ms_goods_by_name[name_clean] = {
            "id": item["id"],
            "name": item["name"].strip(),
            "href": item["meta"]["href"],
            "price": item.get("salePrices", [{}])[0].get("value", 0)
        }
    offset += len(rows)
    if len(rows) < 100:
        break

print(f"В МоемСкладе найдено товаров: {len(ms_goods_by_name)}")

# --- 3. СБОР ВСЕХ ТОВАРОВ ИЗ POSIFLORA (ПОСТОЯННЫЕ + ВИТРИНА) ---
print("\n2. Сбор каталога и номенклатуры из Posiflora...")
pos_catalog = {}  # {name_clean: {"title": ..., "price": ..., "stock": ..., "image_url": ...}}

# А. Букеты витрины
page = 1
while True:
    r = pos_session.get(f"{POSIFLORA_URL}/bouquets?page[size]=100&page[number]={page}&include=logo", timeout=25)
    if r.status_code != 200:
        break
    res = r.json()
    b_data = res.get("data", [])
    if not b_data:
        break
    
    # Сбор ссылок на фото из included
    logos = {str(inc["id"]): inc.get("attributes", {}).get("file", {}).get("url") 
             for inc in res.get("included", []) if inc.get("type") == "files"}

    for b in b_data:
        attrs = b.get("attributes", {})
        title = attrs.get("title", "").strip()
        status = attrs.get("status", "")
        price = float(attrs.get("price", 0) or 0) * 100  # МойСклад хранит цены в копейках
        
        logo_rel = b.get("relationships", {}).get("logo", {}).get("data")
        logo_id = str(logo_rel.get("id")) if logo_rel else None
        img_url = logos.get(logo_id)

        stock = 1.0 if status in ("on_window", "created", "active") else 0.0

        if title:
            pos_catalog[title.lower()] = {
                "title": title,
                "price": price,
                "stock": stock,
                "image_url": img_url
            }
    if len(b_data) < 100:
        break
    page += 1
print(f"-> Букетов витрины получено: {len(pos_catalog)}")

# Б. Основная номенклатура (срезанные цветы, игрушки, декор, подарки)
page = 1
items_added = 0
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
        raw_qty = attrs.get("qty") or attrs.get("quantity") or attrs.get("balance") or attrs.get("currentQty") or 0
        qty = float(raw_qty)
        price = float(attrs.get("price", 0) or attrs.get("salePrice", 0) or 0) * 100

        capped_qty = min(qty, float(MAX_STOCK_FLOWERS)) if qty > 0 else 0.0

        key = title.lower()
        if key not in pos_catalog:
            pos_catalog[key] = {
                "title": title,
                "price": price,
                "stock": capped_qty,
                "image_url": None
            }
            items_added += 1
        else:
            if qty > 0:
                pos_catalog[key]["stock"] = capped_qty
    if len(i_data) < 100:
        break
    page += 1
print(f"-> Постоянных позиций (номенклатура) добавлено: {items_added}")
print(f"Итого уникальных товаров в Posiflora: {len(pos_catalog)}")

# --- 4. АВТОМАТИЧЕСКОЕ СОЗДАНИЕ НОВЫХ ТОВАРОВ В МОЙСКЛАД ---
print("\n3. Проверка на новинки и создание недостающих товаров в МоемСкладе...")
created_count = 0
for key, item_data in pos_catalog.items():
    if key not in ms_goods_by_name:
        # Создаем товар
        payload = {
            "name": item_data["title"],
            "salePrices": [{"value": item_data["price"], "priceType": {"name": "Цена продажи"}}] if item_data["price"] > 0 else []
        }
        res_create = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        if res_create.status_code in (200, 201):
            created_prod = res_create.json()
            ms_goods_by_name[key] = {
                "id": created_prod["id"],
                "name": created_prod["name"],
                "href": created_prod["meta"]["href"],
                "price": item_data["price"]
            }
            created_count += 1
            print(f"  [+] Создан новый товар: {item_data['title']}")

            # Если есть фото — прикрепляем к карточке
            if item_data["image_url"]:
                try:
                    img_data = requests.get(item_data["image_url"], timeout=15).content
                    import base64
                    b64_img = base64.b64encode(img_data).decode("utf-8")
                    ms_session.post(
                        f"{MOYSKLAD_URL}/entity/product/{created_prod['id']}/images",
                        json={"filename": "photo.jpg", "content": b64_img},
                        timeout=20
                    )
                except Exception:
                    pass
        elif res_create.status_code == 429:
            time.sleep(2.0)

print(f"Создано новых товаров в МойСклад: {created_count}")

# --- 5. СИНХРОНИЗАЦИЯ ОСТАТКОВ ---
print("\n4. Синхронизация остатков по складу «Склад Лакма Цветы»...")
stocks_by_store = {}
r_stocks = ms_session.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
if r_stocks.status_code == 200:
    for row in r_stocks.json():
        a_id = row.get("assortmentId")
        s_id = row.get("storeId")
        stock = float(row.get("stock", 0) or 0)
        if a_id and s_id:
            stocks_by_store[(a_id, s_id)] = stock

enter_positions = []
loss_positions_by_store = {}

# Оприходование
for key, item_data in pos_catalog.items():
    target_stock = item_data["stock"]
    if target_stock <= 0:
        continue
    matched = ms_goods_by_name.get(key)
    if not matched:
        continue
    
    cur_stock = stocks_by_store.get((matched["id"], TARGET_STORE_ID), 0.0)
    if cur_stock < target_stock:
        diff = round(target_stock - cur_stock, 2)
        if diff > 0:
            enter_positions.append({
                "quantity": diff,
                "price": matched["price"],
                "assortment": {"meta": {"href": matched["href"], "type": "product", "mediaType": "application/json"}}
            })

# Списание излишков
for (a_id, s_id), cur_store_stock in stocks_by_store.items():
    if cur_store_stock <= 0:
        continue
    # Ищем товар
    matched = next((g for g in ms_goods_by_name.values() if g["id"] == a_id), None)
    if not matched:
        continue
    target = pos_catalog.get(matched["name"].lower(), {}).get("stock", 0.0) if s_id == TARGET_STORE_ID else 0.0
    if cur_store_stock > target:
        diff = round(cur_store_stock - target, 2)
        if diff > 0:
            if s_id not in loss_positions_by_store:
                loss_positions_by_store[s_id] = []
            loss_positions_by_store[s_id].append({
                "quantity": diff,
                "assortment": {"meta": {"href": matched["href"], "type": "product", "mediaType": "application/json"}}
            })

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        chunk = enter_positions[i:i + 1000]
        ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Синхронизация фактических остатков",
            "positions": chunk
        }, timeout=60)
    print(f"  [+] Оприходовано позиций: {len(enter_positions)}")

for s_id, batch in loss_positions_by_store.items():
    s_href = f"https://api.moysklad.ru/api/remap/1.2/entity/store/{s_id}"
    for i in range(0, len(batch), 1000):
        chunk = batch[i:i + 1000]
        ms_session.post(f"{MOYSKLAD_URL}/entity/loss", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": s_href, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Списание излишков под витрину",
            "positions": chunk
        }, timeout=60)
    print(f"  [-] Списано излишков ({len(batch)} поз.) для склада {s_id}")

print("\nСинхронизация полностью завершена. Все новинки созданы, остатки откалиброваны.")