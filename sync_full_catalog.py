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

print("1. Подключение к API...")
pos_s = requests.Session()
pos_s.headers.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json"})
resp = pos_s.post(f"{POSIFLORA_URL}/sessions", json={
    "data": {"type": "sessions", "attributes": {"username": POSIFLORA_USER, "password": POSIFLORA_PASS}}
}, timeout=20)

if resp.status_code in (200, 201):
    pos_s.headers.update({"Authorization": f"Bearer {resp.json()['data']['attributes']['accessToken']}"})
else:
    print(f"[-] Ошибка авторизации Posiflora: {resp.status_code}")
    exit()

ms_s = requests.Session()
ms_s.headers.update({"Authorization": f"Bearer {MOYSKLAD_TOKEN.strip()}", "Content-Type": "application/json"})

print("\n1.5 Получение системного типа цены в МоемСкладе...")
price_type_meta = None
try:
    r_pt = ms_s.get(f"{MOYSKLAD_URL}/context/companysettings/pricetype", timeout=15)
    if r_pt.status_code == 200:
        pts = r_pt.json()
        found_meta = next((pt.get("meta") for pt in pts if pt.get("name") == "Цена продажи"), None)
        price_type_meta = found_meta if found_meta else (pts[0].get("meta") if pts else None)
except Exception as e:
    print(f"[!] Ошибка получения типа цены: {e}")

print("\n2. Сканирование структуры папок МоегоСклада...")
folder_map = {}
default_flower_folder = None
default_decor_folder = None
offset = 0
while True:
    try:
        rf = ms_s.get(f"{MOYSKLAD_URL}/entity/productfolder?limit=100&offset={offset}", timeout=30).json()
        f_rows = rf.get("rows", [])
        if not f_rows:
            break
        for f in f_rows:
            f_name = f.get("name", "").strip().lower()
            f_href = f["meta"]["href"]
            folder_map[f_name] = f_href
            if "витрина tilda" in f_name or "витрина тильда" in f_name:
                folder_map["витрина"] = f_href
            elif "авторские букеты" in f_name:
                folder_map["авторские"] = f_href
            elif f_name in ("цветы", "цветы (стебли)"):
                default_flower_folder = f_href
            elif f_name == "декор":
                default_decor_folder = f_href
        offset += len(f_rows)
        if len(f_rows) < 100:
            break
    except Exception as e:
        print(f"[!] Ошибка сканирования папок: {e}")
        break

def get_folder_href_for_item(name, category_name=""):
    search_text = f"{name} {category_name}".lower()
    if "витрин" in search_text: return folder_map.get("витрина")
    if "свеч" in search_text: return folder_map.get("свечи", default_decor_folder)
    if "ваз" in search_text: return folder_map.get("вазы", default_decor_folder)
    if "подсвечник" in search_text: return folder_map.get("подсвечники", default_decor_folder)
    if "шар" in search_text: return folder_map.get("шары", default_decor_folder)
    if "открытк" in search_text or "конверт" in search_text: return folder_map.get("открытки", default_decor_folder)
    if "коробк" in search_text: return folder_map.get("подарочные коробки", default_decor_folder)
    if "игрушк" in search_text: return folder_map.get("игрушки", default_decor_folder)
    if "лент" in search_text or "упаковк" in search_text or "пленк" in search_text: return folder_map.get("упаковка", default_decor_folder)
    return default_flower_folder or default_decor_folder

print("\n3. Получение текущих остатков склада Лакма Цветы...")
ms_current_stocks = {}
try:
    r_stocks = ms_s.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
    if r_stocks.status_code == 200:
        for row in r_stocks.json():
            if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
                ms_current_stocks[row["assortmentId"]] = float(row.get("stock", 0))
except Exception as e:
    print(f"[!] Ошибка отчета остатков: {e}")

