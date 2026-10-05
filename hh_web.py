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


def _safe_path(url: object) -> str:
    """Keep only a URL path; never persist search parameters or credentials."""
    if not isinstance(url, str):
        return "[unknown]"
    path = urlparse(url).path
    return path if path.startswith("/") else "[unknown]"


def _safe_hh_path(url: object) -> str:
    """Keep only an HH path; never retain an unexpected external URL."""
    if not isinstance(url, str):
        return "[unknown]"
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme != "https" or parsed.hostname not in ("hh.ru", "www.hh.ru")
                or parsed.username or parsed.password):
            return "[external]"
    path = parsed.path
    return path if path.startswith("/") else "[unknown]"


_SAFE_DATA_QA = re.compile(r"[a-z][a-z0-9_-]{0,79}")


def _safe_data_qa_identifier(value: object) -> str | None:
    """Allow only short structural identifiers; omit dynamic/private-looking values."""
    if not isinstance(value, str) or _SAFE_DATA_QA.fullmatch(value) is None:
        return None
    if re.search(r"[0-9a-f]{16,}", value) or re.search(r"\d{9,}", value):
        return None
    return value


def _empty_page_data_qa_snapshot(page) -> list[dict]:
    """Read only data-qa attributes; never inspect text, HTML or form values."""
    elements = page.locator("[data-qa]")
    counts: dict[str, int] = {}
    for index in range(elements.count()):
        identifier = _safe_data_qa_identifier(
            elements.nth(index).get_attribute("data-qa"))
        if identifier is not None:
            counts[identifier] = counts.get(identifier, 0) + 1
    return [{"data_qa": identifier, "count": counts[identifier]}
            for identifier in sorted(counts)]


_DETAIL_DATA_QA_LIMIT = 75
_DETAIL_DATA_QA_SCAN_LIMIT = 500
_DETAIL_PROBE_DELAYS_MS = (100, 200, 300)


def _safe_count(page, selector: str) -> int | None:
    try:
        return page.locator(selector).count()
    except Exception:
        return None


def captcha_present(page) -> bool:
    """Strict current-page CAPTCHA signal used by diagnostics and run control."""
    try:
        path = _safe_hh_path(page.url).rstrip("/")
    except Exception:
        path = "[unknown]"
    if path in ("/captcha", "/account/captcha"):
        return True
    count = _safe_count(page, '[data-qa*="captcha"]')
    return count is not None and count > 0


def _limited_data_qa_snapshot(page) -> tuple[list[dict], bool]:
    """Bounded structural data-qa names only; no text, values or HTML."""
    elements = page.locator("[data-qa]")
    total = elements.count()
    counts: dict[str, int] = {}
    distinct_overflow = False
    for index in range(min(total, _DETAIL_DATA_QA_SCAN_LIMIT)):
        identifier = _safe_data_qa_identifier(
            elements.nth(index).get_attribute("data-qa"))
        if identifier is None:
            continue
        if identifier in counts:
            counts[identifier] += 1
        elif len(counts) < _DETAIL_DATA_QA_LIMIT:
            counts[identifier] = 1
        else:
            distinct_overflow = True
    return ([{"data_qa": identifier, "count": counts[identifier]}
             for identifier in sorted(counts)],
            distinct_overflow or total > _DETAIL_DATA_QA_SCAN_LIMIT)


