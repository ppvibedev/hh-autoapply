"""Небольшой фильтр контактов; подозрительные технические секреты блокируют передачу."""
import re


class PrivacyError(ValueError):
    """Fail-closed privacy rejection with safe, non-content diagnostics."""

    def __init__(self, message="technical_secret_detected", *, rule_id=None, source_field=None):
        super().__init__(message)
        self.privacy_rule_id = rule_id
        self.privacy_source_field = source_field

    def with_source(self, source_field):
        if self.privacy_source_field is None:
            self.privacy_source_field = source_field
        return self


SECRET_RULES = (
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("token_prefix", r"\b(?:sk-ant-|sk-proj-|sk-|AIza|ghp_|github_pat_)[A-Za-z0-9_-]{16,}"),
    ("jwt_like", r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    ("credential_assignment",
     r"(?im:^\s*[\"']?(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
     r"password|passwd|credential|secret)|token|cookies?|authorization|set-cookie|пароль)"
     r"[\"']?\s*[:=]\s*\S)"),
    ("json_credential_field",
     r'"(?:api_key|access_token|refresh_token|password|credentials|authorization)"\s*:\s*"[^" ]+'),
    ("json_cookie_or_origin_array", r'"(?:cookies|origins)"\s*:\s*\['),
    ("auth_scheme", r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=-]{8,}"),
    ("credential_url", r"https?://[^\s/:]+:[^\s/@]+@"),
    ("credential_query_parameter", r"[?&](?:api[_-]?key|token|access_token|signature|password|secret)="),
)


def sanitize_candidate_text(text: str, forbidden_values=()) -> str:
    text = str(text or "")
    # Include disabled and fallback credentials too; never redact only the active key.
    from llm_connections import credentials
    forbidden_values = tuple(forbidden_values) + credentials()

    # Не пытаться интерпретировать файлы с credentials как профессиональный текст.
    for rule_id, pattern in SECRET_RULES:
        if re.search(pattern, text, re.I):
            raise PrivacyError(rule_id=rule_id)
    if any(v and v in text for v in forbidden_values):
        raise PrivacyError(rule_id="configured_credential_value")
    text = re.sub(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", "[email удалён]", text)
    # Явно подписанные адреса и контактные каналы, не строки «город/страна».
    text = re.sub(
        r"(?im)^.*(?:домашний адрес|почтовый адрес|адрес проживания|home address|postal address|"
        r"mailing address|street address|^\s*адрес\s*:|^\s*address\s*:|"
        r"(?:телеграм|telegram)(?:\s+для\s+связи)?\s*:|whatsapp\s*:|skype\s*:).*$", "[контакт удалён]", text)
    text = re.sub(r"(?im)^.*\b(?:ул\.|улица|проспект|переулок|street|avenue|road)\s+[^\n]*\d[^\n]*$",
                  "[адрес удалён]", text)
    text = re.sub(r"(?im)^.*\b\d+\s+[^\n,]{1,60}\s(?:street|avenue|road|st\.|rd\.)[^\n]*$",
                  "[адрес удалён]", text)
    text = re.sub(r"(?i)(?:https?://)?(?:t\.me|wa\.me)/[^\s)<>]+", "[контакт удалён]", text)
    phone = r"(?<![\w])(?:\+\d[\d ()\u00a0.\-]{6,}\d|\(?\d{2,4}\)?[ .\-][\d ()\u00a0.\-]{4,}\d|\d{9,15})(?![\w])"
    def remove_phone(match):
        value = match.group()
        digits = re.sub(r"\D", "", value)
        if re.fullmatch(r"\d{4}\s*[-–]\s*\d{4}", value):
            return value  # периоды работы
        return "[телефон удалён]" if 7 <= len(digits) <= 15 else value
    return re.sub(phone, remove_phone, text)


def sanitize_record(value):
    """Очищать только содержательные текстовые поля, не технические метаданные."""
    text_fields = {"title", "company", "employer", "reason", "why", "letter", "matched",
                   "missing", "risks", "remote_reason", "role_alignment_reason", "role_evidence"}
    def clean(text):
        if isinstance(text, str):
            import config
            return sanitize_candidate_text(text, (config.LLM_API_KEY, config.ACCESS_TOKEN))
        if isinstance(text, list):
            return [clean(item) for item in text]
        return text
    if isinstance(value, dict):
        result = {key: clean(item) if key in text_fields else item for key, item in value.items()}
        if isinstance(result.get("vacancy_role"), dict):
            result["vacancy_role"] = {key: clean(item) for key, item in result["vacancy_role"].items()}
        if isinstance(result.get("matching_result"), dict):
            result["matching_result"] = sanitize_record(result["matching_result"])
        if "questions" in result:
            result["questions"] = [{k: clean(v) if k in ("question", "answer") else v
                                    for k, v in q.items()} for q in result["questions"]]
        return result
    return value


def sanitize_exception_message(error):
    """Только известные технические фрагменты; произвольное содержимое не выводить.

    Playwright может включать DOM, URL, аргументы и call log с приватным текстом.
    Поэтому не сохраняем весь message даже после замены известных ключей.
    """
    message = str(error)
    patterns = (
        r"(?:BrowserType\.launch|Browser\.new_context|BrowserContext\.new_page|Page\.goto|Locator\.evaluate)",
        r"Executable doesn't exist", r"Target page, context or browser has been closed",
        r"Timeout \d+ms exceeded", r"net::ERR_[A-Z_]+", r"Permission denied",
        r"SyntaxError: (?:Invalid or unexpected token|Unexpected token|Unexpected identifier)",
        r"ReferenceError", r"TypeError", r"strict mode violation", r"Connection closed",
    )
    fragments = [match.group(0) for pattern in patterns for match in re.finditer(pattern, message)]
    return "; ".join(dict.fromkeys(fragments)) or "Exception message withheld: no safe technical fragment"
