#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Односторонняя синхронизация каталога и остатков: Posiflora -> МойСклад.
# Запуск: python sync_full_catalog.py (GitHub Actions, cron каждые 10-15 минут).
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

# Порядок важен: первое совпадение побеждает.
FOLDER_RULES = [
    ("showcase", ["витрина"]),
    ("author", ["авторские"]),
    ("cut", ["цветы"]),
    ("balloon", ["шар"]),
    ("decor", ["декор"]),
    ("vase", ["ваз"]),
    ("candle", ["свеч"]),
    ("packaging", ["упаковк"]),
]

# Тип логики остатков по категории папки.
STOCK_KIND = {
    "showcase": "showcase",
    "author": "author",
    "cut": "cut",
    "balloon": "piece",
    "decor": "piece",
    "vase": "piece",
    "candle": "piece",
    "packaging": "piece",
}

# Ключевые слова категории Posiflora -> категория папки МоегоСклада.
INVENTORY_CATEGORY_RULES = [
    ("balloon", ["шар"]),
    ("vase", ["ваз"]),
    ("candle", ["свеч"]),
    ("packaging", ["упаковк"]),
    ("decor", ["декор"]),
]
INVENTORY_DEFAULT_CATEGORY = "decor"

PRICE_KEYS = ("price", "salePrice", "retailPrice", "sellingPrice", "value", "amount")
IMAGE_URL_KEYS = ("url", "originalUrl", "original", "fullUrl", "link", "src", "path", "thumbnailUrl")
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
    if value is None:
        return default
    if isinstance(value, bool):
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
    for key in ("title", "name"):
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
        result = []
        for item in value:
            if isinstance(item, dict):
                result.append(item)
        return result
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
    refs = as_ref_list(safe_get(row, "relationships", "logo", "data"))
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
        if path.startswith("http"):
            url = path
        else:
            url = self.base_url + path
        attempt = 0
        reauthorized = False
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self.build_headers(),
                    timeout=HTTP_TIMEOUT,
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
                reauthorized = self.refresh_auth()
                if reauthorized:
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
                "attributes": {
                    "username": self.username,
                    "password": self.password,
                },
            }
        }
        self.token = ""
        response = self.request("POST", "/sessions", json_body=body, expected=(200, 201))
        token = safe_get(response, "data", "attributes", "accessToken")
        if not token:
            token = safe_get(response, "data", "accessToken")
        if not token and isinstance(response, dict):
            token = response.get("accessToken")
        if not token:
            raise ApiError("Posiflora: accessToken не найден в ответе /sessions")
        self.token = str(token)
        LOG.info("Posiflora: авторизация успешна")

    def refresh_auth(self):
        LOG.warning("Posiflora: 401, повторная авторизация")
        try:
            self.login()
        except ApiError as exc:
            LOG.error("Posiflora: не удалось переавторизоваться: %s", exc)
            return False
        return True

    def paginate(self, path, params=None, page_size=100):
        base_params = dict(params or {})
        rows = []
        included = []
        seen = set()
        page = 1
        while page <= MAX_PAGES:
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
            if include:
                params = {"include": include}
            else:
                params = None
            try:
                body = self.request("GET", path, params=params)
                break
            except ApiError as exc:
                LOG.warning(
                    "Posiflora: детали букета %s (include=%s) недоступны: %s",
                    bouquet_id, include, exc,
                )

        if body is None:
            return {"price": 0.0, "image_url": ""}

        row = safe_get(body, "data", default={})
        if not isinstance(row, dict):
            row = {}
        included_index = build_included_index(safe_get(body, "included", default=[]))
        price = price_from_included(row, included_index)
        if price <= 0:
            price = pick_price(row.get("attributes"))
        return {
            "price": price,
            "image_url": logo_from_included(row, included_index),
        }

    def get_inventory_qty(self, item_id, store_id):
        path = "/inventory-items/{0}/stats-by-store/{1}".format(item_id, store_id)
        body = self.request("GET", path)
        value = safe_get(body, "data", "attributes", "qty")
        if value is None:
            value = safe_get(body, "data", "qty")
        if value is None and isinstance(body, dict):
            value = body.get("qty")
        if value is None:
            return None
        return max(safe_float(value), 0.0)


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
        rows = []
        offset = 0
        limit = 1000
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params:
                params.update(extra_params)
            body = self.request("GET", path, params=params)
            batch = []
            if isinstance(body, dict):
                batch = body.get("rows") or []
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return rows

    def get_sale_price_type_meta(self):
        body = self.request("GET", "/context/companysettings/pricetype")
        if isinstance(body, dict):
            candidates = body.get("rows") or []
        elif isinstance(body, list):
            candidates = body
        else:
            candidates = []
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
        if fallback is None:
            raise ApiError("МойСклад: не удалось получить meta типа цены")
        LOG.warning("МойСклад: тип цены «Цена продажи» не найден, использован первый доступный")
        return fallback

    def create_product(self, name, external_code, folder_meta, price_kopecks, price_type_meta):
        body = {
            "name": name[:PRODUCT_NAME_LIMIT],
            "externalCode": str(external_code),
            "productFolder": {"meta": folder_meta},
            "salePrices": [
                {
                    "value": int(price_kopecks),
                    "priceType": {"meta": price_type_meta},
                }
            ],
        }
        return self.request("POST", "/entity/product", json_body=body)

    def upload_image(self, product_id, filename, content_b64):
        body = {"filename": filename, "content": content_b64}
        return self.request("POST", "/entity/product/{0}/images".format(product_id), json_body=body)


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
    category = match_rules(name, FOLDER_RULES)
    if category is None:
        category = match_rules(full_path, FOLDER_RULES)
    return category


