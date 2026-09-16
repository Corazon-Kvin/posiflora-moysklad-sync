import os
import requests

POSIFLORA_USER = os.getenv("POSIFLORA_USER")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"
STORE_ID = os.getenv("STORE_ID", "cde3de25-51e3-4193-a164-84cdebd01e94")

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

STORE_HREF = "https://api.moysklad.ru/api/remap/1.2/entity/store/fc24f096-af79-11f1-0a80-0c0a0039483e"
ORG_HREF = "https://api.moysklad.ru/api/remap/1.2/entity/organization/b34f89b3-c981-11ee-0a80-07770038ef19"
TARGET_FOLDER_NAME = "Витрина Tilda"

# --- 1. АВТОРИЗАЦИЯ В POSIFLORA ---
pos_session = requests.Session()
pos_session.headers.update({
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
})

resp = pos_session.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=25)

if resp.status_code != 200:
    print(f"Ошибка входа в Posiflora ({resp.status_code}):", resp.text)
    exit(1)

token = resp.json()["data"]["attributes"]["accessToken"]
pos_session.headers.update({"Authorization": f"Bearer {token}"})

# --- 2. АВТОРИЗАЦИЯ В МОЙСКЛАД ---
ms_session = requests.Session()
ms_session.headers.update({
    "Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}",
    "Content-Type": "application/json"
})

# Находим группу «Витрина Tilda» в МоемСкладе
print("1. Поиск папки 'Витрина Tilda' в МойСклад...")
folder_res = ms_session.get(f"{MOYSKLAD_URL}/entity/productfolder?filter=name={TARGET_FOLDER_NAME}", timeout=25).json()
folder_rows = folder_res.get("rows", [])
product_folder_meta = None
if folder_rows:
    product_folder_meta = folder_rows[0]["meta"]
    print(f"-> Папка найдена: {folder_rows[0]['name']}")
else:
    print(f"Внимание: папка '{TARGET_FOLDER_NAME}' не найдена! Товары будут создаваться в корне каталога.")

# Получаем существующие товары из МойСклад
print("\n2. Получение текущего каталога из МойСклад...")
ms_goods = {}
offset = 0
while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}", timeout=30)
    data = res.json()
    rows = data.get("rows", [])
    if not rows:
        break
    for item in rows:
        sale_price = 0
        if item.get("salePrices") and len(item["salePrices"]) > 0:
            sale_price = item["salePrices"][0].get("value", 0)
        
        ms_goods[item["name"].strip().lower()] = {
            "name": item["name"].strip(),
            "href": item["meta"]["href"],
            "price": sale_price
        }
    offset += len(rows)
    if offset >= data.get("meta", {}).get("size", 0):
        break

print(f"Загружено существующих товаров из МойСклад: {len(ms_goods)}")

# --- 3. ВЫГРУЗКА ДАННЫХ ИЗ POSIFLORA (ОСТАТКИ + ЦЕНЫ) ---
print("\n3. Выгрузка каталога, остатков и цен из Posiflora...")
page = 1
pos_items_data = {}  # { title: { "qty": stock, "price_kopecks": price } }

while True:
    params = {
        "page[number]": page,
        "page[size]": 50,
        "include": "category,logo,prices,balances",
        "sort": "title",
        "distribution": "true",
        "concealment": "false",
        "filter[store]": STORE_ID
    }
    r = pos_session.get(f"{POSIFLORA_URL}/catalog", params=params, timeout=25)
    if r.status_code != 200:
        break

    res_json = r.json()
    items = res_json.get("data", [])
    if not items:
        break

    included = res_json.get("included", [])
    
    # Собираем остатки
    balances_map = {}
    prices_map = {}
    for inc in included:
        inc_type = inc.get("type")
        inc_id = str(inc.get("id", ""))
        attrs = inc.get("attributes", {})
        
        if inc_type == "item-balances":
            stock = float(attrs.get("qtyOnWindow", 0) or 0)
            if stock > 0:
                balances_map[inc_id] = stock
        elif inc_type == "prices":
            # Розничная цена в копейках
            price_val = attrs.get("amount", 0) or 0
            if price_val > 0:
                prices_map[inc_id] = int(price_val)

    for item in items:
        title = item.get("attributes", {}).get("title", "").strip()
        item_id = str(item.get("id", ""))
        prices_rel = item.get("relationships", {}).get("prices", {}).get("data", [])
        
        item_stock = 0.0
        item_price_kopecks = 0

        # Поиск цены и остатка
        if isinstance(prices_rel, list):
            for p in prices_rel:
                pid = str(p.get("id", ""))
                if pid in balances_map:
                    item_stock += balances_map[pid]
                if pid in prices_map and item_price_kopecks == 0:
                    item_price_kopecks = prices_map[pid]
        elif isinstance(prices_rel, dict):
            pid = str(prices_rel.get("id", ""))
            if pid in balances_map:
                item_stock += balances_map[pid]
            if pid in prices_map:
                item_price_kopecks = prices_map[pid]

        composite_key = f"{item_id}-{STORE_ID}"
        if composite_key in balances_map:
            item_stock += balances_map[composite_key]

        if item_stock > 0 and title:
            pos_items_data[title] = {
                "qty": item_stock,
                "price": item_price_kopecks
            }

    if len(items) < 50:
        break
    page += 1

