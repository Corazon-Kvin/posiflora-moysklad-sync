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

BOUQUET_SYSTEM_NUMBER = re.compile(r"букет\s*\d{5,}", re.IGNORECASE)
BOUQUET_TECH_TEMPLATE = re.compile(r"^\d{4}\s*\(\d+\)$")

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
