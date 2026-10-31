#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 1: БЫСТРАЯ ВИТРИНА (sync_showcase.py)
# Задача: Синхронизация свежих букетов (создание, цены, фото, остатки) между Posiflora и папкой "Витрина Tilda".
# Запуск: Каждые 15-30 минут.

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
MAX_RETRIES = 3
HTTP_TIMEOUT = 15  # Быстрый таймаут для API
IMAGE_TIMEOUT = 5  # Жесткий таймаут для фото
MAX_PAGES = 5      # Глубина сканирования Posiflora (5 страниц = 500 свежих букетов)
DOCUMENT_CHUNK_SIZE = 500
IMAGE_MAX_SIDE = 1200
IMAGE_JPEG_QUALITY = 85

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Нас интересует только одна папка
SHOWCASE_KEYWORDS = ["витрина tilda", "витрина"]

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
    return included_index.get((ref.get("type"), str(ref.get("id"))))

def pick_price(attributes):
    for key in ("price", "salePrice", "retailPrice", "sellingPrice", "value", "amount", "totalAmount"):
        val = safe_float((attributes or {}).get(key))
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

class MoySkladClient(HttpClient):
    def __init__(self, base_url, token):
        super().__init__(base_url, "МойСклад")
        self.token = token

    def build_headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": "application/json", "Accept-Encoding": "gzip"}

    def list_entities(self, path, extra_params=None):
        rows, offset, limit = [], 0, 1000
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params: params.update(extra_params)
            body = self.request("GET", path, params=params)
            batch = body.get("rows", [])
            rows.extend(batch)
            if len(batch) < limit: break
            offset += limit
        return rows

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

def fetch_showcase_bouquets(posiflora):
    rows, included = posiflora.paginate("/bouquets", {"include": "logo,prices", "sort": "-createdAt", "page[size]": 100}, MAX_PAGES)
    idx = build_included_index(included)
    res = []
    for r in rows:
        attrs = r.get("attributes", {})
        if not attrs.get("onWindowAt"): continue # Только выставленные на витрину
        
        status = str(attrs.get("status") or attrs.get("state") or "").strip().lower()
        if status not in ("assembled", "on_window"): continue # Только собранные (исключаем проданные)
        
        name = str(attrs.get("title") or attrs.get("name") or "").strip()
        if re.match(r"^\d{3,4}$", name): name = f"Букет {name}"
        if not re.search(r"букет", name, re.IGNORECASE) and not name.isdigit(): continue
        
        price = pick_price(attrs) or pick_price(resolve_ref(r.get("relationships", {}).get("prices", {}).get("data", [{}])[0], idx).get("attributes") if resolve_ref(r.get("relationships", {}).get("prices", {}).get("data", [{}])[0], idx) else None)
        if price <= 0: continue
        
        res.append({
            "id": str(r.get("id")), "name": name, "price_kopecks": int(round(price * 100)),
            "image_url": logo_from_included(r, idx)
        })
    LOG.info("Posiflora: Найдено %s актуальных букетов на витрине.", len(res))
    return res

