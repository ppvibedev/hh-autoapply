"""SAFE-прогон: вакансии → выбранное резюме/контекст → matching → письмо → история."""
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import secrets
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import config
import decide
import llm
from llm_errors import LLMUnavailable
from resume_metadata import selected_resume_title
import hh_web
from browser import browser_context, has_session
from candidate_context import load_candidate_context
from privacy import sanitize_candidate_text, sanitize_record, sanitize_exception_message

_stop_requested = False
_active_run_id = None
_active_stage = "run_setup"
_worker_stop_ack_logged = False
_worker_stop_observed_logged = False


class _RunControl:
    """Authenticated local control endpoint; it never exposes run data."""
    def __init__(self, path, token):
        self.path = Path(path)
        self.token = token
        self.server = None
        self.thread = None
        self.closed = False

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # macOS has a small sun_path limit. Refuse before durable state is written.
        if len(os.fsencode(str(self.path.resolve()))) >= 100:
            raise ValueError("run_control_socket_path_too_long")
        # A socket can only be left by an already-dead worker: the exclusive run
        # state is created immediately afterwards, before work begins.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(self.path))
        except OSError:
            self.server.close()
            self.server = None
            raise
        self.path.chmod(0o600)
        self.server.listen(2)
        self.server.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, name="run-control", daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.closed:
            try:
                connection, _ = self.server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with connection:
                try:
                    raw = connection.recv(1024)
                    request = json.loads(raw.decode("utf-8"))
                    allowed = (isinstance(request, dict)
                               and secrets.compare_digest(str(request.get("token", "")), self.token)
                               and request.get("action") in ("probe", "stop"))
                    if not allowed:
                        connection.sendall(b'{"ok":false}')
                        continue
                    if request["action"] == "stop":
                        _request_stop()
                        _worker_stop_acknowledged("authenticated_control_socket")
                        connection.sendall(b'{"ok":true,"status":"stop_requested"}')
                    else:
                        connection.sendall(b'{"ok":true,"status":"active"}')
                except (OSError, UnicodeError, ValueError, TypeError):
                    try: connection.sendall(b'{"ok":false}')
                    except OSError: pass

    def close(self):
        self.closed = True
        if self.server is not None:
            try: self.server.close()
            except OSError: pass
        if self.thread is not None:
            self.thread.join(timeout=0.5)
        try: self.path.unlink()
        except FileNotFoundError: pass


