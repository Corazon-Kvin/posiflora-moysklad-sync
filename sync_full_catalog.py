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

TARGET_STORE_ID = "fc24f096-af79-11f1-0a80-0c0a0039483e"
ORG_ID = "b34f89b3-c981-11ee-0a80-07770038ef19"

STORE_HREF = f"{MOYSKLAD_URL}/entity/store/{TARGET_STORE_ID}"
ORG_HREF = f"{MOYSKLAD_URL}/entity/organization/{ORG_ID}"

# === БЕТОННЫЙ ФИЛЬТР МУСОРА ===
STOP_WORDS = ["круассан", "сосиска", "слойка", "датская", "кремибон", "хлеб", "чиабатта", "багет", "сырник", "кофе", "напиток", "эспрессо", "капучино", "макарон", "эклер", "тарталет", "кофейня"]

def is_junk(name, price=None):
    n = name.lower()
    if any(w in n for w in STOP_WORDS): return True
    if re.search(r'букет\s*\d{6,}', n): return True
    if price is not None and price <= 0: return True
    return False

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

print("\n1.5 Получение системного типа цены...")
price_type_meta = None
try:
    r_pt = ms_s.get(f"{MOYSKLAD_URL}/context/companysettings/pricetype", timeout=15)
    if r_pt.status_code == 200:
        pts = r_pt.json()
        found_meta = next((pt.get("meta") for pt in pts if pt.get("name") == "Цена продажи"), None)
        price_type_meta = found_meta if found_meta else (pts[0].get("meta") if pts else None)
except Exception as e:
    print(f"[!] Ошибка цены: {e}")

print("\n2. Умное сканирование папок МоегоСклада...")
folder_map = {}
default_flower_folder = None
default_decor_folder = None
offset = 0
while True:
    try:
        rf = ms_s.get(f"{MOYSKLAD_URL}/entity/productfolder?limit=100&offset={offset}", timeout=30).json()
        f_rows = rf.get("rows", [])
        if not f_rows: break
        for f in f_rows:
            f_name = f.get("name", "").strip().lower()
            f_href = f["meta"]["href"]
            if "витрина" in f_name: folder_map["витрина"] = f_href
            if "авторские" in f_name: folder_map["авторские"] = f_href
            if "цветы" in f_name: default_flower_folder = f_href
            if "декор" in f_name: default_decor_folder = f_href
            if "шар" in f_name: folder_map["шары"] = f_href
            if "ваз" in f_name: folder_map["вазы"] = f_href
            if "свеч" in f_name: folder_map["свечи"] = f_href
            if "упаковк" in f_name or "лент" in f_name: folder_map["упаковка"] = f_href
        offset += len(f_rows)
        if len(f_rows) < 100: break
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

print("\n3. Сбор остатков склада...")
ms_current_stocks = {}
try:
    r_stocks = ms_s.get(f"{MOYSKLAD_URL}/report/stock/bystore/current", timeout=45)
    if r_stocks.status_code == 200:
        for row in r_stocks.json():
            if row.get("assortmentId") and row.get("storeId") == TARGET_STORE_ID:
                ms_current_stocks[row["assortmentId"]] = float(row.get("stock", 0))
except Exception as e:
    pass

print("\n4. Сбор существующих позиций в МоемСкладе...")
offset = 0
ms_products = []
ms_ext_codes = set()
ms_names = set()

while True:
    try:
        res = ms_s.get(f"{MOYSKLAD_URL}/entity/product?limit=100&offset={offset}&expand=productFolder", timeout=30)
        if res.status_code != 200: break
        rows = res.json().get("rows", [])
        if not rows: break

        for item in rows:
            name = str(item.get("name") or "").strip()
            folder_obj = item.get("productFolder", {})
            folder_href = folder_obj.get("meta", {}).get("href", "")
            ext_code = str(item.get("externalCode") or "")

            if is_junk(name): continue # Игнорируем пекарню внутри МС

            if ext_code: ms_ext_codes.add(ext_code)
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
        if len(rows) < 100: break
    except Exception as e:
        break

