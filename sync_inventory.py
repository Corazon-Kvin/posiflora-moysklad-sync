#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт-спасатель: ВОССТАНОВЛЕНИЕ ТОВАРОВ КОФЕЙНИ
# Задача: Найти все заархивированные товары вне папки "Цветы лакма" и снять с них архив.

import logging
import os
import sys
import time
import requests

REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("restore_coffee")

class ApiError(Exception): pass

class MoySkladClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()

    def build_headers(self):
        return {
            "Authorization": f"Bearer {self.token}", 
            "Accept": "application/json;charset=utf-8", 
            "Content-Type": "application/json"
        }

    def request(self, method, path, params=None, json_body=None):
        url = self.base_url + path
        attempt = 0
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=self.build_headers(), timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES: raise ApiError(f"Ошибка сети: {exc}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code in (429, 502, 503, 504):
                if attempt >= MAX_RETRIES: raise ApiError(f"HTTP {resp.status_code}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code not in (200, 201):
                raise ApiError(f"HTTP {resp.status_code} :: {resp.text[:200]}")
            return resp.json() if resp.content else {}

    def list_entities(self, path, extra_params=None):
        offset, limit = 0, 100
        out = []
        while True:
            params = {"limit": limit, "offset": offset}
            if extra_params: params.update(extra_params)
            body = self.request("GET", path, params=params)
            batch = body.get("rows", []) if isinstance(body, dict) else body
            out.extend(batch)
            if len(batch) < limit: break
            offset += limit
        return out

def env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")

def main():
    keys = ["MOYSKLAD_TOKEN", "MOYSKLAD_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(config.values()): sys.exit("Ошибка: не заданы переменные МоегоСклада")
    
    dry_run = env_bool("DRY_RUN")

    try:
        ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])

        LOG.info("Собираем структуру папок...")
        folders = ms.list_entities("/entity/productfolder")
        flower_hrefs = set()
        
        # Находим все папки, которые относятся к цветам
        for f in folders:
            path_name = f.get("pathName", "")
            name = f.get("name", "")
            if "Цветы лакма" in path_name or "Цветы лакма" in name:
                flower_hrefs.add(f.get("meta", {}).get("href"))

        LOG.info("Запрашиваем базу товаров (включая архивные)...")
        # Запрашиваем ВСЕ товары, включая те, что в архиве
        products = ms.list_entities("/entity/product", {"includeArchived": "true"})
        
        restored_count = 0

        for p in products:
            is_archived = p.get("archived", False)
            if is_archived:
                folder_href = p.get("productFolder", {}).get("meta", {}).get("href", "")
                
                # Если товар заархивирован и НЕ принадлежит к папкам цветов — восстанавливаем
                if folder_href not in flower_hrefs:
                    if not dry_run:
                        ms.request("PUT", f"/entity/product/{p['id']}", json_body={"archived": False})
                    restored_count += 1
                    LOG.info(f"Восстановлен товар: {p.get('name')}")

        LOG.info(f"Готово! Успешно восстановлено товаров кофейни: {restored_count}")

    except Exception as e:
        LOG.error(f"Ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
