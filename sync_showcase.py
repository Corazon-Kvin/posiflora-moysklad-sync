#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Скрипт 1: ДИАГНОСТИКА СТАТУСОВ (sync_showcase.py)
# Безопасный режим: только чтение данных из Posiflora.

import logging
import os
import sys
import time
import requests

REQUEST_PAUSE = 0.05
MAX_RETRIES = 5
HTTP_TIMEOUT = 60
MAX_PAGES = 1000
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("sync_diagnostic")

class ApiError(Exception): pass

class PosifloraClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = ""
        self.session = requests.Session()

    def request(self, method, path, params=None, json_body=None):
        url = self.base_url + path
        h = {"Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json", "User-Agent": BROWSER_UA}
        if self.token: h["Authorization"] = f"Bearer {self.token}"
        
        attempt = 0
        while True:
            attempt += 1
            time.sleep(REQUEST_PAUSE)
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=h, timeout=HTTP_TIMEOUT)
            except Exception as exc:
                if attempt >= MAX_RETRIES: raise ApiError(f"Ошибка сети: {exc}")
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < MAX_RETRIES:
                continue
            if resp.status_code not in (200, 201):
                raise ApiError(f"HTTP {resp.status_code}")
            return resp.json() if resp.content else {}

    def login(self):
        body = {"data": {"type": "sessions", "attributes": {"username": self.username, "password": self.password}}}
        resp = self.request("POST", "/sessions", json_body=body)
        self.token = resp.get("data", {}).get("attributes", {}).get("accessToken")

    def paginate(self, path, params, max_pages):
        rows = []
        page = 1
        while page <= max_pages:
            p = dict(params); p["page[number]"] = page
            body = self.request("GET", path, params=p)
            data = body.get("data", [])
            if not data: break
            rows.extend(data if isinstance(data, list) else [data])
            if len(data) < p.get("page[size]", 100): break
            page += 1
        return rows

def main():
    keys = ["POSIFLORA_USER", "POSIFLORA_PASS", "POSIFLORA_URL"]
    config = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(config.values()): sys.exit("Ошибка: не заданы переменные доступа к Posiflora")

    try:
        pf = PosifloraClient(config["POSIFLORA_URL"], config["POSIFLORA_USER"], config["POSIFLORA_PASS"])
        pf.login()
        
        LOG.info("Запрашиваем базу собранных букетов...")
        rows = pf.paginate("/bouquets", {"page[size]": 100}, MAX_PAGES)
        
        active_bouquets = []
        for r in rows:
            attrs = r.get("attributes", {})
            status = str(attrs.get("status", "")).lower()
            
            # Отбрасываем только 100% мертвые букеты
            if status not in ("purchased", "disassembled", "written_off", "deleted", "archived"):
                active_bouquets.append((r.get("id"), attrs))
                
        LOG.info(f"Найдено активных букетов: {len(active_bouquets)}\n")
        LOG.info(f"{'НАЗВАНИЕ':<30} | {'СТАТУС (status)':<15} | {'НА ВИТРИНЕ (onwindowAt)':<25} | {'PUBLIC'}")
        LOG.info("-" * 90)
        
        for b_id, attrs in active_bouquets:
            name = str(attrs.get("title") or attrs.get("name") or f"Букет {str(b_id)[-4:]}")
            status = str(attrs.get("status", "None"))
            onwindow = str(attrs.get("onwindowAt", "None"))
            pub = str(attrs.get("public", "None"))
            
            LOG.info(f"{name[:28]:<30} | {status:<15} | {onwindow:<25} | {pub}")
            
        LOG.info("\nДиагностика завершена. Ничего не отправлено в МойСклад.")

    except Exception as e:
        LOG.error(f"Ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
