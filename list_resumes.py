"""Собирает список твоих резюме с hh.ru в state/resumes.json (для выбора в дашборде).

Запускается дашбордом по кнопке «Обновить список резюме». Нужна сохранённая сессия.
"""
import json
import sys
import time

import config
from browser import browser_context, has_session
from resume_metadata import write_resume_metadata


def main() -> int:
    if not has_session():
        print("Нет сессии — сначала войди в hh.ru", file=sys.stderr)
        return 1

    out: list[dict] = []
    with browser_context() as (ctx, page):
        # /applicant/resumes редиректит на профиль — резюме всё равно там.
        page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
        time.sleep(4)

        # Заголовок резюме — текст; ссылку /resume/<hash> ищем рядом (closest/внутри).
        raw = page.eval_on_selector_all(
            '[data-qa="resume-title"]',
            """els => els.map(e => {
                const a = e.closest('a[href*="/resume/"]')
                       || e.querySelector('a[href*="/resume/"]')
                       || (e.matches('a[href*="/resume/"]') ? e : null);
                return { title: (e.innerText||'').trim(), href: a ? a.getAttribute('href') : '' };
            })""",
        )
        import re
        seen = set()
        for i, item in enumerate(raw):
            title = item.get("title", "").strip()
            href = item.get("href", "")
            rid = ""
            if href:
                m = re.search(r"/resume/([0-9a-f]+)", href)
                rid = m.group(1) if m else ""
            if not rid or not title or rid in seen:
                continue
            seen.add(rid)
            out.append({"resume_id": rid, "url": f"https://hh.ru/resume/{rid}", "title": title})

    write_resume_metadata(out)
    print(f"Резюме найдено: {len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
