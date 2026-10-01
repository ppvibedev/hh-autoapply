"""Safe provider failures: never retain a requests exception, response or URL."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re


class LLMUnavailable(RuntimeError):
    def __init__(self, failures=()):
        super().__init__('llm_infrastructure_unavailable')
        self.failures = list(failures)


class LLMStopped(RuntimeError):
    def __init__(self):
        super().__init__('user_stop')


@dataclass(frozen=True)
class ProviderFailure:
    category: str
    http_status: int | None = None
    retry_delay: float | None = None
    quota_types: tuple = ()
    provider_status: str | None = None
    transport_phase: str | None = None
    transport_failure_detail: str | None = None

    def public(self):
        return dict(provider_error_category=self.category, http_status=self.http_status,
                    retry_delay=self.retry_delay, quota_types=list(self.quota_types),
                    provider_status=self.provider_status, transport_phase=self.transport_phase,
                    transport_failure_detail=self.transport_failure_detail)


def _seconds(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def classify_http(provider, response):
    status = response.status_code
    category = ('auth/provider_access' if status in (401, 403) else 'rate_limit_unknown'
                if status == 429 else 'provider_server_error' if 500 <= status <= 599
                else 'provider_http_error')
    delays, quotas, provider_status = [], set(), None
    if status != 429:
        return ProviderFailure(category, status)
    try:
        value = response.headers.get('Retry-After')
        delay = _seconds(value)
        if delay is None and isinstance(value, str):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo:
                    delay = max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None:
            delays.append(delay)
        if provider == 'gemini':
            body = response.json()
            error = body.get('error') if isinstance(body, dict) else None
            if isinstance(error, dict):
                if error.get('status') == 'RESOURCE_EXHAUSTED':
                    provider_status = 'RESOURCE_EXHAUSTED'
                details = error.get('details', [])
                if isinstance(details, list):
                    for detail in details:
                        if not isinstance(detail, dict):
                            continue
                        if detail.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo':
                            value = detail.get('retryDelay')
                            if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d{1,9})?s', value):
                                delay = _seconds(value[:-1])
                                if delay is not None:
                                    delays.append(delay)
                        if detail.get('@type') != 'type.googleapis.com/google.rpc.QuotaFailure':
                            continue
                        violations = detail.get('violations')
                        if not isinstance(violations, list):
                            quotas.add('unknown')
                            continue
                        for violation in violations:
                            qid = violation.get('quotaId') if isinstance(violation, dict) else None
                            # Closed grammar of observed Gemini quota identifiers; no message heuristics.
                            if not isinstance(qid, str):
                                quotas.add('unknown')
                            elif re.fullmatch(r'GenerateRequestsPerDayPerProjectPerModel(?:-(?:Free|Paid)Tier)?', qid):
                                quotas.add('daily_requests')
                            elif re.fullmatch(r'GenerateContentInputTokensPerModelPerDay(?:-(?:Free|Paid)Tier)?', qid):
                                quotas.add('daily_tokens')
                            elif re.fullmatch(r'GenerateRequestsPerMinutePerProjectPerModel(?:-(?:Free|Paid)Tier)?', qid):
                                quotas.add('requests_per_minute')
                            elif re.fullmatch(r'GenerateContentInputTokensPerModelPerMinute(?:-(?:Free|Paid)Tier)?', qid):
                                quotas.add('tokens_per_minute')
                            else:
                                quotas.add('unknown')
    except (ValueError, TypeError, AttributeError, OverflowError):
        # An unexpected response is never interpreted as proof of daily exhaustion.
        quotas = set()
    if quotas & {'daily_requests', 'daily_tokens'}:
        category = 'quota_daily'
    elif quotas and 'unknown' not in quotas:
        category = 'rate_limit_tokens' if 'tokens_per_minute' in quotas else 'rate_limit_requests'
    return ProviderFailure(category, status, max(delays) if delays else None,
                           tuple(sorted(quotas)), provider_status)
