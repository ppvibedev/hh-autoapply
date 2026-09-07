"""Помощник входа для веб-дашборда: открывает окно hh.ru и ждёт сигнала «я вошёл».

Запускается дашбордом как фоновый процесс. Открывает видимый Chromium на странице
входа hh.ru. Ты логинишься руками в этом окне. Когда в дашборде жмёшь «Я вошёл»,
появляется файл-флаг state/login_done.flag — тогда сессия сохраняется и окно
закрывается. Пароль здесь не обрабатывается — сохраняются только куки сессии.
"""
from __future__ import annotations

import os
import time

from playwright.sync_api import sync_playwright

import config

FLAG = "state/login_done.flag"
TIMEOUT = 600  # максимум ждём вход 10 минут


def main() -> int:
    os.makedirs("state", exist_ok=True)
    if os.path.exists(FLAG):
        os.remove(FLAG)  # убрать несвежий сигнал

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="ru-RU")
        page = context.new_page()

        # Загрузку делаем устойчивой: ошибка/таймаут НЕ должны закрывать окно.
        for url in ("https://hh.ru/account/login", "https://hh.ru/"):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception:  # noqa: BLE001 — окно оставляем открытым в любом случае
                continue

        saved = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            try:
                if page.is_closed():
                    break
            except Exception:  # noqa: BLE001
                break
            if os.path.exists(FLAG):
                context.storage_state(path=config.SESSION_FILE)
                saved = True
                try:
                    os.remove(FLAG)
                except OSError:
                    pass
                break
            time.sleep(1.5)

        try:
            context.close()
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
