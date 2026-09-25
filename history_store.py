"""Atomic operational journal retention and compact exact-vacancy safety state."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


SAFETY_STATUSES = frozenset({"applied_confirmed", "already_applied", "submission_unknown"})
_VACANCY_ID = re.compile(r"[1-9][0-9]*")


class SafetyStateCorrupt(ValueError):
    pass


@contextmanager
def _locked(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
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


def append_jsonl(path, record):
    """Append one complete physical JSONL record under the retention lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, ensure_ascii=False) + "\n"
    with _locked(path):
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())


def read_tail(path, identity=None, offset=0):
    """Read an append-only byte range while excluding append/atomic replacement races."""
    path = Path(path)
    with _locked(path):
        stat = path.stat()
        current_identity = (stat.st_dev, stat.st_ino)
        rebuilt = identity != current_identity or stat.st_size < offset
        if rebuilt:
            offset = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
        return current_identity, offset + len(chunk), chunk, rebuilt


def _load_safety_unlocked(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise SafetyStateCorrupt("application_safety_state_corrupt") from error
    vacancies = raw.get("vacancies") if isinstance(raw, dict) and raw.get("version") == 1 else None
    if not isinstance(vacancies, dict):
        raise SafetyStateCorrupt("application_safety_state_corrupt")
    result = {}
    for vacancy_id, status in vacancies.items():
        if not isinstance(vacancy_id, str) or not _VACANCY_ID.fullmatch(vacancy_id):
            raise SafetyStateCorrupt("application_safety_state_corrupt")
        if status not in SAFETY_STATUSES:
            raise SafetyStateCorrupt("application_safety_state_corrupt")
        result[vacancy_id] = status
    return result


def load_safety(path):
    with _locked(path):
        return _load_safety_unlocked(path)


def _save_safety_unlocked(path, vacancies):
    payload = {"version": 1, "vacancies": dict(sorted(vacancies.items()))}
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_safety(path, vacancy_id, status):
    vacancy_id = str(vacancy_id)
    if not _VACANCY_ID.fullmatch(vacancy_id) or status not in SAFETY_STATUSES:
        raise ValueError("invalid_application_safety_record")
    with _locked(path):
        vacancies = _load_safety_unlocked(path)
        vacancies[vacancy_id] = status
        _save_safety_unlocked(path, vacancies)


def merge_safety(path, records):
    """Migrate only exact vacancy IDs and safety statuses; never candidate data."""
    with _locked(path):
        vacancies = _load_safety_unlocked(path)
        changed = False
        for record in records:
            vacancy_id = str(record.get("vacancy_id", ""))
            status = record.get("application_status")
            if _VACANCY_ID.fullmatch(vacancy_id) and status in SAFETY_STATUSES:
                if vacancies.get(vacancy_id) != status:
                    vacancies[vacancy_id] = status
                    changed = True
        if changed:
            _save_safety_unlocked(path, vacancies)
        return vacancies


def retained_records(records, completed_limit=5, active_run_id=None):
    """Keep the newest completed runs and an explicitly active current run."""
    completed = []
    for record in records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if record.get("event") == "run_finished" and run_id not in completed:
            completed.append(run_id)
    keep = set(completed[-completed_limit:])
    if isinstance(active_run_id, str) and active_run_id:
        keep.add(active_run_id)
    return [record for record in records if record.get("run_id") in keep]


def compact_history(path, records, completed_limit=5, active_run_id=None):
    """Atomically replace a strictly parsed journal with its retained run window."""
    path = Path(path)
    retained = retained_records(records, completed_limit, active_run_id)
    serialized = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in retained)
    with _locked(path):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        current_records = []
        for line in current.split("\n"):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("live_history_corrupt")
                current_records.append(value)
        if current_records != list(records):
            raise RuntimeError("history_changed_during_compaction")
        _atomic_text(path, serialized)
    return retained
