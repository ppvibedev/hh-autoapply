#!/usr/bin/env bash
# Запуск дашборда. Открой потом http://127.0.0.1:8000
cd "$(dirname "$0")"
./.venv/bin/python webapp.py