print("\n4. Сканирование существующих позиций в МоемСкладе...")
offset = 0
ms_products = []
ms_ext_codes = set()
ms_names = set()
bakery_stop = ["круассан", "сосиска", "слойка", "датская", "кремибон", "хлеб", "чиабатта", "багет", "булочк", "пирож", "сырник", "кофе", "напиток", "эспрессо", "капучино", "торт", "макарон", "десерт"]

while True:
    try:
        res = ms_s.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}&expand=productFolder,images", timeout=30)
        if res.status_code != 200:
            break
        rows = res.json().get("rows", [])
        if not rows:
            break

        for item in rows:
            name = str(item.get("name") or "").strip()
            folder_obj = item.get("productFolder", {})
            folder_name = str(folder_obj.get("name") or "").strip()
            folder_href = folder_obj.get("meta", {}).get("href", "")
            ext_code = str(item.get("externalCode") or "")

            if any(w in name.lower() for w in bakery_stop) or any(w in folder_name.lower() for w in bakery_stop):
                continue

            if ext_code:
                ms_ext_codes.add(ext_code)
            ms_names.add(norm_name(name))

            ms_products.append({
                "id": item["id"],
                "name": name,
                "external_code": ext_code,
                "href": item["meta"]["href"],
                "type": item.get("meta", {}).get("type", "product"),
                "folder_href": folder_href,
                "price": item.get("salePrices", [{}])[0].get("value", 0) if item.get("salePrices") else 0,
                "stock": ms_current_stocks.get(item["id"], 0.0)
            })

        offset += len(rows)
        if len(rows) < 100:
            break
    except Exception as e:
        print(f"[!] Ошибка сбора товаров МС: {e}")
        break

def create_ms_product(title, ext_id, folder_href, price_rub, logo_id=None):
    if not folder_href:
        return None
    payload = {
        "name": title,
        "externalCode": ext_id,
        "productFolder": {"meta": {"href": folder_href, "type": "productfolder", "mediaType": "application/json"}}
    }
    
    if price_type_meta and price_rub > 0:
        payload["salePrices"] = [{"value": int(price_rub * 100), "priceType": {"meta": price_type_meta}}]
        
    cr = ms_s.post(f"{MOYSKLAD_URL}/entity/product", json=payload, timeout=20)
    if cr.status_code in (200, 201):
        prod = cr.json()
        pid = prod["id"]
        if logo_id:
            try:
                r_img = pos_s.get(f"{POSIFLORA_URL}/images/{logo_id}", timeout=15)
                if r_img.status_code == 200:
                    img_url = r_img.json().get("data", {}).get("attributes", {}).get("url")
                    if img_url:
                        raw = requests.get(img_url, timeout=20).content
                        compressed = compress_image(raw)
                        b64 = base64.b64encode(compressed).decode("utf-8")
                        ms_s.post(f"{MOYSKLAD_URL}/entity/product/{pid}/images", json={"filename": f"{pid}.jpg", "content": b64}, timeout=25)
            except Exception:
                pass
        return prod
    else:
        print(f"      [!] Ошибка создания {title} в МС: {cr.text}")
    return None

print("\n5. Сбор витринных букетов из Posiflora (в обход бага API)...")
pos_showcase_bare = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/bouquets?page[number]={page}&page[size]=100", timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        items = d.get("data", [])
        if not items:
            break

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            it_id = str(it.get("id"))
            pos_showcase_bare.append({"id": it_id, "title": title})
            
        if len(items) < 100:
            break
        page += 1
    except Exception as e:
        print(f"  [!] Ошибка API витрины: {e}")
        break

pos_showcase_ids = {norm_name(x["title"]) for x in pos_showcase_bare}

