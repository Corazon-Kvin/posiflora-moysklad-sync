import os
import requests

POSIFLORA_USER = os.getenv("POSIFLORA_USER")
POSIFLORA_PASS = os.getenv("POSIFLORA_PASS")
POSIFLORA_URL = "https://lakmaflowers.posiflora.com/api/v1"
STORE_ID = os.getenv("STORE_ID", "cde3de25-51e3-4193-a164-84cdebd01e94")

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")
MOYSKLAD_URL = "https://api.moysklad.ru/api/remap/1.2"

STORE_ID_MS = "fc24f096-af79-11f1-0a80-0c0a0039483e"
STORE_HREF = f"https://api.moysklad.ru/api/remap/1.2/entity/store/{STORE_ID_MS}"
ORG_HREF = "https://api.moysklad.ru/api/remap/1.2/entity/organization/b34f89b3-c981-11ee-0a80-07770038ef19"
TARGET_FOLDER_NAME = "Витрина Tilda"

# --- 1. АВТОРИЗАЦИЯ В POSIFLORA ---
print("1. Вход в Posiflora...")
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
print("-> Успешная авторизация в Posiflora")

# --- 2. ВЫГРУЗКА ВИТРИННЫХ БУКЕТОВ (СТАТУС demonstrated) ---
print("\n2. Выгрузка актуальных витринных букетов (demonstrated)...")
page = 1
active_bouquets = {}

while True:
    bouquets_resp = pos_session.get(f"{POSIFLORA_URL}/bouquets", params={
        "filter[store]": STORE_ID,
        "filter[state]": "demonstrated",
        "page[number]": page,
        "page[size]": 50,
        "sort": "-updatedAt"
    }, timeout=25)

    if bouquets_resp.status_code != 200:
        print(f"Ошибка запроса букетов ({bouquets_resp.status_code}):", bouquets_resp.text)
        break

    b_json = bouquets_resp.json()
    b_list = b_json.get("data", [])
    if not b_list:
        break

    for b in b_list:
        attrs = b.get("attributes", {})
        state = attrs.get("state") or attrs.get("status")
        if state == "demonstrated":
            title = (attrs.get("title") or attrs.get("name") or "").strip()
            price = float(attrs.get("price") or attrs.get("amount") or 0)
            b_id = b.get("id")
            if title:
                active_bouquets[title] = {
                    "price_rub": price,
                    "pos_id": b_id
                }

    if len(b_list) < 50:
        break
    page += 1

print(f"Найдено букетов на витрине прямо сейчас: {len(active_bouquets)}")
for name, info in list(active_bouquets.items()):
    print(f"-> {name}: {info['price_rub']} ₽")

# --- 3. АВТОРИЗАЦИЯ В МОЙСКЛАД, ПОИСК ПАПКИ И ТИПА ЦЕНЫ ---
ms_session = requests.Session()
ms_session.headers.update({
    "Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}",
    "Content-Type": "application/json"
})

print("\n3. Получение типа цены продажи из МойСклад...")
price_type_meta = None
price_types_res = ms_session.get(f"{MOYSKLAD_URL}/context/companysettings/pricetype", timeout=25)
if price_types_res.status_code == 200:
    pts = price_types_res.json()
    if pts:
        price_type_meta = pts[0]["meta"]
        print(f"-> Тип цены: {pts[0].get('name', 'Основная')}")

print("\n4. Поиск папки 'Витрина Tilda' в МойСклад...")
folder_res = ms_session.get(f"{MOYSKLAD_URL}/entity/productfolder?filter=name={TARGET_FOLDER_NAME}", timeout=25).json()
folder_rows = folder_res.get("rows", [])
product_folder_meta = None
if folder_rows:
    product_folder_meta = folder_rows[0]["meta"]
    print(f"-> Папка найдена: {folder_rows[0]['name']}")

# --- 5. ПОЛУЧЕНИЕ КАТАЛОГА ТОВАРОВ ИЗ МОЙСКЛАД ---
print("\n5. Получение каталога товаров из МойСклад...")
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
            sale_price = item["salePrices"][0].get("value", 0) / 100
        
        item_id = item.get("id")
        norm_name = item["name"].strip().lower()
        ms_goods[norm_name] = {
            "id": item_id,
            "name": item["name"].strip(),
            "href": item["meta"]["href"],
            "price": sale_price
        }
    offset += len(rows)
    if offset >= data.get("meta", {}).get("size", 0):
        break

print(f"Существующих товаров в МойСклад: {len(ms_goods)}")

