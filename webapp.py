"""Локальный веб-дашборд автопилота hh.ru.

Запуск:  python webapp.py   →  http://127.0.0.1:8000

Поверх скриптов: показывает статус, ленту решений (state/decisions.jsonl),
управляет прогоном (запуск run_web.py в фоне + kill-switch) и правит фильтры (.env).
Слушает только localhost — наружу ничего не открывает.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

import config
import envfile
from hh_web import resume_id_from_url
from privacy import sanitize_record, sanitize_candidate_text

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
    "RUN_MODE", "SCORE_THRESHOLD", "TARGET_APPLICATIONS_PER_RUN",
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
                    item = sanitize_record(json.loads(line))
                    if isinstance(item, dict):
                        rows.append(item)
                except (ValueError, TypeError):
                    pass
    return rows[-limit:][::-1]  # новые сверху


_pending_after_run = None
_display_run_id = None
_active_mode = "SAFE"


def _log_stop_telemetry(event, *, run_id=None, **fields):
    """Durable, technical-only stop lifecycle evidence; never includes control tokens."""
    entry = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
    if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id):
        entry["run_id"] = run_id
    entry.update(fields)
    entry = sanitize_record(entry)
    path = Path(config.DECISIONS_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _current_run_rows():
    """Only a run started in this server session; never restore history on startup."""
    global _display_run_id
    if _pending_after_run is None:
        return None, []
    rows = _read_decisions(limit=100000)
    if _display_run_id is None:
        run_id = next((r.get("run_id") for r in rows if r.get("run_id")), None)
        if not run_id or run_id == _pending_after_run:
            return None, []
        _display_run_id = run_id
    return _display_run_id, [r for r in rows if r.get("run_id") == _display_run_id]



def _run_log(rows):
    # Structured events already carry run_id; raw stdout mixes all past runs.
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in reversed(rows)
                     if r.get("event"))


def _counts(rows: list[dict]) -> dict[str, int]:
    c = {"safe_ready": 0, "rejected_score": 0, "rejected_hard_filter": 0,
         "rejected_remote": 0, "needs_manual_review": 0, "applied_confirmed": 0,
         "apply_failed": 0, "submission_unknown": 0, "already_applied": 0, "scanned": 0}
    for r in rows:
        if not r.get("event") and r.get("vacancy_id"):
            c["scanned"] += 1
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


def _read_run_state():
    """Read v2 durable state. Legacy/invalid state is never process identity."""
    path = Path(config.RUN_PID_FILE)
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 2048:
            return None
        state = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    pid, run_id, token = state.get("pid"), state.get("run_id"), state.get("control_token")
    if (state.get("version") != 2 or not isinstance(pid, int) or pid <= 1
            or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
        return None
    return {"pid": pid, "run_id": run_id, "control_token": token}


def _legacy_run_pid():
    """A numeric pre-v2 PID is liveness-only state, never proof of ownership."""
    try:
        raw = Path(config.RUN_PID_FILE).read_text(encoding="utf-8")
        return int(raw) if re.fullmatch(r"[1-9][0-9]{0,9}", raw) else None
    except (OSError, ValueError, TypeError):
        return None


def _remove_run_state(state=None):
    """Only remove the exact durable v2 state we previously read."""
    path = Path(config.RUN_PID_FILE)
    try:
        if state is None:
            path.unlink()
            return True
        current = _read_run_state()
        if current == state:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _run_pid() -> int | None:
    """A live PID blocks a new run; dead or invalid state is safely discarded."""
    state = _read_run_state()
    if state is None:
        # A live legacy PID cannot be authenticated, but must not be cleared while
        # it could still belong to a genuine pre-v2 worker.
        legacy_pid = _legacy_run_pid()
        if legacy_pid is not None and _pid_alive(legacy_pid):
            return legacy_pid
        if os.path.exists(config.RUN_PID_FILE):
            _remove_run_state()
        return None
    if _pid_alive(state["pid"]):
        return state["pid"]
    _remove_run_state(state)
    return None


def _any_run_alive() -> bool:
    """Идёт ли прогон — по PID-файлу (точно, без ложных срабатываний по имени)."""
    return _run_pid() is not None


def _control_request(state, action):
    """A successful response proves the current worker holds this state's token."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.75)
            client.connect(config.RUN_CONTROL_SOCKET)
            client.sendall(json.dumps({"action": action, "token": state["control_token"]}).encode("utf-8"))
            response = json.loads(client.recv(1024).decode("utf-8"))
        return response if isinstance(response, dict) else {}
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}