print("\n6. Автосоздание новых витринных букетов...")
for item in pos_showcase_bare:
    n_n = norm_name(item["title"])
    if item["id"] not in ms_ext_codes and n_n not in ms_names:
        print(f"  [+] Найден новый витринный букет: {item['title']}. Запрашиваем детали...")
        price_val = 0
        logo_id = None
        
        try:
            r_item = pos_s.get(f"{POSIFLORA_URL}/bouquets/{item['id']}?include=prices,logo", timeout=15)
            if r_item.status_code == 200:
                d = r_item.json()
                
                inc_map = {(inc.get("type"), str(inc.get("id"))): inc for inc in d.get("included", [])}
                rel = d.get("data", {}).get("relationships", {})
                for pr_rel in rel.get("prices", {}).get("data", []):
                    pr_obj = inc_map.get(("prices", str(pr_rel.get("id"))))
                    if pr_obj:
                        price_val = float(pr_obj.get("attributes", {}).get("value", 0))
                
                if price_val == 0:
                    price_val = float(d.get("data", {}).get("attributes", {}).get("price", 0))
                    
                logo_rel = rel.get("logo", {}).get("data")
                if logo_rel:
                    logo_id = str(logo_rel.get("id"))
        except Exception as e:
            print(f"      [!] Ошибка получения деталей: {e}")
            
        p = create_ms_product(item["title"], item["id"], folder_map.get("витрина"), price_val, logo_id)
        if p:
            ms_products.append({"id": p["id"], "name": item["title"], "external_code": item["id"], "href": p["meta"]["href"], "type": "product", "folder_href": folder_map.get("витрина"), "price": int(price_val * 100), "stock": 0.0})
            ms_ext_codes.add(item["id"])
            ms_names.add(n_n)

print("\n7. Сбор рецептов авторских букетов из Posiflora...")
pos_recipes_bare = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/recipes?page[number]={page}&page[size]=100", timeout=30)
        if r.status_code == 404:
            print("  [-] Раздел рецептов отключен (404). Пропускаем.")
            break
        if r.status_code != 200:
            break
        d = r.json()
        items = d.get("data", [])
        if not items:
            break

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            it_id = str(it.get("id"))
            pos_recipes_bare.append({"id": it_id, "title": title})
            
        if len(items) < 100:
            break
        page += 1
    except Exception as e:
        print(f"  [!] Ошибка API рецептов: {e}")
        break

for item in pos_recipes_bare:
    n_n = norm_name(item["title"])
    if item["id"] not in ms_ext_codes and n_n not in ms_names:
        print(f"  [+] Найден новый рецепт: {item['title']}. Запрашиваем детали...")
        price_val = 0
        logo_id = None
        try:
            r_item = pos_s.get(f"{POSIFLORA_URL}/recipes/{item['id']}?include=prices,logo", timeout=15)
            if r_item.status_code == 200:
                d = r_item.json()
                inc_map = {(inc.get("type"), str(inc.get("id"))): inc for inc in d.get("included", [])}
                rel = d.get("data", {}).get("relationships", {})
                for pr_rel in rel.get("prices", {}).get("data", []):
                    pr_obj = inc_map.get(("prices", str(pr_rel.get("id"))))
                    if pr_obj:
                        price_val = float(pr_obj.get("attributes", {}).get("value", 0))
                
                if price_val == 0:
                    price_val = float(d.get("data", {}).get("attributes", {}).get("price", 0))
                    
                logo_rel = rel.get("logo", {}).get("data")
                if logo_rel:
                    logo_id = str(logo_rel.get("id"))
        except Exception:
            pass
            
        p = create_ms_product(item["title"], item["id"], folder_map.get("авторские"), price_val, logo_id)
        if p:
            ms_products.append({"id": p["id"], "name": item["title"], "external_code": item["id"], "href": p["meta"]["href"], "type": "product", "folder_href": folder_map.get("авторские"), "price": int(price_val * 100), "stock": 0.0})
            ms_ext_codes.add(item["id"])
            ms_names.add(n_n)

