#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 1: ЖИВАЯ ВИТРИНА (sync_showcase.py)
# Задача: Синхронизация СОБРАННЫХ ЭКЗЕМПЛЯРОВ букетов и авто-архивация проданных.
# Запуск: Каждые 30 минут.

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
IMAGE_MAX_SIDE = 1200
IMAGE_JPEG_QUALITY = 85

TARGET_FOLDER_NAME = "Витрина Tilda"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_showcase")

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

def build_included_index(included):
    return {(item.get("type"), str(item.get("id"))): item for item in (included or []) if isinstance(item, dict)}

def resolve_ref(ref, included_index):
    if not ref or not isinstance(ref, dict): return None
    return included_index.get((ref.get("type"), str(ref.get("id"))))

def pick_price(attributes):
    if not attributes: return 0.0
    for key in ("price", "salePrice", "retailPrice", "sellingPrice", "value", "amount", "totalAmount", "maxPrice", "minPrice", "basePrice", "regularPrice"):
        val = attributes.get(key)
        if isinstance(val, dict):
            val = val.get("amount") or val.get("value")
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
    if ref_obj:
        return pick_price(ref_obj.get("attributes"))
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
            if isinstance(body, list): return body
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

def fetch_live_bouquets(pf, store_id):
    res = []
    try:
        LOG.info("Posiflora: Стучимся в раздел собранных букетов (/bouquets)...")
        # Убрали 'prices' из списка include, чтобы не было ошибки 422
        rows, inc = pf.paginate("/bouquets", {"include": "logo,images", "page[size]": 100}, MAX_PAGES)
        idx = build_included_index(inc)
        
        LOG.info(f"Posiflora: Сервер отдал {len(rows)} записей в /bouquets.")
        
        for r in rows:
            attrs = r.get("attributes", {})
            
            name = str(attrs.get("title") or attrs.get("name") or "").strip()
            if not name: name = f"Букет {str(r.get('id'))[-4:]}"
            
            price = extract_safe_price(r, attrs, idx)
            item_id = str(r.get("id"))
            
            img_url = logo_from_included(r, idx)
            
            res.append({"id": item_id, "name": name, "price_kopecks": int(round(price * 100)), "image_url": img_url, "qty": 1.0})
            
    except Exception as e:
        LOG.error(f"Posiflora: ОШИБКА доступа к /bouquets: {e}")

    LOG.info(f"Posiflora: В итоге отобрано {len(res)} ЖИВЫХ букетов для витрины.")
    return res

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    
    missing = [k for k, v in config.items() if not v]
    if missing: sys.exit(f"Ошибка: не заданы переменные: {', '.join(missing)}")
        
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()

        folders = ms.list_entities("/entity/productfolder")
        target_meta = None
        for f in folders:
            if normalize_name(f.get("name")) == normalize_name(TARGET_FOLDER_NAME):
                target_meta = f.get("meta")
                break
        
        if not target_meta: sys.exit(f"Папка {TARGET_FOLDER_NAME} не найдена в МоемСкладе!")

        price_types = ms.list_entities("/context/companysettings/pricetype")
        price_meta = next((pt["meta"] for pt in price_types if pt.get("name", "").lower() == "цена продажи"), price_types[0]["meta"])

        ms_products = ms.list_entities("/entity/product", {"includeArchived": "false"})
        ms_index = {str(p.get("externalCode")): p for p in ms_products}
        
        showcase_items = fetch_live_bouquets(pf, config["TARGET_STORE_ID"])
        stats = {"created": 0, "updated": 0, "archived_sold": 0, "img_ok": 0, "img_err": 0}

        # 1. Обновляем или создаем живые букеты
        for b in showcase_items:
            existing = ms_index.get(b["id"])
            payload = {}
            if existing:
                if existing.get("name") != b["name"]: payload["name"] = b["name"][:255]
                
                curr_folder_href = existing.get("productFolder", {}).get("meta", {}).get("href", "")
                if target_meta["href"] != curr_folder_href:
                    payload["productFolder"] = {"meta": target_meta}
                
                prices = existing.get("salePrices", [])
                curr_price = prices[0].get("value", 0) if (prices and isinstance(prices, list)) else 0
                if curr_price != b["price_kopecks"] and b["price_kopecks"] > 0:
                    payload["salePrices"] = [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                
                if payload and not dry_run:
                    ms.request("PUT", f"/entity/product/{existing['id']}", json_body=payload)
                    stats["updated"] += 1
                prod_id = existing["id"]
                
                has_image = existing.get("images", {}).get("meta", {}).get("size", 0) > 0
            else:
                payload = {
                    "name": b["name"][:255], "externalCode": b["id"],
                    "productFolder": {"meta": target_meta},
                    "salePrices": [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                }
                if not dry_run:
                    created = ms.request("POST", "/entity/product", json_body=payload)
                    prod_id = created["id"]
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

        # 2. АВТО-УДАЛЕНИЕ ПРОДАННЫХ БУКЕТОВ (Архивация)
        valid_showcase_ids = {b["id"] for b in showcase_items}
        for code, p in ms_index.items():
            if p.get("productFolder", {}).get("meta", {}).get("href") == target_meta["href"]:
                if code not in valid_showcase_ids and not p.get("archived"):
                    if not dry_run:
                        ms.request("PUT", f"/entity/product/{p['id']}", json_body={"archived": True})
                        stats["archived_sold"] += 1

        LOG.info(f"Итоги витрины: {stats}")

    except Exception as e:
        LOG.error(f"Критическая ошибка витрины: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