def _kill_all_runs() -> dict:
    """Stop in-memory worker or an authenticated recovered worker; never signal PID state."""
    if _running():
        try:
            _proc.terminate()
            return {"ok": True, "stopped": True, "status": "in_memory_stop_requested",
                    "delivery_path": "in_memory_process", "delivery_accepted": True}
        except ProcessLookupError:
            return {"ok": True, "stopped": False, "status": "already_stopped",
                    "delivery_path": "already_dead", "delivery_accepted": False}
    state = _read_run_state()
    if state is None:
        legacy_pid = _legacy_run_pid()
        if legacy_pid is not None and _pid_alive(legacy_pid):
            return {"ok": False, "stopped": False, "status": "worker_identity_not_verified",
                    "delivery_path": "no_verified_worker", "delivery_accepted": False}
        if os.path.exists(config.RUN_PID_FILE):
            _remove_run_state()
        return {"ok": True, "stopped": False, "status": "already_stopped",
                "delivery_path": "already_dead", "delivery_accepted": False}
    if not _pid_alive(state["pid"]):
        _remove_run_state(state)
        return {"ok": True, "stopped": False, "status": "already_stopped",
                "delivery_path": "already_dead", "delivery_accepted": False}
    response = _control_request(state, "stop")
    if response.get("ok") is True and response.get("status") == "stop_requested":
        return {"ok": True, "stopped": True, "status": "recovered_stop_requested",
                "delivery_path": "authenticated_control_socket", "delivery_accepted": True}
    # A live PID without token-authenticated control may be unrelated or an old worker.
    # Preserve it rather than risking a signal to an arbitrary process.
    if not _pid_alive(state["pid"]):
        _remove_run_state(state)
        return {"ok": True, "stopped": False, "status": "already_stopped",
                "delivery_path": "already_dead", "delivery_accepted": False}
    return {"ok": False, "stopped": False, "status": "worker_identity_not_verified",
            "delivery_path": "no_verified_worker", "delivery_accepted": False}


def _login_running() -> bool:
    return _login_proc is not None and _login_proc.poll() is None


def _resumes_running() -> bool:
    return _resumes_proc is not None and _resumes_proc.poll() is None


def _read_resumes() -> list:
    if os.path.exists(config.RESUMES_FILE):
        try:
            with open(config.RESUMES_FILE, encoding="utf-8") as f:
                rows = json.load(f)
            return [{"url": "https://hh.ru/resume/" + resume_id_from_url(r["url"]),
                     "title": sanitize_candidate_text(r["title"])} for r in rows]

        except (ValueError, TypeError, KeyError, OSError):
            return []
    return []