def _write_run_state(path, run_id, token):
    state = {"version": 2, "pid": os.getpid(), "run_id": run_id, "control_token": token}
    with path.open("x", encoding="utf-8") as stream:
        json.dump(state, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def _request_stop(*_):
    global _stop_requested
    _stop_requested = True


def _worker_stop_acknowledged(source="worker_signal"):
    global _worker_stop_ack_logged
    if _worker_stop_ack_logged or not _active_run_id:
        return
    _worker_stop_ack_logged = True
    _log({"event": "worker_stop_acknowledged", "run_id": _active_run_id,
          "stage": _active_stage, "source": source})


def _observe_stop():
    global _worker_stop_observed_logged
    if not _stop_requested:
        return False
    _worker_stop_acknowledged()
    if not _worker_stop_observed_logged and _active_run_id:
        _worker_stop_observed_logged = True
        _log({"event": "worker_stop_observed", "run_id": _active_run_id,
              "stage": _active_stage})
    return True


def _error_code(error):
    # Не публиковать произвольное сообщение исключения: оно может содержать ключ.
    known = {"technical_secret_detected", "select_specific_hh_resume", "resume_identity_mismatch",
             "resume_identity_not_verified", "resume_content_not_found", "resume_content_empty",
             "hh_session_missing", "invalid_run_settings", "matching_invalid_score",
             "matching_invalid_json", "matching_invalid_schema", "matching_inconsistent_decision",
             "matching_invalid_remote_status", "matching_invalid_role_alignment", "matching_llm_unavailable", "letter_llm_unavailable",
             "selected_resume_title_unavailable",
             "letter_invalid", "candidate_context_symlink_not_allowed", "candidate_context_path_not_allowed",
             "candidate_context_too_large_120000_chars", "llm_context_too_large_180000_chars",
             "vacancy_description_empty", "vacancy_description_not_found", "resume_cache_symlink_not_allowed"}
    return str(error) if str(error) in known else type(error).__name__



def _exception_details(error, stage):
    frames = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        # Filename only: never include username/private parent directories or locals.
        frames.append({"filename": Path(code.co_filename).name,
                       "line_number": tb.tb_lineno, "function_name": code.co_name})
        tb = tb.tb_next
    details = {"stage": stage,
            "error_type": type(error).__module__ + "." + type(error).__qualname__,
            "error_message": sanitize_exception_message(error), "traceback_frames": frames}
    if isinstance(error, LLMUnavailable):
        details["llm_failures"] = error.failures
    return details


def _log(entry):
    entry = dict(entry, ts=datetime.now(timezone.utc).isoformat())
    entry = sanitize_record(entry)
    path = Path(config.DECISIONS_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _get_resume(page, resume_id, on_stage=None):
    if on_stage:
        on_stage("resume_refresh")
    if hh_web.resume_id_from_url(config.RESUME_URL) != resume_id:
        raise ValueError("resume_identity_mismatch")
    folder = Path(config.RESUME_CACHE_DIR)
    path = folder / f"{resume_id}.txt"
    if folder.is_symlink() or path.is_symlink():
        raise ValueError("resume_cache_symlink_not_allowed")
    # A same-ID HH resume can be edited. Refresh exactly once at run preflight;
    # the returned in-memory value is the immutable snapshot for this run.
    text = hh_web.load_resume_text(page, config.RESUME_URL, on_stage=on_stage)
    if not text.strip():
        raise ValueError("resume_content_empty")
    if on_stage:
        on_stage("resume_cache_write")
    folder.mkdir(parents=True, exist_ok=True)
    text = sanitize_candidate_text(text, (config.LLM_API_KEY, config.ACCESS_TOKEN))
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return text


def main():
    global _stop_requested, _active_run_id, _active_stage, _worker_stop_ack_logged, _worker_stop_observed_logged
    _stop_requested = False
    llm.reset_runtime()
    mode = "SAFE" if config.DRY_RUN else "LIVE"
    run_id = uuid4().hex
    _active_run_id = run_id
    _active_stage = "run_setup"
    _worker_stop_ack_logged = False
    _worker_stop_observed_logged = False
    counts = {"scanned": 0, "hard_rejected": 0, "score_rejected": 0,
              "safe_ready": 0, "manual_review": 0, "applied_confirmed": 0,
              "apply_failed": 0, "submission_unknown": 0, "already_applied": 0, "apply_attempted": 0}
    stop_reason, run_status = "pages_exhausted", "COMPLETED"
    pid_path = Path(config.RUN_PID_FILE)
    control = None
    token = None
    previous_handlers = {}
    blocked_vacancies = {}
    halt = False
    stage = "run_setup"
    def mark_stage(value):
        nonlocal stage
        global _active_stage
        stage = value
        _active_stage = value

    try:
        llm.start_runtime()
        resume_id = hh_web.resume_id_from_url(config.RESUME_URL)
        if not has_session():
            raise ValueError("hh_session_missing")
        if not 0 <= config.SCORE_THRESHOLD <= 100 or config.PAGES_PER_RUN < 1:
            raise ValueError("invalid_run_settings")
        if config.PAUSE_SECONDS < 0 or config.TARGET_APPLICATIONS_PER_RUN < 1:
            raise ValueError("invalid_run_settings")
        mark_stage("resume_metadata_preflight")
        resume_title = selected_resume_title(resume_id)
        if not resume_title:
            raise ValueError("selected_resume_title_unavailable")
        instructions, materials = load_candidate_context()
        if mode == "LIVE" and Path(config.DECISIONS_LOG).exists():
            for line in Path(config.DECISIONS_LOG).read_text(encoding="utf-8").splitlines():
                try:
                    previous = json.loads(line)
                except ValueError:
                    # A corrupt journal must not silently remove LIVE dedup protection.
                    raise ValueError("live_history_corrupt") from None
                status = previous.get("application_status")
                if status in ("applied_confirmed", "already_applied", "submission_unknown"):
                    blocked_vacancies[str(previous.get("vacancy_id"))] = status
                # Unknown submissions stay blocked even if later records look benign.

        # The socket starts before durable state becomes visible. A recovered
        # dashboard can therefore authenticate the worker without signalling a PID.
        token = secrets.token_urlsafe(32)
        control = _RunControl(config.RUN_CONTROL_SOCKET, token)
        control.start()
        try:
            _write_run_state(pid_path, run_id, token)
        except Exception:
            control.close()
            control = None
            raise
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, _request_stop)
        _log({"event": "run_started", "run_id": run_id, "mode": mode,
              "selected_resume_id": resume_id, "threshold": config.SCORE_THRESHOLD})
        print(mode + (": поиск и анализ; отправка заблокирована." if mode == "SAFE" else ": реальные отклики разрешены для этого запуска."), flush=True)
        seen_this_run = set()  # SAFE не использует и не меняет старый seen.json.
        with browser_context(on_stage=mark_stage) as (_, page):
            resume_text = _get_resume(page, resume_id, on_stage=mark_stage)
            for page_num in range(config.PAGES_PER_RUN):
                if _observe_stop() or halt:
                    break
                mark_stage("search_start")
                vacs = hh_web.search_page(page, text=config.SEARCH_QUERY, area=config.AREA,
                    salary_min=config.SALARY_MIN, work_format_codes=["REMOTE"], page_num=page_num,
                    extra_params=config.HH_SEARCH_PARAMS)
                if not vacs:
                    stop_reason = "no_results_or_page_not_recognized"
                    break
                for vac in vacs:
                    if _observe_stop():
                        break
                    vid = str(vac.get("id", ""))
                    if not vid.isdigit() or vid in seen_this_run:
                        continue
                    seen_this_run.add(vid)
                    counts["scanned"] += 1
                    entry = {"run_id": run_id, "vacancy_id": vid, "vid": vid,
                        "title": vac.get("name", ""), "company": (vac.get("employer") or {}).get("name", ""),
                        "url": f"https://hh.ru/vacancy/{vid}", "selected_resume_id": resume_id,
                        "match_score": None, "threshold": config.SCORE_THRESHOLD,
                        "decision": "needs_manual_review", "reason": "", "application_status": "safe_not_sent" if mode == "SAFE" else "not_attempted",
                        "error": None}
                    if mode == "LIVE" and vid in blocked_vacancies:
                        status = blocked_vacancies[vid]
                        entry.update(decision="already_applied" if status != "submission_unknown" else "submission_unknown",
                                     application_status=status, reason="blocked_by_persistent_history")
                        _log(entry)
                        continue
                    try:
                        reason = decide.hard_filters(vac)
                        if reason:
                            counts["hard_rejected"] += 1
                            entry.update(decision="rejected_hard_filter", reason=reason)
                        else:
                            mark_stage("vacancy_open")
                            full = hh_web.load_full(page, vac)
                            if _observe_stop():
                                break
                            mark_stage("matching")
                            result = decide.score(full, resume_text, materials, instructions,
                                                  search_query=config.SEARCH_QUERY, resume_title=resume_title or "")
                            entry.update(match_score=result["score"], score=result["score"],
                                matched=result["matched"], missing=result["missing"], risks=result["risks"],
                                reason=result["reason"], match_decision=result["decision"],
                                remote_status=result["remote_status"], remote_reason=result["remote_reason"],
                                capability_score=result["capability_score"], vacancy_role=result["vacancy_role"],
                                role_alignment=result["role_alignment"],
                                role_alignment_reason=result["role_alignment_reason"], role_evidence=result["role_evidence"],
                                matching_result=result)
                            if result["role_alignment"] != "aligned":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review",
                                    reason_code="role_" + result["role_alignment"],
                                    reason=result["role_alignment_reason"])
                            elif result["remote_status"] == "ineligible":
                                counts["hard_rejected"] += 1
                                entry.update(decision="rejected_remote", reason=result["remote_reason"])
                            elif result["remote_status"] == "unclear":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review", reason=result["remote_reason"])
                            elif result["score"] < config.SCORE_THRESHOLD:
                                counts["score_rejected"] += 1
                                entry["decision"] = "rejected_score"
                            elif not _observe_stop():
                                mark_stage("cover_letter")
                                entry["letter"] = decide.cover_letter(full, resume_text, materials, instructions)
                                if mode == "SAFE":
                                    entry["decision"] = "safe_ready"
                                    counts["safe_ready"] += 1
                                else:
                                    mark_stage("application")
                                    counts["apply_attempted"] += 1
                                    def before_submit():
                                        _log({"event":"apply_submit_started", "run_id":run_id,
                                              "vacancy_id":vid, "selected_resume_id":resume_id,
                                              "application_status":"submission_unknown"})
                                        blocked_vacancies[vid] = "submission_unknown"
                                    outcome = hh_web.apply(page, full, entry["letter"], dry_run=False,
                                        resume_url=config.RESUME_URL, resume_text=resume_text,
                                        candidate_text=materials, instructions=instructions,
                                        stopped=_observe_stop, before_submit=before_submit)
                                    entry.update(outcome)
                                    status = outcome["application_status"]
                                    entry["decision"] = status
                                    if status in counts:
                                        counts[status] += 1
                                    if status == "needs_manual_review": counts["manual_review"] += 1
                                    if status in ("applied_confirmed", "already_applied", "submission_unknown"):
                                        blocked_vacancies[vid] = status
                                    if status == "submission_unknown":
                                        halt, stop_reason = True, "submission_unknown"
                                        run_status = "STOPPED"
                                    if counts["applied_confirmed"] >= config.TARGET_APPLICATIONS_PER_RUN:
                                        halt, stop_reason = True, "target_reached"
                    except LLMUnavailable as error:
                        halt, run_status, stop_reason = True, "STOPPED", "llm_infrastructure_unavailable"
                        entry.update(_exception_details(error, stage))
                        entry.update(decision="needs_manual_review", error=stop_reason,
                                     reason_code=stop_reason, reason="LLM infrastructure unavailable")
                        counts["manual_review"] += 1
                    except Exception as error:
                        counts["manual_review"] += 1
                        entry.update(_exception_details(error, stage))
                        entry.update(decision="needs_manual_review", error=_error_code(error),
                                     reason_code=_error_code(error),
                                     reason=f"{stage}: {_error_code(error)}; требуется проверка.")
                    routing_events = llm.consume_routing_events()
                    if routing_events:
                        entry["llm_routing"] = routing_events
                    try:
                        _log(entry)
                    except ValueError:
                        # Не выводить и не сохранять подозрительное содержимое.
                        _log({"run_id": run_id, "vacancy_id": vid, "url": f"https://hh.ru/vacancy/{vid}",
                              "decision": "needs_manual_review", "application_status": "safe_not_sent",
                              "error": "unsafe_content_blocked"})
                    if halt:
                        break
                    print(f"{mode}: обработано {counts['scanned']}, подготовлено {counts['safe_ready']}", flush=True)
        if _observe_stop():
            run_status, stop_reason = "STOPPED", "user_stop"
    except LLMUnavailable as error:
        run_status, stop_reason = "STOPPED", "llm_infrastructure_unavailable"
        _log(dict(event="run_error", run_id=run_id, reason_code=stop_reason,
                  **_exception_details(error, stage)))
    except Exception as error:
        run_status, stop_reason = "FAILED", _error_code(error)
        diagnostic = dict(event="run_error", run_id=run_id, reason_code=stop_reason,
                          **_exception_details(error, stage))
        _log(diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
        print("Прогон остановлен:", stop_reason, flush=True)
    finally:
        _log({"event": "run_finished", "run_id": run_id, "mode": mode, "status": run_status,
              "stop_reason": stop_reason, "counts": counts})
        try:
            if pid_path.exists():
                state = json.loads(pid_path.read_text(encoding="utf-8"))
                if token and isinstance(state, dict) and state.get("pid") == os.getpid() and state.get("control_token") == token:
                    pid_path.unlink()
        except (OSError, ValueError, TypeError):
            pass
        if control is not None:
            control.close()
        _active_run_id = None
        _active_stage = "run_setup"
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 1 if run_status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
