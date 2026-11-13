"""Structural CAPTCHA detection and bounded image-to-text service adapter."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
import threading
import time

import requests


BALANCE_URL = "https://api.anti-captcha.com/getBalance"
TASK_RESULT_URL = "https://api.anti-captcha.com/getTaskResult"
CREATE_TASK_URL = "https://api.anti-captcha.com/createTask"
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


def _image_captcha_dialog_count(page) -> tuple[int | None, str]:
    """Count only complete visible image/input/action CAPTCHA dialogs.

    A generic image or text input on a response form is never enough. No
    image bytes, labels, input values, DOM text or arbitrary attributes leave
    the browser evaluation.
    """
    try:
        count = page.locator('[role="dialog"]:visible').evaluate_all("""dialogs => {
          const visible = el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
              style.display !== 'none' && style.visibility !== 'hidden';
          };
          let candidates = 0;
          for (const dialog of dialogs.slice(0, 10)) {
            const markers = [...dialog.querySelectorAll('[data-qa*="captcha"]')]
              .filter(visible);
            if (!markers.length) continue;
            const images = markers.filter(el => el.tagName === 'IMG')
              .concat(markers.flatMap(el => [...el.querySelectorAll('img')])).filter(visible);
            const inputs = [...dialog.querySelectorAll(
              'input[type="text"], input:not([type]), input[type="search"]')]
              .filter(el => visible(el) && !el.disabled);
            const actions = [...dialog.querySelectorAll('button, [role="button"]')]
              .filter(el => visible(el) && !el.disabled);
            if (images.length === 1 && inputs.length === 1 && actions.length === 1)
              candidates++;
          }
          return candidates;
        }""")
    except Exception:
        return None, "unavailable"
    return (count, "ok") if type(count) is int and 0 <= count <= 10 else (None, "unavailable")


def _hh_image_pair_count(page) -> int:
    """HH's observed image and answer field have exact, challenge-specific QA IDs."""
    try:
        image = page.locator('img[data-qa="account-captcha-picture"]:visible')
        field = page.locator('input[data-qa="account-captcha-input"]:visible')
        return int(image.count() == 1 and field.count() == 1 and field.is_enabled())
    except Exception:
        return 0


def _unknown_image_structure(page) -> list[list[dict]]:
    """Bounded structural clues for an unrecognized live challenge; no DOM content."""
    try:
        raw = page.locator('[data-qa*="captcha"]:visible').evaluate_all("""nodes => {
          const safeQa = el => {
            const value = el.getAttribute('data-qa') || '';
            return /^[a-zA-Z0-9_-]{1,80}$/.test(value) &&
              (value.startsWith('captcha-') || value.startsWith('account-captcha-'))
              ? value : null;
          };
          const visible = el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
              style.display !== 'none' && style.visibility !== 'hidden';
          };
          const shape = el => ({
            tag: el.tagName.toLowerCase(),
            role: ['dialog', 'alertdialog', 'button', 'textbox'].includes(el.getAttribute('role'))
              ? el.getAttribute('role') : null,
            data_qa: safeQa(el),
            visible: visible(el),
            img_count: el.querySelectorAll('img').length,
            canvas_count: el.querySelectorAll('canvas').length,
            input_count: el.querySelectorAll('input').length,
            button_count: el.querySelectorAll('button, [role="button"]').length,
            buttons: [...el.querySelectorAll('button, [role="button"]')].slice(0, 6)
              .map(button => ({tag: button.tagName.toLowerCase(), data_qa: safeQa(button),
                type: ['submit', 'button', 'reset'].includes(button.getAttribute('type'))
                  ? button.getAttribute('type') : null,
                enabled: !button.disabled, visible: visible(button)})),
            background_image: getComputedStyle(el).backgroundImage.startsWith('url(')
          });
          return nodes.slice(0, 4).map(node => {
            const chain = [];
            for (let el = node, depth = 0; el && depth < 8; el = el.parentElement, depth++)
              chain.push(shape(el));
            return chain;
          });
        }""")
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    def safe_shape(node):
        if not isinstance(node, dict):
            return None
        tag = node.get("tag")
        if not isinstance(tag, str) or tag not in {"div", "span", "section", "form", "img", "input", "button", "label"}:
            return None
        role = node.get("role")
        qa = node.get("data_qa")
        if not isinstance(role, (str, type(None))) or role not in {None, "dialog", "alertdialog", "button", "textbox"}:
            role = None
        if not (isinstance(qa, str) and re.fullmatch(r"(?:captcha|account-captcha)-[A-Za-z0-9_-]{1,64}", qa)):
            qa = None
        result = {"tag": tag, "role": role, "data_qa": qa}
        for key in ("visible", "background_image"):
            result[key] = node.get(key) if type(node.get(key)) is bool else False
        for key in ("img_count", "canvas_count", "input_count", "button_count"):
            count = node.get(key)
            result[key] = min(count, 100) if type(count) is int and count >= 0 else 0
        buttons = node.get("buttons")
        result["buttons"] = []
        if isinstance(buttons, list):
            for button in buttons[:6]:
                if (not isinstance(button, dict) or not isinstance(button.get("tag"), str)
                        or button.get("tag") not in {"button", "div", "span"}):
                    continue
                bqa = button.get("data_qa")
                if not (isinstance(bqa, str) and re.fullmatch(r"(?:captcha|account-captcha)-[A-Za-z0-9_-]{1,64}", bqa)):
                    bqa = None
                button_type = button.get("type")
                result["buttons"].append({
                    "tag": button["tag"], "data_qa": bqa,
                    "type": button_type if isinstance(button_type, str) and button_type in {"submit", "button", "reset"} else None,
                    "enabled": button.get("enabled") is True,
                    "visible": button.get("visible") is True,
                })
        return result
    output = []
    for chain in raw[:4]:
        if not isinstance(chain, list):
            continue
        safe_chain = [shape for node in chain[:8] if (shape := safe_shape(node)) is not None]
        if safe_chain:
            output.append(safe_chain)
    return output