def _spawn(script: str, *, live_confirmed=False) -> subprocess.Popen:
    os.makedirs("state", exist_ok=True)
    log_path = "state/safe_run.log" if script == "run_web.py" else f"state/{script}.log"
    logf = open(log_path, "a", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop("HH_LIVE_CONFIRMED_FOR_RUN", None)
    if live_confirmed:
        env["HH_LIVE_CONFIRMED_FOR_RUN"] = "1"
    #  # чтобы лог прогона писался сразу
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
    run_id, rows = _current_run_rows()
    return jsonify({
        "running": _running() or _any_run_alive(),  # учитывает и осиротевшие процессы
        "login_running": _login_running(),
        "dry_run": _active_mode != "LIVE",
        "mode": _active_mode,
        "has_session": os.path.exists(config.SESSION_FILE),
        "config": {**{k: env.get(k, "") for k in EDITABLE}, "RUN_MODE": env.get("RUN_MODE", "SAFE"),
                   "WORK_FORMAT": "remote_only",
                   "TARGET_APPLICATIONS_PER_RUN": env.get("TARGET_APPLICATIONS_PER_RUN", env.get("DAILY_LIMIT", "30"))},
        "llm_key_set": bool(env.get("LLM_API_KEY", "").strip()),
        "resumes": _read_resumes(),
        "resumes_refreshing": _resumes_running(),
        "run_id": run_id,
        "run_log": _run_log(rows),
        "counts": _counts(rows),
        "decisions": [r for r in rows if not r.get("event")][:100],
        "last_run": next((r for r in rows if r.get("event") == "run_finished"), None),
    })


@app.get("/api/llm-connections")
def api_llm_connections():
    import llm_connections as connections
    try:
        items = connections.load_connections(envfile.read_env())
        return jsonify({"connections": connections.public_connections(items)})
    except connections.ConnectionConfigError:
        return jsonify({"error": "Не удалось прочитать настройки подключений"}), 400


@app.post("/api/llm-connections")
@app.patch("/api/llm-connections/<connection_id>")
def api_llm_connection_save(connection_id=None):
    import llm_connections as connections
    # Same-origin JSON only: no form submissions and no credential reflection.
    if (not request.is_json or (request.headers.get("Origin")
            and request.headers["Origin"].rstrip("/") != request.host_url.rstrip("/"))):
        return jsonify({"error": "Ожидаются настройки подключения"}), 400
    try:
        items = connections.save_change(request.get_json(silent=True), envfile.read_env(), connection_id)
        return jsonify({"ok": True, "connections": items})
    except (connections.ConnectionConfigError, ValueError, TypeError):
        return jsonify({"error": "Проверьте провайдера, модель и настройки подключения"}), 400


@app.post("/api/config")
def api_config():
    payload = request.get_json(force=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Некорректные настройки"}), 400
    updates = {k: str(v) for k, v in payload.items() if k in EDITABLE}
    if updates.get("RUN_MODE", "SAFE") not in ("SAFE", "LIVE"):
        return jsonify({"ok": False, "error": "Неизвестный режим"}), 400
    for k, v in updates.items():
        if "\n" in v or "\r" in v:
            return jsonify({"ok": False, "error": "Переводы строк в настройках недопустимы"}), 400
    for k, bounds in {"SCORE_THRESHOLD": (0, 100), "TARGET_APPLICATIONS_PER_RUN": (1, 1000),
                      "PAGES_PER_RUN": (1, 100), "PAUSE_SECONDS": (0, 300)}.items():
        if k in updates:
            try:
                valid = bounds[0] <= int(updates[k]) <= bounds[1]
            except ValueError:
                valid = False
            if not valid:
                return jsonify({"ok": False, "error": f"Некорректное значение {k}"}), 400
    updates.setdefault("RUN_MODE", "SAFE")
    updates["WORK_FORMAT"] = "remote_only"
    if updates.get("RESUME_URL"):
        try:
            rid = resume_id_from_url(updates["RESUME_URL"])
            updates["RESUME_URL"] = f"https://hh.ru/resume/{rid}"
        except ValueError:
            return jsonify({"ok": False, "error": "Выберите конкретное резюме HH"}), 400
    # Секретные ключи обновляем, только если прислано непустое значение.
    for k in SECRET_KEYS:
        val = str(payload.get(k, "")).strip()
        if "\n" in val or "\r" in val:
            return jsonify({"ok": False, "error": "Некорректный формат ключа"}), 400
        if val:
            updates[k] = val
    if updates:
        envfile.update_env(updates)
    return jsonify({"ok": True, "updated": [k for k in updates if k not in SECRET_KEYS]})


@app.post("/api/run")
def api_run():
    global _proc, _pending_after_run, _display_run_id, _active_mode
    if _running() or _any_run_alive():
        return jsonify({"ok": False, "error": "Прогон уже идёт (или остался фоновый) — нажми Стоп"}), 409
    if not os.path.exists(config.SESSION_FILE):
        return jsonify({"ok": False, "error": "Нет сессии — сначала войди в hh.ru"}), 400
    env = envfile.read_env()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Некорректный запрос"}), 400
    mode = payload.get("mode", "SAFE")
    if mode not in ("SAFE", "LIVE") or (mode == "LIVE" and payload.get("confirm_live") is not True):
        return jsonify({"ok": False, "error": "LIVE требует явного подтверждения этого запуска"}), 400
    if mode != env.get("RUN_MODE", "SAFE").strip().upper():
        return jsonify({"ok": False, "error": "Сохраните выбранный режим перед запуском"}), 400
    try:
        resume_id_from_url(env.get("RESUME_URL", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Выберите конкретное резюме HH"}), 400
    previous_id = next((r.get("run_id") for r in _read_decisions(limit=100000)
                        if r.get("run_id")), None)
    _proc = _spawn("run_web.py", live_confirmed=True) if mode == "LIVE" else _spawn("run_web.py")
    _active_mode = mode
    _pending_after_run = previous_id or ""
    _display_run_id = None
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
    if _running() or _any_run_alive():
        return jsonify({"ok": False, "error": "Сначала остановите прогон"}), 409
    for path in (config.DECISIONS_LOG, config.SEEN_FILE, config.RESUME_FILE):
        if os.path.exists(path):
            os.remove(path)
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    """Request a graceful stop only from a known or authenticated worker."""
    state = _read_run_state()
    run_id = state.get("run_id") if state else None
    _log_stop_telemetry("stop_requested_by_user", run_id=run_id)
    _log_stop_telemetry("stop_api_received", run_id=run_id,
                        in_memory_process_exists=_running(), durable_worker_state_exists=state is not None)
    result = _kill_all_runs()
    _log_stop_telemetry("stop_delivery_path", run_id=run_id,
                        delivery_path=result.get("delivery_path", "other"))
    _log_stop_telemetry("stop_delivery_result", run_id=run_id,
                        delivery_status=result.get("status", "other"),
                        delivery_accepted=result.get("delivery_accepted") is True)
    if not result["ok"]:
        return jsonify({"ok": False, "stopped": False,
                        "error": "Нельзя безопасно подтвердить фоновый прогон; сигнал не отправлен",
                        "status": result["status"]}), 409
    return jsonify(result)


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
    run_id, rows = _current_run_rows()
    return jsonify({"run_id": run_id, "log": _run_log(rows)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
