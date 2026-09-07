"""Точка входа автопилота. По умолчанию — SHADOW (без реальных откликов).

Проход: поиск → дедуп → HARD-фильтры → скоринг → решение.
В shadow-режиме реальный POST /negotiations НЕ вызывается: пишем в лог
«откликнулся бы». Смотришь state/decisions.jsonl, калибруешь decide.py,
только потом ставишь DRY_RUN=0.
"""
import json
import os
import sys
from datetime import datetime, timezone

import config
import decide
from hh_client import HHClient


def _load_seen() -> set[str]:
    if os.path.exists(config.SEEN_FILE):
        with open(config.SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _save_seen(seen: set[str]) -> None:
    os.makedirs(os.path.dirname(config.SEEN_FILE), exist_ok=True)
    with open(config.SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)


def _log(entry: dict) -> None:
    os.makedirs(os.path.dirname(config.DECISIONS_LOG), exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with open(config.DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    if not config.ACCESS_TOKEN:
        print("Нет HH_ACCESS_TOKEN — заполни .env (см. .env.example).", file=sys.stderr)
        return 1

    mode = "SHADOW (без реальных откликов)" if config.DRY_RUN else "БОЕВОЙ (реальные отклики!)"
    print(f"Режим: {mode} · порог {config.SCORE_THRESHOLD} · лимит {config.DAILY_LIMIT}")

    hh = HHClient(config.ACCESS_TOKEN)
    seen = _load_seen()
    sent = skipped = rejected = 0

    res = hh.search(
        text=config.SEARCH_TEXT, area=config.AREA,
        salary_min=config.SALARY_MIN, work_format_codes=config.WORK_FORMAT_CODES,
    )
    found = res.get("found", 0)
    items = res.get("items", [])
    print(f"Найдено {found}, разбираем {len(items)} с первой страницы.\n")

    for vac in items:
        vid = vac["id"]
        title = vac.get("name", "")
        employer = (vac.get("employer") or {}).get("name", "")

        if vid in seen:
            skipped += 1
            continue

        # 1) HARD-фильтры
        reason = decide.hard_filters(vac)
        if reason:
            rejected += 1
            seen.add(vid)
            _log({"decision": "reject", "vid": vid, "title": title,
                  "employer": employer, "reason": reason})
            print(f"  отбой  · {title[:50]:50} · {reason}")
            continue

        # 2) Скоринг (нужно полное описание)
        full = hh.vacancy(vid)
        sc, why = decide.score(full)
        seen.add(vid)

        if sc < config.SCORE_THRESHOLD:
            _log({"decision": "below_threshold", "vid": vid, "title": title,
                  "employer": employer, "score": sc, "why": why})
            print(f"  мимо   · {title[:50]:50} · скор {sc} · {why}")
            continue

        # 3) Решение откликнуться
        if sent >= config.DAILY_LIMIT:
            print("  ! дневной лимит исчерпан — стоп")
            break

        letter = decide.cover_letter(full, "")  # API-путь: резюме не тянем

        if config.DRY_RUN:
            _log({"decision": "would_apply", "vid": vid, "title": title,
                  "employer": employer, "score": sc, "why": why, "letter": letter})
            print(f"  [БЫ]   · {title[:50]:50} · скор {sc}  ← откликнулся бы")
        else:
            code = hh.negotiate(vacancy_id=vid, resume_id=config.RESUME_ID, message=letter)
            _log({"decision": "applied", "vid": vid, "title": title,
                  "employer": employer, "score": sc, "http": code})
            print(f"  ОТКЛИК · {title[:50]:50} · скор {sc}  (HTTP {code})")
        sent += 1

    _save_seen(seen)
    verb = "откликнулся бы" if config.DRY_RUN else "отправлено откликов"
    print(f"\nИтог: {verb} {sent} · мимо/отбой {rejected} · дубли {skipped}")
    print(f"Лог решений: {config.DECISIONS_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
