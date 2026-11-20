#!/usr/bin/env bash
# Установка на новом компьютере. Запуск: bash setup.sh
set -e
cd "$(dirname "$0")"

echo "==> Проверяю нативный Python 3.11..."
PYTHON_BIN=""
NATIVE_ARCH="$(uname -m)"
# A translated shell on Apple Silicon must not select an x86_64 interpreter.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = "1" ]; then
  NATIVE_ARCH="arm64"
fi
for candidate in python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import platform, sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and platform.machine() == sys.argv[1] else 1)' "$NATIVE_ARCH"; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "Нужен нативный Python 3.11 ($NATIVE_ARCH). Установите Python 3.11 с python.org,"
  echo "проверьте python3.11 --version и повторите bash setup.sh."
  exit 1
fi
if [ -e .venv ]; then
  if [ ! -x .venv/bin/python ] || ! ./.venv/bin/python -c 'import platform, sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and platform.machine() == sys.argv[1] else 1)' "$NATIVE_ARCH"; then
    echo "Существующая .venv несовместима. Пересоздайте только .venv вручную; .env, state/ и private/ сохраните."
    exit 1
  fi
else
  echo "==> Создаю виртуальное окружение (.venv)..."
  "$PYTHON_BIN" -m venv .venv
fi

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
