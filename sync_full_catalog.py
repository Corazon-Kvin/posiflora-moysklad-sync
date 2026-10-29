#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Односторонняя синхронизация каталога и остатков: Posiflora -> МойСклад.
# Запуск: python sync_full_catalog.py (GitHub Actions, cron каждые 30 минут).
# Зависимости: requests, Pillow.

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
MAX_PAGES = 2000
DOCUMENT_CHUNK_SIZE = 1000
AUTHOR_STOCK_LIMIT = 5.0
IMAGE_MAX_SIDE = 1200
IMAGE_JPEG_QUALITY = 85
PRODUCT_NAME_LIMIT = 255

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

COFFEE_BLACKLIST = [
    "эклер", "тарталет", "кофе", "сырник", "чиабатта", "багет", "булочк",
    "круассан", "сосиска", "датская", "кремибон", "макарон", "десерт", "напиток",
]

FOLDER_RULES = [
    ("showcase", ["витрина tilda", "витрина"]),
    ("author", ["авторские букеты", "авторские"]),
    ("candle", ["свеч"]),
    ("vase", ["ваз"]),
    ("balloon", ["шар"]),
    ("packaging", ["подарочные коробки", "упаковк"]),
    ("decor", ["декор", "игрушк", "подсвечник"]),
    ("recipes", ["каталог/цветы"]),
]

POSIFLORA_CATEGORY_RULES = [
    ("recipes", ["рецепт"]),
    ("author", ["авторски", "композици"]),
    ("balloon", ["шар", "гелий"]),
    ("vase", ["ваз"]),
    ("candle", ["свеч"]),
    ("packaging", ["подарочник", "упаковк", "коробк", "лент", "пакет", "открытк"]),
    ("decor", ["декор", "игрушк", "топпер"]),
]

STOCK_KIND = {
    "showcase": "showcase",
    "author": "author",
    "recipes": "author",
    "balloon": "piece",
    "decor": "piece",
    "vase": "piece",
    "candle": "piece",
    "packaging": "piece",
}

