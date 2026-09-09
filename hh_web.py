"""Поиск и отклик через сайт hh.ru (Playwright), без официального API.

Возвращает вакансии в том же виде, что и API-клиент, чтобы decide.py работал
без изменений: {id, name, employer:{name}, salary:{from,to}, url, description}.

ВНИМАНИЕ: селекторы hh.ru меняются. Все они собраны в SEL ниже — если что-то
перестало находиться, правь здесь. Проверять на живой залогиненной сессии.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlencode, urlparse

import config

# --- Селекторы (единственное место правки при смене вёрстки hh.ru) ---
SEL = {
    "card": '[data-qa="vacancy-serp__vacancy"], [data-qa="vacancy-serp__vacancy_premium"]',
    "card_title": '[data-qa="serp-item__title"]',
    "card_employer": '[data-qa="vacancy-serp__vacancy-employer"]',
    "card_salary": '[data-qa="vacancy-serp__vacancy-compensation"]',
    # страница вакансии
    "vac_title": '[data-qa="vacancy-title"]',
    "vac_desc": '[data-qa="vacancy-description"]',
    "vac_salary": '[data-qa="vacancy-salary"]',
    "vac_employer": '[data-qa="vacancy-company-name"]',
    "respond_btn": '[data-qa="vacancy-response-link-top"]',
    # попап отклика
    "cookie_accept": '[data-qa="cookies-policy-informer-accept"]',
    "add_cover": '[data-qa="add-cover-letter"]',              # раскрывает поле письма
    "letter_area": '[data-qa="vacancy-response-popup-form-letter-input"]',
    "submit_btn": '[data-qa="vacancy-response-submit-popup"]',
}


def _pause():
    time.sleep(config.PAUSE_SECONDS)


def _parse_salary(text: str) -> dict | None:
    """Из «от 250 000 ₽», «250 000 – 300 000 ₽» вытащить {from,to}."""
    if not text:
        return None
    # Убираем разделители тысяч (обычный и неразрывный пробелы), берём числа.
    clean = re.sub(r"[\s  ]", "", text)
    nums = [int(n) for n in re.findall(r"\d{4,}", clean)]
    if not nums:
        return None
    lo = nums[0]
    hi = nums[1] if len(nums) > 1 else None
    # Only positively identified rubles may be compared with the RUB minimum.
    currency = "RUR" if re.search(r"₽|\b(?:RUB|RUR|руб(?:лей|ля|ль)?\.?)\b", text, re.I) else None
    if len(nums) == 1 and re.search(r"\bдо\b", text, re.I):
        lo, hi = None, lo
    return {"from": lo, "to": hi, "currency": currency}


def _vac_id(url: str) -> str:
    m = re.search(r"/vacancy/(\d+)", url or "")
    return m.group(1) if m else (url or "")


def search_page(page, *, text: str, area: str, salary_min: int,
                work_format_codes: list[str], page_num: int,
                extra_params: dict | None = None) -> list[dict]:
    """Открывает страницу выдачи и парсит карточки вакансий.

    extra_params — родные фильтры hh (experience/employment/schedule/…).
    """
    params: dict = {"text": text, "page": page_num}
    if area:
        params["area"] = area
    # search_field не ограничиваем — чтобы hh находил синонимы и англ/рус варианты.
    # Точность обеспечивает ИИ-оценка названия в decide.score.
    if salary_min:
        params["salary"] = salary_min       # сам порог зарплаты (как в hh)
        params["currency"] = "RUR"
    if work_format_codes:
        params["work_format"] = work_format_codes  # список → повторяющийся параметр
    for k, v in (extra_params or {}).items():
        if v:
            params[k] = v
    url = "https://hh.ru/search/vacancy?" + urlencode(params, doseq=True)
    page.goto(url, wait_until="domcontentloaded")
    _pause()

    cards = page.locator(SEL["card"])
    out: list[dict] = []
    for i in range(cards.count()):
        c = cards.nth(i)
        title_el = c.locator(SEL["card_title"]).first
        if not title_el.count():
            continue
        href = title_el.get_attribute("href") or ""
        name = (title_el.inner_text() or "").strip()
        emp = c.locator(SEL["card_employer"]).first
        employer = (emp.inner_text().strip() if emp.count() else "")
        sal_el = c.locator(SEL["card_salary"]).first
        salary = _parse_salary(sal_el.inner_text()) if sal_el.count() else None
        out.append({
            "id": _vac_id(href),
            "name": name,
            "employer": {"name": employer},
            "salary": salary,
            "url": href.split("?")[0],
            "description": "",  # заполним при открытии карточки
        })
    return out


def load_full(page, vac: dict) -> dict:
    """Открывает страницу вакансии, дозаполняет описание/зарплату."""
    page.goto(vac["url"], wait_until="domcontentloaded")
    _pause()
    desc = page.locator(SEL["vac_desc"]).first
    if not desc.count():
        raise ValueError("vacancy_description_not_found")
    vac["description"] = (desc.inner_text() or "").strip()
    if not vac["description"]:
        raise ValueError("vacancy_description_empty")
    # Отдельная строка условий; отсутствие не подменяем native-фильтром.
    fmt = page.locator('[data-qa="vacancy-view-work-format"]').first
    vac["work_format_text"] = fmt.inner_text().strip() if fmt.count() else ""
    if not vac["work_format_text"]:
        body = page.locator("body").inner_text()
        match = re.search(r"Формат работы:\s*([^\n]+)", body, re.I)
        if match:
            vac["work_format_text"] = match.group(1).strip()
    if not vac.get("salary"):
        sal = page.locator(SEL["vac_salary"]).first
        if sal.count():
            vac["salary"] = _parse_salary(sal.inner_text())
    return vac


def resume_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("hh.ru", "www.hh.ru") or parsed.username or parsed.password:
        raise ValueError("select_specific_hh_resume")
    match = re.fullmatch(r"/resume/([0-9a-fA-F]+)/*", parsed.path)
    if not match:
        raise ValueError("select_specific_hh_resume")
    return match.group(1).lower()


def load_resume_text(page, direct_url: str = "", *, on_stage=None) -> str:
    """Только явно выбранное резюме, подтверждённое URL и заголовком страницы."""
    rid = resume_id_from_url(direct_url)
    if on_stage:
        on_stage("resume_open")
    page.goto(f"https://hh.ru/resume/{rid}", wait_until="domcontentloaded")
    _pause()
    if resume_id_from_url(page.url) != rid:
        raise ValueError("resume_identity_mismatch")
    if on_stage:
        on_stage("resume_extract")
    title = page.locator('[data-qa="resume-block-title-position"], [data-qa="resume-title"]').first
    if not title.count():
        raise ValueError("resume_identity_not_verified")
    # Контактные блоки исключаем на клоне DOM до получения текста.
    main = page.locator('[data-qa="resume"], main').first
    if not main.count():
        raise ValueError("resume_content_not_found")
    text = main.evaluate("""el => {
        const copy = el.cloneNode(true);
        copy.querySelectorAll('[data-qa*="contact"], [data-qa*="phone"], [data-qa*="email"], [data-qa*="address"], a[href^="mailto:"], a[href^="tel:"], script, style')
            .forEach(node => node.remove());
        copy.querySelectorAll('div, p, section, li, br, h1, h2, h3').forEach(node => node.append('\\n'));
        return copy.textContent || '';
    }""")
    from privacy import sanitize_candidate_text
    text = sanitize_candidate_text(text, (config.LLM_API_KEY, config.ACCESS_TOKEN)).strip()
    if not text:
        raise ValueError("resume_content_empty")
    return text


def apply(page, vac: dict, letter: str, *, dry_run: bool, resume_url: str = "", **kwargs):
    """SAFE barrier remains before all page actions; guarded LIVE uses explicit resume."""
    if config.DRY_RUN or dry_run:
        return "safe_not_sent"
    if not kwargs.get("before_submit"):
        return "needs_manual_review"
    from application import apply as apply_form
    return apply_form(page, vac, letter, resume_id=resume_id_from_url(resume_url),
                      dry_run=dry_run, **kwargs)
