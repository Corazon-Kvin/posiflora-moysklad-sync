#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт-проверка: ОСТАЛОСЬ ЛИ ЧТО-ТО В АРХИВЕ?
# Задача: Найти заархивированные товары вне папки "Цветы лакма". Только чтение!

import logging
import os
import sys
import time
import requests

REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("check_archive")

class MoySkladClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()

    def build_headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json;charset=utf-8"}

    def request(self, method, path, params=None):
        url = self.base_url + path
        attempt = 0
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                resp = self.session.request(method, url, params=params, headers=self.build_headers(), timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES: sys.exit(f"Ошибка сети: {exc}")
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < MAX_RETRIES: continue
            return resp.json() if resp.content else {}

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

def main():
    keys = ["MOYSKLAD_TOKEN", "MOYSKLAD_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(config.values()): sys.exit("Ошибка: не заданы переменные МоегоСклада")

    ms = MoySkladClient(config["MOYSKLAD_URL"], config["MOYSKLAD_TOKEN"])
    
    LOG.info("Проверяем архивные товары...")
    folders = ms.list_entities("/entity/productfolder")
    flower_hrefs = {f.get("meta", {}).get("href") for f in folders if "Цветы лакма" in f.get("pathName", "") or "Цветы лакма" in f.get("name", "")}

    products = ms.list_entities("/entity/product", {"filter": "archived=true"})
    
    left_in_archive = []
    for p in products:
        folder_href = p.get("productFolder", {}).get("meta", {}).get("href", "")
        if folder_href not in flower_hrefs:
            left_in_archive.append(p.get("name"))

    if not left_in_archive:
        LOG.info("ИДЕАЛЬНО! В архиве не осталось ни одного товара кофейни.")
    else:
        LOG.warning(f"ВНИМАНИЕ! В архиве осталось товаров кофейни: {len(left_in_archive)}")
        for name in left_in_archive:
            LOG.info(f"Остался в архиве: {name}")

if __name__ == "__main__":
    main()
