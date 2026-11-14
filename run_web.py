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
import anti_captcha
import decide
import llm
from llm_errors import LLMUnavailable, LLMStopped
import history_store
from resume_metadata import selected_resume_title
import hh_web
from browser import browser_context, has_session
from candidate_context import load_candidate_context
from privacy import PrivacyError, sanitize_candidate_text, sanitize_record, sanitize_exception_message

_stop_requested = False
_active_run_id = None
_active_stage = "run_setup"
_worker_stop_ack_logged = False
_worker_stop_observed_logged = False
_active_stop_attempt_id = None
_captcha_resume_requested = threading.Event()
_captcha_wait_active = threading.Event()
_STOP_ATTEMPT_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
LIVE_HISTORY_REREAD_DELAY_SECONDS = 0.02
CAPTCHA_MANUAL_SOLVE_TIMEOUT_SECONDS = config.CAPTCHA_REMINDER_SECONDS
CAPTCHA_POLL_INTERVAL_SECONDS = 1
CAPTCHA_IMAGE_APPEAR_SECONDS = 4
CAPTCHA_PREFLIGHT_REFRESH_LIMIT = 3
CAPTCHA_PAID_TASK_LIMIT = 3
CAPTCHA_HH_VERIFY_SECONDS = 10
CAPTCHA_HH_VERIFY_POLL_SECONDS = 0.5
WORKER_HEARTBEAT_SECONDS = 10
WORKER_OPERATION_STALL_SECONDS = 180
HH_NAVIGATION_MAX_ATTEMPTS = 3
HH_NAVIGATION_RETRY_DELAYS_SECONDS = (1.0, 2.0)
HH_NAVIGATION_RETRY_POLL_SECONDS = 0.1
_HH_TRANSIENT_NETWORK_MARKERS = {
    "net::ERR_INTERNET_DISCONNECTED": "internet_disconnected",
    "net::ERR_NAME_NOT_RESOLVED": "name_not_resolved",
    "net::ERR_NETWORK_IO_SUSPENDED": "network_io_suspended",
}


class HHNetworkUnavailable(RuntimeError):
    """Bounded safe navigation retries could not restore HH connectivity."""

    def __init__(self, *, operation, network_error_code, attempts):
        super().__init__("hh_network_unavailable")
        self.operation = operation
        self.network_error_code = network_error_code
        self.attempts = attempts


class _WorkerProgress:
    """Observe a synchronous worker; a stalled Playwright call is not killed here."""
    def __init__(self, run_id, telemetry, clock=None):
        self.run_id = run_id
        self.telemetry = telemetry
        self.clock = clock or time.perf_counter
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.operation = None
        self.stage = "run_setup"
        self.vacancy_id = None
        self.changed_at = self.clock()
        self.timeout_reported = False
        self.thread = threading.Thread(target=self._watch, name="worker-heartbeat", daemon=True)

    def start(self):
        self.thread.start()

    def mark(self, stage, vacancy_id=None):
        now = self.clock()
        with self.lock:
            previous = self.operation
            previous_vacancy_id = self.vacancy_id
            elapsed = max(0, round((now - self.changed_at) * 1000))
            self.operation = stage
            self.stage = stage
            self.vacancy_id = vacancy_id
            self.changed_at = now
            self.timeout_reported = False
        if previous is not None:
            self.telemetry({"event": "operation_finished", "run_id": self.run_id,
                            "operation": previous, "vacancy_context_id": previous_vacancy_id,
                            "elapsed_ms": elapsed})
        self.telemetry({"event": "operation_started", "run_id": self.run_id,
                        "operation": stage, "vacancy_context_id": vacancy_id})

    def tick(self):
        with self.lock:
            self.changed_at = self.clock()
            self.timeout_reported = False

    def _watch(self):
        while not self.closed.wait(WORKER_HEARTBEAT_SECONDS):
            with self.lock:
                operation, stage, vacancy_id = self.operation, self.stage, self.vacancy_id
                elapsed = max(0, self.clock() - self.changed_at)
                timed_out = (operation is not None and stage != "captcha_wait"
                             and elapsed >= WORKER_OPERATION_STALL_SECONDS
                             and not self.timeout_reported)
                if timed_out:
                    self.timeout_reported = True
            self.telemetry({"event": "worker_heartbeat", "run_id": self.run_id,
                            "stage": stage, "vacancy_context_id": vacancy_id,
                            "operation": operation,
                            "state": ("paused_for_captcha" if stage == "captcha_wait"
                                      else "stalled" if self.timeout_reported
                                      else "waiting" if elapsed >= WORKER_HEARTBEAT_SECONDS
                                      else "working"),
                            "seconds_since_progress": round(elapsed)})
            if timed_out:
                self.telemetry({"event": "operation_timeout", "run_id": self.run_id,
                                "stage": stage, "vacancy_context_id": vacancy_id,
                                "operation": operation,
                                "timeout_seconds": WORKER_OPERATION_STALL_SECONDS,
                                "action": "cooperative_stop_requested"})
                _request_stop()  # Playwright sync calls cannot safely be killed from this thread.

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.5)


