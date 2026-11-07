#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 1: ДИАГНОСТИКА ЖИВОЙ ВИТРИНЫ (sync_showcase.py)
# Задача: Найти живые букеты (0601, 0605 и др.) и распечатать их скрытые параметры.
# БЕЗОПАСНЫЙ РЕЖИМ: В МойСклад ничего не отправляется.

import logging
import os
import re
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
LOG = logging.getLogger("sync_showcase_diagnostic")

class ApiError(Exception): pass

# -----------------------------------------------------------------------------
# УТИЛИТЫ И API КЛИЕНТЫ
# -----------------------------------------------------------------------------
class PosifloraClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = ""
        self.session = requests.Session()

    def request(self, method, path, params=None, json_body=None, expected=(200, 201)):
        url = self.base_url + path
        attempt = 0
        h = {"Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json", "User-Agent": BROWSER_UA}
        if self.token: h["Authorization"] = f"Bearer {self.token}"
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=h, timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES: raise ApiError(f"Ошибка сети: {exc}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code in (429, 502, 503, 504):
                if attempt >= MAX_RETRIES: raise ApiError(f"HTTP {resp.status_code}")
                time.sleep(2 * attempt)
                continue
            if resp.status_code not in expected:
                raise ApiError(f"HTTP {resp.status_code} :: {resp.text[:200]}")
            return resp.json() if resp.content else {}

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

# -----------------------------------------------------------------------------
# ЛОГИКА
# -----------------------------------------------------------------------------
def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(config.values()): sys.exit("Ошибка: не заданы переменные доступа к Posiflora")

    try:
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()
        
        LOG.info("Запрашиваем собранные букеты для рентгена...")
        rows, inc = pf.paginate("/bouquets", {"page[size]": 100}, MAX_PAGES)
        
        found = 0
        for r in rows:
            attrs = r.get("attributes", {})
            name = str(attrs.get("title") or attrs.get("name") or "").strip()
            if not name: name = f"Букет {str(r.get('id'))[-4:]}"
            
            # Ищем конкретные букеты с витрины, которые мы видели на скриншоте
            if "0605" in name or "0601" in name or "0603" in name or "0602" in name:
                LOG.info(f"\n=== РЕНТГЕН ЖИВОГО БУКЕТА: {name} ===")
                LOG.info(f"АТРИБУТЫ (attributes): {attrs}")
                LOG.info(f"СВЯЗИ (relationships): {r.get('relationships')}")
                LOG.info("==========================================\n")
                found += 1
                
                if found >= 3: 
                    break
                    
        if found == 0:
            LOG.warning("Искомые букеты не найдены. Вывожу данные первого попавшегося букета:")
            if rows:
                r = rows[0]
                attrs = r.get("attributes", {})
                name = str(attrs.get("title") or attrs.get("name") or "").strip()
                LOG.info(f"\n=== РЕНТГЕН ПЕРВОГО ПОПАВШЕГОСЯ БУКЕТА: {name} ===")
                LOG.info(f"АТРИБУТЫ: {attrs}")
                LOG.info("==========================================\n")

        LOG.info("Диагностика завершена. Никакие данные в МойСклад не отправлялись.")

    except Exception as e:
        LOG.error(f"Ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