def build_folder_registry(rows):
    folder_category = {}
    folder_meta = {}
    best_depth = {}
    for row in rows:
        folder_id = row.get("id")
        if not folder_id:
            continue
        if row.get("archived"):
            continue
        name = row.get("name") or ""
        path_name = row.get("pathName") or ""
        if path_name:
            full_path = path_name + "/" + name
        else:
            full_path = name
        if is_blacklisted(full_path):
            continue
        category = classify_folder(name, full_path)
        if category is None:
            continue
        folder_category[folder_id] = category
        depth = full_path.count("/")
        current_best = best_depth.get(category)
        if current_best is None or depth < current_best:
            best_depth[category] = depth
            folder_meta[category] = row.get("meta")
    return folder_category, folder_meta


# -----------------------------------------------------------------------------
# ИНДЕКС ТОВАРОВ МОЕГОСКЛАДА
# -----------------------------------------------------------------------------
class ProductIndex:
    def __init__(self):
        self.items = []
        self.by_code = {}
        self.by_name = {}

    def add(self, entry):
        self.items.append(entry)
        code = entry.get("code")
        if code:
            self.by_code[str(code)] = entry
        norm = normalize_name(entry.get("name"))
        if norm and norm not in self.by_name:
            self.by_name[norm] = entry

    def find(self, external_id, name):
        entry = self.by_code.get(str(external_id))
        if entry is not None:
            return entry
        return self.by_name.get(normalize_name(name))


def build_product_index(rows, folder_category):
    index = ProductIndex()
    skipped_blacklist = 0
    for row in rows:
        product_id = row.get("id")
        name = row.get("name") or ""
        if not product_id:
            continue
        if is_blacklisted(name):
            skipped_blacklist += 1
            continue
        folder_id = last_segment(safe_get(row, "productFolder", "meta", "href"))
        entry = {
            "id": product_id,
            "name": name,
            "code": row.get("externalCode") or "",
            "meta": row.get("meta"),
            "category": folder_category.get(folder_id),
            "archived": bool(row.get("archived")),
        }
        index.add(entry)
    LOG.info("МойСклад: товаров в индексе %s, пропущено по blacklist %s", len(index.items), skipped_blacklist)
    return index


