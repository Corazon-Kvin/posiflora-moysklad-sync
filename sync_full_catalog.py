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

print("1. Авторизация API...")
pos_s = requests.Session()
pos_s.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})
resp = pos_s.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)
pos_s.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})

ms_s = requests.Session()
ms_s.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

print("\n2. Сбор фото витринных букетов из Posiflora...")
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
    except Exception:
        break

print("\n3. Сбор остатков срезки из каталога Posiflora (витрина + склад)...")
pos_flowers = {}
page = 1
while True:
    try:
        params = {"page[number]": page, "page[size]": 100, "include": "balances", "distribution": "true", "concealment": "false"}
        r = pos_s.get(f"{POSIFLORA_URL}/catalog", params=params, timeout=30)
        if r.status_code != 200: break
        d = r.json()
        items = d.get("data", [])
        if not items: break

        bal_map = {}
        for inc in d.get("included", []):
            if inc.get("type") == "item-balances":
                attrs = inc.get("attributes", {})
                w_qty = float(attrs.get("qtyOnWindow") or 0)
                t_qty = float(attrs.get("qty") or 0)
                bal_map[str(inc.get("id"))] = max(w_qty, t_qty)

        for it in items:
            title = str(it.get("attributes", {}).get("title") or "").strip()
            st = 0.0
            bal_rel = it.get("relationships", {}).get("balances", {}).get("data", [])
            if isinstance(bal_rel, list):
                for b in bal_rel:
                    bid = str(b.get("id"))
                    if bid in bal_map:
                        st += bal_map[bid]
            pos_flowers[norm_name(title)] = st

        if len(items) < 100: break
        page += 1
    except Exception:
        break

print("\n4. Сбор остатков со склада «Лакма Цветы» из МойСклад...")
ms_current_stocks = {}
try:
    r_stocks = ms_s.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
    if r_stocks.status_code == 200:
        for row in r_stocks.json():
            if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
                ms_current_stocks[row["assortmentId"]] = float(row.get("stock", 0))
except Exception as e:
    print(f"[!] Ошибка остатков: {e}")

print("\n5. Анализ ассортимента и расчет целевых остатков...")
offset = 0
enter_positions = []
loss_positions = []
updated_log = []
bakery_stop = ["круассан", "сосиска", "слойка", "датская", "кремибон", "хлеб", "чиабатта", "багет", "булочк", "пирож", "сырник", "кофе", "напиток", "эспрессо", "капучино", "торт", "макарон", "десерт"]

while True:
    try:
        res = ms_s.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}&expand=productFolder,images", timeout=30)
        if res.status_code != 200: break
        rows = res.json().get("rows", [])
        if not rows: break

        for item in rows:
            name = str(item.get("name") or "").strip()
            folder_name = str(item.get("productFolder", {}).get("name") or "").strip()
            folder_href = item.get("productFolder", {}).get("meta", {}).get("href", "")

            # Фильтр пекарни/кофейни
            if any(w in name.lower() for w in bakery_stop) or any(w in folder_name.lower() for w in bakery_stop):
                continue

            item_id = item["id"]
            cur_stock = ms_current_stocks.get(item_id, 0.0)
            ext_code = str(item.get("externalCode") or "")
            n_name = norm_name(name)
            has_image = bool(item.get("images", {}).get("rows"))
            target_stock = None

            # Витрина Tilda
            if FOLDER_VITRINA_ID in folder_href:
                target_stock = 1.0
                if not has_image and n_name in vitrina_photos:
                    try:
                        r_img_meta = pos_s.get(f"{POSIFLORA_URL}/images/{vitrina_photos[n_name]}", timeout=15)
                        if r_img_meta.status_code == 200:
                            img_url = r_img_meta.json().get("data", {}).get("attributes", {}).get("url")
                            if img_url:
                                raw_bytes = requests.get(img_url, timeout=20).content
                                compressed = compress_image(raw_bytes)
                                b64 = base64.b64encode(compressed).decode("utf-8")
                                ms_s.post(f"{MOYSKLAD_URL}/entity/product/{item_id}/images", json={
                                    "filename": f"{item_id}.jpg",
                                    "content": b64
                                }, timeout=25)
                                print(f"  [+] Загружено фото: {name}")
                    except Exception:
                        pass

            # Авторские букеты
            elif FOLDER_AUTH_ID in folder_href:
                target_stock = 5.0

            # Штучные товары (по внешнему коду Posiflora)
            elif len(ext_code) >= 32:
                try:
                    r_st = pos_s.get(f"{POSIFLORA_URL}/inventory-items/{ext_code}/stats-by-store/{POSIFLORA_STORE_ID}", timeout=5)
                    if r_st.status_code == 200:
                        target_stock = float(r_st.json().get("data", {}).get("attributes", {}).get("qty", 0))
                    time.sleep(0.02)
                except Exception:
                    pass

            # Срезка по наименованию
            if target_stock is None and n_name in pos_flowers:
                target_stock = pos_flowers[n_name]

            if target_stock is not None:
                diff = round(target_stock - cur_stock, 2)
                if diff != 0:
                    assort_meta = {"meta": {"href": item["meta"]["href"], "type": "product", "mediaType": "application/json"}}
                    price = item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0
                    if diff > 0:
                        enter_positions.append({
                            "quantity": diff,
                            "price": price,
                            "assortment": assort_meta
                        })
                        updated_log.append(f"  [+] {name}: было {cur_stock} -> станет {target_stock} (+{diff})")
                    elif diff < 0 and cur_stock > 0:
                        loss_positions.append({
                            "quantity": min(abs(diff), cur_stock),
                            "assortment": assort_meta
                        })
                        updated_log.append(f"  [-] {name}: было {cur_stock} -> станет {target_stock} ({diff})")

        offset += len(rows)
        if len(rows) < 100: break
    except Exception as e:
        print(f"[!] Ошибка ассортимента: {e}")
        break

print(f"\nВсего изменений: {len(enter_positions) + len(loss_positions)}")
for line in updated_log[:20]:
    print(line)
if len(updated_log) > 20:
    print(f"  ... и еще {len(updated_log) - 20} позиций")

# 6. Оприходование
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
            print(f"[!] Ошибка enter: {e}")
    print(f"[+] Оприходовано: {len(enter_positions)} позиций")

# 7. Списание
if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        try:
            ms_s.post(f"{MOYSKLAD_URL}/entity/loss", json={
                "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
                "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
                "applicable": True,
                "description": "Автосписание проданных остатков",
                "positions": loss_positions[i:i+1000]
            }, timeout=60)
        except Exception as e:
            print(f"[!] Ошибка loss: {e}")
    print(f"[-] Списано: {len(loss_positions)} позиций")

print("\nСинхронизация завершена успешно!")