print(f"Найдено активных позиций в наличии в Posiflora: {len(pos_items_data)}")

# --- 4. АВТОСОЗДАНИЕ И ОБНОВЛЕНИЕ КАРТОЧЕК В МОЙСКЛАД ---
print("\n4. Синхронизация каталога товаров (создание новых и обновление цен)...")

for title, item_info in pos_items_data.items():
    norm_title = title.lower()
    price_kopecks = item_info["price"]
    
    # 4.1 Если товара нет в МойСклад — создаем его
    if norm_title not in ms_goods:
        print(f"-> Создаем новый товар в МойСклад: '{title}' (цена: {price_kopecks / 100:.2f} ₽)")
        new_prod_payload = {
            "name": title,
            "salePrices": [{"value": price_kopecks}] if price_kopecks > 0 else []
        }
        if product_folder_meta:
            new_prod_payload["productFolder"] = {"meta": product_folder_meta}

        create_res = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=new_prod_payload, timeout=20)
        if create_res.status_code in (200, 201):
            created_data = create_res.json()
            ms_goods[norm_title] = {
                "name": title,
                "href": created_data["meta"]["href"],
                "price": price_kopecks
            }
        else:
            print(f"   Ошибка создания '{title}':", create_res.text)

    # 4.2 Если товар уже есть, но цена не задана (0) или изменилась
    else:
        current = ms_goods[norm_title]
        if price_kopecks > 0 and current["price"] != price_kopecks:
            print(f"-> Обновляем цену для '{title}': было {current['price'] / 100:.2f} ₽, стало {price_kopecks / 100:.2f} ₽")
            update_payload = {
                "salePrices": [{"value": price_kopecks}]
            }
            upd_res = ms_session.put(current["href"], json=update_payload, timeout=20)
            if upd_res.status_code == 200:
                current["price"] = price_kopecks

# --- 5. ФОРМИРОВАНИЕ И ПРОВОДКА ОПРИХОДОВАНИЯ ---
print("\n5. Формирование строк оприходования остатков...")
positions = []
for title, item_info in pos_items_data.items():
    matched = ms_goods.get(title.lower())
    if matched:
        positions.append({
            "quantity": item_info["qty"],
            "price": matched["price"],
            "assortment": {
                "meta": {
                    "href": matched["href"],
                    "type": "product",
                    "mediaType": "application/json"
                }
            }
        })

print(f"Готово к оприходованию: {len(positions)} позиций")

if positions:
    print("\n6. Отправка документа 'Оприходование' в МойСклад...")
    payload = {
        "organization": {
            "meta": {
                "href": ORG_HREF,
                "type": "organization",
                "mediaType": "application/json"
            }
        },
        "store": {
            "meta": {
                "href": STORE_HREF,
                "type": "store",
                "mediaType": "application/json"
            }
        },
        "applicable": True,
        "description": "Автосинхронизация остатков и цен из Posiflora (GitHub Actions)",
        "positions": positions
    }

    doc_res = ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json=payload, timeout=60)
    if doc_res.status_code in (200, 201):
        print("\n>>> УСПЕШНО: Каталог актуализирован, остатки и цены оприходованы в МойСклад!")
    else:
        print(f"Ошибка оприходования ({doc_res.status_code}):", doc_res.text)
else:
    print("Позиций для оприходования не найдено.")