# -----------------------------------------------------------------------------
# СБОР ДАННЫХ ИЗ POSIFLORA
# -----------------------------------------------------------------------------
def is_valid_bouquet(name, price):
    if not name:
        return False
    if price <= 0:
        return False
    
    # ЖЕСТКИЙ БЕЛЫЙ СПИСОК ВИТРИНЫ:
    # Разрешаем создавать букеты ТОЛЬКО со строгим названием формата "Букет 0603" 
    # (слово Букет + от 3 до 4 цифр). Любой другой текст флористов блокируется.
    if not re.match(r'^букет\s*\d{3,4}$', name.strip(), re.IGNORECASE):
        return False
        
    if is_blacklisted(name):
        return False
        
    return True


def fetch_valid_bouquets(posiflora):
    rows, included = posiflora.paginate("/bouquets")
    included_index = build_included_index(included)
    result = []
    dropped = 0
    for row in rows:
        attrs = row.get("attributes") or {}
        
        # --- ФИЛЬТР АКТУАЛЬНОСТИ (ТОЛЬКО В НАЛИЧИИ) ---
        # Ищем статус букета (в Posiflora это поле status или state)
        status = str(attrs.get("status") or attrs.get("state") or "").strip().lower()
        
        # Блокируем букеты, которые были проданы, разобраны или перенесены в архив
        if status in ("sold", "disassembled", "archived", "draft", "deleted"):
            dropped += 1
            continue
            
        # Если статус указан, пропускаем только те, что официально на витрине
        if status and status not in ("showcase", "active", "published"):
            dropped += 1
            continue
        # ----------------------------------------------
        
        name = get_title(attrs)
        price = pick_price(attrs)
        if price <= 0:
            price = price_from_included(row, included_index)
            
        if not is_valid_bouquet(name, price):
            dropped += 1
            continue
            
        result.append({
            "id": str(row.get("id")),
            "name": name,
            "price": price,
            "image_url": logo_from_included(row, included_index),
            "needs_detail": True,
            "require_price": True,
        })
        
    LOG.info("Posiflora: витринных букетов %s (отфильтровано %s старых и мусорных)", len(result), dropped)
    return result


def fetch_cut_flowers(posiflora):
    rows, included = posiflora.paginate("/catalog", {"include": "balances"})
    included_index = build_included_index(included)
    result = []
    for row in rows:
        attrs = row.get("attributes") or {}
        name = get_title(attrs)
        if not name or is_blacklisted(name):
            continue
        total = 0.0
        refs = as_ref_list(safe_get(row, "relationships", "balances", "data"))
        for ref in refs:
            balance = resolve_ref(ref, included_index)
            if balance is None:
                continue
            balance_attrs = balance.get("attributes") or {}
            qty_on_window = safe_float(balance_attrs.get("qtyOnWindow"))
            qty = safe_float(balance_attrs.get("qty"))
            total += max(qty_on_window, qty)
        result.append({
            "id": str(row.get("id")),
            "name": name,
            "price": pick_price(attrs),
            "image_url": logo_from_included(row, included_index),
            "needs_detail": False,
            "require_price": False,
            "qty": max(total, 0.0),
        })
    LOG.info("Posiflora: позиций срезки %s", len(result))
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
        category_refs = as_ref_list(safe_get(row, "relationships", "category", "data"))
        for ref in category_refs:
            category_item = resolve_ref(ref, included_index)
            if category_item is not None:
                category_title = get_title(category_item.get("attributes"))
                break
        if is_blacklisted(category_title):
            continue
        price = price_from_included(row, included_index)
        if price <= 0:
            price = pick_price(attrs)
        result.append({
            "id": str(row.get("id")),
            "name": name,
            "price": price,
            "image_url": logo_from_included(row, included_index),
            "category_title": category_title,
            "needs_detail": False,
            "require_price": False,
            "qty": None,
        })
    LOG.info("Posiflora: сопутствующих товаров %s", len(result))
    return result