def main():
    config = {k: os.environ.get(k, "").strip() for k in ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID"]}
    if not all(config.values()): sys.exit("Ошибка: не заданы переменные окружения.")
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()

        # 1. Находим метаданные папки "Витрина Tilda" и типа цены
        folders = ms.list_entities("/entity/productfolder")
        showcase_meta = None
        for f in folders:
            if normalize_name(f.get("name")) in SHOWCASE_KEYWORDS:
                showcase_meta = f.get("meta")
                break
        if not showcase_meta: raise ApiError("Папка 'Витрина Tilda' не найдена в МоемСкладе.")

        price_types = ms.list_entities("/context/companysettings/pricetype")
        price_meta = next((pt["meta"] for pt in price_types if pt.get("name", "").lower() == "цена продажи"), price_types[0]["meta"])

        # 2. Выгружаем товары ТОЛЬКО из папки "Витрина Tilda" (очень быстро)
        ms_products = ms.list_entities("/entity/product", {
            "filter": f"productFolder={showcase_meta['href']}",
            "includeArchived": "true"
        })
        ms_index = {str(p.get("externalCode")): p for p in ms_products}
        
        # 3. Выгружаем актуальные букеты с витрины Posiflora
        pf_bouquets = fetch_showcase_bouquets(pf)
        pf_active_codes = {b["id"] for b in pf_bouquets}

        stats = {"created": 0, "updated": 0, "archived": 0, "img_ok": 0, "img_err": 0}

        # 4. Создаем и обновляем букеты
        for b in pf_bouquets:
            existing = ms_index.get(b["id"])
            payload = {}
            if existing:
                if existing.get("archived"): payload["archived"] = False
                if existing.get("name") != b["name"]: payload["name"] = b["name"][:255]
                
                prices = existing.get("salePrices", [])
                curr_price = prices[0].get("value", 0) if prices else 0
                if curr_price != b["price_kopecks"]:
                    payload["salePrices"] = [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                
                if payload and not dry_run:
                    ms.request("PUT", f"/entity/product/{existing['id']}", json_body=payload)
                    stats["updated"] += 1
                prod_id = existing["id"]
                has_image = bool(existing.get("images", {}).get("meta", {}).get("size", 0) or "image" in existing)
            else:
                payload = {
                    "name": b["name"][:255], "externalCode": b["id"],
                    "productFolder": {"meta": showcase_meta},
                    "salePrices": [{"value": b["price_kopecks"], "priceType": {"meta": price_meta}}]
                }
                if not dry_run:
                    created = ms.request("POST", "/entity/product", json_body=payload)
                    prod_id = created["id"]
                    stats["created"] += 1
                has_image = False

            if not has_image and b["image_url"] and not dry_run:
                try:
                    img_data = download_image(b["image_url"], config["POSIFLORA_URL"])
                    ms.request("POST", f"/entity/product/{prod_id}/images", json_body={"filename": f"{b['id']}.jpg", "content": img_data})
                    stats["img_ok"] += 1
                except Exception as e:
                    stats["img_err"] += 1
                    LOG.warning(f"Не удалось загрузить фото для {b['name']}: {e}")

        # 5. Архивируем те, что ушли с витрины
        for code, p in ms_index.items():
            if code not in pf_active_codes and not p.get("archived"):
                if not dry_run:
                    ms.request("PUT", f"/entity/product/{p['id']}", json_body={"archived": True})
                    stats["archived"] += 1

        # 6. Остатки: всем актуальным = 1, архивным = 0
        current_stocks = ms.request("GET", "/report/stock/bystore/current", params={"filter": f"storeId={config['TARGET_STORE_ID']}"}).get("rows", [])
        stock_dict = {s.get("assortmentId") or s.get("meta", {}).get("href", "").split("/")[-1]: safe_float(s.get("stock")) for s in current_stocks}
        
        enters, losses = [], []
        
        # Обновляем индекс после создания для расчета остатков
        ms_products = ms.list_entities("/entity/product", {"filter": f"productFolder={showcase_meta['href']}", "includeArchived": "true"})
        for p in ms_products:
            is_active = str(p.get("externalCode")) in pf_active_codes
            target_qty = 1.0 if is_active else 0.0
            curr_qty = stock_dict.get(p["id"], 0.0)
            diff = round(target_qty - curr_qty, 3)
            
            if diff > 0: enters.append({"assortment": {"meta": p["meta"]}, "quantity": diff, "price": 0})
            elif diff < 0 and curr_qty > 0: losses.append({"assortment": {"meta": p["meta"]}, "quantity": min(abs(diff), curr_qty), "price": 0})

        org = {"href": f"{config['MOYSKLAD_URL']}/entity/organization/{config['ORG_ID']}", "type": "organization", "mediaType": "application/json"}
        store = {"href": f"{config['MOYSKLAD_URL']}/entity/store/{config['TARGET_STORE_ID']}", "type": "store", "mediaType": "application/json"}

        if enters and not dry_run:
            ms.request("POST", "/entity/enter", json_body={"organization": {"meta": org}, "store": {"meta": store}, "applicable": True, "positions": enters})
        if losses and not dry_run:
            ms.request("POST", "/entity/loss", json_body={"organization": {"meta": org}, "store": {"meta": store}, "applicable": True, "positions": losses})

        LOG.info(f"Остатки: оприходовано {len(enters)}, списано {len(losses)}")
        LOG.info(f"Итоги витрины: {stats}")

    except Exception as e:
        LOG.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
