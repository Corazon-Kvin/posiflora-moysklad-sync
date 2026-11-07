#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 2: ИНВЕНТАРЬ И ОСТАТКИ (sync_inventory.py)
# С умным поиском по названию и поддержкой ПОДАРОЧНЫХ СЕРТИФИКАТОВ

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

REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60
IMAGE_TIMEOUT = 10
MAX_PAGES = 1000
IMAGE_MAX_SIDE = 1200
IMAGE_JPEG_QUALITY = 85

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ДОБАВЛЕНА КАТЕГОРИЯ СЕРТИФИКАТОВ
MS_FOLDER_KEYWORDS = {
    "certificate": ["подарочный сертификат", "сертификаты", "сертификат"],
    "author": ["авторские букеты", "рецепты"], 
    "vase": ["вазы", "ваза"],
    "toy": ["игрушки", "игрушк", "медведь", "заяц"],
    "packaging": ["подарочные коробки", "упаковк", "лента", "коробка", "пакет", "крафт", "открытка", "конверт", "бумага", "сумка"],
    "candlestick": ["подсвечники", "подсвечник"],
    "candle": ["свечи", "свеч", "цифра", "фонтан"],
    "flower": ["цветы", "срезка", "зелень", "роза", "хризантема", "питоспорум", "гвоздика", "антирринум", "диантус", "эвкалипт", "гипсофила", "рускус", "амариллис"],
    "balloon": ["шары", "шар", "сфера", "баблс", "фольга", "бабл"]
}

