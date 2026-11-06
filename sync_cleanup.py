#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 3: НОЧНАЯ ОЧИСТКА (sync_cleanup.py)
# Задача: Удалять (архивировать) в МоемСкладе товары, которые были удалены в Posiflora.
# СТРОГОЕ ОГРАНИЧЕНИЕ: Скрипт проверяет и удаляет товары ТОЛЬКО внутри папки "Цветы лакма". Кофейный ассортимент игнорируется.
# Запуск: Раз в сутки ночью.

import logging
import os
import sys
import time

import requests

# -----------------------------------------------------------------------------
# КОНСТАНТЫ И НАСТРОЙКИ
# -----------------------------------------------------------------------------
REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60
MAX_PAGES = 1000

# Корневая папка, внутри которой разрешено удаление. Все, что вне ее - НЕ ТРОГАЕМ.
ROOT_FLOWER_FOLDER = "Цветы лакма"

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_cleanup")

class ApiError(Exception): pass

# -----------------------------------------------------------------------------
# API КЛИЕНТЫ
# -----------------------------------------------------------------------------
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
        rows, page = [], 1
        while page <= max_pages:
            p = dict(params); p["page[number]"] = page
            body = self.request("GET", path, params=p)
            data = body.get("data", [])
            if isinstance(data, dict): data = [data]
            if not data: break
            rows.extend(data)
            if len(data) < p.get("page[size]", 100): break
            page += 1
        return rows

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
def get_all_posiflora_ids(pf):
    """Собирает ВСЕ активные ID из Posiflora (инвентарь + рецепты)."""
    active_ids = set()
    
    # 1. Забираем штучный инвентарь
    inv_rows = pf.paginate("/inventory-items", {"page[size]": 100}, MAX_PAGES)
    for r in inv_rows:
        active_ids.add(str(r.get("id")))
        
    # 2. Забираем рецепты (Авторские букеты)
    try:
        rec_rows = pf.paginate("/specifications", {"page[size]": 100}, MAX_PAGES)
        for r in rec_rows:
            active_ids.add(str(r.get("id")))
    except Exception as e:
        LOG.error(f"Posiflora: Ошибка при запросе рецептов: {e}")
        
    LOG.info(f"Posiflora: Всего найдено {len(active_ids)} активных позиций (инвентарь + рецепты).")
    return active_ids

def get_flower_folder_paths(ms):
    """
    Возвращает список href всех папок, которые вложены в "Цветы лакма", 
    включая саму корневую папку "Цветы лакма".
    """
    folders = ms.list_entities("/entity/productfolder")
    
    # Находим корневую папку "Цветы лакма"
    root_folder = None
    for f in folders:
        if f.get("name") == ROOT_FLOWER_FOLDER:
            root_folder = f
            break
            
    if not root_folder:
        LOG.error(f"Не найдена корневая папка '{ROOT_FLOWER_FOLDER}' в МоемСкладе!")
        return set()
        
    root_path = root_folder.get("pathName", "")
    full_root_path = f"{root_path}/{ROOT_FLOWER_FOLDER}" if root_path else ROOT_FLOWER_FOLDER
    
    # Собираем href всех папок, чей путь начинается с "Цветы лакма"
    allowed_folder_hrefs = {root_folder.get("meta", {}).get("href")}
    
    for f in folders:
        path = f.get("pathName", "")
        if path.startswith(full_root_path) or path == ROOT_FLOWER_FOLDER:
            allowed_folder_hrefs.add(f.get("meta", {}).get("href"))
            
    return allowed_folder_hrefs

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    
    missing = [k for k, v in config.items() if not v]
    if missing: 
        sys.exit(f"Ошибка: не заданы переменные: {', '.join(missing)}")
        
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "y")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()

        # 1. Получаем все актуальные ID из Posiflora (чтобы знать, что НЕ удалять)
        pf_active_ids = get_all_posiflora_ids(pf)

        # 2. Определяем границы дозволенного в МоемСкладе
        allowed_folder_hrefs = get_flower_folder_paths(ms)
        if not allowed_folder_hrefs:
            sys.exit("Очистка отменена: не удалось определить целевые папки цветов.")

        # 3. Получаем все неархивированные товары из МоегоСклада
        ms_products = ms.list_entities("/entity/product", {"includeArchived": "false"})
        
        archived_count = 0
        skipped_coffee_count = 0
        
        # 4. Сканируем и архивируем
        for p in ms_products:
            ms_id = p.get("id")
            ext_code = str(p.get("externalCode", ""))
            
            # Проверяем, в какой папке лежит товар
            folder_href = p.get("productFolder", {}).get("meta", {}).get("href")
            
            # Если товар лежит ВНЕ папки "Цветы лакма" (например, в "Сырьё" или "Товары") - строго игнорируем!
            if folder_href not in allowed_folder_hrefs:
                skipped_coffee_count += 1
                continue
                
            # Если товар лежит в "Цветах", но его больше нет в Posiflora - архивируем
            if ext_code and ext_code not in pf_active_ids:
                if not dry_run:
                    ms.request("PUT", f"/entity/product/{ms_id}", json_body={"archived": True})
                archived_count += 1
                LOG.info(f"В архив отправлен: {p.get('name')} (отсутствует в Posiflora)")

        LOG.info(f"Очистка завершена. Отправлено в архив: {archived_count} шт. Проигнорировано товаров вне цветочных папок (кофейня и др.): {skipped_coffee_count} шт.")

    except Exception as e:
        LOG.error(f"Критическая ошибка очистки: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