def _navigation_error_code(error):
    """Recognize only proven transient failures from a wrapped safe navigation."""
    message = str(error)
    for marker, code in _HH_TRANSIENT_NETWORK_MARKERS.items():
        if marker in message:
            return code
    error_type = type(error)
    if (error_type.__name__ == "TimeoutError"
            and error_type.__module__.startswith("playwright.")
            and "Page.goto" in message):
        return "navigation_timeout"
    return None


def _sleep_with_stop(delay_seconds, *, stopped, sleep=None):
    """Wait in short slices so dashboard Stop is observed between retries."""
    sleep = sleep or time.sleep
    remaining = max(0.0, float(delay_seconds))
    while remaining > 0:
        if stopped():
            raise LLMStopped()
        interval = min(HH_NAVIGATION_RETRY_POLL_SECONDS, remaining)
        sleep(interval)
        remaining = max(0.0, remaining - interval)
    if stopped():
        raise LLMStopped()


def _safe_navigation(call, *, operation, run_id, stopped=None, telemetry=None,
                     vacancy_id=None, attempts=HH_NAVIGATION_MAX_ATTEMPTS,
                     delays=HH_NAVIGATION_RETRY_DELAYS_SECONDS, sleep=None):
    """Retry only search/detail navigation; never application or submit actions."""
    stopped = stopped or _observe_stop
    telemetry = telemetry or _log
    for attempt in range(1, attempts + 1):
        if stopped():
            raise LLMStopped()
        try:
            return call()
        except Exception as error:
            network_error_code = _navigation_error_code(error)
            if network_error_code is None:
                raise
            common = {
                "run_id": run_id,
                "operation": operation,
                "attempt": attempt,
                "max_attempts": attempts,
                "network_error_code": network_error_code,
            }
            if vacancy_id is not None:
                common["vacancy_id"] = str(vacancy_id)
            if attempt >= attempts:
                telemetry(dict(event="hh_navigation_retries_exhausted", **common))
                raise HHNetworkUnavailable(
                    operation=operation,
                    network_error_code=network_error_code,
                    attempts=attempts,
                ) from error
            delay = delays[min(attempt - 1, len(delays) - 1)]
            telemetry(dict(event="hh_navigation_retry_wait_started",
                           delay_seconds=delay, **common))
            try:
                _sleep_with_stop(delay, stopped=stopped, sleep=sleep)
            except LLMStopped:
                telemetry(dict(event="hh_navigation_retry_wait_stopped", **common))
                raise
            telemetry(dict(event="hh_navigation_retry_wait_finished", **common))