def inventory_category(item):
    text = item.get("category_title") or ""
    category = match_rules(text, INVENTORY_CATEGORY_RULES)
    if category is None:
        category = match_rules(item.get("name"), INVENTORY_CATEGORY_RULES)
    if category is None:
        category = INVENTORY_DEFAULT_CATEGORY
    return category


# -----------------------------------------------------------------------------
# ИЗОБРАЖЕНИЯ
# -----------------------------------------------------------------------------
def download_and_prepare_image(url, posiflora_base_url):
    full_url = url
    if full_url.startswith("//"):
        full_url = "https:" + full_url
    elif not full_url.startswith("http"):
        full_url = urljoin(posiflora_base_url.rstrip("/") + "/", full_url)

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    time.sleep(REQUEST_PAUSE)
    response = requests.get(full_url, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code != 200:
        raise ApiError("Изображение недоступно: HTTP {0} {1}".format(response.status_code, full_url))

    image = Image.open(io.BytesIO(response.content))
    image.load()

    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    else:
        image = image.convert("RGB")

    resampling = getattr(Image, "Resampling", Image)
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), resampling.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# -----------------------------------------------------------------------------
# КОНТЕКСТ ПРИЛОЖЕНИЯ
# -----------------------------------------------------------------------------
class AppContext:
    def __init__(self):
        self.config = {}
        self.posiflora = None
        self.moysklad = None
        self.index = None
        self.folder_meta = {}
        self.folder_category = {}
        self.price_type_meta = None
        self.dry_run = False

        self.bouquet_candidates = []
        self.cut_candidates = []
        self.inventory_candidates = []

        self.bouquets_loaded = False
        self.cut_loaded = False
        self.inventory_loaded = False

        self.active_bouquet_ids = set()
        self.active_bouquet_names = set()
        self.cut_by_code = {}
        self.cut_by_name = {}
        self.inventory_by_code = {}
        self.inventory_by_name = {}

        self.stats = {
            "created": 0,
            "existing": 0,
            "skipped_no_folder": 0,
            "skipped_no_price": 0,
            "images_ok": 0,
            "images_failed": 0,
            "enter_positions": 0,
            "loss_positions": 0,
            "errors": 0,
        }


def load_config():
    names = [
        "POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_STORE_ID", "POSIFLORA_URL",
        "MOYSKLAD_TOKEN", "MOYSKLAD_URL", "TARGET_STORE_ID", "ORG_ID",
    ]
    config = {}
    missing = []
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value:
            missing.append(name)
        config[name] = value
    if missing:
        LOG.error("Не заданы переменные окружения: %s", ", ".join(missing))
        sys.exit(2)
    config["POSIFLORA_URL"] = config["POSIFLORA_URL"].rstrip("/")
    config["MOYSKLAD_URL"] = config["MOYSKLAD_URL"].rstrip("/")
    return config