def _vacancy_detail_diagnostic(page, vac: dict, response, *,
                               initial_description_count: int,
                               initial_check_elapsed_ms: int,
                               stopped=None) -> dict:
    """Collect structural evidence after the canonical description selector misses."""
    requested_path = _safe_hh_path(vac.get("url"))
    try:
        final_url = page.url
    except Exception:
        final_url = None
    final_path = _safe_hh_path(final_url)
    expected_path = f'/vacancy/{vac.get("id", "")}'
    try:
        raw_status = getattr(response, "status", None)
    except Exception:
        raw_status = None
    navigation_status = (raw_status if isinstance(raw_status, int)
                         and not isinstance(raw_status, bool) else None)
    title_count = _safe_count(page, SEL["vac_title"])
    captcha_count = _safe_count(page, '[data-qa*="captcha"]')
    response_control_count = _safe_count(page, SEL["respond_btn"])
    modal_count = _safe_count(page, '[role="dialog"], [data-qa="modal"]')
    try:
        snapshot, snapshot_truncated = _limited_data_qa_snapshot(page)
        snapshot_status = "collected"
    except Exception:
        snapshot, snapshot_truncated, snapshot_status = [], False, "unavailable"

    appeared = False
    appeared_after_ms = None
    waited_ms = 0
    probe_checks = 0
    stopped_during_wait = False
    for delay_ms in _DETAIL_PROBE_DELAYS_MS:
        if stopped is not None and stopped():
            stopped_during_wait = True
            break
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            break
        waited_ms += delay_ms
        probe_checks += 1
        if _safe_count(page, SEL["vac_desc"]):
            appeared = True
            appeared_after_ms = waited_ms
            break

    normalized_requested_path = requested_path.rstrip("/")
    normalized_final_path = final_path.rstrip("/")
    login = normalized_final_path == "/account/login"
    captcha = (True if normalized_final_path in ("/captcha", "/account/captcha") else
               "unknown" if captcha_count is None else captcha_count > 0)
    hh_error = ("unknown" if navigation_status is None
                else navigation_status >= 400)
    not_found = ("unknown" if navigation_status is None
                 else navigation_status == 404)
    vacancy_content = ("unknown" if title_count is None else title_count > 0)
    return {
        "diagnostic_schema_version": 1,
        "requested_path": requested_path,
        "final_path": final_path,
        "same_vacancy_path": (normalized_requested_path == expected_path
                              and normalized_final_path == expected_path),
        "redirect_observed": (requested_path not in ("[unknown]", "[external]")
                              and final_path not in ("[unknown]", "[external]")
                              and normalized_final_path != normalized_requested_path),
        "navigation_status": navigation_status,
        "title_selector_count": title_count,
        "description_selector_count": initial_description_count,
        "page_state_markers": {
            "login_marker_present": login,
            "captcha_marker_present": captcha,
            "captcha_marker_count": captcha_count,
            "hh_error_marker_present": hh_error,
            "not_found_marker_present": not_found,
            "vacancy_content_marker_present": vacancy_content,
            "response_control_count": response_control_count,
            "known_modal_count": modal_count,
            "interstitial_marker": "unknown",
        },
        "limited_safe_data_qa_snapshot": snapshot,
        "data_qa_snapshot_distinct_count": len(snapshot),
        "data_qa_snapshot_truncated": snapshot_truncated,
        "data_qa_snapshot_status": snapshot_status,
        "initial_description_check_elapsed_ms": max(0, initial_check_elapsed_ms),
        "post_navigation_pause_performed": True,
        "description_appeared_during_diagnostic_wait": appeared,
        "description_appeared_after_ms": appeared_after_ms,
        "diagnostic_wait_elapsed_ms": waited_ms,
        "diagnostic_probe_checks": probe_checks,
        "diagnostic_wait_stopped": stopped_during_wait,
    }