def _wait_for_manual_captcha(page, *, operation, run_id, vacancy_id=None,
                             stopped=None, telemetry=None, on_stage=None, clock=None, sleep=None,
                             timeout_seconds=CAPTCHA_MANUAL_SOLVE_TIMEOUT_SECONDS,
                             poll_seconds=CAPTCHA_POLL_INTERVAL_SECONDS):
    """Hold the same operation until explicit user continuation and HH state check."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    stopped = stopped or _observe_stop
    challenge_structure = anti_captcha.capture_challenge_structure(page)
    if (challenge_structure.get("captcha_type") == "unknown" and
            challenge_structure.get("probe_status") == "ok" and
            config.ANTI_CAPTCHA_ENABLED and config.ANTI_CAPTCHA_API_KEY and
            hh_web.captcha_present(page)):
        # The CAPTCHA shell can appear before its image and field. This short
        # read-only wait never continues or retries a possible HH submission.
        for _ in range(int(CAPTCHA_IMAGE_APPEAR_SECONDS / 0.2)):
            if stopped():
                break
            sleep(0.2)
            challenge_structure = anti_captcha.capture_challenge_structure(page)
            if challenge_structure.get("captcha_type") != "unknown":
                break

    def emit(event, **safe_fields):
        if telemetry is None:
            return
        row = {"event": event, "run_id": run_id, "operation": operation,
               "timeout_seconds": timeout_seconds}
        if vacancy_id is not None:
            row["vacancy_id"] = str(vacancy_id)
        row["page_path"] = hh_web.safe_captcha_path(page)
        if event == "captcha_wait_started":
            row.update(challenge_structure)
        row.update(safe_fields)
        telemetry(row)

    def try_auto_image() -> bool:
        if not config.ANTI_CAPTCHA_ENABLED:
            emit("captcha_auto_skipped", reason="integration_disabled")
            return False
        if not config.ANTI_CAPTCHA_API_KEY:
            emit("captcha_auto_skipped", reason="api_key_missing")
            return False
        if challenge_structure.get("captcha_type") != "image_to_text":
            emit("captcha_auto_skipped", reason="unsupported_or_unknown_type")
            return False
        if stopped():
            return False
        client = anti_captcha.AntiCaptchaClient(config.ANTI_CAPTCHA_API_KEY)
        paid_tasks = 0
        answer_submitted = False
        preflight_refreshes = 0
        image_load_deadline = clock() + CAPTCHA_IMAGE_APPEAR_SECONDS
        image_wait_started = clock()
        def resolved_after_answer():
            if not answer_submitted or stopped() or not hh_web.captcha_resolution_verified(page, operation, vacancy_id):
                return False
            sleep(CAPTCHA_HH_VERIFY_POLL_SECONDS)
            if stopped() or not hh_web.captcha_resolution_verified(page, operation, vacancy_id):
                return False
            emit("captcha_hh_verification_result", verified=True, state="captcha_cleared")
            emit("captcha_auto_resolved")
            return True
        while paid_tasks < CAPTCHA_PAID_TASK_LIMIT:
            if stopped():
                return False
            if anti_captcha.capture_challenge_structure(page).get("captcha_type") != "image_to_text":
                if resolved_after_answer():
                    return True
                remaining = image_load_deadline - clock()
                if hh_web.captcha_present(page) and remaining > 0:
                    sleep(min(0.25, remaining))
                    continue
                emit("captcha_auto_fallback", reason="challenge_unrecognized")
                return False
            controls = anti_captcha.image_challenge_controls(page)
            if controls is None:
                if resolved_after_answer():
                    return True
                remaining = image_load_deadline - clock()
                if hh_web.captcha_present(page) and remaining > 0:
                    sleep(min(0.25, remaining))
                    continue
                emit("captcha_auto_fallback",
                     reason=anti_captcha.image_control_failure_reason(page))
                return False
            image = controls[0]
            if not anti_captcha.hh_image_loaded(image):
                remaining = image_load_deadline - clock()
                if remaining <= 0:
                    emit("captcha_auto_fallback", reason="image_not_loaded")
                    return False
                sleep(min(0.25, remaining))
                continue
            try:
                image_bytes = image.screenshot(timeout=3000)
            except Exception:
                emit("captcha_auto_fallback", reason="image_capture_failed")
                return False
            if not image_bytes or len(image_bytes) > 500_000:
                emit("captcha_auto_fallback", reason="image_invalid")
                return False
            if stopped():
                return False
            # Observe the loaded image twice without pressing HH's refresh control.
            sleep(0.5)
            if stopped():
                return False
            try:
                current = anti_captcha.image_challenge_controls(page)
                if (current is None or not anti_captcha.hh_image_loaded(current[0]) or
                        current[0].screenshot(timeout=3000) != image_bytes):
                    if current is not None and preflight_refreshes < CAPTCHA_PREFLIGHT_REFRESH_LIMIT:
                        preflight_refreshes += 1
                        emit("captcha_auto_retry", reason="preflight_challenge_changed",
                             preflight_refreshes=preflight_refreshes, paid_tasks=paid_tasks)
                        image_load_deadline = clock() + CAPTCHA_IMAGE_APPEAR_SECONDS
                        continue
                    emit("captcha_auto_fallback", reason="preflight_refresh_limit" if current is not None
                         else "challenge_controls_missing")
                    return False
            except Exception:
                emit("captcha_auto_fallback", reason="challenge_changed")
                return False
            balance = client.get_balance()
            if not balance.available or balance.amount_usd is None or balance.amount_usd <= 0:
                emit("captcha_auto_fallback", reason="balance_unavailable")
                return False
            if stopped():
                return False
            emit("captcha_image_ready", elapsed_ms=max(0, int((clock() - image_wait_started) * 1000)),
                 preflight_refreshes=preflight_refreshes, paid_tasks=paid_tasks)
            if not anti_captcha.IMAGE_TASKS.claim(image_bytes, config.ANTI_CAPTCHA_API_KEY):
                emit("captcha_auto_fallback", reason="duplicate_challenge")
                return False
            if stopped():
                return False
            # A failed transport may still have created a paid task, so count
            # the request before sending and never retry that same image.
            paid_tasks += 1
            emit("captcha_auto_started", attempt=paid_tasks)
            task_id, reason = client.create_image_task(image_bytes)
            if task_id is None:
                emit("captcha_auto_fallback", reason=reason)
                return False
            emit("captcha_auto_task_created", attempt=paid_tasks)
            result = client.wait_for_image_solution(
                task_id, max_wait_seconds=config.ANTI_CAPTCHA_MAX_WAIT_SECONDS,
                stopped=stopped, sleep=sleep)
            emit("captcha_auto_task_result", attempt=paid_tasks,
                 state=result.state if isinstance(result.state, str) and
                 result.state in {"ready", "error", "stopped", "timeout"}
                 else "unknown")
            if result.state != "ready":
                emit("captcha_auto_fallback", reason=result.reason or result.state)
                return False
            if stopped():
                return False
            try:
                current = anti_captcha.image_challenge_controls(page)
                if current is None or current[0].screenshot(timeout=3000) != image_bytes:
                    if current is not None and paid_tasks < CAPTCHA_PAID_TASK_LIMIT:
                        emit("captcha_auto_retry", reason="challenge_changed_before_answer",
                             paid_tasks=paid_tasks)
                        image_load_deadline = clock() + CAPTCHA_IMAGE_APPEAR_SECONDS
                        image_wait_started = clock()
                        continue
                    emit("captcha_auto_fallback", reason="challenge_changed")
                    return False
                current[1].fill(result.text, timeout=3000)
                if stopped():
                    return False
                current[2].click(timeout=3000)
                answer_submitted = True
            except Exception:
                emit("captcha_auto_fallback", reason="answer_application_failed")
                return False
            verified = 0
            changed = False
            explicit_rejection = False
            deadline = clock() + CAPTCHA_HH_VERIFY_SECONDS
            image_change_reported = False
            error_trace = []
            def remember_error_structure(phase, *, final=False):
                if len(error_trace) >= (8 if final else 7):
                    return
                snapshot = anti_captcha.hh_error_structure_snapshot(page)
                if final or not error_trace or error_trace[-1]["structure"] != snapshot:
                    error_trace.append({
                        "elapsed_ms": max(0, int((clock() - (deadline - CAPTCHA_HH_VERIFY_SECONDS)) * 1000)),
                        "phase": phase, "structure": snapshot})
            for tick in range(int(CAPTCHA_HH_VERIFY_SECONDS / CAPTCHA_HH_VERIFY_POLL_SECONDS) + 1):
                if stopped():
                    return False
                if tick:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        break
                    sleep(min(CAPTCHA_HH_VERIFY_POLL_SECONDS, remaining))
                    if stopped():
                        return False
                if hh_web.captcha_resolution_verified(page, operation, vacancy_id):
                    verified += 1
                    if verified >= 2:
                        emit("captcha_hh_verification_result", verified=True, state="captcha_cleared")
                        emit("captcha_auto_resolved")
                        return True
                else:
                    verified = 0
                explicit_rejection = (explicit_rejection or
                                      anti_captcha.hh_wrong_image_answer_visible(page))
                try:
                    next_controls = anti_captcha.image_challenge_controls(page)
                    changed = (next_controls is not None and
                               next_controls[0].screenshot(timeout=3000) != image_bytes)
                except Exception:
                    changed = False
                if changed and not image_change_reported:
                    emit("captcha_hh_image_changed", attempt=paid_tasks,
                         elapsed_ms=max(0, int((clock() - (deadline - CAPTCHA_HH_VERIFY_SECONDS)) * 1000)),
                         rejection_probe=anti_captcha.hh_rejection_probe(page))
                    image_change_reported = True
                if tick in (0, 1, 2, 4, 8, 12, 20) or (changed and explicit_rejection):
                    remember_error_structure("image_changed" if changed else "observed")
                if changed and explicit_rejection:
                    break
            state = ("explicit_rejected" if explicit_rejection else
                     "new_challenge" if changed else "unknown")
            remember_error_structure("final", final=True)
            emit("captcha_hh_verification_result", verified=False, state=state,
                 rejection_probe=anti_captcha.hh_rejection_probe(page))
            if not (changed and explicit_rejection) or paid_tasks >= CAPTCHA_PAID_TASK_LIMIT:
                emit("captcha_hh_error_structure", attempt=paid_tasks, samples=error_trace)
            if not (changed and explicit_rejection) or paid_tasks >= CAPTCHA_PAID_TASK_LIMIT:
                emit("captcha_auto_fallback", reason="hh_not_verified")
                return False
            emit("captcha_auto_retry", reason="hh_rejected_new_challenge",
                 paid_tasks=paid_tasks)
            image_load_deadline = clock() + CAPTCHA_IMAGE_APPEAR_SECONDS
            image_wait_started = clock()
        return False

    if on_stage is not None:
        on_stage("captcha_wait")
    _captcha_resume_requested.clear()
    _captcha_wait_active.set()
    emit("captcha_wait_started")
    started = clock()
    expiry_reported = False
    try:
        if try_auto_image():
            emit("captcha_wait_resolved")
            return
        while True:
            if stopped():
                emit("captcha_wait_stopped")
                raise LLMStopped()
            if _captcha_resume_requested.is_set():
                _captcha_resume_requested.clear()
                if hh_web.captcha_resolution_verified(page, operation, vacancy_id):
                    emit("captcha_wait_resolved")
                    return
                emit("captcha_resume_rejected")
            if not expiry_reported and clock() - started >= timeout_seconds:
                emit("captcha_wait_expired")
                expiry_reported = True
            sleep(poll_seconds)
    finally:
        _captcha_wait_active.clear()
        _captcha_resume_requested.clear()


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
                               and request.get("action") in ("probe", "stop", "captcha_continue"))
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
                    elif request["action"] == "captcha_continue":
                        if _captcha_wait_active.is_set():
                            _captcha_resume_requested.set()
                            connection.sendall(b'{"ok":true,"status":"captcha_continue_requested"}')
                        else:
                            connection.sendall(b'{"ok":false,"status":"captcha_not_waiting"}')
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
    if isinstance(error, PrivacyError):
        if error.privacy_source_field:
            details["privacy_source_field"] = error.privacy_source_field
        if error.privacy_rule_id:
            details["privacy_rule_id"] = error.privacy_rule_id
        if error.privacy_match_shape:
            details["privacy_match_shape"] = error.privacy_match_shape
    if isinstance(error, HHNetworkUnavailable):
        details.update(operation=error.operation,
                       network_error_code=error.network_error_code,
                       attempts=error.attempts)
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
    _captcha_resume_requested.clear()
    _captcha_wait_active.clear()
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
    progress = None
    consecutive_description_not_found = 0
    def mark_stage(value):
        nonlocal stage
        global _active_stage
        stage = value
        _active_stage = value
        if progress is not None:
            progress.mark(value, current_vacancy_id)

    def record_llm_timing(event):
        if progress is not None:
            progress.tick()
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
        progress = _WorkerProgress(run_id, _log)
        progress.start()
        print(mode + (": поиск и анализ; отправка заблокирована." if mode == "SAFE" else ": реальные отклики разрешены для этого запуска."), flush=True)
        seen_this_run = set()  # SAFE не использует и не меняет старый seen.json.
        with browser_context(on_stage=mark_stage) as (_, page):
            resume_text = _get_resume(page, resume_id, on_stage=mark_stage)
            for page_num in range(config.PAGES_PER_RUN):
                if _observe_stop() or halt:
                    break
                while True:
                    mark_stage("search_start")
                    search_diagnostic = {"event": "search_page_diagnostic", "run_id": run_id,
                                         "page_num": page_num}
                    try:
                        vacs = _safe_navigation(
                            lambda: hh_web.search_page(
                                page, text=config.SEARCH_QUERY, area=config.AREA,
                                salary_min=config.SALARY_MIN, work_format_codes=["REMOTE"],
                                page_num=page_num, extra_params=config.HH_SEARCH_PARAMS,
                                diagnostics=search_diagnostic),
                            operation="search", run_id=run_id, stopped=_observe_stop,
                            telemetry=_log)
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
                    if search_diagnostic.get("outcome") != "captcha_detected":
                        break
                    _wait_for_manual_captcha(
                        page, operation="search", run_id=run_id, stopped=_observe_stop,
                        telemetry=_log, on_stage=mark_stage)
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
                            while True:
                                mark_stage("vacancy_open")
                                detail_diagnostic = {}
                                try:
                                    full = _safe_navigation(
                                        lambda: hh_web.load_full(
                                            page, vac, diagnostics=detail_diagnostic,
                                            stopped=_observe_stop),
                                        operation="vacancy_detail", run_id=run_id,
                                        vacancy_id=vid, stopped=_observe_stop,
                                        telemetry=_log)
                                except Exception as error:
                                    if _error_code(error) != "vacancy_description_not_found":
                                        raise
                                    captcha = ((detail_diagnostic.get("page_state_markers") or {})
                                               .get("captcha_marker_present") is True)
                                    if not captcha:
                                        consecutive_description_not_found += 1
                                    detail_diagnostic.update(
                                        event="vacancy_detail_diagnostic",
                                        run_id=run_id,
                                        vacancy_id=vid,
                                        consecutive_description_not_found=(
                                            consecutive_description_not_found),
                                    )
                                    _log(detail_diagnostic)
                                    if not captcha:
                                        raise
                                    _wait_for_manual_captcha(
                                        page, operation="vacancy_detail", run_id=run_id,
                                        vacancy_id=vid, stopped=_observe_stop,
                                        telemetry=_log, on_stage=mark_stage)
                                    # Retry the exact same known vacancy URL. This covers both
                                    # HH auto-return and a CAPTCHA page that does not navigate back.
                                    continue
                                else:
                                    consecutive_description_not_found = 0
                                    break
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
                            elif result["score"] < config.SCORE_THRESHOLD:
                                counts["score_rejected"] += 1
                                entry["decision"] = "rejected_score"
                            elif result["remote_status"] == "unclear":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review", reason=result["remote_reason"])
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
                                    while True:
                                        outcome = hh_web.apply(page, full, entry["letter"], dry_run=False,
                                            resume_url=config.RESUME_URL, resume_text=resume_text,
                                            candidate_text=materials, instructions=instructions,
                                            stopped=_observe_stop, before_submit=before_submit,
                                            on_stage=mark_stage)
                                        captcha_reason = outcome.get("reason")
                                        if captcha_reason not in ("captcha_before_submit", "captcha_after_possible_submit"):
                                            break
                                        if captcha_reason == "captcha_after_possible_submit":
                                            history_store.record_safety(
                                                config.APPLICATION_SAFETY_FILE, vid, "submission_unknown")
                                            blocked_vacancies[vid] = "submission_unknown"
                                        _wait_for_manual_captcha(
                                            page, operation=("application_pre_submit" if captcha_reason == "captcha_before_submit"
                                                             else "application_post_submit"),
                                            run_id=run_id, vacancy_id=vid, stopped=_observe_stop,
                                            telemetry=_log, on_stage=mark_stage)
                                        if captcha_reason == "captcha_after_possible_submit":
                                            from application import _signal
                                            if (_signal(page, str(vid)) == "applied_confirmed" or
                                                    _signal(page, str(vid), before=True) == "already_applied"):
                                                outcome.update(application_status="applied_confirmed",
                                                               reason="HH explicit fresh success message",
                                                               confirmation="explicit_success_marker_same_vacancy")
                                            break  # Persistent unknown result: never retry this submit.
                                        mark_stage("application")
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
                    except HHNetworkUnavailable:
                        # The current vacancy was never opened and therefore was not
                        # processed. Do not turn a shared outage into a manual outcome.
                        counts["scanned"] = max(0, counts["scanned"] - 1)
                        raise
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
    except HHNetworkUnavailable as error:
        run_status, stop_reason = "STOPPED", "hh_network_unavailable"
        _log(dict(event="run_error", run_id=run_id, reason_code=stop_reason,
                  **_exception_details(error, stage)))
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
        if progress is not None:
            progress.close()
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
