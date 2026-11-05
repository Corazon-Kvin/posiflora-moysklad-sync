#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 2: ИНВЕНТАРЬ И ОСТАТКИ (sync_inventory.py)
# Задача: Синхронизация каталога штучных товаров, распределение по строгим категориям, выравнивание остатков.
# Запуск: Каждые 2-3 часа.

import base64
import io
import logging
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
from PIL import Image

# -----------------------------------------------------------------------------
# КОНСТАНТЫ
# -----------------------------------------------------------------------------
REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60
IMAGE_TIMEOUT = 10
MAX_PAGES = 1000
DOCUMENT_CHUNK_SIZE = 500
IMAGE_MAX_SIDE = 1200
IMAGE_JPEG_QUALITY = 85
PRODUCT_NAME_LIMIT = 255

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Строгое сопоставление категорий (по ТЗ)
MS_FOLDER_KEYWORDS = {
    "author": ["авторские букеты", "рецепты"],
    "vase": ["вазы", "ваза"],
    "toy": ["игрушки", "игрушк"],
    "packaging": ["подарочные коробки", "упаковк"],
    "candlestick": ["подсвечники", "подсвечник"],
    "candle": ["свечи", "свеч"],
    "flower": ["цветы"],
    "balloon": ["шары", "шар"]
}

