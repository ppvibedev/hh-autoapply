"""Offline preparation checks; no paid task, HH, or external API call."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

import requests

from anti_captcha import (AntiCaptchaClient, BalanceResult, TaskStatus, ImageSolution,
                          ImageTaskRegistry, capture_challenge_structure, image_challenge_controls,
                          image_control_failure_reason, hh_wrong_image_answer_visible,
                          classify_challenge_markers)


class AntiCaptchaPreparationTests(unittest.TestCase):
    def test_visible_structural_marker_snapshot_is_allowlisted(self):
        private = "secret-sitekey-or-contact@example.test"
        page = Mock()
        image_probe, iframe_probe = Mock(), Mock()
        page.locator.side_effect = lambda selector: image_probe if selector == '[role="dialog"]:visible' else iframe_probe
        image_probe.evaluate_all.return_value = 0
        iframe_probe.evaluate_all.return_value = {
            "visible": 2,
            "markers": [["www.google.com", "/recaptcha/"],
                        ["private.example", private]],
            "text": private, "sitekey": private, "cookies": private}
        snapshot = capture_challenge_structure(page)
        self.assertEqual(snapshot, {
            "captcha_type": "recaptcha", "classification_confidence": "structural",
            "visible_iframe_count": 2, "recognized_marker_count": 1,
            "image_candidate_dialog_count": 0, "image_probe_status": "ok",
            "hh_image_pair_count": 0, "probe_status": "ok"})
        self.assertNotIn(private, repr(snapshot))

    def test_image_dialog_is_structural_and_privacy_safe(self):
        private = "private-captcha-answer-or-contact@example.test"
        page = Mock()
        image_probe, iframe_probe = Mock(), Mock()
        page.locator.side_effect = lambda selector: image_probe if selector == '[role="dialog"]:visible' else iframe_probe
        image_probe.evaluate_all.return_value = 1
        iframe_probe.evaluate_all.return_value = {
            "visible": 0, "markers": [], "text": private, "image": private}
        snapshot = capture_challenge_structure(page)
        self.assertEqual(snapshot["captcha_type"], "image_to_text")
        self.assertEqual(snapshot["classification_confidence"], "structural")
        self.assertEqual(snapshot["image_candidate_dialog_count"], 1)
        self.assertNotIn(private, repr(snapshot))
        iframe_probe.evaluate_all.return_value = {
            "visible": 1, "markers": [["www.google.com", "/recaptcha/"]]}
        self.assertEqual(capture_challenge_structure(page)["captcha_type"], "unknown")

    def test_real_hh_image_pair_without_dialog_is_classified(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<div><div>
                  <img data-qa="account-captcha-picture" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
                  <button data-qa="captcha-renew-text" type="button"></button>
                  <button data-qa="captcha-language" type="button"></button>
                  <input data-qa="account-captcha-input" type="text">
                  </div></div>''')
                snapshot = capture_challenge_structure(page)
                self.assertEqual(snapshot["captcha_type"], "image_to_text")
                self.assertEqual(snapshot["image_candidate_dialog_count"], 0)
                self.assertEqual(snapshot["hh_image_pair_count"], 1)
                self.assertNotIn("R0lGOD", repr(snapshot))
                self.assertIsNone(image_challenge_controls(page))
                self.assertEqual(image_control_failure_reason(page), "captcha_control_scope_missing")
                page.set_content('''<form><div>
                  <img data-qa="account-captcha-picture" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
                  <button data-qa="captcha-renew-text" type="button"></button>
                  <button data-qa="captcha-language" type="button"></button>
                  <input data-qa="account-captcha-input" type="text">
                  <button type="submit"></button>
                  </div></form>''')
                controls = image_challenge_controls(page)
                self.assertIsNotNone(controls)
                self.assertEqual(controls[0].get_attribute("data-qa"), "account-captcha-picture")
                self.assertEqual(controls[1].get_attribute("data-qa"), "account-captcha-input")
                self.assertEqual(controls[2].get_attribute("type"), "submit")
                page.locator('button[type="submit"]').evaluate('button => button.disabled = true')
                self.assertIsNone(image_challenge_controls(page))
                self.assertEqual(image_control_failure_reason(page), "captcha_submit_disabled")
            finally:
                browser.close()

    def test_observed_hh_dialog_routes_to_offline_handler_without_main_submit(self):
        import run_web
        from playwright.sync_api import sync_playwright
        from unittest.mock import MagicMock
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<main><button type="submit" id="application-submit"></button></main>
                  <div role="dialog" style="width:300px;height:200px">
                    <button type="button" id="close"></button>
                    <div><div><div><div><div><div>
                      <img data-qa="account-captcha-picture" width="80" height="30"
                           src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
                      <button data-qa="captcha-renew-text" type="button"></button>
                      <button data-qa="captcha-language" type="button"></button>
                      <input data-qa="account-captcha-input" type="text">
                    </div></div></div></div></div></div>
                    <input type="text"><button type="submit" id="captcha-submit"
                      onclick="event.preventDefault(); this.closest('[role=dialog]').remove()"></button>
                  </div>''')
                client = MagicMock()
                client.get_balance.return_value = BalanceResult(True, Decimal("1"))
                client.create_image_task.return_value = (123, None)
                client.wait_for_image_solution.return_value = ImageSolution("ready", "sample")
                rows = []
                with patch("config.ANTI_CAPTCHA_ENABLED", True), \
                     patch("config.ANTI_CAPTCHA_API_KEY", "offline-test-key"), \
                     patch("anti_captcha.AntiCaptchaClient", return_value=client), \
                     patch("anti_captcha.IMAGE_TASKS", ImageTaskRegistry()), \
                     patch("hh_web.safe_captcha_path", return_value="/vacancy/123"), \
                     patch("hh_web.captcha_resolution_verified",
                           side_effect=lambda *_: page.locator('[role="dialog"]').count() == 0):
                    run_web._wait_for_manual_captcha(
                        page, operation="application_post_submit", run_id="f" * 32,
                        vacancy_id="123", stopped=lambda: False, telemetry=rows.append,
                        sleep=lambda _: None)
                self.assertEqual(client.create_image_task.call_count, 1)
                self.assertEqual(page.locator('[role="dialog"]').count(), 0)
                self.assertEqual(page.locator('#application-submit').count(), 1)
                self.assertEqual(rows[0]["captcha_type"], "image_to_text")
                self.assertIn("captcha_auto_resolved", [row["event"] for row in rows])
                self.assertEqual(rows[-1]["event"], "captcha_wait_resolved")
                self.assertNotIn("offline-test-key", repr(rows))
            finally:
                browser.close()

    def test_observed_hh_dialog_ambiguous_submit_stays_manual(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<button type="submit" id="application-submit"></button>
                  <div role="dialog" style="width:300px;height:200px">
                    <img data-qa="account-captcha-picture" width="80" height="30"
                      src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
                    <input data-qa="account-captcha-input" type="text">
                    <button type="submit"></button><button type="submit"></button>
                  </div>''')
                self.assertIsNone(image_challenge_controls(page))
                self.assertEqual(image_control_failure_reason(page), "captcha_submit_ambiguous")
                self.assertEqual(page.locator('#application-submit').count(), 1)
            finally:
                browser.close()

    def test_hh_wrong_answer_marker_is_boolean_only(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<div role="dialog" style="width:300px;height:200px">
                  <img data-qa="account-captcha-picture" width="80" height="30"
                    src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
                  <p>Неверный текст. Пожалуйста, повторите попытку</p></div>''')
                self.assertIs(hh_wrong_image_answer_visible(page), True)
                page.locator("p").evaluate("element => element.remove()")
                self.assertIs(hh_wrong_image_answer_visible(page), False)
            finally:
                browser.close()

    def test_unknown_ambiguous_and_probe_failure_stay_unknown(self):
        page = Mock()
        image_probe, iframe_probe = Mock(), Mock()
        page.locator.side_effect = lambda selector: image_probe if selector == '[role="dialog"]:visible' else iframe_probe
        image_probe.evaluate_all.return_value = 0
        probe = iframe_probe.evaluate_all
        probe.return_value = {"visible": 1, "markers": []}
        self.assertEqual(capture_challenge_structure(page)["captcha_type"], "unknown")
        probe.return_value = {"visible": 2, "markers": [
            ["www.google.com", "/recaptcha/"],
            ["hcaptcha.com", "/1/"]]}
        self.assertEqual(capture_challenge_structure(page)["captcha_type"], "unknown")
        probe.side_effect = RuntimeError("private URL?sitekey=secret")
        snapshot = capture_challenge_structure(page)
        self.assertEqual(snapshot["probe_status"], "unavailable")
        self.assertNotIn("secret", repr(snapshot))

    def test_known_structures_and_unknown_or_ambiguous_type(self):
        self.assertEqual(classify_challenge_markers(
            [("www.google.com", "/recaptcha/api2/anchor")]), "recaptcha")
        self.assertEqual(classify_challenge_markers(
            [("hcaptcha.com", "/1/api.js")]), "hcaptcha")
        self.assertEqual(classify_challenge_markers(
            [("challenges.cloudflare.com", "/turnstile/v0/api.js")]), "turnstile")
        self.assertEqual(classify_challenge_markers(
            [("www.google.com", "/maps")]), "unknown")
        self.assertEqual(classify_challenge_markers([]), "unknown")
        self.assertEqual(classify_challenge_markers([
            ("www.google.com", "/recaptcha/api2/anchor"),
            ("hcaptcha.com", "/1/api.js")]), "unknown")

    def test_missing_key_never_calls_transport(self):
        post = Mock(side_effect=AssertionError("network forbidden"))
        result = AntiCaptchaClient("", post=post).get_balance()
        self.assertEqual(result.reason, "missing_api_key")
        post.assert_not_called()

    def test_official_balance_is_cached_and_never_exposes_key(self):
        clock = Mock(return_value=10.0)
        post = Mock(return_value=Mock(status_code=200,
                                      json=lambda: {"errorId": 0, "balance": 4.5}))
        client = AntiCaptchaClient("PRIVATE_KEY", post=post, clock=clock)
        self.assertEqual(client.get_balance().amount_usd, Decimal("4.5"))
        self.assertEqual(client.get_balance().amount_usd, Decimal("4.5"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 10)
        self.assertNotIn("PRIVATE_KEY", repr(client.get_balance()))
        clock.return_value = 41.0
        client.get_balance()
        self.assertEqual(post.call_count, 2)

    def test_transport_timeout_and_service_error_are_redacted(self):
        secret = "PRIVATE_KEY"
        post = Mock(side_effect=requests.Timeout(secret + " private URL"))
        result = AntiCaptchaClient(secret, post=post).get_balance()
        self.assertEqual(result.reason, "balance_transport_error")
        self.assertNotIn(secret, repr(result))
        post = Mock(return_value=Mock(status_code=200,
                                      json=lambda: {"errorId": 1,
                                                    "errorDescription": secret}))
        result = AntiCaptchaClient(secret, post=post).get_balance()
        self.assertEqual(result.reason, "balance_service_error")
        self.assertNotIn(secret, repr(result))

    def test_image_task_uses_only_cropped_image_and_solution_is_redacted(self):
        secret = "PRIVATE_CAPTCHA_ANSWER"
        post = Mock(side_effect=[
            Mock(status_code=200, json=lambda: {"errorId": 0, "taskId": 123}),
            Mock(status_code=200, json=lambda: {"errorId": 0, "status": "ready",
                                                  "solution": {"text": secret}, "cost": "0.001"})])
        client = AntiCaptchaClient("DUMMY_KEY", post=post)
        task_id, reason = client.create_image_task(b"synthetic-image-only")
        self.assertEqual((task_id, reason), (123, "created"))
        payload = post.call_args_list[0].kwargs["json"]
        self.assertEqual(set(payload), {"clientKey", "task"})
        self.assertEqual(set(payload["task"]), {"type", "body", "languagePool"})
        self.assertEqual(payload["task"]["languagePool"], "rn")
        result = client.get_image_solution(task_id)
        self.assertEqual(result.text, secret)
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result.cost_usd, Decimal("0.001"))

    def test_concurrent_duplicate_image_creates_one_claim(self):
        registry = ImageTaskRegistry()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: registry.claim(b"synthetic-image", "DUMMY_KEY"),
                                    range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertTrue(registry.claim(b"different-image", "DUMMY_KEY"))

    def test_image_task_timeout_and_stop_are_bounded(self):
        now = [0.0]
        client = AntiCaptchaClient("DUMMY_KEY", post=Mock(), clock=lambda: now[0])
        client.get_image_solution = Mock(return_value=ImageSolution("processing"))
        def sleep(seconds):
            now[0] += seconds
        result = client.wait_for_image_solution(7, max_wait_seconds=3,
                                                stopped=lambda: False, sleep=sleep)
        self.assertEqual(result.state, "timeout")
        self.assertLessEqual(now[0], 3)
        client.get_image_solution.reset_mock()
        result = client.wait_for_image_solution(7, max_wait_seconds=3,
                                                stopped=lambda: True, sleep=sleep)
        self.assertEqual(result.state, "stopped")
        client.get_image_solution.assert_not_called()

    def test_existing_task_status_discards_solution_and_private_error(self):
        secret = "PRIVATE_KEY"
        post = Mock(return_value=Mock(status_code=200,
                                      json=lambda: {"errorId": 0, "status": "ready",
                                                    "cost": "0.002", "solution": {"text": secret}}))
        client = AntiCaptchaClient(secret, post=post)
        result = client.get_task_status(123)
        self.assertEqual(result, TaskStatus("ready", Decimal("0.002")))
        self.assertNotIn(secret, repr(result))
        self.assertEqual(post.call_args.kwargs["timeout"], 10)

    def test_task_polling_timeout_stop_and_service_error(self):
        now = [0.0]
        def clock():
            return now[0]
        def sleep(seconds):
            now[0] += seconds
        post = Mock(return_value=Mock(status_code=200,
                                      json=lambda: {"errorId": 0, "status": "processing"}))
        client = AntiCaptchaClient("DUMMY_KEY", post=post, clock=clock)
        self.assertEqual(client.wait_for_task(123, max_wait_seconds=5,
                                              stopped=lambda: False, sleep=sleep).state,
                         "timeout")
        self.assertLessEqual(post.call_count, 3)
        post.reset_mock()
        self.assertEqual(client.wait_for_task(123, max_wait_seconds=5,
                                              stopped=lambda: True, sleep=sleep).state,
                         "stopped")
        post.assert_not_called()
        post.return_value = Mock(status_code=200,
                                 json=lambda: {"errorId": 1, "errorDescription": "private"})
        self.assertEqual(client.get_task_status(123).reason, "task_service_error")


if __name__ == "__main__":
    unittest.main()