def capture_challenge_structure(page) -> dict:
    """Inspect visible iframes and return only allowlisted structural metadata.

    The browser returns canonical host/path prefixes, never a full URL, query,
    sitekey, image, token, text or frame contents. Unknown HH challenges stay
    unknown and must be handled by the existing manual pause.
    """
    image_dialogs, image_probe = _image_captcha_dialog_count(page)
    hh_image_pairs = _hh_image_pair_count(page)
    empty = {"captcha_type": "unknown", "classification_confidence": "none",
             "visible_iframe_count": None, "recognized_marker_count": 0,
             "image_candidate_dialog_count": image_dialogs,
             "hh_image_pair_count": hh_image_pairs,
             "image_probe_status": image_probe, "probe_status": "unavailable"}
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
    if image_dialogs == 1 or hh_image_pairs == 1:
        kind = "image_to_text" if not safe_markers else "unknown"
    result = {"captcha_type": kind,
            "classification_confidence": "structural" if kind != "unknown" else "none",
            "visible_iframe_count": visible,
            "recognized_marker_count": len(safe_markers),
            "image_candidate_dialog_count": image_dialogs,
            "hh_image_pair_count": hh_image_pairs,
            "image_probe_status": image_probe,
            "probe_status": "ok"}
    if kind in {"unknown", "image_to_text"} and hh_image_pairs == 1:
        result["hh_image_structure"] = _unknown_image_structure(page)
    elif kind == "unknown":
        result["unknown_image_structure"] = _unknown_image_structure(page)
    return result


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


@dataclass(frozen=True, repr=False)
class ImageSolution:
    state: str  # ready | processing | error | stopped | timeout
    text: str = ""
    cost_usd: Decimal | None = None
    reason: str = ""

    def __repr__(self):
        return f"ImageSolution(state={self.state!r}, reason={self.reason!r})"