# -----------------------------------------------------------------------------
# СОЗДАНИЕ КАРТОЧЕК
# -----------------------------------------------------------------------------
def process_candidate(ctx, candidate, category):
    source_id = candidate["id"]
    name = candidate["name"]

    existing = ctx.index.find(source_id, name)
    if existing is not None:
        ctx.stats["existing"] += 1
        return

    folder_meta = ctx.folder_meta.get(category)
    if folder_meta is None:
        ctx.stats["skipped_no_folder"] += 1
        LOG.warning("Нет папки категории «%s» в МоемСкладе, пропуск: %s", category, name)
        return

    price = candidate.get("price", 0.0)
    image_url = candidate.get("image_url") or ""

    if candidate.get("needs_detail"):
        details = ctx.posiflora.get_bouquet_details(source_id)
        detail_price = details.get("price", 0.0)
        if detail_price > 0:
            price = detail_price
        detail_image = details.get("image_url")
        if detail_image:
            image_url = detail_image

    if candidate.get("require_price") and price <= 0:
        ctx.stats["skipped_no_price"] += 1
        LOG.info("Нет актуальной цены, пропуск: %s", name)
        return

    if ctx.dry_run:
        LOG.info("[DRY_RUN] Создана бы карточка: %s (%s) -> %s", name, source_id, category)
        return

    price_kopecks = int(round(price * 100))
    created = ctx.moysklad.create_product(name, source_id, folder_meta, price_kopecks, ctx.price_type_meta)
    product_id = created.get("id") if isinstance(created, dict) else None
    if not product_id:
        raise ApiError("МойСклад: в ответе на создание товара нет id ({0})".format(name))

    ctx.index.add({
        "id": product_id,
        "name": name,
        "code": str(source_id),
        "meta": created.get("meta"),
        "category": category,
        "archived": False,
    })
    ctx.stats["created"] += 1
    LOG.info("Создана карточка: %s [%s]", name, category)

    if image_url:
        try:
            content = download_and_prepare_image(image_url, ctx.config["POSIFLORA_URL"])
            ctx.moysklad.upload_image(product_id, str(product_id) + ".jpg", content)
            ctx.stats["images_ok"] += 1
        except Exception as exc:
            ctx.stats["images_failed"] += 1
            LOG.warning("Не удалось загрузить изображение для «%s»: %s", name, exc)


def run_creation_phase(ctx):
    for candidate in ctx.bouquet_candidates:
        try:
            process_candidate(ctx, candidate, "showcase")
        except Exception as exc:
            ctx.stats["errors"] += 1
            LOG.error("Ошибка создания букета «%s»: %s", candidate.get("name"), exc)

    for candidate in ctx.cut_candidates:
        try:
            process_candidate(ctx, candidate, "cut")
        except Exception as exc:
            ctx.stats["errors"] += 1
            LOG.error("Ошибка создания срезки «%s»: %s", candidate.get("name"), exc)

    for candidate in ctx.inventory_candidates:
        try:
            process_candidate(ctx, candidate, inventory_category(candidate))
        except Exception as exc:
            ctx.stats["errors"] += 1
            LOG.error("Ошибка создания товара «%s»: %s", candidate.get("name"), exc)


# -----------------------------------------------------------------------------
# ОСТАТКИ
# -----------------------------------------------------------------------------
def fetch_current_stock(ctx):
    store_id = ctx.config["TARGET_STORE_ID"]
    body = ctx.moysklad.request(
        "GET",
        "/report/stock/bystore/current",
        params={"filter": "storeId=" + store_id},
    )
    if isinstance(body, dict):
        rows = body.get("rows") or []
    elif isinstance(body, list):
        rows = body
    else:
        rows = []

    stock = {}
    parsed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_store = row.get("storeId")
        if row_store and row_store != store_id:
            continue
        assortment_id = row.get("assortmentId")
        if not assortment_id:
            assortment_id = last_segment(safe_get(row, "meta", "href"))
        if not assortment_id:
            continue
        parsed += 1
        stock[assortment_id] = stock.get(assortment_id, 0.0) + safe_float(row.get("stock"))

    if rows and parsed == 0:
        raise ApiError("МойСклад: не удалось разобрать отчёт об остатках (неожиданный формат)")
    LOG.info("МойСклад: позиций с остатком на складе %s", len(stock))
    return stock


def lookup_item(by_code, by_name, entry):
    item = by_code.get(str(entry.get("code")))
    if item is not None:
        return item
    return by_name.get(normalize_name(entry.get("name")))


