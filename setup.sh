#!/usr/bin/env bash
# Установка на новом компьютере. Запуск: bash setup.sh
set -e
cd "$(dirname "$0")"

echo "==> Проверяю Python 3..."
command -v python3 >/dev/null || { echo "Нет python3. Установи Python 3.9+ и повтори."; exit 1; }

echo "==> Создаю виртуальное окружение (.venv)..."
python3 -m venv .venv

echo "==> Ставлю зависимости..."
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "==> Ставлю браузер Chromium для Playwright..."
./.venv/bin/playwright install chromium

echo "==> Готовлю .env..."
[ -f .env ] || cp .env.example .env

echo ""
echo "Готово. Дальше:"
echo "  1) Запусти:  bash start.sh"
echo "  2) Открой:   http://127.0.0.1:8000"
echo "  3) В дашборде: «Открыть окно входа» → залогинься в hh.ru → «Я вошёл — сохранить»"
echo "  4) Впиши API-ключ ИИ в блоке «ИИ для писем» (если нужен) и сохрани"