# --- 6. СОЗДАНИЕ НОВЫХ КАРТОЧЕК И АКТУАЛИЗАЦИЯ ЦЕН ---
print("\n6. Синхронизация каталога и цен...")
synced_bouquets = {}

for name, info in active_bouquets.items():
    norm_name = name.lower()
    price_kopecks = int(info["price_rub"] * 100)
    
    price_entry = {"value": price_kopecks}
    if price_type_meta:
        price_entry["priceType"] = {"meta": price_type_meta}
    sale_prices_payload = [price_entry] if price_kopecks > 0 else []

    if norm_name not in ms_goods:
        print(f"-> Создаем карточку букета: '{name}' | {info['price_rub']} ₽")
        payload = {
            "name": name,
            "salePrices": sale_prices_payload
        }
        if product_folder_meta:
            payload["productFolder"] = {"meta": product_folder_meta}

        c_res = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        if c_res.status_code in (200, 201):
            c_data = c_res.json()
            p_href = c_data["meta"]["href"]
            p_id = c_data.get("id")
            synced_bouquets[name] = {"id": p_id, "href": p_href, "price": price_kopecks}
            ms_goods[norm_name] = {"id": p_id, "name": name, "href": p_href, "price": info["price_rub"]}
            print("   Успешно создан!")
        else:
            print(f"   Ошибка создания '{name}':", c_res.text)
    else:
        current = ms_goods[norm_name]
        p_href = current["href"]
        p_id = current["id"]
        if abs(current["price"] - info["price_rub"]) > 0.01:
            print(f"-> Обновляем цену '{name}': {current['price']} ₽ -> {info['price_rub']} ₽")
            ms_session.put(p_href, json={"salePrices": sale_prices_payload}, timeout=20)
        synced_bouquets[name] = {"id": p_id, "href": p_href, "price": price_kopecks}

# --- 7. ПОЛУЧЕНИЕ ФАКТИЧЕСКИХ ОСТАТКОВ ЧЕРЕЗ ВСЕОБЩИЙ ОТЧЕТ ---
print("\n7. Получение текущих остатков со склада 'Склад Лакма Цветы'...")
stocks_by_id = {}
offset = 0
while True:
    stock_url = f"{MOYSKLAD_URL}/report/stock/all/current?filter=store={STORE_HREF}&limit=100&offset={offset}"
    st_res = ms_session.get(stock_url, timeout=30)
    if st_res.status_code != 200:
        print(f"Ошибка получения отчета остатков ({st_res.status_code}):", st_res.text)
        break
    st_rows = st_res.json()
    if not st_rows:
        break
    for r in st_rows:
        assortment_id = r.get("assortmentId")
        stock_val = float(r.get("stock", 0.0) or 0.0)
        if assortment_id:
            stocks_by_id[assortment_id] = stock_val
    offset += len(st_rows)
    if len(st_rows) < 100:
        break

print(f"Загружено точных складских остатков: {len(stocks_by_id)}")

# --- 8. ВЫРАВНИВАНИЕ ОСТАТКОВ (НЕ БОЛЕЕ 1 ШТ) ---
print("\n8. Проверка и выравнивание остатков (целевой остаток = 1 шт)...")
positions_to_enter = []

for name, data in synced_bouquets.items():
    p_id = data.get("id")
    p_href = data.get("href")
    
    current_qty = stocks_by_id.get(p_id, 0.0)

    if current_qty < 1.0:
        qty_needed = 1.0 - current_qty
        print(f"-> '{name}': текущий остаток {current_qty}, оприходуем {qty_needed} шт")
        positions_to_enter.append({
            "quantity": qty_needed,
            "price": data["price"],
            "assortment": {
                "meta": {
                    "href": p_href,
                    "type": "product",
                    "mediaType": "application/json"
                }
            }
        })
    else:
        print(f"-> '{name}': остаток уже {current_qty} шт, оприходование НЕ требуется")

if positions_to_enter:
    print(f"\nОтправка оприходования для {len(positions_to_enter)} букетов...")
    enter_payload = {
        "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
        "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
        "applicable": True,
        "description": "Автовыравнивание остатков витрины из Posiflora",
        "positions": positions_to_enter
    }
    doc_res = ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json=enter_payload, timeout=60)
    if doc_res.status_code in (200, 201):
        print(">>> Оприходование успешно проведено!")
    else:
        print(f"Ошибка оприходования ({doc_res.status_code}):", doc_res.text)
else:
    print("\nВсе витринные букеты уже имеют остаток >= 1 шт. Новые документы не создавались.")

print("\n>>> Синхронизация успешно завершена!")