def create_ms_product(title, ext_id, folder_href, price_rub, logo_id=None):
    if not folder_href: return None
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
                        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                        raw = requests.get(img_url, headers=hdrs, timeout=20, allow_redirects=True).content
                        compressed = compress_image(raw)
                        b64 = base64.b64encode(compressed).decode("utf-8")
                        ms_s.post(f"{MOYSKLAD_URL}/entity/product/{pid}/images", json={"filename": f"{pid}.jpg", "content": b64}, timeout=25)
            except Exception: pass
        return prod
    return None

print("\n5. Витрина (обход мусора)...")
pos_showcase_bare = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/bouquets?page[number]={page}&page[size]=100", timeout=30)
        if r.status_code != 200: break
        items = r.json().get("data", [])
        if not items: break

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            if not is_junk(title):
                pos_showcase_bare.append({"id": str(it.get("id")), "title": title})
            
        if len(items) < 100: break
        page += 1
    except Exception: break

pos_showcase_ids = {norm_name(x["title"]) for x in pos_showcase_bare}

for item in pos_showcase_bare:
    n_n = norm_name(item["title"])
    if item["id"] not in ms_ext_codes and n_n not in ms_names:
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
                    if pr_obj: price_val = float(pr_obj.get("attributes", {}).get("value", 0))
                
                if price_val == 0: price_val = float(d.get("data", {}).get("attributes", {}).get("price", 0))
                logo_rel = rel.get("logo", {}).get("data")
                if logo_rel: logo_id = str(logo_rel.get("id"))
        except Exception: pass

        if is_junk(item["title"], price_val): continue
            
        print(f"  [+] Создаем витринный букет: {item['title']}")
        p = create_ms_product(item["title"], item["id"], folder_map.get("витрина"), price_val, logo_id)
        if p:
            ms_products.append({"id": p["id"], "name": item["title"], "external_code": item["id"], "href": p["meta"]["href"], "type": "product", "folder_href": folder_map.get("витрина"), "price": int(price_val * 100), "stock": 0.0})
            ms_ext_codes.add(item["id"])
            ms_names.add(n_n)

print("\n6. Авторские букеты...")
pos_recipes_bare = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/recipes?page[number]={page}&page[size]=100", timeout=30)
        if r.status_code != 200: break
        items = r.json().get("data", [])
        if not items: break

        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            if not is_junk(title):
                pos_recipes_bare.append({"id": str(it.get("id")), "title": title})
            
        if len(items) < 100: break
        page += 1
    except Exception: break

for item in pos_recipes_bare:
    n_n = norm_name(item["title"])
    if item["id"] not in ms_ext_codes and n_n not in ms_names:
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
                    if pr_obj: price_val = float(pr_obj.get("attributes", {}).get("value", 0))
                if price_val == 0: price_val = float(d.get("data", {}).get("attributes", {}).get("price", 0))
                logo_rel = rel.get("logo", {}).get("data")
                if logo_rel: logo_id = str(logo_rel.get("id"))
        except Exception: pass

        if is_junk(item["title"], price_val): continue
            
        p = create_ms_product(item["title"], item["id"], folder_map.get("авторские"), price_val, logo_id)
        if p:
            ms_products.append({"id": p["id"], "name": item["title"], "external_code": item["id"], "href": p["meta"]["href"], "type": "product", "folder_href": folder_map.get("авторские"), "price": int(price_val * 100), "stock": 0.0})
            ms_ext_codes.add(item["id"])
            ms_names.add(n_n)

print("\n7. Штучные товары и декор...")
pos_inventory = []
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/inventory-items?page[number]={page}&page[size]=100&include=category,logo,prices", timeout=30)
        if r.status_code != 200: break
        d = r.json()
        items = d.get("data", [])
        if not items: break

        inc_map = {(inc.get("type"), str(inc.get("id"))): inc for inc in d.get("included", [])}
        for it in items:
            title = it.get("attributes", {}).get("title", "").strip()
            if is_junk(title): continue
            
            it_id = str(it.get("id"))
            rel = it.get("relationships", {})
            
            cat_name = ""
            cat_rel = rel.get("category", {}).get("data")
            if cat_rel and cat_rel.get("id"):
                cat_obj = inc_map.get(("categories", str(cat_rel["id"])))
                if cat_obj: cat_name = cat_obj.get("attributes", {}).get("title", "")

            price_val = 0
            for pr_rel in rel.get("prices", {}).get("data", []):
                pr_obj = inc_map.get(("prices", str(pr_rel.get("id"))))
                if pr_obj: price_val = float(pr_obj.get("attributes", {}).get("value", 0))

            if is_junk(title, price_val): continue

            logo_id = None
            logo_rel = rel.get("logo", {}).get("data")
            if logo_rel: logo_id = str(logo_rel.get("id"))
                
            pos_inventory.append({"id": it_id, "title": title, "category": cat_name, "price": price_val, "logo_id": logo_id})
            
        if len(items) < 100: break
        page += 1
    except Exception: break