BLACKLIST = ["сырье", "эклер", "тарталет", "кофе", "сырник", "чиабатта", "багет", "булочк", "круассан", "сосиска", "кремибон", "макарон", "десерт", "напиток"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_inventory")

class ApiError(Exception): pass

def safe_float(value, default=0.0):
    if value is None or isinstance(value, bool): return default
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip().replace(",", ".").replace(" ", "")
    try: return float(text) if text else default
    except ValueError: return default

def normalize_name(name): return re.sub(r"\s+", " ", str(name or "").lower().replace("ё", "е")).strip()

def is_blacklisted(name):
    low = normalize_name(name)
    return any(kw in low for kw in BLACKLIST)

def build_included_index(included): return {(item.get("type"), str(item.get("id"))): item for item in (included or []) if isinstance(item, dict)}

def resolve_ref(ref, included_index):
    if not ref or not isinstance(ref, dict): return None
    return included_index.get((ref.get("type"), str(ref.get("id"))))

def pick_price(attributes):
    if not attributes: return 0.0
    for key in ("price", "salePrice", "retailPrice", "sellingPrice", "value", "amount", "totalAmount", "maxPrice", "minPrice", "basePrice", "regularPrice"):
        val = attributes.get(key)
        if isinstance(val, dict): val = val.get("amount") or val.get("value")
        val = safe_float(val)
        if val > 0: return val
    return 0.0

def extract_safe_price(row, attrs, idx):
    p = pick_price(attrs)
    if p > 0: return p
    prices_data = row.get("relationships", {}).get("prices", {}).get("data")
    if not prices_data: return 0.0
    ref_item = prices_data[0] if isinstance(prices_data, list) and len(prices_data) > 0 else prices_data
    ref_obj = resolve_ref(ref_item, idx)
    if ref_obj: return pick_price(ref_obj.get("attributes"))
    return 0.0

def logo_from_included(row, included_index):
    for rel_name in ("images", "logo", "image", "picture", "photo", "files", "file"):
        refs = row.get("relationships", {}).get(rel_name, {}).get("data")
        if not refs: continue
        refs = [refs] if isinstance(refs, dict) else (refs if isinstance(refs, list) else [])
        for ref in refs:
            inc = resolve_ref(ref, included_index)
            if inc:
                attrs = inc.get("attributes") or {}
                for k in ("file", "fileMedium", "fileShop", "url", "originalUrl", "fullUrl", "link", "src", "path", "fileUrl", "fileSmall"):
                    val = attrs.get(k)
                    if val: return str(val).strip()
    return ""

def env_bool(name): return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")

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
                if attempt >= MAX_RETRIES: raise ApiError(f"{self.label}: ошибка {exc}")
                time.sleep(2 * attempt); continue
            if resp.status_code in (429, 502, 503, 504):
                if attempt >= MAX_RETRIES: raise ApiError(f"{self.label}: HTTP {resp.status_code}")
                time.sleep(2 * attempt); continue
            if resp.status_code not in expected: raise ApiError(f"{self.label}: HTTP {resp.status_code}")
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

    def get_first_store_id(self):
        try:
            resp = self.request("GET", "/stores")
            return str(resp.get("data", [])[0]["id"])
        except: return None

    def get_inventory_qty(self, item_id, store_id):
        if not store_id: return 0.0
        try:
            body = self.request("GET", f"/inventory-items/{item_id}/stats-by-store/{store_id}")
            val = body.get("data", {}).get("attributes", {}).get("qty") or body.get("data", {}).get("qty")
            return max(safe_float(val), 0.0) if val is not None else 0.0
        except ApiError: return 0.0

class MoySkladClient(HttpClient):
    def __init__(self, base_url, token):
        super().__init__(base_url, "МойСклад")
        self.token = token

    def build_headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json;charset=utf-8", "Content-Type": "application/json"}

    def list_entities(self, path, extra_params=None):
        offset, limit, out = 0, 100, []
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params: params.update(extra_params)
            body = self.request("GET", path, params=params)
            batch = body.get("rows", []) if isinstance(body, dict) else body
            out.extend(batch)
            if len(batch) < limit: break
            offset += limit
        return out

def download_image(url, base_url):
    full_url = f"https:{url}" if url.startswith("//") else (url if url.startswith("http") else urljoin(base_url.rstrip("/") + "/", url))
    resp = requests.get(full_url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*"}, timeout=IMAGE_TIMEOUT)
    if resp.status_code != 200: raise Exception("HTTP " + str(resp.status_code))
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), getattr(Image, "Resampling", Image).LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def match_category(name, cat_title):
    text_to_check = normalize_name(f"{name} {cat_title}")
    for cat_key, keywords in MS_FOLDER_KEYWORDS.items():
        if any(kw in text_to_check for kw in keywords): return cat_key
    return None

def fetch_inventory_from_pf(pf, store_id):
    rows, included = pf.paginate("/inventory-items", {"include": "category,logo,images,prices", "page[size]": 100}, MAX_PAGES)
    idx, res = build_included_index(included), []
    for r in rows:
        attrs = r.get("attributes", {})
        name = str(attrs.get("title") or attrs.get("name") or "").strip()
        if not name or is_blacklisted(name): continue
        cat_ref = resolve_ref(r.get("relationships", {}).get("category", {}).get("data"), idx)
        cat_title = str(cat_ref.get("attributes", {}).get("title", "")) if cat_ref else ""
        if is_blacklisted(cat_title): continue
        mapped_cat = match_category(name, cat_title)
        if not mapped_cat: continue 
        res.append({
            "id": str(r.get("id")), "name": name, "price_kopecks": int(round(extract_safe_price(r, attrs, idx) * 100)),
            "image_url": logo_from_included(r, idx), "category": mapped_cat, "qty": pf.get_inventory_qty(str(r.get("id")), store_id)
        })
    return res

def fetch_recipes_from_pf(pf, store_id):
    try: rows, included = pf.paginate("/specifications", {"include": "category,logo,images,prices", "page[size]": 100}, MAX_PAGES)
    except: return []
    idx, res = build_included_index(included), []
    for r in rows:
        attrs = r.get("attributes", {})
        name = str(attrs.get("title") or attrs.get("name") or "").strip()
        if not name or is_blacklisted(name): continue
        cat_ref = resolve_ref(r.get("relationships", {}).get("category", {}).get("data"), idx)
        cat_title = str(cat_ref.get("attributes", {}).get("title", "")) if cat_ref else ""
        res.append({
            "id": str(r.get("id")), "name": name, "price_kopecks": int(round(extract_safe_price(r, attrs, idx) * 100)),
            "image_url": logo_from_included(r, idx), "category": match_category(name, cat_title) or "author",
            "qty": pf.get_inventory_qty(str(r.get("id")), store_id)
        })
    LOG.info(f"Posiflora: Получено рецептов - {len(res)} шт.")
    return res

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(config.values()): sys.exit("Ошибка конфигурации")
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()
        pf_store_id = pf.get_first_store_id()

        folders = ms.list_entities("/entity/productfolder")
        folder_meta = {}
        for f in folders:
            fname = normalize_name(f.get("name"))
            for cat_key, keywords in MS_FOLDER_KEYWORDS.items():
                if fname in keywords: folder_meta[cat_key] = f.get("meta"); break
        
        price_types = ms.list_entities("/context/companysettings/pricetype")
        price_meta = next((pt["meta"] for pt in price_types if pt.get("name", "").lower() == "цена продажи"), price_types[0]["meta"])

        ms_products = ms.list_entities("/entity/product", {"includeArchived": "false"})
        
        ms_index_by_code = {str(p.get("externalCode")): p for p in ms_products if p.get("externalCode")}
        ms_index_by_name = {normalize_name(p.get("name")): p for p in ms_products}
        
        pf_all = fetch_inventory_from_pf(pf, pf_store_id) + fetch_recipes_from_pf(pf, pf_store_id)
        stats = {"created": 0, "updated": 0, "img_ok": 0, "img_err": 0}

        for b in pf_all:
            target_folder = folder_meta.get(b["category"])
            if not target_folder: continue
            
            existing = ms_index_by_code.get(b["id"])
            if not existing:
                existing = ms_index_by_name.get(normalize_name(b["name"]))

            if existing:
                payload = {}
                if str(existing.get("externalCode")) != str(b["id"]): payload["externalCode"] = b["id"]
                if existing.get("name") != b["name"]: payload["name"] = b["name"][:255]
                if target_folder["href"] != existing.get("productFolder", {}).get("meta", {}).get("href", ""): payload["productFolder"] = {"meta": target_folder}
                
                prices = existing.get("salePrices", [])
                curr_price = prices[0].get("value", 0) if (prices and isinstance(prices, list)) else 0
                if curr_price != b["price_kopecks"] and b["price_kopecks"] > 0: payload["salePrices"] = [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                
                if payload and not dry_run: 
                    ms.request("PUT", f"/entity/product/{existing['id']}", json_body=payload)
                    stats["updated"] += 1
                
                prod_id, has_image = existing["id"], existing.get("images", {}).get("meta", {}).get("size", 0) > 0
                ms_index_by_code[b["id"]] = existing
            else:
                payload = {"name": b["name"][:255], "externalCode": b["id"], "productFolder": {"meta": target_folder}, "salePrices": [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]}
                if not dry_run:
                    created = ms.request("POST", "/entity/product", json_body=payload)
                    prod_id = created["id"]
                    ms_index_by_code[b["id"]] = created
                    stats["created"] += 1
                has_image = False

            if not has_image and b["image_url"] and not dry_run:
                try:
                    img_data = download_image(b["image_url"], config["POSIFLORA_URL"])
                    ms.request("POST", f"/entity/product/{prod_id}/images", json_body={"filename": f"{b['id']}.jpg", "content": img_data})
                    stats["img_ok"] += 1
                except: stats["img_err"] += 1

        stock_resp = ms.request("GET", "/report/stock/bystore/current", params={"filter": f"storeId={config['TARGET_STORE_ID']}"})
        stock_dict = {s.get("assortmentId"): safe_float(s.get("stock")) for s in (stock_resp if isinstance(stock_resp, list) else stock_resp.get("rows", []))}
        
        enters, losses = [], []
        for pf_item in pf_all:
            ms_item = ms_index_by_code.get(pf_item["id"])
            if not ms_item: continue
            target_qty, curr_qty = pf_item["qty"], stock_dict.get(ms_item["id"], 0.0)
            diff = round(target_qty - curr_qty, 3)
            if diff > 0: enters.append({"assortment": {"meta": ms_item["meta"]}, "quantity": diff, "price": 0})
            elif diff < 0 and curr_qty > 0: losses.append({"assortment": {"meta": ms_item["meta"]}, "quantity": min(abs(diff), curr_qty), "price": 0})

        org = {"href": f"{config['MOYSKLAD_URL']}/entity/organization/{config['ORG_ID']}", "type": "organization", "mediaType": "application/json"}
        store = {"href": f"{config['MOYSKLAD_URL']}/entity/store/{config['TARGET_STORE_ID']}", "type": "store", "mediaType": "application/json"}
        def post_docs(endpoint, positions):
            for i in range(0, len(positions), 100):
                if not dry_run: ms.request("POST", endpoint, json_body={"organization": {"meta": org}, "store": {"meta": store}, "applicable": True, "positions": positions[i:i + 100]})
        if enters: post_docs("/entity/enter", enters)
        if losses: post_docs("/entity/loss", losses)

        LOG.info(f"Итоги инвентаря: {stats} Оприходовано/Списано строк: {len(enters)}/{len(losses)}")
    except Exception as e: LOG.error(f"Критическая ошибка: {e}"); sys.exit(1)

if __name__ == "__main__": main()
