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
import time
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import config
import decide
import llm
from llm_errors import LLMUnavailable, LLMStopped
import history_store
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
_active_stop_attempt_id = None
_STOP_ATTEMPT_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
LIVE_HISTORY_REREAD_DELAY_SECONDS = 0.02


class LiveHistoryCorrupt(ValueError):
    """Fail-closed LIVE history error carrying technical-only diagnostics."""

    def __init__(self, diagnostics):
        super().__init__("live_history_corrupt")
        self.history_diagnostics = diagnostics


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
                        attempt_id = request.get("stop_attempt_id")
                        if attempt_id is not None and (not isinstance(attempt_id, str)
                                or _STOP_ATTEMPT_RE.fullmatch(attempt_id) is None):
                            connection.sendall(b'{"ok":false}')
                            continue
                        _request_stop(stop_attempt_id=attempt_id)
                        _worker_stop_acknowledged("authenticated_control_socket", attempt_id)
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


def _request_stop(*_, stop_attempt_id=None):
    global _stop_requested, _active_stop_attempt_id
    _stop_requested = True
    if isinstance(stop_attempt_id, str) and _STOP_ATTEMPT_RE.fullmatch(stop_attempt_id):
        _active_stop_attempt_id = stop_attempt_id


def _worker_stop_acknowledged(source="worker_signal", stop_attempt_id=None):
    global _worker_stop_ack_logged
    if _worker_stop_ack_logged or not _active_run_id:
        return
    _worker_stop_ack_logged = True
    entry = {"event": "worker_stop_acknowledged", "run_id": _active_run_id,
             "stage": _active_stage, "source": source}
    attempt_id = stop_attempt_id or _active_stop_attempt_id
    if isinstance(attempt_id, str) and _STOP_ATTEMPT_RE.fullmatch(attempt_id):
        entry["stop_attempt_id"] = attempt_id
    _log(entry)


def _observe_stop():
    global _worker_stop_observed_logged
    if not _stop_requested:
        return False
    _worker_stop_acknowledged()
    if not _worker_stop_observed_logged and _active_run_id:
        _worker_stop_observed_logged = True
        entry = {"event": "worker_stop_observed", "run_id": _active_run_id,
                 "stage": _active_stage}
        if isinstance(_active_stop_attempt_id, str) and _STOP_ATTEMPT_RE.fullmatch(_active_stop_attempt_id):
            entry["stop_attempt_id"] = _active_stop_attempt_id
        _log(entry)
    return True


def _error_code(error):
    # Не публиковать произвольное сообщение исключения: оно может содержать ключ.
    known = {"technical_secret_detected", "select_specific_hh_resume", "resume_identity_mismatch",
             "resume_identity_not_verified", "resume_content_not_found", "resume_content_empty",
             "hh_session_missing", "invalid_run_settings", "matching_invalid_score",
             "matching_invalid_json", "matching_invalid_schema", "matching_inconsistent_decision",
             "matching_invalid_remote_status", "matching_invalid_role_alignment", "matching_llm_unavailable", "letter_llm_unavailable",
             "selected_resume_title_unavailable", "live_history_corrupt",
             "application_safety_state_corrupt",
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
    if isinstance(error, LiveHistoryCorrupt):
        details["history_diagnostics"] = error.history_diagnostics
    return details


def _log(entry):
    entry = dict(entry, ts=datetime.now(timezone.utc).isoformat())
    entry = sanitize_record(entry)
    history_store.append_jsonl(config.DECISIONS_LOG, entry)


def _history_snapshot(path, attempt):
    """Read and strictly parse one complete JSONL snapshot without exposing records."""
    text = path.read_text(encoding="utf-8")
    try:
        stat = path.stat()
    except OSError:
        stat = None
    metadata = {
        "attempt": attempt,
        "snapshot_size_bytes": len(text.encode("utf-8")),
        "file_size": stat.st_size if stat is not None else None,
        "mtime_ns": stat.st_mtime_ns if stat is not None else None,
        "ends_with_newline": not text or text.endswith("\n"),
    }
    # JSONL records are delimited by literal LF only. Unicode separators such as
    # U+2028/U+2029 are valid JSON string content and must stay inside a record.
    lines = text.split("\n")
    last_nonempty_line = max(
        (line_number for line_number, line in enumerate(lines, 1) if line.strip()),
        default=0,
    )
    records = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError) as error:
            failure = dict(metadata, line_number=line_number,
                           line_length_chars=len(line),
                           line_length_bytes=len(line.encode("utf-8")),
                           error_position=(error.pos if isinstance(error, json.JSONDecodeError)
                                           else None),
                           is_last_line=line_number == last_nonempty_line)
            return None, failure, metadata
        if not isinstance(record, dict):
            failure = dict(metadata, line_number=line_number,
                           line_length_chars=len(line),
                           line_length_bytes=len(line.encode("utf-8")),
                           error_position=None,
                           is_last_line=line_number == last_nonempty_line,
                           record_type_invalid=True)
            return None, failure, metadata
        records.append(record)
    return records, None, metadata