class AntiCaptchaClient:
    """Official API client with a 30-second balance cache.

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

    def create_image_task(self, image: bytes) -> tuple[int | None, str]:
        """Send only the cropped challenge image; never expose the API response text."""
        if not self._key or not image or len(image) > 500_000:
            return None, "invalid_image_or_key"
        try:
            response = self._post(CREATE_TASK_URL, json={
                "clientKey": self._key,
                "task": {"type": "ImageToTextTask",
                         "body": base64.b64encode(image).decode("ascii"),
                         "languagePool": "rn"}}, timeout=10)
            body = response.json() if response.status_code == 200 else None
            task_id = body.get("taskId") if isinstance(body, dict) and body.get("errorId") == 0 else None
            return (task_id, "created") if type(task_id) is int and task_id > 0 else (None, "task_creation_failed")
        except (requests.RequestException, ValueError, TypeError):
            return None, "task_transport_error"

    def get_image_solution(self, task_id: int, *, timeout_seconds: float = 10) -> ImageSolution:
        if not self._key or type(task_id) is not int or task_id <= 0:
            return ImageSolution("error", reason="invalid_task_or_key")
        try:
            response = self._post(TASK_RESULT_URL,
                                  json={"clientKey": self._key, "taskId": task_id},
                                  timeout=min(10, timeout_seconds))
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or body.get("errorId") != 0:
                return ImageSolution("error", reason="task_result_failed")
            if body.get("status") == "processing":
                return ImageSolution("processing")
            solution = body.get("solution")
            answer = solution.get("text") if isinstance(solution, dict) else None
            if (body.get("status") == "ready" and isinstance(answer, str)
                    and 0 < len(answer) <= 128 and answer.isprintable()):
                return ImageSolution("ready", text=answer, cost_usd=_money(body.get("cost")))
            return ImageSolution("error", reason="task_result_invalid")
        except (requests.RequestException, ValueError, TypeError):
            return ImageSolution("error", reason="task_transport_error")

    def wait_for_image_solution(self, task_id: int, *, max_wait_seconds: int,
                                stopped, sleep=time.sleep, poll_seconds: int = 2) -> ImageSolution:
        if max_wait_seconds <= 0 or poll_seconds <= 0:
            return ImageSolution("error", reason="invalid_wait_limit")
        deadline = self._clock() + max_wait_seconds
        while True:
            if stopped():
                return ImageSolution("stopped")
            remaining = deadline - self._clock()
            if remaining <= 0:
                return ImageSolution("timeout")
            result = self.get_image_solution(task_id, timeout_seconds=min(10, remaining))
            if result.state != "processing":
                return result
            sleep(min(poll_seconds, max(0, deadline - self._clock())))

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


def image_challenge_controls(page):
    """Return unambiguous visible image/field/action controls, or None."""
    try:
        # The HH image and input have exact challenge IDs. In the observed
        # modal, the CAPTCHA submit is the sole submit button inside its
        # dialog; the main application submit is outside that dialog.
        if _hh_image_pair_count(page) == 1:
            image = page.locator('img[data-qa="account-captcha-picture"]:visible')
            field = page.locator('input[data-qa="account-captcha-input"]:visible')
            dialog = image.locator('xpath=ancestor::*[@role="dialog"][1]')
            if dialog.count() == 1:
                if dialog.locator('input[data-qa="account-captcha-input"]:visible').count() != 1:
                    return None
                action = dialog.locator('button[type="submit"]:visible')
                return (image, field, action) if action.count() == 1 and action.is_enabled() else None
            # Keep the existing form variant, but require the exact same pair
            # and a unique semantic submit within that form.
            form = field.locator('xpath=ancestor::form[1]')
            if (form.count() == 1 and
                    form.locator('img[data-qa="account-captcha-picture"]:visible').count() == 1):
                action = form.locator('button[type="submit"]:visible')
                if action.count() == 1 and action.is_enabled():
                    return image, field, action
            return None
        dialog = page.locator('[role="dialog"]:visible').filter(
            has=page.locator('[data-qa*="captcha"]:visible'))
        if dialog.count() != 1:
            return None
        image = dialog.locator('img[data-qa*="captcha"]:visible, '
                               '[data-qa*="captcha"] img:visible')
        field = dialog.locator('input[type="text"]:visible, input:not([type]):visible, '
                              'input[type="search"]:visible')
        action = dialog.locator('button:visible, [role="button"]:visible')
        if image.count() != 1 or field.count() != 1 or action.count() != 1:
            return None
        if not field.is_enabled() or not action.is_enabled():
            return None
        return image, field, action
    except Exception:
        return None


def image_control_failure_reason(page) -> str:
    """Allowlisted reason for a failed structural CAPTCHA control lookup."""
    try:
        if _hh_image_pair_count(page) != 1:
            return "captcha_image_or_input_not_unique"
        image = page.locator('img[data-qa="account-captcha-picture"]:visible')
        dialog = image.locator('xpath=ancestor::*[@role="dialog"][1]')
        if dialog.count() == 1:
            if dialog.locator('input[data-qa="account-captcha-input"]:visible').count() != 1:
                return "captcha_input_outside_dialog"
            actions = dialog.locator('button[type="submit"]:visible')
        else:
            field = page.locator('input[data-qa="account-captcha-input"]:visible')
            form = field.locator('xpath=ancestor::form[1]')
            if (form.count() != 1 or
                    form.locator('img[data-qa="account-captcha-picture"]:visible').count() != 1):
                return "captcha_control_scope_missing"
            actions = form.locator('button[type="submit"]:visible')
        count = actions.count()
        if count == 0:
            return "captcha_submit_missing"
        if count != 1:
            return "captcha_submit_ambiguous"
        if not actions.is_enabled():
            return "captcha_submit_disabled"
        return "captcha_controls_unavailable"
    except Exception:
        return "captcha_controls_unavailable"


def hh_wrong_image_answer_visible(page) -> bool:
    """Recognize the observed HH rejection only beside an active exact CAPTCHA pair."""
    try:
        if _hh_image_pair_count(page) != 1:
            return False
        error = page.get_by_text("Неверный текст. Пожалуйста, повторите попытку", exact=True)
        # Hidden copies and nested portal nodes must not hide one visible error.
        return any(error.nth(index).is_visible() for index in range(min(error.count(), 20)))
    except Exception:
        return False


def hh_image_loaded(image) -> bool:
    """Return only image readiness; never read or persist its source or pixels."""
    try:
        return image.evaluate("el => el.complete && el.naturalWidth > 0 && el.naturalHeight > 0") is True
    except Exception:
        return False


def hh_rejection_probe(page) -> dict:
    """Bounded structural/error-state counts for a failed HH CAPTCHA check."""
    result = {"exact_error_visible_count": 0, "input_aria_invalid": False,
              "active_image_pair": False}
    try:
        if _hh_image_pair_count(page) != 1:
            return result
        result["active_image_pair"] = True
        field = page.locator('input[data-qa="account-captcha-input"]:visible')
        result["input_aria_invalid"] = field.get_attribute("aria-invalid") == "true"
        error = page.get_by_text("Неверный текст. Пожалуйста, повторите попытку", exact=True)
        result["exact_error_visible_count"] = sum(
            error.nth(index).is_visible() for index in range(min(error.count(), 20)))
    except Exception:
        pass
    return result


_HH_ERROR_SHAPE_JS = """el => {
  const known = 'Неверный текст. Пожалуйста, повторите попытку';
  const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
  const qa = el.getAttribute('data-qa') || '';
  const role = el.getAttribute('role') || '';
  const ariaInvalid = el.getAttribute('aria-invalid');
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return {
    tag: ['div', 'span', 'p', 'small', 'label', 'input', 'form', 'section'].includes(el.tagName.toLowerCase())
      ? el.tagName.toLowerCase() : 'other',
    role: ['alert', 'status', 'dialog', 'alertdialog', 'textbox'].includes(role) ? role : null,
    data_qa: /^[a-zA-Z0-9_-]{1,80}$/.test(qa) &&
      (/^(account-captcha|captcha)-[a-zA-Z0-9_-]+$/.test(qa) ||
       ['error', 'form-error', 'validation-error'].includes(qa))
      ? qa : null,
    aria_invalid: ariaInvalid === 'true' || ariaInvalid === 'false' ? ariaInvalid : null,
    visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden',
    known_message_match: normalize(el.textContent).includes(known)
  };
}"""


def hh_error_structure_snapshot(page) -> dict:
    """One bounded DOM shape after CAPTCHA submit; never return text or field values."""
    result = {"status": "unavailable", "field_total_count": 0, "field_visible_count": 0,
              "field_aria_invalid": None, "field_has_aria_describedby": False,
              "field_has_aria_errormessage": False, "nearby": [], "linked_message_nodes": [],
              "semantic_message_nodes": [], "semantic_message_total_count": 0,
              "exact_message_nodes": [],
              "exact_message_total_count": 0}
    try:
        fields = page.locator('input[data-qa="account-captcha-input"]')
        visible_fields = page.locator('input[data-qa="account-captcha-input"]:visible')
        result["field_total_count"] = min(fields.count(), 20)
        result["field_visible_count"] = min(visible_fields.count(), 20)
        if visible_fields.count() != 1:
            result["status"] = "field_not_unique"
            return result
        field = visible_fields
        aria_invalid = field.get_attribute("aria-invalid")
        result["field_aria_invalid"] = aria_invalid if aria_invalid in ("true", "false") else None
        result["field_has_aria_describedby"] = bool(field.get_attribute("aria-describedby"))
        result["field_has_aria_errormessage"] = bool(field.get_attribute("aria-errormessage"))
        result["nearby"] = field.evaluate("""el => {
          const parent = el.parentElement;
          const grandparent = parent && parent.parentElement;
          const nodes = [parent, parent && parent.previousElementSibling,
            parent && parent.nextElementSibling, grandparent,
            ...(grandparent ? [...grandparent.children].slice(0, 8) : [])];
          const shape = """ + _HH_ERROR_SHAPE_JS + """;
          return [...new Set(nodes.filter(Boolean))].slice(0, 8).map(shape);
        }""")
        result["linked_message_nodes"] = field.evaluate("""el => {
          const shape = """ + _HH_ERROR_SHAPE_JS + """;
          const ids = ['aria-errormessage', 'aria-describedby']
            .flatMap(name => (el.getAttribute(name) || '').split(/\\s+/).filter(Boolean));
          return [...new Set(ids.slice(0, 4).map(id => document.getElementById(id)).filter(Boolean))]
            .map(shape);
        }""")
        semantic = page.locator('[role="alert"], [role="status"], '
                                '[aria-live="assertive"], [aria-live="polite"]')
        result["semantic_message_total_count"] = min(semantic.count(), 100)
        result["semantic_message_nodes"] = semantic.evaluate_all("""nodes => {
          const shape = """ + _HH_ERROR_SHAPE_JS + """;
          const field = document.querySelector('input[data-qa="account-captcha-input"]');
          const scope = field && field.closest('[role="dialog"], form');
          const shaped = nodes.slice(0, 100).map(node => ({...shape(node),
            relation: scope && scope.contains(node) ? 'same_captcha_scope' : 'outside_captcha_scope'}));
          return [...shaped.filter(node => node.known_message_match ||
            node.relation === 'same_captcha_scope'), ...shaped].filter(
              (node, index, all) => all.indexOf(node) === index).slice(0, 8);
        }""")
        error = page.get_by_text("Неверный текст. Пожалуйста, повторите попытку", exact=True)
        result["exact_message_total_count"] = min(error.count(), 20)
        result["exact_message_nodes"] = error.evaluate_all("""nodes => {
          const shape = """ + _HH_ERROR_SHAPE_JS + """;
          const field = document.querySelector('input[data-qa="account-captcha-input"]');
          const scope = field && field.closest('[role="dialog"], form');
          return nodes.slice(0, 8).map(node => ({...shape(node),
            relation: scope && scope.contains(node) ? 'same_captcha_scope' : 'outside_captcha_scope'}));
        }""")
        result["status"] = "ok"
    except Exception:
        result["status"] = "probe_error"
        result["nearby"] = []
        result["linked_message_nodes"] = []
        result["semantic_message_nodes"] = []
        result["exact_message_nodes"] = []
    return result


def challenge_digest(image: bytes, api_key: str) -> str:
    return hmac.new(api_key.encode("utf-8"), image, hashlib.sha256).hexdigest()


class ImageTaskRegistry:
    """Claim each image before createTask, including uncertain transport results."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def claim(self, image: bytes, api_key: str) -> bool:
        digest = challenge_digest(image, api_key)
        with self._lock:
            if digest in self._seen:
                return False
            self._seen.add(digest)
            return True


IMAGE_TASKS = ImageTaskRegistry()
