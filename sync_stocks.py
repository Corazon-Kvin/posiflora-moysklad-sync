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

# 1. Сессия Posiflora
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

# 2. Сессия МойСклад
ms_session = requests.Session()
ms_session.headers.update({
    "Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}",
    "Content-Type": "application/json"
})

print("1. Получение товаров из МойСклад...")
ms_goods = {}
offset = 0
while True:
    res = ms_session.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}", timeout=30)
    data = res.json()
    rows = data.get("rows", [])
    if not rows:
        break
    for item in rows:
        ms_goods[item["name"].strip().lower()] = {
            "href": item["meta"]["href"],
            "price": item.get("salePrices", [{}])[0].get("value", 0)
        }
    offset += len(rows)
    if offset >= data.get("meta", {}).get("size", 0):
        break

print(f"Загружено товаров из МойСклад: {len(ms_goods)}")

print("\n2. Выгрузка остатков из Posiflora...")
page = 1
pos_stocks = {}

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
        print(f"Завершение / ошибка: {r.status_code}")
        break

    res_json = r.json()
    items = res_json.get("data", [])
    if not items:
        break

    balances_map = {}
    for inc in res_json.get("included", []):
        if inc.get("type") == "item-balances":
            b_id = str(inc.get("id", ""))
            attrs = inc.get("attributes", {})
            stock = float(attrs.get("qtyOnWindow", 0) or 0)
            if stock > 0:
                balances_map[b_id] = stock

    for item in items:
        title = item.get("attributes", {}).get("title", "").strip()
        item_id = str(item.get("id", ""))
        prices_rel = item.get("relationships", {}).get("prices", {}).get("data", [])
        
        item_stock = 0.0

        if isinstance(prices_rel, list):
            for p in prices_rel:
                pid = str(p.get("id", ""))
                if pid in balances_map:
                    item_stock += balances_map[pid]
        elif isinstance(prices_rel, dict):
            pid = str(prices_rel.get("id", ""))
            if pid in balances_map:
                item_stock += balances_map[pid]

        composite_key = f"{item_id}-{STORE_ID}"
        if composite_key in balances_map:
            item_stock += balances_map[composite_key]

        if item_stock > 0 and title:
            pos_stocks[title] = item_stock

    if len(items) < 50:
        break
    page += 1

print(f"Всего позиций с ненулевым остатком: {len(pos_stocks)}")

print("\n3. Формирование строк оприходования...")
positions = []
for title, qty in pos_stocks.items():
    matched = ms_goods.get(title.lower())
    if matched:
        positions.append({
            "quantity": qty,
            "price": matched["price"],
            "assortment": {
                "meta": {
                    "href": matched["href"],
                    "type": "product",
                    "mediaType": "application/json"
                }
            }
        })
    else:
        print(f"[Не сопоставлен с МойСклад]: '{title}' ({qty} шт)")

print(f"Готово к оприходованию: {len(positions)} позиций")

if positions:
    print("\n4. Отправка документа 'Оприходование' в МойСклад...")
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
        "description": "Автосинхронизация остатков из Posiflora (GitHub Actions)",
        "positions": positions
    }

    doc_res = ms_session.post(f"{MOYSKLAD_URL}/entity/enter", json=payload, timeout=60)
    if doc_res.status_code in (200, 201):
        print("\n>>> УСПЕШНО: Документ оприходования создан и проведен в МойСклад!")
    else:
        print(f"Ошибка создания оприходования ({doc_res.status_code}):", doc_res.text)
else:
    print("Позиций для оприходования не найдено.")