print("\n8. Сбор штучных товаров и срезки из Posiflora...")
pos_flowers = {}
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/catalog?page[number]={page}&page[size]=100&include=balances", timeout=30)
        if r.status_code != 200:
            break
        d = r.json()
        items = d.get("data", [])
        if not items:
            break

        bal_map = {}
        for inc in d.get("included", []):
            if inc.get("type") == "item-balances":
                attrs = inc.get("attributes", {})
                bal_map[str(inc.get("id"))] = max(float(attrs.get("qtyOnWindow") or 0), float(attrs.get("qty") or 0))

        for it in items:
            title = str(it.get("attributes", {}).get("title") or "").strip()
            it_id = str(it.get("id"))
            st = 0.0
            for b in it.get("relationships", {}).get("balances", {}).get("data", []):
                bid = str(b.get("id"))
                if bid in bal_map:
                    st += bal_map[bid]
            pos_flowers[norm_name(title)] = {"id": it_id, "title": title, "stock": st}
            
        if len(items) < 100:
            break
        page += 1
    except Exception as e:
        print(f"  [!] Ошибка API срезки: {e}")
        break

for n_n, f_data in pos_flowers.items():
    if f_data["id"] not in ms_ext_codes and n_n not in ms_names:
        f_target = default_flower_folder
        if f_target:
            p = create_ms_product(f_data["title"], f_data["id"], f_target, 0)
            if p:
                ms_products.append({"id": p["id"], "name": f_data["title"], "external_code": f_data["id"], "href": p["meta"]["href"], "type": "product", "folder_href": f_target, "price": 0, "stock": 0.0})
                ms_ext_codes.add(f_data["id"])
                ms_names.add(n_n)

print("\n9. Расчет остатков и актуализация базы...")
enter_positions = []
loss_positions = []
updated_log = []

vitrina_folder_href = folder_map.get("витрина", "")
auth_folder_href = folder_map.get("авторские", "")

for p in ms_products:
    f_href = p["folder_href"]
    cur_stock = p["stock"]
    ext_code = p["external_code"]
    name = p["name"]
    n_name = norm_name(name)
    target_stock = None

    if vitrina_folder_href and vitrina_folder_href in f_href:
        target_stock = 1.0 if n_name in pos_showcase_ids else 0.0
    elif auth_folder_href and auth_folder_href in f_href:
        target_stock = 5.0
    elif len(ext_code) >= 32:
        try:
            r_st = pos_s.get(f"{POSIFLORA_URL}/inventory-items/{ext_code}/stats-by-store/{POSIFLORA_STORE_ID}", timeout=5)
            if r_st.status_code == 200:
                target_stock = float(r_st.json().get("data", {}).get("attributes", {}).get("qty", 0))
            time.sleep(0.02)
        except Exception:
            pass

    if target_stock is None and n_name in pos_flowers:
        target_stock = pos_flowers[n_name]["stock"]

    if target_stock is not None:
        diff = round(target_stock - cur_stock, 2)
        if diff != 0:
            assort_meta = {"meta": {"href": p["href"], "type": p["type"], "mediaType": "application/json"}}
            if diff > 0:
                enter_positions.append({"quantity": diff, "price": p["price"], "assortment": assort_meta})
                updated_log.append(f"  [+] {name}: было {cur_stock} -> станет {target_stock} (+{diff})")
            elif diff < 0 and cur_stock > 0:
                loss_positions.append({"quantity": min(abs(diff), cur_stock), "assortment": assort_meta})
                updated_log.append(f"  [-] {name}: было {cur_stock} -> станет {target_stock} ({diff})")

print(f"\nВсего изменений остатков: {len(enter_positions) + len(loss_positions)}")
for line in updated_log[:20]:
    print(line)

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        try:
            ms_s.post(f"{MOYSKLAD_URL}/entity/enter", json={
                "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
                "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
                "applicable": True,
                "description": "Автосинхронизация остатков: Posiflora -> МойСклад",
                "positions": enter_positions[i:i+1000]
            }, timeout=60)
        except Exception as e:
            print(f"[!] Ошибка enter: {e}")
    print(f"[+] Оприходовано: {len(enter_positions)} позиций")

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

print("\nПолная синхронизация завершена успешно!")
