"""Поиск и отклик через сайт hh.ru (Playwright), без официального API.

Возвращает вакансии в том же виде, что и API-клиент, чтобы decide.py работал
без изменений: {id, name, employer:{name}, salary:{from,to}, url, description}.

ВНИМАНИЕ: селекторы hh.ru меняются. Все они собраны в SEL ниже — если что-то
перестало находиться, правь здесь. Проверять на живой залогиненной сессии.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlencode

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
    return {"from": lo, "to": hi}


def _vac_id(url: str) -> str:
    m = re.search(r"/vacancy/(\d+)", url or "")
    return m.group(1) if m else (url or "")


def search_page(page, *, text: str, area: str, salary_min: int,
                work_format_codes: list[str], page_num: int,
                extra_params: dict | None = None) -> list[dict]:
    """Открывает страницу выдачи и парсит карточки вакансий.

    extra_params — родные фильтры hh (experience/employment/schedule/…).
    """
    params: dict = {"text": text, "area": area, "page": page_num}
    # search_field не ограничиваем — чтобы hh находил синонимы и англ/рус варианты.
    # Точность обеспечивает ИИ-оценка названия в decide.score.
    if salary_min:
        params["salary"] = salary_min       # сам порог зарплаты (как в hh)
        params["only_with_salary"] = "true"
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
    if desc.count():
        vac["description"] = (desc.inner_text() or "").strip()
    if not vac.get("salary"):
        sal = page.locator(SEL["vac_salary"]).first
        if sal.count():
            vac["salary"] = _parse_salary(sal.inner_text())
    return vac


def load_resume_text(page, direct_url: str = "") -> str:
    """Подтягивает текст твоего резюме с hh.ru (по залогиненной сессии).

    Без direct_url открывает список резюме и берёт первое. Возвращает текст
    (обрезанный), который пойдёт в ИИ как бэкграунд для письма.
    """
    if direct_url:
        page.goto(direct_url, wait_until="domcontentloaded")
    else:
        page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
        _pause()
        link = page.locator('a[href*="/resume/"]').first
        href = link.get_attribute("href") if link.count() else None
        if href:
            full = href if href.startswith("http") else "https://hh.ru" + href
            page.goto(full, wait_until="domcontentloaded")
    _pause()
    main = page.locator('[data-qa="resume"], main').first
    text = main.inner_text() if main.count() else page.locator("body").inner_text()
    return (text or "").strip()[:5000]


def apply(page, vac: dict, letter: str, *, dry_run: bool, resume_url: str = "") -> str:
    """Отклик через сайт. В dry_run НЕ жмёт финальную кнопку.

    resume_url — какое резюме прикрепить (если у тебя их несколько).
    Возвращает статус: 'would_apply' | 'applied' | 'no_button' | 'error:...'.
    """
    page.goto(vac["url"], wait_until="domcontentloaded")
    _pause()

    # Закрыть cookie-баннер — иначе он перехватывает клик по «Откликнуться».
    ck = page.locator(SEL["cookie_accept"]).first
    if ck.count():
        try:
            ck.click()
            time.sleep(1)
        except Exception:  # noqa: BLE001
            pass

    btn = page.locator(SEL["respond_btn"]).first
    if not btn.count():
        return "no_button"  # уже откликались / вакансия закрыта / сменился селектор

    if dry_run:
        # Ничего не кликаем — только фиксируем, что кнопка есть и мы бы нажали.
        return "would_apply"

    # 1) Клик «Откликнуться» → открывается попап (отправки ещё НЕТ).
    btn.click()
    _pause()

    # 2) Попап не открылся и submit-кнопки нет → это «быстрый отклик»,
    #    он ушёл мгновенно без письма. Помечаем, не молчим.
    submit = page.locator(SEL["submit_btn"]).first
    if not submit.count():
        return "applied_no_letter"

    # 3) Выбор резюме в попапе (если несколько) — по id из URL.
    if resume_url:
        rid = resume_url.rstrip("/").split("/")[-1]
        try:
            opt = page.locator(f'[href*="{rid}"], input[value*="{rid}"], [data-qa*="{rid}"]').first
            if opt.count():
                opt.click()
                time.sleep(1)
        except Exception:  # noqa: BLE001 — выбор резюме best-effort
            pass

    # 4) Раскрыть поле письма («Добавить сопроводительное») и заполнить.
    if letter:
        acl = page.locator(SEL["add_cover"]).first
        if acl.count():
            try:
                acl.click()
                time.sleep(1)
            except Exception:  # noqa: BLE001
                pass
        area = page.locator(SEL["letter_area"]).first
        if area.count():
            area.fill(letter)
            time.sleep(1)

    # 5) Отправка (только теперь отклик реально уходит — уже с письмом).
    submit = page.locator(SEL["submit_btn"]).first
    if not submit.count():
        return "error:no_submit"
    submit.click()
    _pause()
    return "applied"
