"""Автопилот через браузер (Playwright). Только боевой режим (реальные отклики).

Проход: поиск по страницам → дедуп → HARD-фильтры → скоринг → отклик.
Все решения пишутся в state/decisions.jsonl.

Логин делается заранее: python login.py

Запуск: python run_web.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import config
import decide
import envfile
import hh_web
from browser import browser_context, has_session


def _current_limit() -> int:
    """Лимит откликов, читаемый из .env на ходу (без стейл-конфига)."""
    try:
        return int(envfile.read_env().get("DAILY_LIMIT", config.DAILY_LIMIT))
    except (TypeError, ValueError):
        return config.DAILY_LIMIT


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


def _get_resume(page) -> str:
    """Текст резюме: из кэша, иначе тянем с hh.ru и сохраняем."""
    if os.path.exists(config.RESUME_FILE):
        with open(config.RESUME_FILE, encoding="utf-8") as f:
            cached = f.read().strip()
        if cached:
            return cached
    text = hh_web.load_resume_text(page, config.RESUME_URL)
    if text:
        with open(config.RESUME_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def main() -> int:
    if not has_session():
        print("Нет сохранённой сессии. Сначала: python login.py", file=sys.stderr)
        return 1

    print(f"Режим: БОЕВОЙ (реальные отклики!) · запрос «{config.SEARCH_TEXT}» · регион {config.AREA} "
          f"· порог {config.SCORE_THRESHOLD} · лимит {_current_limit()}", flush=True)

    # Каждый прогон начинает ленту заново — чтобы не путать с прошлым запросом.
    os.makedirs("state", exist_ok=True)
    open(config.DECISIONS_LOG, "w", encoding="utf-8").close()

    # PID активного прогона — точная проверка «идёт ли» (без поиска по имени).
    with open(config.RUN_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(config.RUN_PID_FILE) and os.remove(config.RUN_PID_FILE))

    seen = _load_seen()
    sent = rejected = skipped = 0

    with browser_context() as (ctx, page):
        try:
            resume_text = _get_resume(page)
            print(f"Резюме: {len(resume_text)} символов" if resume_text else "! резюме не подтянулось")
        except Exception as e:  # noqa: BLE001
            resume_text = ""
            print(f"! резюме не подтянулось: {e}")

        for page_num in range(config.PAGES_PER_RUN):
            try:
                vacs = hh_web.search_page(
                    page, text=config.SEARCH_QUERY, area=config.AREA,
                    salary_min=config.SALARY_MIN,
                    work_format_codes=config.WORK_FORMAT_CODES,
                    page_num=page_num,
                    extra_params=config.HH_SEARCH_PARAMS,
                )
            except Exception as e:  # noqa: BLE001 — прогон не должен падать целиком
                print(f"  ! ошибка поиска стр.{page_num}: {e}")
                break

            if not vacs:
                print(f"Страница {page_num}: пусто — стоп.")
                break
            print(f"\nСтраница {page_num}: {len(vacs)} вакансий")

            for vac in vacs:
                vid = vac["id"]
                title = vac["name"]
                if not vid or vid in seen:
                    skipped += 1
                    continue

                reason = decide.hard_filters(vac)
                if reason:
                    rejected += 1
                    seen.add(vid)
                    _log({"decision": "reject", "vid": vid, "title": title,
                          "employer": vac["employer"]["name"], "reason": reason})
                    print(f"  отбой  · {title[:48]:48} · {reason}")
                    continue

                try:
                    full = hh_web.load_full(page, vac)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! не открыл {vid}: {e}")
                    continue

                sc, why = decide.score(full)
                seen.add(vid)

                if sc < config.SCORE_THRESHOLD:
                    _log({"decision": "below_threshold", "vid": vid, "title": title,
                          "score": sc, "why": why})
                    print(f"  мимо   · {title[:48]:48} · скор {sc}")
                    continue

                limit = _current_limit()
                if sent >= limit:
                    print(f"  ! лимит {limit} исчерпан (отправлено {sent}) — стоп")
                    _save_seen(seen)
                    _summary(sent, rejected, skipped)
                    return 0

                letter = decide.cover_letter(full, resume_text)
                status = hh_web.apply(page, full, letter, dry_run=config.DRY_RUN,
                                      resume_url=config.RESUME_URL)
                _log({"decision": status, "vid": vid, "title": title,
                      "employer": vac["employer"]["name"], "score": sc,
                      "why": why, "letter": letter, "url": vac["url"]})
                mark = "[БЫ]  " if status == "would_apply" else status.upper()[:6]
                print(f"  {mark} · {title[:48]:48} · скор {sc}")
                if status in ("would_apply", "applied", "applied_no_letter"):
                    sent += 1

    _save_seen(seen)
    _summary(sent, rejected, skipped)
    return 0


def _summary(sent: int, rejected: int, skipped: int) -> None:
    verb = "откликнулся бы" if config.DRY_RUN else "отправлено откликов"
    print(f"\nИтог: {verb} {sent} · отбой {rejected} · дубли {skipped}")
    print(f"Лог решений: {config.DECISIONS_LOG}")


if __name__ == "__main__":
    raise SystemExit(main())
