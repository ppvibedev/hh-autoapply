"""Небольшой фильтр контактов; подозрительные технические секреты блокируют передачу."""
import base64
import binascii
import re


_MATCH_SHAPE_SCHEMES = frozenset({'basic','bearer'})
_MATCH_SHAPE_SEPARATORS = frozenset({'space','tab','newline','mixed_whitespace','other_whitespace'})
_MATCH_SHAPE_LENGTHS = frozenset({'8_11','12_23','24_47','48_plus'})


def _safe_match_shape(value):
    if not isinstance(value,dict):
        return None
    keys = ('scheme','separator','suffix_length_bucket','letters_only','has_digit',
            'has_token_punctuation','basic_base64_valid','basic_decoded_has_colon')
    if set(value) != set(keys) or value['scheme'] not in _MATCH_SHAPE_SCHEMES \
            or value['separator'] not in _MATCH_SHAPE_SEPARATORS \
            or value['suffix_length_bucket'] not in _MATCH_SHAPE_LENGTHS:
        return None
    if any(not isinstance(value[key],bool)
           for key in ('letters_only','has_digit','has_token_punctuation')):
        return None
    basic_values=(value['basic_base64_valid'],value['basic_decoded_has_colon'])
    if value['scheme'] == 'basic':
        if any(not isinstance(item,bool) for item in basic_values):
            return None
    elif basic_values != (None,None):
        return None
    return {key:value[key] for key in keys}


class PrivacyError(ValueError):
    """Fail-closed privacy rejection with safe, non-content diagnostics."""

    def __init__(self, message="technical_secret_detected", *, rule_id=None, source_field=None,
                 match_shape=None):
        super().__init__(message)
        self.privacy_rule_id = rule_id
        self.privacy_source_field = source_field
        self.privacy_match_shape = _safe_match_shape(match_shape)

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


def _auth_scheme_match_shape(match):
    parsed = re.fullmatch(r'(Bearer|Basic)(\s+)([A-Za-z0-9+/_.=-]{8,})',match.group(),re.I)
    if parsed is None:
        return None
    scheme, separator, suffix = parsed.groups()
    scheme = scheme.lower()
    chars = set(separator)
    if chars == {' '}:
        separator_shape = 'space'
    elif chars == {'\t'}:
        separator_shape = 'tab'
    elif chars and chars <= {'\r','\n'}:
        separator_shape = 'newline'
    elif len(chars) > 1:
        separator_shape = 'mixed_whitespace'
    else:
        separator_shape = 'other_whitespace'
    length = len(suffix)
    length_bucket = ('8_11' if length <= 11 else '12_23' if length <= 23
                     else '24_47' if length <= 47 else '48_plus')
    base64_valid = decoded_has_colon = None
    if scheme == 'basic':
        try:
            decoded = base64.b64decode(suffix,validate=True)
            base64_valid = True
            decoded_has_colon = b':' in decoded
        except (ValueError,binascii.Error):
            base64_valid = False
            decoded_has_colon = False
    return _safe_match_shape({
        'scheme':scheme,'separator':separator_shape,'suffix_length_bucket':length_bucket,
        'letters_only':re.fullmatch(r'[A-Za-z]+',suffix) is not None,
        'has_digit':re.search(r'[0-9]',suffix) is not None,
        'has_token_punctuation':re.search(r'[+/_.=-]',suffix) is not None,
        'basic_base64_valid':base64_valid,'basic_decoded_has_colon':decoded_has_colon,
    })


def sanitize_candidate_text(text: str, forbidden_values=()) -> str:
    text = str(text or "")
    # Include disabled and fallback credentials too; never redact only the active key.
    from llm_connections import credentials
    forbidden_values = tuple(forbidden_values) + credentials()

    # Не пытаться интерпретировать файлы с credentials как профессиональный текст.
    for rule_id, pattern in SECRET_RULES:
        match = re.search(pattern, text, re.I)
        if match:
            shape = _auth_scheme_match_shape(match) if rule_id == 'auth_scheme' else None
            raise PrivacyError(rule_id=rule_id,match_shape=shape)
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
