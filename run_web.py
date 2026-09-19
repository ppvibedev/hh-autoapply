"""SAFE-прогон: вакансии → выбранное резюме/контекст → matching → письмо → история."""
import json
import os
import re
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import config
import decide
import llm
from llm_errors import LLMUnavailable
import hh_web
from browser import browser_context, has_session
from candidate_context import load_candidate_context
from privacy import sanitize_candidate_text, sanitize_record, sanitize_exception_message

_stop_requested = False


def _request_stop(*_):
    global _stop_requested
    _stop_requested = True


def _error_code(error):
    # Не публиковать произвольное сообщение исключения: оно может содержать ключ.
    known = {"technical_secret_detected", "select_specific_hh_resume", "resume_identity_mismatch",
             "resume_identity_not_verified", "resume_content_not_found", "resume_content_empty",
             "hh_session_missing", "invalid_run_settings", "matching_invalid_score",
             "matching_invalid_json", "matching_invalid_schema", "matching_inconsistent_decision",
             "matching_invalid_remote_status", "matching_llm_unavailable", "letter_llm_unavailable",
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
        on_stage("resume_cache_read")
    if hh_web.resume_id_from_url(config.RESUME_URL) != resume_id:
        raise ValueError("resume_identity_mismatch")
    folder = Path(config.RESUME_CACHE_DIR)
    path = folder / f"{resume_id}.txt"
    if folder.is_symlink() or path.is_symlink():
        raise ValueError("resume_cache_symlink_not_allowed")
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return sanitize_candidate_text(text, (config.LLM_API_KEY, config.ACCESS_TOKEN))
    text = hh_web.load_resume_text(page, config.RESUME_URL, on_stage=on_stage)
    if not text.strip():
        raise ValueError("resume_content_empty")
    if on_stage:
        on_stage("resume_cache_write")
    folder.mkdir(parents=True, exist_ok=True)
    text = sanitize_candidate_text(text, (config.LLM_API_KEY, config.ACCESS_TOKEN))
    path.write_text(text, encoding="utf-8")
    return text


def main():
    global _stop_requested
    _stop_requested = False
    llm.reset_runtime()
    mode = "SAFE" if config.DRY_RUN else "LIVE"
    run_id = uuid4().hex
    counts = {"scanned": 0, "hard_rejected": 0, "score_rejected": 0,
              "safe_ready": 0, "manual_review": 0, "applied_confirmed": 0,
              "apply_failed": 0, "submission_unknown": 0, "already_applied": 0, "apply_attempted": 0}
    stop_reason, run_status = "pages_exhausted", "COMPLETED"
    pid_path = Path(config.RUN_PID_FILE)
    previous_handlers = {}
    blocked_vacancies = {}
    halt = False
    stage = "run_setup"
    def mark_stage(value):
        nonlocal stage
        stage = value

    try:
        llm.start_runtime()
        resume_id = hh_web.resume_id_from_url(config.RESUME_URL)
        if not has_session():
            raise ValueError("hh_session_missing")
        if not 0 <= config.SCORE_THRESHOLD <= 100 or config.PAGES_PER_RUN < 1:
            raise ValueError("invalid_run_settings")
        if config.PAUSE_SECONDS < 0 or config.TARGET_APPLICATIONS_PER_RUN < 1:
            raise ValueError("invalid_run_settings")
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

        pid_path.parent.mkdir(parents=True, exist_ok=True)
        # Один процесс; не перезаписывать файл чужого активного прогона.
        with pid_path.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, _request_stop)
        _log({"event": "run_started", "run_id": run_id, "mode": mode,
              "selected_resume_id": resume_id, "threshold": config.SCORE_THRESHOLD})
        print(mode + (": поиск и анализ; отправка заблокирована." if mode == "SAFE" else ": реальные отклики разрешены для этого запуска."), flush=True)
        seen_this_run = set()  # SAFE не использует и не меняет старый seen.json.
        with browser_context(on_stage=mark_stage) as (_, page):
            resume_text = _get_resume(page, resume_id, on_stage=mark_stage)
            for page_num in range(config.PAGES_PER_RUN):
                if _stop_requested or halt:
                    break
                mark_stage("search_start")
                vacs = hh_web.search_page(page, text=config.SEARCH_QUERY, area=config.AREA,
                    salary_min=config.SALARY_MIN, work_format_codes=["REMOTE"], page_num=page_num,
                    extra_params=config.HH_SEARCH_PARAMS)
                if not vacs:
                    stop_reason = "no_results_or_page_not_recognized"
                    break
                for vac in vacs:
                    if _stop_requested:
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
                            if _stop_requested:
                                break
                            mark_stage("matching")
                            result = decide.score(full, resume_text, materials, instructions)
                            entry.update(match_score=result["score"], score=result["score"],
                                matched=result["matched"], missing=result["missing"], risks=result["risks"],
                                reason=result["reason"], match_decision=result["decision"],
                                remote_status=result["remote_status"], remote_reason=result["remote_reason"])
                            if result["remote_status"] == "ineligible":
                                counts["hard_rejected"] += 1
                                entry.update(decision="rejected_remote", reason=result["remote_reason"])
                            elif result["remote_status"] == "unclear":
                                counts["manual_review"] += 1
                                entry.update(decision="needs_manual_review", reason=result["remote_reason"])
                            elif result["score"] < config.SCORE_THRESHOLD:
                                counts["score_rejected"] += 1
                                entry["decision"] = "rejected_score"
                            elif not _stop_requested:
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
                                        stopped=lambda: _stop_requested, before_submit=before_submit)
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
        if _stop_requested:
            run_status, stop_reason = "STOPPED", "user_stop"
    except LLMUnavailable as error:
        run_status, stop_reason = "STOPPED", "llm_infrastructure_unavailable"
        _log(dict(event="run_error", run_id=run_id, reason_code=stop_reason,
                  **_exception_details(error, stage)))
    except Exception as error:
        run_status, stop_reason = "FAILED", _error_code(error)
        diagnostic = dict(event="run_error", run_id=run_id, **_exception_details(error, stage))
        _log(diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
        print("Прогон остановлен:", stop_reason, flush=True)
    finally:
        _log({"event": "run_finished", "run_id": run_id, "mode": mode, "status": run_status,
              "stop_reason": stop_reason, "counts": counts})
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 1 if run_status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