PRICE_KEYS = (
    "price", "salePrice", "retailPrice", "sellingPrice", "value",
    "amount", "totalAmount", "trueAmount", "docPrice", "sum",
)
IMAGE_URL_KEYS = ("url", "originalUrl", "original", "fullUrl", "link", "src", "path", "thumbnailUrl", "file")
ROW_IMAGE_KEYS = ("logoUrl", "imageUrl", "image", "logo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("posiflora_sync")


class ApiError(Exception):
    pass


# -----------------------------------------------------------------------------
# УТИЛИТЫ
# -----------------------------------------------------------------------------
def safe_get(data, *path, default=None):
    current = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and 0 <= key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


def safe_float(value, default=0.0):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".").replace(" ", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def normalize_name(name):
    text = str(name or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def is_blacklisted(name):
    low = normalize_name(name)
    for keyword in COFFEE_BLACKLIST:
        if keyword in low:
            return True
    return False


def get_title(attributes):
    attrs = attributes or {}
    for key in ("title", "name", "docNo"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pick_price(attributes):
    attrs = attributes or {}
    for key in PRICE_KEYS:
        value = safe_float(attrs.get(key))
        if value > 0:
            return value
    return 0.0


def as_ref_list(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def build_included_index(included):
    index = {}
    for item in included or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("type"), str(item.get("id")))
        index[key] = item
    return index


def resolve_ref(ref, included_index):
    key = (ref.get("type"), str(ref.get("id")))
    return included_index.get(key)


def price_from_included(row, included_index):
    refs = as_ref_list(safe_get(row, "relationships", "prices", "data"))
    for ref in refs:
        included_item = resolve_ref(ref, included_index)
        if included_item is None:
            continue
        price = pick_price(included_item.get("attributes"))
        if price > 0:
            return price
    return 0.0


def logo_from_included(row, included_index):
    for rel_name in ("logo", "images", "image", "photos"):
        refs = as_ref_list(safe_get(row, "relationships", rel_name, "data"))
        for ref in refs:
            included_item = resolve_ref(ref, included_index)
            if included_item is None:
                continue
            attrs = included_item.get("attributes") or {}
            for key in IMAGE_URL_KEYS:
                value = attrs.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    attrs_row = row.get("attributes") or {}
    for key in ROW_IMAGE_KEYS:
        value = attrs_row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def last_segment(href):
    if not isinstance(href, str) or not href:
        return ""
    clean = href.split("?")[0].rstrip("/")
    return clean.split("/")[-1]


def build_meta(base_url, entity, entity_id):
    return {
        "href": base_url + "/entity/" + entity + "/" + entity_id,
        "type": entity,
        "mediaType": "application/json",
    }


def env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")


# -----------------------------------------------------------------------------
# HTTP-КЛИЕНТЫ
# -----------------------------------------------------------------------------
class HttpClient:
    def __init__(self, base_url, label):
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.session = requests.Session()

    def build_headers(self):
        return {}

    def refresh_auth(self):
        return False

    def retry_delay(self, response, attempt):
        header_ms = safe_float(response.headers.get("X-Lognex-Retry-TimeInterval"))
        if header_ms > 0:
            return max(header_ms / 1000.0, 1.0)
        header_sec = safe_float(response.headers.get("Retry-After"))
        if header_sec > 0:
            return header_sec
        return float(2 * attempt)

    def request(self, method, path, params=None, json_body=None, expected=(200, 201)):
        url = path if path.startswith("http") else self.base_url + path
        attempt = 0
        reauthorized = False
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=self.build_headers(), timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise ApiError("{0}: сетевая ошибка {1} {2}: {3}".format(self.label, method, url, exc))
                LOG.warning("%s: сетевая ошибка (%s), попытка %s", self.label, exc, attempt)
                time.sleep(2 * attempt)
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                if attempt >= MAX_RETRIES:
                    raise ApiError("{0}: HTTP {1} {2} {3}".format(self.label, status, method, url))
                delay = self.retry_delay(response, attempt)
                LOG.warning("%s: HTTP %s, пауза %.1f c (попытка %s)", self.label, status, delay, attempt)
                time.sleep(delay)
                continue

            if status == 401 and not reauthorized:
                if self.refresh_auth():
                    reauthorized = True
                    continue

            if status not in expected:
                snippet = response.text[:500] if response.text else ""
                raise ApiError("{0}: HTTP {1} {2} {3} :: {4}".format(self.label, status, method, url, snippet))

            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                raise ApiError("{0}: невалидный JSON от {1}".format(self.label, url))


class PosifloraClient(HttpClient):
    def __init__(self, base_url, username, password):
        HttpClient.__init__(self, base_url, "Posiflora")
        self.username = username
        self.password = password
        self.token = ""

    def build_headers(self):
        headers = {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "User-Agent": BROWSER_UA,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def login(self):
        body = {
            "data": {
                "type": "sessions",
                "attributes": {"username": self.username, "password": self.password},
            }
        }
        self.token = ""
        response = self.request("POST", "/sessions", json_body=body, expected=(200, 201))
        token = safe_get(response, "data", "attributes", "accessToken") or safe_get(response, "data", "accessToken")
        if not token and isinstance(response, dict):
            token = response.get("accessToken")
        if not token:
            raise ApiError("Posiflora: accessToken не найден")
        self.token = str(token)
        LOG.info("Posiflora: авторизация успешна")

    def refresh_auth(self):
        try:
            self.login()
        except ApiError:
            return False
        return True

    def paginate(self, path, params=None, page_size=100, max_pages=MAX_PAGES):
        base_params = dict(params or {})
        rows, included, seen = [], [], set()
        page = 1
        while page <= max_pages:
            page_params = dict(base_params)
            page_params["page[number]"] = page
            page_params["page[size]"] = page_size
            body = self.request("GET", path, params=page_params)
            if not isinstance(body, dict):
                break
            data = body.get("data")
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list) or not data:
                break
            new_count = 0
            for row in data:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id"))
                if row_id in seen:
                    continue
                seen.add(row_id)
                rows.append(row)
                new_count += 1
            extra = body.get("included")
            if isinstance(extra, list):
                included.extend(extra)
            if len(data) < page_size or new_count == 0:
                break
            page += 1
        return rows, included

    def get_bouquet_details(self, bouquet_id):
        path = "/bouquets/" + str(bouquet_id)
        body = None
        for include in ("logo", None):
            try:
                body = self.request("GET", path, params={"include": include} if include else None)
                break
            except ApiError:
                pass
        if body is None:
            return {"price": 0.0, "image_url": ""}
        row = safe_get(body, "data", default={})
        included_index = build_included_index(safe_get(body, "included", default=[]))
        price = price_from_included(row, included_index)
        if price <= 0:
            price = pick_price(row.get("attributes"))
        return {"price": price, "image_url": logo_from_included(row, included_index)}

    def get_inventory_qty(self, item_id, store_id):
        path = "/inventory-items/{0}/stats-by-store/{1}".format(item_id, store_id)
        body = self.request("GET", path)
        value = safe_get(body, "data", "attributes", "qty") or safe_get(body, "data", "qty")
        if value is None and isinstance(body, dict):
            value = body.get("qty")
        return max(safe_float(value), 0.0) if value is not None else None


class MoySkladClient(HttpClient):
    def __init__(self, base_url, token):
        HttpClient.__init__(self, base_url, "МойСклад")
        self.token = token

    def build_headers(self):
        return {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json;charset=utf-8",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    def list_entities(self, path, extra_params=None):
        rows, offset, limit = [], 0, 1000
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params:
                params.update(extra_params)
            body = self.request("GET", path, params=params)
            batch = body.get("rows", []) if isinstance(body, dict) else []
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return rows

    def get_sale_price_type_meta(self):
        body = self.request("GET", "/context/companysettings/pricetype")
        candidates = body.get("rows", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
        fallback = None
        for item in candidates:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta")
            if not meta:
                continue
            if fallback is None:
                fallback = meta
            if normalize_name(item.get("name")) == "цена продажи":
                return meta
        return fallback

    def create_product(self, name, external_code, folder_meta, price_kopecks, price_type_meta):
        body = {
            "name": name[:PRODUCT_NAME_LIMIT],
            "externalCode": str(external_code),
            "productFolder": {"meta": folder_meta},
            "salePrices": [{"value": int(price_kopecks), "priceType": {"meta": price_type_meta}}],
        }
        return self.request("POST", "/entity/product", json_body=body)

    def update_product(self, product_id, payload):
        return self.request("PUT", "/entity/product/" + str(product_id), json_body=payload)

    def upload_image(self, product_id, filename, content_b64):
        return self.request(
            "POST",
            "/entity/product/{0}/images".format(product_id),
            json_body={"filename": filename, "content": content_b64},
        )


# -----------------------------------------------------------------------------
# КАТЕГОРИИ ПАПОК
# -----------------------------------------------------------------------------
def match_rules(text, rules):
    low = normalize_name(text)
    for category, keywords in rules:
        for keyword in keywords:
            if keyword in low:
                return category
    return None


def classify_folder(name, full_path):
    norm_name = normalize_name(name)
    norm_path = normalize_name(full_path)
    if norm_name in ("цветы лакма", "каталог", "товары"):
        return None
    if norm_name == "цветы" and "каталог" in norm_path:
        return "recipes"
    return match_rules(norm_path, FOLDER_RULES) or match_rules(norm_name, FOLDER_RULES)


def build_folder_registry(rows):
    folder_category, folder_meta, best_depth = {}, {}, {}
    for row in rows:
        folder_id = row.get("id")
        if not folder_id:
            continue
        name, path_name = row.get("name") or "", row.get("pathName") or ""
        full_path = path_name + "/" + name if path_name else name
        if is_blacklisted(full_path):
            continue
        category = classify_folder(name, full_path)
        if category is None:
            continue
        folder_category[folder_id] = category
        depth = full_path.count("/")
        if category not in best_depth or depth < best_depth[category]:
            best_depth[category] = depth
            folder_meta[category] = row.get("meta")
    return folder_category, folder_meta


# -----------------------------------------------------------------------------
# ИНДЕКС ТОВАРОВ МОЕГОСКЛАДА (ВКЛЮЧАЯ АРХИВ)
# -----------------------------------------------------------------------------
class ProductIndex:
    def __init__(self):
        self.items, self.by_code, self.by_name = [], {}, {}

    def add(self, entry):
        self.items.append(entry)
        if entry.get("code"):
            self.by_code[str(entry["code"])] = entry
        norm = normalize_name(entry.get("name"))
        if norm and norm not in self.by_name:
            self.by_name[norm] = entry

    def find(self, external_id, name):
        return self.by_code.get(str(external_id)) or self.by_name.get(normalize_name(name))


def build_product_index(rows, folder_category):
    index = ProductIndex()
    for row in rows:
        if not row.get("id") or is_blacklisted(row.get("name")):
            continue
        folder_id = last_segment(safe_get(row, "productFolder", "meta", "href"))
        sale_prices = row.get("salePrices") or []
        current_price = safe_float(sale_prices[0].get("value")) if sale_prices and isinstance(sale_prices[0], dict) else 0.0
        index.add({
            "id": row.get("id"),
            "name": row.get("name") or "",
            "code": row.get("externalCode") or "",
            "meta": row.get("meta"),
            "category": folder_category.get(folder_id),
            "archived": bool(row.get("archived")),
            "has_image": bool(row.get("images", {}).get("meta", {}).get("size", 0) or "image" in row),
            "price_kopecks": int(current_price),
        })
    return index


# -----------------------------------------------------------------------------
# СБОР ДАННЫХ ИЗ POSIFLORA (СТРОГО ВИТРИНА)
# -----------------------------------------------------------------------------
def is_valid_bouquet_name(name):
    if not name or is_blacklisted(name):
        return False
    return bool(re.search(r"букет", name, re.IGNORECASE) or name.strip().isdigit())


def fetch_valid_bouquets(posiflora):
    # Увеличиваем глубину до 20 страниц (2000 букетов), чтобы точно захватить старые букеты, стоящие на витрине
    try:
        rows, included = posiflora.paginate("/bouquets", {"include": "logo,prices", "sort": "-createdAt"}, page_size=100, max_pages=20)
    except ApiError:
        rows, included = posiflora.paginate("/bouquets", page_size=100, max_pages=20)

    included_index = build_included_index(included)
    result = []
    dropped = 0

    for row in rows:
        attrs = row.get("attributes") or {}
        
        # ГЛАВНЫЙ ФИЛЬТР ВИТРИНЫ: проверяем поле onWindowAt 
        # (если в нем есть дата, значит букет сейчас физически выставлен на витрину магазина)
        on_window_at = attrs.get("onWindowAt")
        if not on_window_at:
            dropped += 1
            continue

        status = str(attrs.get("status") or attrs.get("state") or "").strip().lower()

        # Отсекаем проданные, архивные или разобранные (даже если у них осталась дата витрины)
        if status in ("sold", "disassembled", "archived", "draft", "deleted", "canceled", "cancelled", "written_off", "write_off"):
            dropped += 1
            continue

        name = get_title(attrs)
        if re.match(r"^\d{3,4}$", name.strip()):
            name = "Букет " + name.strip()

        if not is_valid_bouquet_name(name):
            dropped += 1
            continue

        price = pick_price(attrs)
        if price <= 0:
            price = price_from_included(row, included_index)
        if price <= 0:
            dropped += 1
            continue

        image_url = logo_from_included(row, included_index)

        result.append({
            "id": str(row.get("id")),
            "name": name,
            "price": price,
            "image_url": image_url,
            "category": "showcase",
            "needs_detail": True,
            "require_price": True,
        })

    LOG.info("Posiflora: отобрано с витрины %s актуальных букетов (отсеяно %s)", len(result), dropped)
    return result


def fetch_inventory_items(posiflora):
    rows, included = posiflora.paginate("/inventory-items", {"include": "category,logo,prices"})
    included_index = build_included_index(included)
    result = []
    for row in rows:
        attrs = row.get("attributes") or {}
        name = get_title(attrs)
        if not name or is_blacklisted(name):
            continue

        category_title = ""
        for ref in as_ref_list(safe_get(row, "relationships", "category", "data")):
            category_item = resolve_ref(ref, included_index)
            if category_item is not None:
                category_title = get_title(category_item.get("attributes"))
                break
        if is_blacklisted(category_title):
            continue

        cat = match_rules(category_title, POSIFLORA_CATEGORY_RULES) or match_rules(name, POSIFLORA_CATEGORY_RULES) or "decor"
        price = price_from_included(row, included_index) or pick_price(attrs)

        result.append({
            "id": str(row.get("id")),
            "name": name,
            "price": price,
            "image_url": logo_from_included(row, included_index),
            "category": cat,
            "needs_detail": False,
            "require_price": False,
            "qty": None,
        })
    return result


# -----------------------------------------------------------------------------
# ИЗОБРАЖЕНИЯ
# -----------------------------------------------------------------------------
def download_and_prepare_image(url, posiflora_base_url):
    full_url = "https:" + url if url.startswith("//") else (url if url.startswith("http") else urljoin(posiflora_base_url.rstrip("/") + "/", url))
    headers = {"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    time.sleep(REQUEST_PAUSE)
    response = requests.get(full_url, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code != 200:
        raise ApiError("Изображение недоступно: HTTP {0}".format(response.status_code))
    image = Image.open(io.BytesIO(response.content))
    image.load()
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), getattr(Image, "Resampling", Image).LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# -----------------------------------------------------------------------------
# КОНТЕКСТ ПРИЛОЖЕНИЯ
# -----------------------------------------------------------------------------
class AppContext:
    def __init__(self):
        self.config = {}
        self.stats = {"created": 0, "updated": 0, "existing": 0, "errors": 0, "images_ok": 0, "images_failed": 0}
        self.folder_meta = {}
        self.posiflora = self.moysklad = self.index = self.price_type_meta = None
        self.dry_run = False
        self.bouquet_candidates = self.inventory_candidates = []
        self.bouquets_loaded = False
        self.active_bouquet_ids, self.active_bouquet_names = set(), set()
        self.inventory_by_code, self.inventory_by_name = {}, {}


def load_config():
    names = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_STORE_ID", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID"]
    config = {name: os.environ.get(name, "").strip() for name in names}
    if not all(config.values()):
        raise ApiError("Не заданы ключи окружения")
    config["POSIFLORA_URL"], config["MOYSKLAD_URL"] = config["POSIFLORA_URL"].rstrip("/"), config["MOYSKLAD_URL"].rstrip("/")
    return config


# -----------------------------------------------------------------------------
# САНИТАРНЫЙ БЛОК: ОЧИСТКА ПАПОК ОТ МУСОРА
# -----------------------------------------------------------------------------
def enforce_category_rules(ctx):
    """
    Принудительно очищает папки от мусора (эклеров, кофе)
    и переносит свечи в папку 'Свечи'.
    """
    candle_meta = ctx.folder_meta.get("candle")
    recipes_meta = ctx.folder_meta.get("recipes")
        
    for entry in ctx.index.items:
        name_low = normalize_name(entry.get("name", ""))
        
        # 1. Перенос свечей
        if "свеч" in name_low and entry.get("category") != "candle" and candle_meta:
            if not ctx.dry_run:
                try:
                    ctx.moysklad.update_product(entry["id"], {"productFolder": {"meta": candle_meta}})
                    entry["category"] = "candle"
                    ctx.stats["updated"] += 1
                    LOG.info("Перемещена свеча в правильную папку 'Свечи': %s", entry.get("name"))
                except Exception as exc:
                    LOG.error("Ошибка при перемещении свечи %s: %s", entry.get("name"), exc)
                    
        # 2. Архивирование мусора
        if not entry.get("archived") and is_blacklisted(name_low):
            if not ctx.dry_run:
                try:
                    ctx.moysklad.update_product(entry["id"], {"archived": True})
                    entry["archived"] = True
                    ctx.stats["updated"] += 1
                    LOG.info("Удален мусор из каталога (в архив): %s", entry.get("name"))
                except Exception as exc:
                    LOG.error("Ошибка архивации мусора %s: %s", entry.get("name"), exc)


# -----------------------------------------------------------------------------
# СОЗДАНИЕ И ОБНОВЛЕНИЕ КАРТОЧЕК
# -----------------------------------------------------------------------------
def process_candidate(ctx, candidate):
    source_id, name, category = str(candidate["id"]), candidate["name"], candidate["category"]
    price, image_url = candidate.get("price", 0.0), candidate.get("image_url") or ""

    if candidate.get("needs_detail"):
        details = ctx.posiflora.get_bouquet_details(source_id)
        if details.get("price", 0.0) > 0:
            price = details["price"]
        if details.get("image_url"):
            image_url = details["image_url"]

    if candidate.get("require_price") and price <= 0:
        return

    folder_meta = ctx.folder_meta.get(category)
    if folder_meta is None:
        return

    price_kopecks = int(round(price * 100))
    existing = ctx.index.find(source_id, name)

    if existing is not None:
        ctx.stats["existing"] += 1
        update_payload = {}
        is_reused_bouquet = (category == "showcase" and str(existing.get("code")) != source_id)

        if is_reused_bouquet:
            update_payload["externalCode"] = source_id

        if existing.get("archived"):
            update_payload["archived"] = False

        if existing.get("category") != category:
            update_payload["productFolder"] = {"meta": folder_meta}

        if existing["name"] != name:
            update_payload["name"] = name[:PRODUCT_NAME_LIMIT]

        if price_kopecks > 0 and (is_reused_bouquet or existing.get("price_kopecks") != price_kopecks):
            update_payload["salePrices"] = [{"value": price_kopecks, "priceType": {"meta": ctx.price_type_meta}}]

        if update_payload and not ctx.dry_run:
            try:
                ctx.moysklad.update_product(existing["id"], update_payload)
                existing["category"] = category
                existing["name"] = name
                existing["code"] = source_id
                existing["archived"] = False
                existing["price_kopecks"] = price_kopecks
                ctx.index.by_code[source_id] = existing
                ctx.stats["updated"] += 1
                LOG.info("Обновлен актуальный букет: %s (id=%s, %s ₽)", name, source_id, price)
            except Exception as exc:
                LOG.error("Ошибка при обновлении товара %s: %s", name, exc)

        need_photo = (not existing.get("has_image")) or is_reused_bouquet
        if need_photo and image_url and "placeholder" not in image_url.lower() and not ctx.dry_run:
            try:
                content = download_and_prepare_image(image_url, ctx.config["POSIFLORA_URL"])
                ctx.moysklad.upload_image(existing["id"], str(source_id) + ".jpg", content)
                existing["has_image"] = True
                ctx.stats["images_ok"] += 1
                LOG.info("Загружено фото для: %s", name)
            except Exception as exc:
                ctx.stats["images_failed"] += 1
        return

    if ctx.dry_run:
        return

    created = ctx.moysklad.create_product(name, source_id, folder_meta, price_kopecks, ctx.price_type_meta)
    product_id = created.get("id") if isinstance(created, dict) else None
    if not product_id:
        raise ApiError("В ответе на создание товара нет id")

    new_entry = {
        "id": product_id,
        "name": name,
        "code": source_id,
        "meta": created.get("meta"),
        "category": category,
        "archived": False,
        "has_image": False,
        "price_kopecks": price_kopecks,
    }
    ctx.index.add(new_entry)
    ctx.stats["created"] += 1
    LOG.info("Создана новая карточка витрины: %s [%s] (%s ₽)", name, category, price)

    if image_url and "placeholder" not in image_url.lower():
        try:
            content = download_and_prepare_image(image_url, ctx.config["POSIFLORA_URL"])
            ctx.moysklad.upload_image(product_id, str(source_id) + ".jpg", content)
            new_entry["has_image"] = True
            ctx.stats["images_ok"] += 1
        except Exception as exc:
            ctx.stats["images_failed"] += 1


def run_creation_phase(ctx):
    for candidate in ctx.bouquet_candidates + ctx.inventory_candidates:
        try:
            process_candidate(ctx, candidate)
        except Exception as exc:
            ctx.stats["errors"] += 1
            LOG.error("Ошибка обработки товара «%s»: %s", candidate.get("name"), exc)


# -----------------------------------------------------------------------------
# ОСТАТКИ И АРХИВАЦИЯ
# -----------------------------------------------------------------------------
def fetch_current_stock(ctx):
    store_id = ctx.config["TARGET_STORE_ID"]
    body = ctx.moysklad.request("GET", "/report/stock/bystore/current", params={"filter": "storeId=" + store_id})
    rows = body.get("rows", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
    stock = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("storeId") not in (None, store_id):
            continue
        aid = r.get("assortmentId") or last_segment(safe_get(r, "meta", "href"))
        if aid:
            stock[aid] = stock.get(aid, 0.0) + safe_float(r.get("stock"))
    return stock


def compute_target(ctx, entry):
    kind = STOCK_KIND.get(entry.get("category"))
    if kind == "showcase":
        if not ctx.bouquets_loaded:
            return None
        return 1.0 if str(entry.get("code")) in ctx.active_bouquet_ids or normalize_name(entry.get("name")) in ctx.active_bouquet_names else 0.0
    if kind == "author":
        return AUTHOR_STOCK_LIMIT

    item = (
        ctx.inventory_by_code.get(str(entry.get("code"))) or ctx.inventory_by_name.get(normalize_name(entry.get("name")))
    )

    if not item:
        return None
    if kind == "piece" and item.get("qty") is None:
        try:
            item["qty"] = ctx.posiflora.get_inventory_qty(item["id"], ctx.config["POSIFLORA_STORE_ID"])
        except Exception:
            return None
    return item.get("qty")


def build_stock_plan(ctx, current_stock):
    enter_pos, loss_pos = [], []
    for entry in ctx.index.items:
        if not entry.get("category") or not entry.get("meta"):
            continue
            
        target = 0.0 if entry.get("archived") else compute_target(ctx, entry)
        if target is None:
            continue
            
        current = current_stock.get(entry["id"], 0.0)
        diff = round(target - current, 3)
        if diff > 0:
            enter_pos.append({"assortment": {"meta": entry["meta"]}, "quantity": diff, "price": 0})
        elif diff < 0 and current > 0:
            loss_pos.append({"assortment": {"meta": entry["meta"]}, "quantity": round(min(abs(diff), current), 3), "price": 0})
    return enter_pos, loss_pos


def post_documents(ctx, endpoint, positions, label):
    if not positions:
        LOG.info("%s: изменений нет", label)
        return
    org = build_meta(ctx.config["MOYSKLAD_URL"], "organization", ctx.config["ORG_ID"])
    store = build_meta(ctx.config["MOYSKLAD_URL"], "store", ctx.config["TARGET_STORE_ID"])
    for chunk_number in range((len(positions) + DOCUMENT_CHUNK_SIZE - 1) // DOCUMENT_CHUNK_SIZE):
        chunk = positions[chunk_number * DOCUMENT_CHUNK_SIZE : (chunk_number + 1) * DOCUMENT_CHUNK_SIZE]
        if not ctx.dry_run:
            ctx.moysklad.request(
                "POST",
                endpoint,
                json_body={
                    "organization": {"meta": org},
                    "store": {"meta": store},
                    "applicable": True,
                    "description": "Синхронизация Posiflora",
                    "positions": chunk,
                },
            )
            LOG.info("%s: проведено позиций %s", label, len(chunk))


def run_stock_phase(ctx):
    e, l = build_stock_plan(ctx, fetch_current_stock(ctx))
    LOG.info("План остатков: оприходовать %s, списать %s", len(e), len(l))
    post_documents(ctx, "/entity/enter", e, "Оприходование")
    post_documents(ctx, "/entity/loss", l, "Списание")


def archive_old_showcase_bouquets(ctx):
    LOG.info("Проверка папки 'Витрина Tilda' на наличие устаревших букетов...")
    archived_count = 0
    for entry in ctx.index.items:
        if entry.get("category") == "showcase" and not entry.get("archived"):
            code = str(entry.get("code"))
            name_norm = normalize_name(entry.get("name"))
            if code not in ctx.active_bouquet_ids and name_norm not in ctx.active_bouquet_names:
                if not ctx.dry_run:
                    try:
                        ctx.moysklad.update_product(entry["id"], {"archived": True})
                        entry["archived"] = True
                        archived_count += 1
                        LOG.info("Отправлен в архив устаревший букет витрины: %s", entry.get("name"))
                    except Exception as exc:
                        LOG.error("Ошибка архивации %s: %s", entry.get("name"), exc)
    LOG.info("Всего отправлено в архив устаревших букетов витрины: %s", archived_count)


# -----------------------------------------------------------------------------
# ГЛАВНЫЙ СЦЕНАРИЙ
# -----------------------------------------------------------------------------
def load_posiflora_data(ctx):
    try:
        ctx.bouquet_candidates = fetch_valid_bouquets(ctx.posiflora)
        ctx.bouquets_loaded = True
    except ApiError as exc:
        LOG.error("Ошибка загрузки букетов: %s", exc)
    try:
        ctx.inventory_candidates = fetch_inventory_items(ctx.posiflora)
    except ApiError as exc:
        LOG.error("Ошибка загрузки инвентаря: %s", exc)

    ctx.active_bouquet_ids = {str(c["id"]) for c in ctx.bouquet_candidates}
    ctx.active_bouquet_names = {normalize_name(c["name"]) for c in ctx.bouquet_candidates}
    ctx.inventory_by_code = {str(c["id"]): c for c in ctx.inventory_candidates}
    ctx.inventory_by_name = {normalize_name(c["name"]): c for c in ctx.inventory_candidates}


def main():
    ctx = AppContext()
    ctx.config, ctx.dry_run = load_config(), env_bool("DRY_RUN")
    try:
        ctx.posiflora = PosifloraClient(ctx.config["POSIFLORA_URL"], ctx.config["POSIFLORA_USER"], ctx.config["POSIFLORA_PASS"])
        ctx.moysklad = MoySkladClient(ctx.config["MOYSKLAD_URL"], ctx.config["MOYSKLAD_TOKEN"])
        ctx.posiflora.login()
        ctx.folder_category, ctx.folder_meta = build_folder_registry(ctx.moysklad.list_entities("/entity/productfolder"))
        LOG.info("Категории папок: %s", ", ".join(sorted(ctx.folder_meta.keys())))
        ctx.price_type_meta = ctx.moysklad.get_sale_price_type_meta()
        
        ctx.index = build_product_index(ctx.moysklad.list_entities("/entity/product", extra_params={"includeArchived": "true"}), ctx.folder_category)
        
        load_posiflora_data(ctx)
        
        # 1. Возвращаем активные букеты из архива и создаем новые
        run_creation_phase(ctx)
        
        # 2. Очищаем папки от кофе, эклеров, переносим свечи
        enforce_category_rules(ctx)
        
        # 3. Выставляем актуальным букетам остаток 1, а пропавшим списываем в 0
        run_stock_phase(ctx)
        
        # 4. Архивируем те букеты витрины, которые исчезли из Posiflora
        archive_old_showcase_bouquets(ctx)
        
        LOG.info("Итоги: %s", ctx.stats)
    except ApiError as exc:
        LOG.error("Критическая ошибка: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
