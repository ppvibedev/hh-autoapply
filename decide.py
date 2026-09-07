"""Принятие решения без человека: HARD-фильтры + скоринг + письмо.

Логика калибруется здесь. В shadow-прогоне именно эти функции ты и проверяешь
по логу decisions.jsonl, прежде чем включать боевой режим.
"""
from __future__ import annotations

import config


def hard_filters(vac: dict) -> str | None:
    """Возвращает причину ОТБОЯ (str) или None, если вакансия проходит.

    Это то, что раньше отсекалось глазами. Отработало — скор даже не считаем.
    """
    title = (vac.get("name") or "").lower()
    employer = ((vac.get("employer") or {}).get("name") or "").lower()

    # Чёрный список работодателей
    for bad in config.BLACKLIST_EMPLOYERS:
        if bad in employer:
            return f"blacklist:{bad}"

    # Стоп-слова в заголовке
    for w in config.STOPWORDS:
        if w in title:
            return f"stopword:{w}"

    # Зарплатный фильтр: отбой, если верхняя/нижняя граница ниже минимума.
    sal = vac.get("salary") or {}
    if config.SALARY_MIN and sal:
        top = sal.get("to") or sal.get("from")
        if top and top < config.SALARY_MIN:
            return f"salary_below:{top}"

    return None


def score(vac_full: dict) -> tuple[int, str]:
    """Скоринг вакансии 0..100 + краткое объяснение.

    По смыслу через ИИ: понимает синонимы и перевод (Project Manager =
    проджект-менеджер = менеджер проектов) и уровень/грейд. Если ИИ недоступен
    (нет ключа) — откат на подсчёт слов в названии.
    """
    title = (vac_full.get("name") or "").strip()
    phrases = config.SEARCH_PHRASES or [config.SEARCH_TEXT.strip()]
    roles = "; ".join(phrases)

    # 1) ИИ-оценка: соответствует ли название ХОТЯ БЫ ОДНОЙ из искомых ролей.
    try:
        import re
        import llm
        prompt = (
            "Оцени по шкале 0–100, насколько НАЗВАНИЕ вакансии соответствует ХОТЯ БЫ ОДНОЙ "
            "из искомых ролей (совпадение с любой из них = высокий балл).\n"
            "Учитывай синонимы и перевод: «Project Manager» = «проджект-менеджер» = "
            "«менеджер проектов»; англ. и рус. варианты равнозначны. Учитывай уровень/грейд "
            "(ассистент/junior/зам — ниже, чем сама роль).\n"
            "100 — та же роль, что одна из искомых; 0 — совсем другая профессия.\n\n"
            f"Искомые роли: {roles}\n"
            f"Название вакансии: «{title}»\n\n"
            "Ответь ТОЛЬКО числом от 0 до 100, без пояснений."
        )
        out = llm.generate(prompt, max_tokens=8)
        if out:
            m = re.search(r"\d{1,3}", out)
            if m:
                val = min(100, int(m.group()))
                return val, f"ИИ: соответствие роли {val}"
    except Exception:  # noqa: BLE001 — при сбое ИИ откатываемся на слова
        pass

    # 2) Фолбэк без ИИ: берём ЛУЧШЕЕ совпадение слов среди всех словосочетаний.
    best = 0
    for phrase in phrases:
        kw = [w for w in phrase.lower().split() if len(w) > 2]
        if not kw:
            continue
        hits = sum(1 for k in kw if k in title.lower())
        best = max(best, int(100 * hits / len(kw)))
    return best, f"эвристика: лучшее совпадение по названию {best}"


def _strip_signature(text: str) -> str:
    """Чистит письмо: markdown, плейсхолдеры «[Имя]», висящую подпись «С уважением»."""
    import re
    text = re.sub(r"\[[^\]]*\]", "", text)          # [Имя кандидата] / [Ваше имя]
    text = text.replace("**", "").replace("__", "").replace("#", "")  # markdown-жирный/заголовки
    lines = []
    for ln in text.splitlines():
        ln = ln.rstrip()
        # снять маркеры списков в начале строки: «1. », «2) », «- », «• », «* »
        ln = re.sub(r"^\s*(\d+[.)]\s+|[-*•]\s+)", "", ln)
        lines.append(ln)
    # срезать хвостовые строки-подписи и пустые
    while lines:
        low = lines[-1].strip().lower().rstrip(",.")
        if low == "" or low.startswith("с уважением") or low.startswith("с наилучшими"):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


def cover_letter(vac_full: dict, resume_text: str) -> str:
    """Персональное письмо под вакансию через ИИ. Фолбэк на шаблон, если ИИ недоступен."""
    name = vac_full.get("name") or "вашу вакансию"
    company = (vac_full.get("employer") or {}).get("name", "")
    desc = (vac_full.get("description") or "")[:3000]

    prompt = (
        "Ты пишешь сопроводительное письмо для отклика на КОНКРЕТНУЮ вакансию на hh.ru.\n\n"
        "ЗАДАЧА:\n"
        "1. Внимательно прочитай ОПИСАНИЕ вакансии и выдели её реальные требования "
        "(обязанности, навыки, опыт, специфику компании и отрасли).\n"
        "2. По каждому значимому требованию найди в РЕЗЮМЕ подтверждающий опыт и покажи "
        "именно его — с конкретикой (проекты, роли, цифры, результаты) строго из резюме.\n"
        "3. Собери всё в цельное письмо, где видно: требование вакансии → чем кандидат его "
        "закрывает. Письмо должно быть заметно про ЭТУ вакансию, а не общим.\n\n"
        "ПРАВИЛА:\n"
        "- Русский язык, деловой тон, без воды, штампов и общих фраз.\n"
        "- НЕ выдумывай фактов, которых нет в резюме. Нет подтверждения требованию — "
        "не пиши про него.\n"
        "- ПЛОСКИЙ ТЕКСТ: без markdown, без символов * и #, без нумерованных и "
        "маркированных списков, без заголовков — только связные абзацы.\n"
        "- Объём: 2–3 коротких абзаца.\n"
        "- Без подписи и плейсхолдеров вроде «[Ваше имя]» — имя подставит hh.ru.\n"
        "- Верни только текст письма.\n\n"
        f"ВАКАНСИЯ: {name} — {company}\n"
        f"ОПИСАНИЕ ВАКАНСИИ:\n{desc}\n\n"
        f"РЕЗЮМЕ КАНДИДАТА:\n{resume_text or '(резюме не загружено)'}\n"
    )

    try:
        import llm
        out = llm.generate(prompt, max_tokens=900)
        if out:
            return _strip_signature(out)
    except Exception:  # noqa: BLE001 — при сбое ИИ не роняем прогон
        pass

    return (
        f"Здравствуйте! Заинтересовала вакансия «{name}». "
        f"Опыт и навыки совпадают с требованиями, готов обсудить детали."
    )
