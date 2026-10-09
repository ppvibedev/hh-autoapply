"""Private local application contacts used without involving an LLM."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path


STORAGE_PATH = Path(__file__).resolve().parent / "private" / "application_profile.json"
_TELEGRAM_RE = re.compile(r"@[A-Za-z0-9_]{5,32}")
_LOCK = threading.RLock()


class ApplicationProfileError(ValueError):
    pass


def normalize_telegram(value: object) -> str:
    if not isinstance(value, str):
        raise ApplicationProfileError("telegram_invalid")
    value = value.strip()
    if value and _TELEGRAM_RE.fullmatch(value) is None:
        raise ApplicationProfileError("telegram_invalid")
    return value


def _safe_path() -> Path:
    path = Path(STORAGE_PATH)
    if path.is_symlink() or path.parent.is_symlink():
        raise ApplicationProfileError("application_profile_invalid")
    return path


def load_profile() -> dict:
    path = _safe_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise ApplicationProfileError("application_profile_invalid") from None
    if not isinstance(data, dict):
        raise ApplicationProfileError("application_profile_invalid")
    return data


def telegram() -> str:
    profile = load_profile()
    contacts = profile.get("contacts", {})
    if contacts is None:
        contacts = {}
    if not isinstance(contacts, dict):
        raise ApplicationProfileError("application_profile_invalid")
    return normalize_telegram(contacts.get("telegram", ""))


def save_telegram(value: object) -> str:
    """Update one contact atomically while preserving every other profile field."""
    value = normalize_telegram(value)
    with _LOCK:
        profile = load_profile()
        contacts = profile.get("contacts", {})
        if contacts is None:
            contacts = {}
        if not isinstance(contacts, dict):
            raise ApplicationProfileError("application_profile_invalid")
        profile["contacts"] = dict(contacts, telegram=value)
        path = _safe_path()
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".application-profile-", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(profile, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise ApplicationProfileError("application_profile_write_failed") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return value