def _read_live_history(path, *, sleep=time.sleep, telemetry=None):
    """Parse LIVE history, allowing one bounded reread but never skipping corruption."""
    path = Path(path)
    records, initial_failure, initial_snapshot = _history_snapshot(path, "initial")
    if initial_failure is None:
        return records

    sleep(LIVE_HISTORY_REREAD_DELAY_SECONDS)
    records, reread_failure, reread_snapshot = _history_snapshot(path, "reread")
    changed = {
        "size_changed": (initial_snapshot["snapshot_size_bytes"]
                         != reread_snapshot["snapshot_size_bytes"]),
        "mtime_changed": initial_snapshot["mtime_ns"] != reread_snapshot["mtime_ns"],
    }
    if reread_failure is None:
        if telemetry is not None:
            telemetry({"event": "history_read_recovered",
                       "initial_failure": initial_failure,
                       "reread_snapshot": reread_snapshot,
                       **changed})
        return records

    raise LiveHistoryCorrupt({"attempts": [initial_failure, reread_failure], **changed})


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
    global _stop_requested, _active_run_id, _active_stage, _worker_stop_ack_logged, _worker_stop_observed_logged, _active_stop_attempt_id
    _stop_requested = False
    llm.reset_runtime()
    mode = "SAFE" if config.DRY_RUN else "LIVE"
    run_id = uuid4().hex
    _active_run_id = run_id
    _active_stage = "run_setup"
    _worker_stop_ack_logged = False
    _worker_stop_observed_logged = False
    _active_stop_attempt_id = None
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
    current_vacancy_id = None
    def mark_stage(value):
        nonlocal stage
        global _active_stage
        stage = value
        _active_stage = value

    def record_llm_timing(event):
        row = dict(event, run_id=run_id)
        if current_vacancy_id is not None:
            row["vacancy_context_id"] = current_vacancy_id
        _log(row)

    try:
        llm.start_runtime(telemetry=record_llm_timing, stopped=_observe_stop)
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
        if mode == "LIVE":
            previous_records = (_read_live_history(
                                    config.DECISIONS_LOG,
                                    telemetry=lambda event: _log(dict(event, run_id=run_id)))
                                if Path(config.DECISIONS_LOG).exists() else [])
            blocked_vacancies.update(history_store.merge_safety(
                config.APPLICATION_SAFETY_FILE, previous_records))
            if previous_records:
                # Recovery telemetry may have appended after the first snapshot.
                compact_records = _read_live_history(config.DECISIONS_LOG)
                history_store.compact_history(
                    config.DECISIONS_LOG, compact_records, active_run_id=run_id)

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
              "selected_resume_id": resume_id, "threshold": config.SCORE_THRESHOLD,
              "search_config": {
                  "search_text": config.SEARCH_TEXT,
                  "search_query": config.SEARCH_QUERY,
                  "area": config.AREA,
                  "area_scope": "worldwide" if not config.AREA else "specific_area",
                  "pages_per_run": config.PAGES_PER_RUN,
                  "score_threshold": config.SCORE_THRESHOLD,
                  "target_confirmed_applications": config.TARGET_APPLICATIONS_PER_RUN,
                  "work_format": config.WORK_FORMAT,
                  "work_format_codes": list(config.WORK_FORMAT_CODES),
                  "salary_min": config.SALARY_MIN,
                  "experience": config.EXPERIENCE,
                  "employment": config.EMPLOYMENT,
                  "schedule": config.SCHEDULE,
                  "search_period": config.SEARCH_PERIOD,
                  "order_by": config.ORDER_BY,
              }})
        print(mode + (": поиск и анализ; отправка заблокирована." if mode == "SAFE" else ": реальные отклики разрешены для этого запуска."), flush=True)
        seen_this_run = set()  # SAFE не использует и не меняет старый seen.json.
        with browser_context(on_stage=mark_stage) as (_, page):
            resume_text = _get_resume(page, resume_id, on_stage=mark_stage)
            for page_num in range(config.PAGES_PER_RUN):
                if _observe_stop() or halt:
                    break
                mark_stage("search_start")
                search_diagnostic = {"event": "search_page_diagnostic", "run_id": run_id,
                                     "page_num": page_num}
                try:
                    vacs = hh_web.search_page(page, text=config.SEARCH_QUERY, area=config.AREA,
                        salary_min=config.SALARY_MIN, work_format_codes=["REMOTE"], page_num=page_num,
                        extra_params=config.HH_SEARCH_PARAMS, diagnostics=search_diagnostic)
                except Exception:
                    search_diagnostic.setdefault("requested_path", "/search/vacancy")
                    search_diagnostic.setdefault("final_path", "[unknown]")
                    search_diagnostic.setdefault("redirect_observed", False)
                    search_diagnostic.setdefault("navigation_http_status", None)
                    search_diagnostic.setdefault("raw_card_count", None)
                    search_diagnostic.setdefault("parsed_vacancy_count", None)
                    search_diagnostic.setdefault("new_unique_vacancy_count", None)
                    search_diagnostic.setdefault("empty_results_marker", "unknown")
                    search_diagnostic.setdefault("next_page_marker", "unknown")
                    search_diagnostic.setdefault("login_marker_present", "unknown")
                    search_diagnostic.setdefault("captcha_marker_present", "unknown")
                    search_diagnostic.setdefault("hh_error_marker_present", "unknown")
                    search_diagnostic.setdefault("interstitial_marker", "unknown")
                    search_diagnostic.setdefault("outcome", "search_page_exception")
                    _log(search_diagnostic)
                    raise
                new_ids = {str(vac.get("id", "")) for vac in vacs
                           if str(vac.get("id", "")).isdigit()
                           and str(vac.get("id", "")) not in seen_this_run}
                search_diagnostic.setdefault("requested_path", "/search/vacancy")
                search_diagnostic.setdefault("final_path", "[unknown]")
                search_diagnostic.setdefault("redirect_observed", False)
                search_diagnostic.setdefault("navigation_http_status", None)
                search_diagnostic.setdefault("raw_card_count", None)
                search_diagnostic.setdefault("parsed_vacancy_count", len(vacs))
                search_diagnostic["new_unique_vacancy_count"] = len(new_ids)
                search_diagnostic.setdefault("empty_results_marker", "unknown")
                search_diagnostic.setdefault("next_page_marker", "unknown")
                search_diagnostic.setdefault("login_marker_present", "unknown")
                search_diagnostic.setdefault("captcha_marker_present", "unknown")
                search_diagnostic.setdefault("hh_error_marker_present", "unknown")
                search_diagnostic.setdefault("interstitial_marker", "unknown")
                search_diagnostic.setdefault(
                    "outcome", "vacancies_parsed" if vacs else "empty_unclassified")
                _log(search_diagnostic)
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
                    current_vacancy_id = vid
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
                            if result["remote_status"] == "ineligible":
                                counts["hard_rejected"] += 1
                                entry.update(decision="rejected_remote", reason=result["remote_reason"])
                            elif result["remote_status"] == "unclear":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review", reason=result["remote_reason"])
                            elif result["score"] < config.SCORE_THRESHOLD:
                                counts["score_rejected"] += 1
                                entry["decision"] = "rejected_score"
                            elif result["role_alignment"] != "aligned":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review",
                                    reason_code="role_" + result["role_alignment"],
                                    reason=result["role_alignment_reason"])
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
                                        history_store.record_safety(
                                            config.APPLICATION_SAFETY_FILE, vid, "submission_unknown")
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
                                        history_store.record_safety(
                                            config.APPLICATION_SAFETY_FILE, vid, status)
                                        blocked_vacancies[vid] = status
                                    # Ambiguous submission is local to this vacancy. The
                                    # durable safety ledger prevents a blind repeat while
                                    # the run remains free to process other vacancies.
                                    if counts["applied_confirmed"] >= config.TARGET_APPLICATIONS_PER_RUN:
                                        halt, stop_reason = True, "target_reached"
                    except LLMStopped:
                        halt, run_status, stop_reason = True, "STOPPED", "user_stop"
                        break
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
    except LLMStopped:
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
        final = {"event": "run_finished", "run_id": run_id, "mode": mode, "status": run_status,
                 "stop_reason": stop_reason, "counts": counts}
        if (stop_reason == "user_stop" and isinstance(_active_stop_attempt_id, str)
                and _STOP_ATTEMPT_RE.fullmatch(_active_stop_attempt_id)):
            final["stop_attempt_id"] = _active_stop_attempt_id
        _log(final)
        try:
            records = _read_live_history(config.DECISIONS_LOG)
            history_store.compact_history(config.DECISIONS_LOG, records)
        except (OSError, ValueError, RuntimeError, LiveHistoryCorrupt):
            # A future LIVE preflight still fails closed; never rewrite uncertain data.
            pass
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
        _active_stop_attempt_id = None
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 1 if run_status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
