"""Локальный веб-дашборд автопилота hh.ru.

Запуск:  python webapp.py   →  http://127.0.0.1:8000

Поверх скриптов: показывает статус, ленту решений (state/decisions.jsonl),
управляет прогоном (запуск run_web.py в фоне + kill-switch) и правит фильтры (.env).
Слушает только localhost — наружу ничего не открывает.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import requests
from flask import Flask, jsonify, request, send_from_directory

import config
import envfile

app = Flask(__name__, static_folder="web")

# Хэндл фонового прогона (один за раз).
_proc: subprocess.Popen | None = None
# Хэндл окна входа.
_login_proc: subprocess.Popen | None = None
_resumes_proc: subprocess.Popen | None = None
LOGIN_FLAG = "state/login_done.flag"
# Кэш справочника регионов hh.ru (грузим один раз).
_areas_cache: list | None = None

# Ключи .env, которые разрешено править из интерфейса.
EDITABLE = [
    "DRY_RUN", "SCORE_THRESHOLD", "DAILY_LIMIT",
    "SEARCH_TEXT", "AREA", "SALARY_MIN", "WORK_FORMAT",
    "EXPERIENCE", "EMPLOYMENT", "SCHEDULE", "SEARCH_PERIOD", "ORDER_BY",
    "STOPWORDS", "BLACKLIST_EMPLOYERS",
    "PAGES_PER_RUN", "PAUSE_SECONDS", "HEADLESS",
    "LLM_PROVIDER", "LLM_MODEL", "YANDEX_FOLDER_ID", "RESUME_URL",
]
# Ключ ИИ — чувствительный: наружу не отдаём, пустым не затираем.
SECRET_KEYS = ["LLM_API_KEY"]


def _read_decisions(limit: int = 100) -> list[dict]:
    path = config.DECISIONS_LOG
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows[-limit:][::-1]  # новые сверху


def _counts(rows: list[dict]) -> dict[str, int]:
    c = {"would_apply": 0, "applied": 0, "below_threshold": 0,
         "reject": 0, "no_button": 0}
    for r in rows:
        d = r.get("decision", "")
        if d in c:
            c[d] += 1
    return c


def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _run_pid() -> int | None:
    """PID активного прогона из PID-файла, если процесс реально жив. Стейл-файл чистим."""
    if not os.path.exists(config.RUN_PID_FILE):
        return None
    try:
        pid = int(open(config.RUN_PID_FILE, encoding="utf-8").read().strip())
    except (ValueError, OSError):
        return None
    if _pid_alive(pid):
        return pid
    try:  # мёртвый PID — убрать стейл-файл
        os.remove(config.RUN_PID_FILE)
    except OSError:
        pass
    return None


def _any_run_alive() -> bool:
    """Идёт ли прогон — по PID-файлу (точно, без ложных срабатываний по имени)."""
    return _run_pid() is not None


def _kill_all_runs() -> None:
    """Убить активный прогон по PID + подчистить осиротевшие (точный паттерн)."""
    pid = _run_pid()
    if pid:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    try:
        if os.path.exists(config.RUN_PID_FILE):
            os.remove(config.RUN_PID_FILE)
    except OSError:
        pass
    # бэкап на случай осиротевших: точный паттерн «python … run_web.py»
    subprocess.run(["pkill", "-9", "-f", r"[p]ython.*run_web\.py"])


def _login_running() -> bool:
    return _login_proc is not None and _login_proc.poll() is None


def _resumes_running() -> bool:
    return _resumes_proc is not None and _resumes_proc.poll() is None


def _read_resumes() -> list:
    if os.path.exists(config.RESUMES_FILE):
        try:
            with open(config.RESUMES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _spawn(script: str) -> subprocess.Popen:
    os.makedirs("state", exist_ok=True)
    logf = open(f"state/{script}.log", "w", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1")  # чтобы лог прогона писался сразу
    return subprocess.Popen(
        [sys.executable, script],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
    )


@app.get("/")
def index():
    return send_from_directory("web", "index.html")


@app.get("/api/state")
def api_state():
    env = envfile.read_env()
    rows = _read_decisions()
    all_rows = _read_decisions(limit=100000)
    return jsonify({
        "running": _any_run_alive(),  # учитывает и осиротевшие процессы
        "login_running": _login_running(),
        "dry_run": config.DRY_RUN,  # всегда False — программа только боевая, shadow убран
        "has_session": os.path.exists(config.SESSION_FILE),
        "config": {k: env.get(k, "") for k in EDITABLE},
        "llm_key_set": bool(env.get("LLM_API_KEY", "").strip()),
        "resumes": _read_resumes(),
        "resumes_refreshing": _resumes_running(),
        "counts": _counts(all_rows),
        "decisions": rows,
    })


@app.post("/api/config")
def api_config():
    payload = request.get_json(force=True) or {}
    updates = {k: str(v) for k, v in payload.items() if k in EDITABLE}
    # Секретные ключи обновляем, только если прислано непустое значение.
    for k in SECRET_KEYS:
        val = str(payload.get(k, "")).strip()
        if val:
            updates[k] = val
    if updates:
        envfile.update_env(updates)
    # Сменили выбранное резюме → сбрасываем кэш его текста, чтобы письмо строилось по нему.
    if "RESUME_URL" in updates and os.path.exists(config.RESUME_FILE):
        os.remove(config.RESUME_FILE)
    return jsonify({"ok": True, "updated": [k for k in updates if k not in SECRET_KEYS]})


@app.post("/api/run")
def api_run():
    global _proc
    if _any_run_alive():  # ловим и осиротевшие процессы, не только свой
        return jsonify({"ok": False, "error": "Прогон уже идёт (или остался фоновый) — нажми Стоп"}), 409
    if not os.path.exists(config.SESSION_FILE):
        return jsonify({"ok": False, "error": "Нет сессии — сначала войди в hh.ru"}), 400
    _kill_all_runs()  # подчистить любые зависшие перед стартом — гарантия одного прогона
    _proc = _spawn("run_web.py")
    return jsonify({"ok": True})


@app.post("/api/login/start")
def api_login_start():
    """Открыть окно браузера для входа в hh.ru."""
    global _login_proc
    if _login_running():
        return jsonify({"ok": False, "error": "Окно входа уже открыто"}), 409
    if os.path.exists(LOGIN_FLAG):
        os.remove(LOGIN_FLAG)
    _login_proc = _spawn("login_web.py")
    return jsonify({"ok": True})


@app.post("/api/login/finish")
def api_login_finish():
    """Сигнал «я вошёл» — помощник сохранит сессию и закроет окно."""
    if not _login_running():
        return jsonify({"ok": False, "error": "Окно входа не открыто"}), 400
    os.makedirs("state", exist_ok=True)
    with open(LOGIN_FLAG, "w", encoding="utf-8") as f:
        f.write("1")
    return jsonify({"ok": True})


@app.post("/api/login/cancel")
def api_login_cancel():
    global _login_proc
    if _login_running():
        _login_proc.terminate()
    return jsonify({"ok": True})


@app.post("/api/resumes/refresh")
def api_resumes_refresh():
    """Подтянуть список твоих резюме с hh.ru в фоне."""
    global _resumes_proc
    if _resumes_running():
        return jsonify({"ok": False, "error": "Обновление уже идёт"}), 409
    if not os.path.exists(config.SESSION_FILE):
        return jsonify({"ok": False, "error": "Нет сессии — сначала войди в hh.ru"}), 400
    _resumes_proc = _spawn("list_resumes.py")
    return jsonify({"ok": True})


@app.post("/api/reset")
def api_reset():
    """Очистить ленту решений и список просмотренных (для чистого повторного прогона)."""
    for path in (config.DECISIONS_LOG, config.SEEN_FILE, config.RESUME_FILE):
        if os.path.exists(path):
            os.remove(path)
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    """Kill-switch: убивает ВСЕ процессы прогона, включая осиротевшие."""
    was_alive = _any_run_alive()
    _kill_all_runs()
    return jsonify({"ok": True, "stopped": was_alive})


@app.get("/api/areas")
def api_areas():
    """Справочник регионов/городов России из hh.ru: [{id,name,cities:[...]}]."""
    global _areas_cache
    if _areas_cache is None:
        r = requests.get("https://api.hh.ru/areas/113", timeout=30)
        r.raise_for_status()
        russia = r.json()
        regions = []
        for reg in russia.get("areas", []):
            cities = [{"id": c["id"], "name": c["name"]} for c in reg.get("areas", [])]
            regions.append({"id": reg["id"], "name": reg["name"], "cities": cities})
        _areas_cache = regions
    return jsonify({"regions": _areas_cache})


@app.get("/api/runlog")
def api_runlog():
    path = "state/run.log"
    if not os.path.exists(path):
        return jsonify({"log": ""})
    with open(path, encoding="utf-8") as f:
        return jsonify({"log": f.read()[-8000:]})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