def compute_target(ctx, entry):
    category = entry.get("category")
    kind = STOCK_KIND.get(category)

    if kind == "showcase":
        if not ctx.bouquets_loaded:
            return None
        if str(entry.get("code")) in ctx.active_bouquet_ids:
            return 1.0
        if normalize_name(entry.get("name")) in ctx.active_bouquet_names:
            return 1.0
        return 0.0

    if kind == "author":
        return AUTHOR_STOCK_LIMIT

    if kind == "cut":
        if not ctx.cut_loaded:
            return None
        item = lookup_item(ctx.cut_by_code, ctx.cut_by_name, entry)
        if item is None:
            return None
        return item.get("qty")

    if kind == "piece":
        if not ctx.inventory_loaded:
            return None
        item = lookup_item(ctx.inventory_by_code, ctx.inventory_by_name, entry)
        if item is None:
            return None
        if item.get("qty") is None:
            try:
                item["qty"] = ctx.posiflora.get_inventory_qty(item["id"], ctx.config["POSIFLORA_STORE_ID"])
            except Exception as exc:
                ctx.stats["errors"] += 1
                LOG.error("Ошибка получения остатка Posiflora для «%s»: %s", entry.get("name"), exc)
                return None
        return item.get("qty")

    return None


def build_stock_plan(ctx, current_stock):
    enter_positions = []
    loss_positions = []
    for entry in ctx.index.items:
        if entry.get("category") is None:
            continue
        if entry.get("archived"):
            continue
        meta = entry.get("meta")
        if not meta:
            continue

        target = compute_target(ctx, entry)
        if target is None:
            continue

        current = current_stock.get(entry["id"], 0.0)
        diff = round(target - current, 3)
        if diff > 0:
            enter_positions.append({
                "assortment": {"meta": meta},
                "quantity": diff,
                "price": 0,
            })
        elif diff < 0 and current > 0:
            quantity = round(min(abs(diff), current), 3)
            if quantity > 0:
                loss_positions.append({
                    "assortment": {"meta": meta},
                    "quantity": quantity,
                    "price": 0,
                })
    return enter_positions, loss_positions


def post_documents(ctx, endpoint, positions, label):
    if not positions:
        LOG.info("%s: изменений нет", label)
        return
    base = ctx.config["MOYSKLAD_URL"]
    organization_meta = build_meta(base, "organization", ctx.config["ORG_ID"])
    store_meta = build_meta(base, "store", ctx.config["TARGET_STORE_ID"])

    total_chunks = (len(positions) + DOCUMENT_CHUNK_SIZE - 1) // DOCUMENT_CHUNK_SIZE
    for chunk_number in range(total_chunks):
        start = chunk_number * DOCUMENT_CHUNK_SIZE
        chunk = positions[start:start + DOCUMENT_CHUNK_SIZE]
        body = {
            "organization": {"meta": organization_meta},
            "store": {"meta": store_meta},
            "applicable": True,
            "description": "Автосинхронизация остатков с Posiflora",
            "positions": chunk,
        }
        if ctx.dry_run:
            LOG.info("[DRY_RUN] %s: документ %s/%s, позиций %s", label, chunk_number + 1, total_chunks, len(chunk))
            continue
        try:
            ctx.moysklad.request("POST", endpoint, json_body=body)
            LOG.info("%s: документ %s/%s отправлен, позиций %s", label, chunk_number + 1, total_chunks, len(chunk))
        except ApiError as exc:
            ctx.stats["errors"] += 1
            LOG.error("%s: ошибка отправки документа %s/%s: %s", label, chunk_number + 1, total_chunks, exc)


def run_stock_phase(ctx):
    current_stock = fetch_current_stock(ctx)
    enter_positions, loss_positions = build_stock_plan(ctx, current_stock)
    ctx.stats["enter_positions"] = len(enter_positions)
    ctx.stats["loss_positions"] = len(loss_positions)
    LOG.info("План остатков: оприходовать %s, списать %s", len(enter_positions), len(loss_positions))
    post_documents(ctx, "/entity/enter", enter_positions, "Оприходование")
    post_documents(ctx, "/entity/loss", loss_positions, "Списание")