def _search_page_diagnostic(*, requested_path: str, final_url: object,
                            navigation_http_status: object, raw_card_count: int,
                            parsed_vacancy_count: int,
                            captcha_marker_present: object) -> dict:
    """Classify safe structural observations without changing search behavior."""
    final_path = _safe_path(final_url)
    status = (navigation_http_status if isinstance(navigation_http_status, int)
              and not isinstance(navigation_http_status, bool) else None)
    captcha = captcha_marker_present if isinstance(captcha_marker_present, bool) else "unknown"
    if final_path.rstrip("/") in ("/captcha", "/account/captcha"):
        captcha = True
    login = final_path.rstrip("/") == "/account/login"
    hh_error = status is not None and status >= 400

    # No reliable empty-results, pagination or interstitial selector is currently
    # established in this codebase. Preserve that uncertainty instead of guessing.
    empty_marker = "unknown"
    next_marker = "unknown"
    interstitial = "unknown"
    if login:
        outcome = "login_page_detected"
    elif captcha is True:
        outcome = "captcha_detected"
    elif hh_error:
        outcome = "hh_error_detected"
    elif parsed_vacancy_count > 0:
        outcome = "vacancies_parsed"
    elif raw_card_count > 0:
        outcome = "raw_cards_parse_failed"
    else:
        outcome = "empty_unclassified"
    return {
        "requested_path": requested_path,
        "final_path": final_path,
        "redirect_observed": final_path not in ("[unknown]", requested_path),
        "navigation_http_status": status,
        "raw_card_count": raw_card_count,
        "parsed_vacancy_count": parsed_vacancy_count,
        "empty_results_marker": empty_marker,
        "next_page_marker": next_marker,
        "login_marker_present": login,
        "captcha_marker_present": captcha,
        "hh_error_marker_present": hh_error,
        "interstitial_marker": interstitial,
        "outcome": outcome,
    }


def search_page(page, *, text: str, area: str, salary_min: int,
                work_format_codes: list[str], page_num: int,
                extra_params: dict | None = None,
                diagnostics: dict | None = None) -> list[dict]:
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
    response = page.goto(url, wait_until="domcontentloaded")
    _pause()

    cards = page.locator(SEL["card"])
    raw_card_count = cards.count()
    out: list[dict] = []
    for i in range(raw_card_count):
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
    if diagnostics is not None:
        try:
            captcha_present = page.locator('[data-qa*="captcha"]').count() > 0
        except Exception:
            captcha_present = "unknown"
        try:
            final_url = getattr(page, "url", None)
        except Exception:
            final_url = None
        try:
            navigation_status = getattr(response, "status", None)
        except Exception:
            navigation_status = None
        try:
            diagnostics.update(_search_page_diagnostic(
                requested_path="/search/vacancy",
                final_url=final_url,
                navigation_http_status=navigation_status,
                raw_card_count=raw_card_count,
                parsed_vacancy_count=len(out),
                captcha_marker_present=captcha_present,
            ))
            if raw_card_count == 0 and not out:
                try:
                    snapshot = _empty_page_data_qa_snapshot(page)
                    diagnostics["empty_page_data_qa_snapshot"] = snapshot
                    diagnostics["empty_page_data_qa_distinct_count"] = len(snapshot)
                    diagnostics["empty_page_data_qa_snapshot_status"] = "collected"
                except Exception:
                    diagnostics["empty_page_data_qa_snapshot"] = []
                    diagnostics["empty_page_data_qa_distinct_count"] = 0
                    diagnostics["empty_page_data_qa_snapshot_status"] = "unavailable"
        except Exception:
            # Telemetry must never turn an otherwise valid search into a failure.
            pass
    return out


def load_full(page, vac: dict, *, diagnostics: dict | None = None, stopped=None) -> dict:
    """Открывает страницу вакансии, дозаполняет описание/зарплату."""
    response = page.goto(vac["url"], wait_until="domcontentloaded")
    navigation_complete = time.monotonic()
    _pause()
    desc = page.locator(SEL["vac_desc"]).first
    description_count = desc.count()
    if not description_count:
        if diagnostics is not None:
            try:
                diagnostics.update(_vacancy_detail_diagnostic(
                    page, vac, response,
                    initial_description_count=description_count,
                    initial_check_elapsed_ms=round(
                        (time.monotonic() - navigation_complete) * 1000),
                    stopped=stopped,
                ))
            except Exception:
                # Passive telemetry must never replace the established outcome.
                diagnostics.update({
                    "diagnostic_schema_version": 1,
                    "requested_path": _safe_hh_path(vac.get("url")),
                    "final_path": "[unknown]",
                    "description_selector_count": description_count,
                    "diagnostic_collection_status": "unavailable",
                })
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
