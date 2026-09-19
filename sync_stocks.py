import os
import json
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

# --- 2. ДИАГНОСТИКА СТРУКТУРЫ ФОТО БУКЕТА ---
print("\n--- Проверка наличия фото в Posiflora /bouquets ---")
try:
    diag_resp = pos_session.get(f"{POSIFLORA_URL}/bouquets", params={
        "filter[store]": STORE_ID,
        "filter[state]": "demonstrated",
        "page[size]": 3,
        "include": "images,photo,attachments",
        "sort": "-updatedAt"
    }, timeout=25)
    
    if diag_resp.status_code == 200:
        d_json = diag_resp.json()
        d_items = d_json.get("data", [])
        if d_items:
            first_b = d_items[0]
            print("Название букета:", first_b.get("attributes", {}).get("title"))
            print("Ключи attributes:", list(first_b.get("attributes", {}).keys()))
            print("Ключи relationships:", list(first_b.get("relationships", {}).keys()))
            
            # Проверяем, есть ли ссылки на фото в attributes
            for k, v in first_b.get("attributes", {}).items():
                if any(sub in k.lower() for sub in ["photo", "image", "pic", "file", "url", "logo"]):
                    print(f"Найдено фото в attributes -> {k}: {v}")

            # Проверяем included
            included = d_json.get("included", [])
            print(f"Количество объектов included: {len(included)}")
            for inc in included[:5]:
                print(f"Included тип: {inc.get('type')}, id: {inc.get('id')}, ключи: {list(inc.get('attributes', {}).keys())}")
        else:
            print("Нет букетов со статусом demonstrated для теста.")
    else:
        print(f"Ответ Posiflora ({diag_resp.status_code}):", diag_resp.text[:300])
except Exception as e:
    print("Ошибка диагностики фото:", e)
print("--------------------------------------------------\n")

# --- 3. ВЫГРУЗКА ВИТРИННЫХ БУКЕТОВ (СТАТУС demonstrated) ---
print("\n2. Выгрузка актуальных витринных букетов...")
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

print(f"Найдено букетов на витрине: {len(active_bouquets)}")

# --- 4. АВТОРИЗАЦИЯ В МОЙСКЛАД ---
ms_session = requests.Session()
ms_session.headers.update({
    "Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}",
    "Content-Type": "application/json"
})

price_type_meta = None
price_types_res = ms_session.get(f"{MOYSKLAD_URL}/context/companysettings/pricetype", timeout=25)
if price_types_res.status_code == 200:
    pts = price_types_res.json()
    if pts:
        price_type_meta = pts[0]["meta"]

folder_res = ms_session.get(f"{MOYSKLAD_URL}/entity/productfolder?filter=name={TARGET_FOLDER_NAME}", timeout=25).json()
folder_rows = folder_res.get("rows", [])
product_folder_meta = folder_rows[0]["meta"] if folder_rows else None

# --- 5. КАТАЛОГ И ЦЕНЫ МОЙСКЛАД ---
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
        ms_goods[item["name"].strip().lower()] = {
            "id": item.get("id"),
            "name": item["name"].strip(),
            "href": item["meta"]["href"],
            "price": sale_price
        }
    offset += len(rows)
    if offset >= data.get("meta", {}).get("size", 0):
        break

synced_bouquets = {}
for name, info in active_bouquets.items():
    norm_name = name.lower()
    price_kopecks = int(info["price_rub"] * 100)
    price_entry = {"value": price_kopecks}
    if price_type_meta:
        price_entry["priceType"] = {"meta": price_type_meta}
    sale_prices_payload = [price_entry] if price_kopecks > 0 else []

    if norm_name not in ms_goods:
        payload = {"name": name, "salePrices": sale_prices_payload}
        if product_folder_meta:
            payload["productFolder"] = {"meta": product_folder_meta}
        c_res = ms_session.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
        if c_res.status_code in (200, 201):
            c_data = c_res.json()
            synced_bouquets[name] = {"id": c_data.get("id"), "href": c_data["meta"]["href"], "price": price_kopecks}
            ms_goods[norm_name] = {"id": c_data.get("id"), "name": name, "href": c_data["meta"]["href"], "price": info["price_rub"]}
    else:
        current = ms_goods[norm_name]
        if abs(current["price"] - info["price_rub"]) > 0.01:
            ms_session.put(current["href"], json={"salePrices": sale_prices_payload}, timeout=20)
        synced_bouquets[name] = {"id": current["id"], "href": current["href"], "price": price_kopecks}

# --- 6. ПРОВЕРКА ОСТАТКОВ ---
stocks_by_id = {}
offset = 0
while True:
    assort_url = f"{MOYSKLAD_URL}/entity/assortment?filter=stockStore={STORE_HREF}&limit=100&offset={offset}"
    st_res = ms_session.get(assort_url, timeout=30)
    if st_res.status_code != 200:
        break
    data = st_res.json()
    st_rows = data.get("rows", [])
    if not st_rows:
        break
    for r in st_rows:
        item_id = r.get("id")
        stock_val = float(r.get("stock", 0.0) or 0.0)
        if item_id:
            stocks_by_id[item_id] = stock_val
    offset += len(st_rows)
    if offset >= data.get("meta", {}).get("size", 0):
        break

positions_to_enter = []
for name, data in synced_bouquets.items():
    p_id = data.get("id")
    p_href = data.get("href")
    current_qty = stocks_by_id.get(p_id, 0.0)
    if current_qty < 1.0:
        qty_needed = 1.0 - current_qty
        positions_to_enter.append({
            "quantity": qty_needed,
            "price": data["price"],
            "assortment": {"meta": {"href": p_href, "type": "product", "mediaType": "application/json"}}
        })

if positions_to_enter:
    enter_payload = {
        "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
        "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
        "applicable": True,
        "description": "Автовыравнивание остатков витрины из Posiflora",
        "positions": positions_to_enter
    }
    ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json=enter_payload, timeout=60)

print(">>> Синхронизация завершена успешно!")