# -----------------------------------------------------------------------------
# ГЛАВНЫЙ СЦЕНАРИЙ
# -----------------------------------------------------------------------------
def load_posiflora_data(ctx):
    ctx.bouquet_candidates = []
    ctx.cut_candidates = []
    ctx.inventory_candidates = []

    try:
        ctx.bouquet_candidates = fetch_valid_bouquets(ctx.posiflora)
        ctx.bouquets_loaded = True
    except ApiError as exc:
        ctx.stats["errors"] += 1
        LOG.error("Не удалось загрузить букеты, остатки витрины не будут изменены: %s", exc)

    try:
        ctx.cut_candidates = fetch_cut_flowers(ctx.posiflora)
        ctx.cut_loaded = True
    except ApiError as exc:
        ctx.stats["errors"] += 1
        LOG.error("Не удалось загрузить срезку, её остатки не будут изменены: %s", exc)

    try:
        ctx.inventory_candidates = fetch_inventory_items(ctx.posiflora)
        ctx.inventory_loaded = True
    except ApiError as exc:
        ctx.stats["errors"] += 1
        LOG.error("Не удалось загрузить сопутствующие товары, их остатки не будут изменены: %s", exc)

    for candidate in ctx.bouquet_candidates:
        ctx.active_bouquet_ids.add(candidate["id"])
        ctx.active_bouquet_names.add(normalize_name(candidate["name"]))

    for candidate in ctx.cut_candidates:
        ctx.cut_by_code[candidate["id"]] = candidate
        norm = normalize_name(candidate["name"])
        if norm not in ctx.cut_by_name:
            ctx.cut_by_name[norm] = candidate

    for candidate in ctx.inventory_candidates:
        ctx.inventory_by_code[candidate["id"]] = candidate
        norm = normalize_name(candidate["name"])
        if norm not in ctx.inventory_by_name:
            ctx.inventory_by_name[norm] = candidate


def main():
    started = time.time()
    ctx = AppContext()
    ctx.config = load_config()
    ctx.dry_run = env_bool("DRY_RUN")
    if ctx.dry_run:
        LOG.warning("Включён DRY_RUN: записи в МойСклад выполняться не будут")

    try:
        ctx.posiflora = PosifloraClient(
            ctx.config["POSIFLORA_URL"],
            ctx.config["POSIFLORA_USER"],
            ctx.config["POSIFLORA_PASS"],
        )
        ctx.moysklad = MoySkladClient(ctx.config["MOYSKLAD_URL"], ctx.config["MOYSKLAD_TOKEN"])

        ctx.posiflora.login()

        # 4.1. Динамическое сопоставление папок
        folder_rows = ctx.moysklad.list_entities("/entity/productfolder")
        ctx.folder_category, ctx.folder_meta = build_folder_registry(folder_rows)
        LOG.info("Сопоставлены категории папок: %s", ", ".join(sorted(ctx.folder_meta.keys())) or "нет")
        if not ctx.folder_category:
            raise ApiError("МойСклад: не найдено ни одной подходящей папки товаров")

        # 4.2. Метаданные типа цены
        ctx.price_type_meta = ctx.moysklad.get_sale_price_type_meta()

        # Индекс существующих товаров МоегоСклада
        product_rows = ctx.moysklad.list_entities("/entity/product")
        ctx.index = build_product_index(product_rows, ctx.folder_category)

        # 4.3. Сбор ассортимента Posiflora
        load_posiflora_data(ctx)

        # 4.4. Создание новых карточек
        run_creation_phase(ctx)

        # 5. Синхронизация остатков
        run_stock_phase(ctx)

    except ApiError as exc:
        LOG.error("Критическая ошибка: %s", exc)
        sys.exit(1)

    elapsed = time.time() - started
    LOG.info("Итоги: %s", ", ".join("{0}={1}".format(key, value) for key, value in ctx.stats.items()))
    LOG.info("Синхронизация завершена за %.1f c", elapsed)

    if ctx.stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
