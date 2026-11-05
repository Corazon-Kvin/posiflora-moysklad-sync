#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 3: ГЕНЕРАЛЬНАЯ УБОРКА (sync_cleanup.py)
# Задача: Ночная архивация товаров в МоемСкладе, которые были удалены в Posiflora.
# Запуск: Раз в сутки ночью (0 3 * * *).

import logging
import os
import sys
import time
import requests

# -----------------------------------------------------------------------------
# КОНСТАНТЫ
# -----------------------------------------------------------------------------
REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60
MAX_PAGES = 1000

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_cleanup")

class ApiError(Exception): pass

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
        rows = []
        page = 1
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

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL", "MOYSKLAD_TOKEN", "MOYSKLAD_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    
    missing = [k for k, v in config.items() if not v]
    if missing: 
        sys.exit(f"Ошибка: не заданы переменные окружения: {', '.join(missing)}")
        
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()

        # 1. Загружаем все товары МоегоСклада
        LOG.info("Загрузка активных товаров из МоегоСклада...")
        ms_products = ms.list_entities("/entity/product", {"includeArchived": "false"})
        
        # Оставляем только те, у которых есть externalCode (чтобы не трогать ручные товары)
        ms_active = {str(p["externalCode"]): p["id"] for p in ms_products if p.get("externalCode")}
        LOG.info(f"Найдено {len(ms_active)} синхронизированных товаров в МоемСкладе.")

        # 2. Загружаем все ID из Posiflora
        LOG.info("Загрузка букетов из Posiflora...")
        pf_bouquets = pf.paginate("/bouquets", {"page[size]": 100}, MAX_PAGES)
        
        LOG.info("Загрузка инвентаря из Posiflora...")
        pf_inventory = pf.paginate("/inventory-items", {"page[size]": 100}, MAX_PAGES)

        pf_active_ids = {str(item.get("id")) for item in pf_bouquets + pf_inventory if item.get("id")}
        LOG.info(f"Posiflora: всего найдено {len(pf_active_ids)} сущностей.")

        # 3. Ищем удаленные товары
        orphans = []
        for ext_code, ms_id in ms_active.items():
            if ext_code not in pf_active_ids:
                orphans.append(ms_id)

        LOG.info(f"Найдено {len(orphans)} удаленных товаров, требующих архивации.")

        # 4. Архивируем
        archived_count = 0
        for ms_id in orphans:
            if not dry_run:
                ms.request("PUT", f"/entity/product/{ms_id}", json_body={"archived": True})
            archived_count += 1
            if archived_count % 50 == 0:
                LOG.info(f"Архивировано {archived_count} из {len(orphans)}...")

        LOG.info(f"Уборка успешно завершена. Отправлено в архив: {archived_count}")

    except Exception as e:
        LOG.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