for item in pos_inventory:
    n_n = norm_name(item["title"])
    if item["id"] not in ms_ext_codes and n_n not in ms_names:
        f_target = get_folder_href_for_item(item["title"], item["category"])
        if f_target:
            p = create_ms_product(item["title"], item["id"], f_target, item["price"], item["logo_id"])
            if p:
                ms_products.append({"id": p["id"], "name": item["title"], "external_code": item["id"], "href": p["meta"]["href"], "type": "product", "folder_href": f_target, "price": int(item["price"] * 100), "stock": 0.0})
                ms_ext_codes.add(item["id"])
                ms_names.add(n_n)

print("\n8. Срезка...")
pos_flowers = {}
page = 1
while True:
    try:
        r = pos_s.get(f"{POSIFLORA_URL}/catalog?page[number]={page}&page[size]=100&include=balances", timeout=30)
        if r.status_code != 200: break
        d = r.json()
        items = d.get("data", [])
        if not items: break

        bal_map = {}
        for inc in d.get("included", []):
            if inc.get("type") == "item-balances":
                attrs = inc.get("attributes", {})
                bal_map[str(inc.get("id"))] = max(float(attrs.get("qtyOnWindow") or 0), float(attrs.get("qty") or 0))

        for it in items:
            title = str(it.get("attributes", {}).get("title") or "").strip()
            if is_junk(title): continue
            it_id = str(it.get("id"))
            st = 0.0
            for b in it.get("relationships", {}).get("balances", {}).get("data", []):
                bid = str(b.get("id"))
                if bid in bal_map: st += bal_map[bid]
            pos_flowers[norm_name(title)] = {"id": it_id, "title": title, "stock": st}
            
        if len(items) < 100: break
        page += 1
    except Exception: break

for n_n, f_data in pos_flowers.items():
    if f_data["id"] not in ms_ext_codes and n_n not in ms_names:
        f_target = default_flower_folder
        if f_target:
            p = create_ms_product(f_data["title"], f_data["id"], f_target, 0)
            if p:
                ms_products.append({"id": p["id"], "name": f_data["title"], "external_code": f_data["id"], "href": p["meta"]["href"], "type": "product", "folder_href": f_target, "price": 0, "stock": 0.0})
                ms_ext_codes.add(f_data["id"])
                ms_names.add(n_n)

print("\n9. Актуализация базы...")
enter_positions = []
loss_positions = []

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
        except Exception: pass

    if target_stock is None and n_name in pos_flowers:
        target_stock = pos_flowers[n_name]["stock"]

    if target_stock is not None:
        diff = round(target_stock - cur_stock, 2)
        if diff != 0:
            assort_meta = {"meta": {"href": p["href"], "type": p["type"], "mediaType": "application/json"}}
            if diff > 0:
                enter_positions.append({"quantity": diff, "price": p["price"], "assortment": assort_meta})
            elif diff < 0 and cur_stock > 0:
                loss_positions.append({"quantity": min(abs(diff), cur_stock), "assortment": assort_meta})

if enter_positions:
    for i in range(0, len(enter_positions), 1000):
        ms_s.post(f"{MOYSKLAD_URL}/entity/enter", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Автосинхронизация остатков",
            "positions": enter_positions[i:i+1000]
        }, timeout=60)

if loss_positions:
    for i in range(0, len(loss_positions), 1000):
        ms_s.post(f"{MOYSKLAD_URL}/entity/loss", json={
            "organization": {"meta": {"href": ORG_HREF, "type": "organization", "mediaType": "application/json"}},
            "store": {"meta": {"href": STORE_HREF, "type": "store", "mediaType": "application/json"}},
            "applicable": True,
            "description": "Автосписание проданных остатков",
            "positions": loss_positions[i:i+1000]
        }, timeout=60)

print("\nПолная синхронизация завершена успешно!")
