"""Offline-safe preparation for Anti-Captcha; no HH challenge is submitted here.

Only the official non-paid getBalance and getTaskResult calls are implemented.
No createTask, solution injection, or HH lifecycle hook is present.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time

import requests


BALANCE_URL = "https://api.anti-captcha.com/getBalance"
TASK_RESULT_URL = "https://api.anti-captcha.com/getTaskResult"
BALANCE_CACHE_SECONDS = 30  # Official API asks clients not to call more often.
_CHALLENGE_PATHS = {
    ("www.google.com", "/recaptcha/"): "recaptcha",
    ("www.recaptcha.net", "/recaptcha/"): "recaptcha",
    ("hcaptcha.com", "/1/"): "hcaptcha",
    ("newassets.hcaptcha.com", "/captcha/"): "hcaptcha",
    ("challenges.cloudflare.com", "/turnstile/"): "turnstile",
}


def classify_challenge_markers(markers: list[tuple[str, str]]) -> str:
    """Classify only unambiguous known host/path markers, never page text.

    This accepts hostnames and paths, not URLs or private iframe queries. It
    is not evidence that any of these challenge types occurs on HH.
    """
    kinds = {kind for host, path in markers if isinstance(host, str) and isinstance(path, str)
             for (known_host, prefix), kind in _CHALLENGE_PATHS.items()
             if host.lower().rstrip(".") == known_host and path.startswith(prefix)}
    return next(iter(kinds)) if len(kinds) == 1 else "unknown"


def capture_challenge_structure(page) -> dict:
    """Inspect visible iframes and return only allowlisted structural metadata.

    The browser returns canonical host/path prefixes, never a full URL, query,
    sitekey, image, token, text or frame contents. Unknown HH challenges stay
    unknown and must be handled by the existing manual pause.
    """
    empty = {"captcha_type": "unknown", "classification_confidence": "none",
             "visible_iframe_count": None, "recognized_marker_count": 0,
             "probe_status": "unavailable"}
    try:
        raw = page.locator("iframe[src]").evaluate_all("""nodes => {
          const markers = [];
          let visible = 0;
          for (const node of nodes.slice(0, 50)) {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' ||
                style.visibility === 'hidden') continue;
            visible++;
            try {
              const url = new URL(node.src, document.baseURI);
              const host = url.hostname.toLowerCase();
              const path = url.pathname;
              if (host === 'www.google.com' && path.startsWith('/recaptcha/'))
                markers.push(['www.google.com', '/recaptcha/']);
              else if (host === 'www.recaptcha.net' && path.startsWith('/recaptcha/'))
                markers.push(['www.recaptcha.net', '/recaptcha/']);
              else if (host === 'hcaptcha.com' && path.startsWith('/1/'))
                markers.push(['hcaptcha.com', '/1/']);
              else if (host === 'newassets.hcaptcha.com' && path.startsWith('/captcha/'))
                markers.push(['newassets.hcaptcha.com', '/captcha/']);
              else if (host === 'challenges.cloudflare.com' && path.startsWith('/turnstile/'))
                markers.push(['challenges.cloudflare.com', '/turnstile/']);
            } catch (_) { /* invalid src: unknown */ }
          }
          return {visible, markers};
        }""")
    except Exception:
        return empty
    if not isinstance(raw, dict):
        return empty
    visible = raw.get("visible")
    markers = raw.get("markers")
    if (type(visible) is not int or not 0 <= visible <= 50 or
            not isinstance(markers, list) or len(markers) > 50):
        return empty
    safe_markers = [tuple(marker) for marker in markers
                    if (isinstance(marker, list) and len(marker) == 2 and
                        all(isinstance(part, str) for part in marker) and
                        tuple(marker) in _CHALLENGE_PATHS)]
    kind = classify_challenge_markers(safe_markers)
    return {"captcha_type": kind,
            "classification_confidence": "structural" if kind != "unknown" else "none",
            "visible_iframe_count": visible,
            "recognized_marker_count": len(safe_markers),
            "probe_status": "ok"}


def _money(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


@dataclass(frozen=True)
class BalanceResult:
    available: bool
    amount_usd: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class TaskStatus:
    state: str  # processing | ready | error | stopped | timeout
    cost_usd: Decimal | None = None
    reason: str = ""


class AntiCaptchaClient:
    """Read-only balance/task status with a 30-second balance cache.

    The key is never stored in a result or diagnostic. The transport is
    injectable so all tests remain offline.
    """

    def __init__(self, api_key: str, *, post=None, clock=None):
        self._key = api_key
        self._post = post or requests.post
        self._clock = clock or time.monotonic
        self._cached: BalanceResult | None = None
        self._cached_at = 0.0

    def get_balance(self) -> BalanceResult:
        if not self._key:
            return BalanceResult(False, reason="missing_api_key")
        now = self._clock()
        if self._cached is not None and now - self._cached_at < BALANCE_CACHE_SECONDS:
            return self._cached
        try:
            response = self._post(BALANCE_URL, json={"clientKey": self._key}, timeout=10)
            if response.status_code != 200:
                result = BalanceResult(False, reason="balance_http_error")
            else:
                body = response.json()
                if not isinstance(body, dict) or body.get("errorId") != 0:
                    result = BalanceResult(False, reason="balance_service_error")
                else:
                    amount = _money(body.get("balance"))
                    result = (BalanceResult(True, amount) if amount is not None else
                              BalanceResult(False, reason="balance_invalid_response"))
        except (requests.RequestException, ValueError, TypeError):
            # Exception messages may contain the URL, request or credentials.
            result = BalanceResult(False, reason="balance_transport_error")
        self._cached = result
        self._cached_at = now
        return result

    def get_task_status(self, task_id: int, *, timeout_seconds: float = 10) -> TaskStatus:
        """Read only safe task metadata; discard solution and arbitrary error text."""
        if not self._key:
            return TaskStatus("error", reason="missing_api_key")
        if type(task_id) is not int or task_id <= 0 or timeout_seconds <= 0:
            return TaskStatus("error", reason="invalid_task_id")
        try:
            response = self._post(TASK_RESULT_URL,
                                  json={"clientKey": self._key, "taskId": task_id},
                                  timeout=min(10, timeout_seconds))
            if response.status_code != 200:
                return TaskStatus("error", reason="task_http_error")
            body = response.json()
            if not isinstance(body, dict) or body.get("errorId") != 0:
                return TaskStatus("error", reason="task_service_error")
            if body.get("status") == "processing":
                return TaskStatus("processing")
            if body.get("status") == "ready":
                return TaskStatus("ready", cost_usd=_money(body.get("cost")))
            return TaskStatus("error", reason="task_invalid_response")
        except (requests.RequestException, ValueError, TypeError):
            return TaskStatus("error", reason="task_transport_error")

    def wait_for_task(self, task_id: int, *, max_wait_seconds: int,
                      stopped, sleep=time.sleep, poll_seconds: int = 2) -> TaskStatus:
        """Bounded, stop-aware polling of an already-created task only."""
        if max_wait_seconds <= 0 or poll_seconds <= 0:
            return TaskStatus("error", reason="invalid_wait_limit")
        deadline = self._clock() + max_wait_seconds
        while True:
            if stopped():
                return TaskStatus("stopped")
            if self._clock() >= deadline:
                return TaskStatus("timeout")
            result = self.get_task_status(task_id,
                                          timeout_seconds=min(10, deadline - self._clock()))
            if result.state != "processing":
                return result
            remaining = deadline - self._clock()
            if remaining <= 0:
                return TaskStatus("timeout")
            sleep(min(poll_seconds, remaining))


def preflight(*, enabled: bool, has_key: bool, challenge_type: str,
              daily_limit_usd: object, task_limit_usd: object,
              spent_today_usd: object, estimated_task_usd: object,
              balance: BalanceResult, hh_permission_confirmed: bool = False) -> str:
    """Return a safe reason; no paid task can be created by this module.

    An unknown price is rejected because the API reports actual cost only
    after a task finishes. In-memory spending alone cannot enforce a daily
    limit across restarts, so this is a preflight, not an accounting ledger.
    """
    if not enabled:
        return "disabled"
    if not has_key:
        return "missing_api_key"
    if challenge_type not in {"recaptcha", "hcaptcha", "turnstile"}:
        return "unsupported_or_unknown_type"
    if not hh_permission_confirmed:
        return "hh_permission_unconfirmed"
    daily = _money(daily_limit_usd)
    task = _money(task_limit_usd)
    spent = _money(spent_today_usd)
    estimate = _money(estimated_task_usd)
    if None in (daily, task, spent, estimate) or daily == 0 or task == 0 or estimate == 0:
        return "budget_unavailable"
    if estimate > task or spent + estimate > daily:
        return "budget_exceeded"
    if not balance.available or balance.amount_usd is None:
        return "balance_unavailable"
    if balance.amount_usd < estimate:
        return "insufficient_balance"
    return "preflight_only_no_paid_task_support"