# Игнорируем сырье и кофейный мусор
BLACKLIST = ["сырье", "эклер", "тарталет", "кофе", "сырник", "чиабатта", "багет", "булочк", "круассан", "сосиска", "кремибон", "макарон", "десерт", "напиток"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_inventory")

class ApiError(Exception): pass

# -----------------------------------------------------------------------------
# УТИЛИТЫ И API КЛИЕНТЫ
# -----------------------------------------------------------------------------
def safe_float(value, default=0.0):
    if value is None or isinstance(value, bool): return default
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip().replace(",", ".").replace(" ", "")
    try: return float(text) if text else default
    except ValueError: return default

def normalize_name(name):
    return re.sub(r"\s+", " ", str(name or "").lower().replace("ё", "е")).strip()

def is_blacklisted(name):
    low = normalize_name(name)
    for kw in BLACKLIST:
        if kw in low: return True
    return False

def build_included_index(included):
    return {(item.get("type"), str(item.get("id"))): item for item in (included or []) if isinstance(item, dict)}

def resolve_ref(ref, included_index):
    return included_index.get((ref.get("type"), str(ref.get("id"))))

def pick_price(attributes):
    if not attributes: return 0.0
    for key in ("price", "salePrice", "retailPrice", "sellingPrice", "value", "amount", "totalAmount"):
        val = attributes.get(key)
        if isinstance(val, dict):
            val = val.get("amount") or val.get("value")
        val = safe_float(val)
        if val > 0: return val
    return 0.0

def logo_from_included(row, included_index):
    for rel_name in ("logo", "images", "image"):
        refs = row.get("relationships", {}).get(rel_name, {}).get("data")
        refs = [refs] if isinstance(refs, dict) else (refs if isinstance(refs, list) else [])
        for ref in refs:
            inc = resolve_ref(ref, included_index)
            if inc:
                attrs = inc.get("attributes") or {}
                for k in ("url", "originalUrl", "fullUrl", "link"):
                    if attrs.get(k): return str(attrs[k]).strip()
    return ""

def env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")

class HttpClient:
    def __init__(self, base_url, label):
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.session = requests.Session()

    def build_headers(self): return {}

    def request(self, method, path, params=None, json_body=None, expected=(200, 201)):
        url = path if path.startswith("http") else self.base_url + path
        attempt = 0
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=self.build_headers(), timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES: raise ApiError(f"{self.label}: ошибка сети {url}: {exc}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code in (429, 502, 503, 504):
                if attempt >= MAX_RETRIES: raise ApiError(f"{self.label}: HTTP {resp.status_code} {url}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code not in expected:
                raise ApiError(f"{self.label}: HTTP {resp.status_code} {url} :: {resp.text[:200]}")
            return resp.json() if resp.content else {}

class PosifloraClient(HttpClient):
    def __init__(self, base_url, username, password):
        super().__init__(base_url, "Posiflora")
        self.username = username
        self.password = password
        self.token = ""

    def build_headers(self):
        h = {"Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json", "User-Agent": BROWSER_UA}
        if self.token: h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self):
        body = {"data": {"type": "sessions", "attributes": {"username": self.username, "password": self.password}}}
        resp = self.request("POST", "/sessions", json_body=body, expected=(200, 201))
        self.token = resp.get("data", {}).get("attributes", {}).get("accessToken") or resp.get("data", {}).get("accessToken")
        if not self.token: raise ApiError("Posiflora: accessToken не найден")

    def paginate(self, path, params, max_pages):
        rows, included, page = [], [], 1
        while page <= max_pages:
            p = dict(params); p["page[number]"] = page
            body = self.request("GET", path, params=p)
            data = body.get("data", [])
            if isinstance(data, dict): data = [data]
            if not data: break
            rows.extend(data)
            included.extend(body.get("included", []))
            if len(data) < p.get("page[size]", 100): break
            page += 1
        return rows, included

    def get_inventory_qty(self, item_id, store_id):
        path = f"/inventory-items/{item_id}/stats-by-store/{store_id}"
        try:
            body = self.request("GET", path)
            val = body.get("data", {}).get("attributes", {}).get("qty") or body.get("data", {}).get("qty")
            return max(safe_float(val), 0.0) if val is not None else 0.0
        except ApiError:
            return 0.0

class MoySkladClient(HttpClient):
    def __init__(self, base_url, token):
        super().__init__(base_url, "МойСклад")
        self.token = token

    def build_headers(self):
        return {
            "Authorization": f"Bearer {self.token}", 
            "Accept": "application/json;charset=utf-8", 
            "Content-Type": "application/json", 
            "Accept-Encoding": "gzip"
        }

    def list_entities(self, path, extra_params=None):
        offset, limit = 0, 100
        out = []
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params: params.update(extra_params)
            body = self.request("GET", path, params=params)
            
            if isinstance(body, list):
                return body
                
            batch = body.get("rows", [])
            out.extend(batch)
            if len(batch) < limit: break
            offset += limit
        return out

# -----------------------------------------------------------------------------
# ЛОГИКА
# -----------------------------------------------------------------------------
def download_image(url, base_url):
    full_url = f"https:{url}" if url.startswith("//") else (url if url.startswith("http") else urljoin(base_url.rstrip("/") + "/", url))
    h = {"User-Agent": BROWSER_UA, "Accept": "image/*"}
    resp = requests.get(full_url, headers=h, timeout=IMAGE_TIMEOUT)
    if resp.status_code != 200: raise Exception("HTTP " + str(resp.status_code))
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), getattr(Image, "Resampling", Image).LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def match_category(name, cat_title):
    text_to_check = f"{name} {cat_title}"
    low = normalize_name(text_to_check)
    for cat_key, keywords in MS_FOLDER_KEYWORDS.items():
        for kw in keywords:
            if kw in low: return cat_key
    return None

def fetch_inventory_from_pf(pf, store_id):
    rows, included = pf.paginate("/inventory-items", {"include": "category,logo,prices", "page[size]": 100}, MAX_PAGES)
    idx = build_included_index(included)
    res = []
    
    for r in rows:
        attrs = r.get("attributes", {})
        name = str(attrs.get("title") or attrs.get("name") or "").strip()
        if not name or is_blacklisted(name): continue
        
        cat_title = ""
        cat_ref = resolve_ref(r.get("relationships", {}).get("category", {}).get("data", {}), idx)
        if cat_ref: cat_title = str(cat_ref.get("attributes", {}).get("title", ""))
        if is_blacklisted(cat_title): continue
        
        mapped_cat = match_category(name, cat_title)
        if not mapped_cat: continue 
        
        price = pick_price(attrs) or pick_price(resolve_ref(r.get("relationships", {}).get("prices", {}).get("data", [{}])[0], idx).get("attributes") if resolve_ref(r.get("relationships", {}).get("prices", {}).get("data", [{}])[0], idx) else None)
        
        item_id = str(r.get("id"))
        qty = pf.get_inventory_qty(item_id, store_id)
        
        res.append({
            "id": item_id, "name": name, "price_kopecks": int(round(price * 100)),
            "image_url": logo_from_included(r, idx), "category": mapped_cat, "qty": qty
        })
    LOG.info("Posiflora: Отобрано %s целевых штучных товаров.", len(res))
    return res

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    
    missing = [k for k, v in config.items() if not v]
    if missing: 
        sys.exit(f"Ошибка: не заданы переменные окружения: {', '.join(missing)}")
        
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()

        # --- НАЧАЛО ОТЛАДКИ РЕЦЕПТОВ (РАДАР) ---
        endpoints_to_try = [
            "/catalog", "/assortment", "/templates", "/items", "/goods", 
            "/recipe-items", "/catalog-items", "/bouquets-templates", "/variants"
        ]
        
        LOG.info("\n=== ЗАПУСК РАДАРА ЭНДПОИНТОВ POSIFLORA ===")
        for ep in endpoints_to_try:
            try:
                test_resp = pf.request("GET", ep, params={"page[size]": 1})
                LOG.info(f"[УСПЕХ] Эндпоинт НАЙДЕН: {ep} -> Ответил без 404!")
            except Exception as e:
                pass # Игнорируем 404 ошибки для чистоты лога
        LOG.info("=== РАДАР ЗАВЕРШЕН ===\n")
        # --- КОНЕЦ ОТЛАДКИ ---

        # 1. Маппинг папок МоегоСклада
        folders = ms.list_entities("/entity/productfolder")
        folder_meta = {}
        for f in folders:
            fname = normalize_name(f.get("name"))
            for cat_key, keywords in MS_FOLDER_KEYWORDS.items():
                if fname in keywords:
                    folder_meta[cat_key] = f.get("meta")
                    break
        
        price_types = ms.list_entities("/context/companysettings/pricetype")
        price_meta = next((pt["meta"] for pt in price_types if pt.get("name", "").lower() == "цена продажи"), price_types[0]["meta"])

        # 2. Выгружаем все товары МоегоСклада для сопоставления
        ms_products = ms.list_entities("/entity/product", {"includeArchived": "false"})
        ms_index = {str(p.get("externalCode")): p for p in ms_products}
        
        # 3. Выгружаем инвентарь из Posiflora
        pf_items = fetch_inventory_from_pf(pf, config["TARGET_STORE_ID"])

        stats = {"created": 0, "updated": 0, "img_ok": 0, "img_err": 0}

        # 4. Обновление карточек в МоемСкладе
        for b in pf_items:
            target_folder = folder_meta.get(b["category"])
            if not target_folder: continue
            
            existing = ms_index.get(b["id"])
            payload = {}
            if existing:
                if existing.get("name") != b["name"]: payload["name"] = b["name"][:255]
                
                curr_folder_href = existing.get("productFolder", {}).get("meta", {}).get("href", "")
                if target_folder["href"] != curr_folder_href:
                    payload["productFolder"] = {"meta": target_folder}
                
                prices = existing.get("salePrices", [])
                curr_price = prices[0].get("value", 0) if prices else 0
                if curr_price != b["price_kopecks"] and b["price_kopecks"] > 0:
                    payload["salePrices"] = [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                
                if payload and not dry_run:
                    ms.request("PUT", f"/entity/product/{existing['id']}", json_body=payload)
                    stats["updated"] += 1
                prod_id = existing["id"]
                has_image = bool(existing.get("images", {}).get("meta", {}).get("size", 0) or "image" in existing)
            else:
                payload = {
                    "name": b["name"][:255], "externalCode": b["id"],
                    "productFolder": {"meta": target_folder},
                    "salePrices": [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                }
                if not dry_run:
                    created = ms.request("POST", "/entity/product", json_body=payload)
                    prod_id = created["id"]
                    # Добавляем созданный товар в индекс памяти для расчета остатков
                    ms_index[b["id"]] = created
                    stats["created"] += 1
                has_image = False

            if not has_image and b["image_url"] and not dry_run:
                try:
                    img_data = download_image(b["image_url"], config["POSIFLORA_URL"])
                    ms.request("POST", f"/entity/product/{prod_id}/images", json_body={"filename": f"{b['id']}.jpg", "content": img_data})
                    stats["img_ok"] += 1
                except Exception as e:
                    stats["img_err"] += 1

        # 5. Синхронизация остатков штучных товаров
        stock_resp = ms.request("GET", "/report/stock/bystore/current", params={"filter": f"storeId={config['TARGET_STORE_ID']}"})
        current_stocks = stock_resp if isinstance(stock_resp, list) else stock_resp.get("rows", [])
        stock_dict = {s.get("assortmentId") or s.get("meta", {}).get("href", "").split("/")[-1]: safe_float(s.get("stock")) for s in current_stocks}
        
        enters, losses = [], []
        
        for pf_item in pf_items:
            ms_item = ms_index.get(pf_item["id"])
            if not ms_item: continue
            
            target_qty = pf_item["qty"]
            curr_qty = stock_dict.get(ms_item["id"], 0.0)
            diff = round(target_qty - curr_qty, 3)
            
            if diff > 0: 
                enters.append({"assortment": {"meta": ms_item["meta"]}, "quantity": diff, "price": 0})
            elif diff < 0 and curr_qty > 0: 
                losses.append({"assortment": {"meta": ms_item["meta"]}, "quantity": min(abs(diff), curr_qty), "price": 0})

        org = {"href": f"{config['MOYSKLAD_URL']}/entity/organization/{config['ORG_ID']}", "type": "organization", "mediaType": "application/json"}
        store = {"href": f"{config['MOYSKLAD_URL']}/entity/store/{config['TARGET_STORE_ID']}", "type": "store", "mediaType": "application/json"}

        # Проводим документы батчами по 100 позиций
        def post_docs(endpoint, positions, label):
            for i in range(0, len(positions), 100):
                chunk = positions[i:i + 100]
                if not dry_run:
                    ms.request("POST", endpoint, json_body={"organization": {"meta": org}, "store": {"meta": store}, "applicable": True, "positions": chunk})
                LOG.info(f"{label}: проведено {len(chunk)} позиций")

        if enters: post_docs("/entity/enter", enters, "Оприходование инвентаря")
        if losses: post_docs("/entity/loss", losses, "Списание инвентаря")

        LOG.info(f"Итоги инвентаря: {stats}")

    except Exception as e:
        LOG.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
