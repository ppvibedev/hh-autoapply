"""Safe, local metadata lookup for the resume selected by the current run."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import config
from privacy import sanitize_candidate_text


def _resume_id(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r"/resume/([0-9a-fA-F]+)/?", urlparse(url).path)
    return match.group(1).lower() if match else None


def selected_resume_title(resume_id: str, path: str | None = None) -> str | None:
    """Return one safe, unambiguous title; never infer it from resume contents."""
    if not re.fullmatch(r"[0-9a-f]+", resume_id or "", re.I):
        return None
    try:
        data = json.loads(Path(path or config.RESUMES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    titles = []
    for item in data:
        if not isinstance(item, dict) or _resume_id(item.get("url")) != resume_id.lower():
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        try:
            title = sanitize_candidate_text(title).strip()
        except ValueError:
            return None
        titles.append(title)
    return titles[0] if len(titles) == 1 else None
