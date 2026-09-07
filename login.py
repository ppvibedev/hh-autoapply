"""Разовый вход в hh.ru. Логин/пароль вводишь ТЫ сам в открывшемся браузере.

Скрипт открывает hh.ru, ждёт, пока ты авторизуешься руками (включая любую
капчу/2FA), затем сохраняет сессию в state/session.json. Пароль скрипт не видит.

Запуск:  python login.py
"""
import os

from playwright.sync_api import sync_playwright

import config


def main() -> int:
    os.makedirs(os.path.dirname(config.SESSION_FILE), exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # всегда видимый — ты логинишься
        context = browser.new_context(locale="ru-RU")
        page = context.new_page()
        page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")

        print("\n=== ВХОД В HH.RU ===")
        print("1. Залогинься в открывшемся окне (логин, пароль, при необходимости капча/код).")
        print("2. Дождись, что видишь свой личный кабинет.")
        print("3. Вернись сюда и нажми Enter — сессия сохранится.\n")
        input("Нажми Enter, когда вошёл... ")

        context.storage_state(path=config.SESSION_FILE)
        print(f"\nСессия сохранена: {config.SESSION_FILE}")
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
