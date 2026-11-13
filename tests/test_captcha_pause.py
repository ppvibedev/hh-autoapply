"""Offline CAPTCHA pause/resume regression tests; no HH or external LLM."""
import unittest
import json
from pathlib import Path
from decimal import Decimal
from unittest.mock import MagicMock, patch

import config
from llm_errors import LLMStopped
import run_web
import test_safe_mvp
import webapp
import hh_web
import application
from anti_captcha import BalanceResult, ImageSolution


class CaptchaPauseTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def _auto_image_patches(self, image, field, action, client, registry):
        structure = {"captcha_type": "image_to_text", "classification_confidence": "structural"}
        image.evaluate.return_value = True
        return (patch("anti_captcha.capture_challenge_structure", return_value=structure),
                patch("anti_captcha.image_challenge_controls", return_value=(image, field, action)),
                patch("anti_captcha.AntiCaptchaClient", return_value=client),
                patch("anti_captcha.IMAGE_TASKS", registry),
                patch.object(config, "ANTI_CAPTCHA_ENABLED", True),
                patch.object(config, "ANTI_CAPTCHA_API_KEY", "DUMMY_KEY"),
                patch.object(config, "ANTI_CAPTCHA_MAX_WAIT_SECONDS", 3),
                patch("hh_web.safe_captcha_path", return_value="/vacancy/123"))

    def test_auto_image_answer_requires_hh_verification(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.return_value = b"synthetic-image"
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "synthetic-answer", Decimal("0.001"))
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=True) as verified:
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append)
        field.fill.assert_called_once_with("synthetic-answer", timeout=3000)
        action.click.assert_called_once_with(timeout=3000)
        self.assertEqual(verified.call_count, 2)
        self.assertEqual([r["event"] for r in rows if r["event"].startswith("captcha_auto_task")],
                         ["captcha_auto_task_created", "captcha_auto_task_result"])
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_auto_task_result")["state"],
                         "ready")
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_hh_verification_result")["verified"],
                         True)
        self.assertEqual(rows[-1]["event"], "captcha_wait_resolved")
        self.assertNotIn("synthetic-answer", json.dumps(rows))
        self.assertNotIn("DUMMY_KEY", json.dumps(rows))

    def test_changed_image_discards_stale_answer_and_retries_new_image(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"old-image", b"old-image", b"new-image",
                                        b"new-image", b"new-image", b"new-image", b"new-image"]
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "stale"),
                                                      ImageSolution("ready", "fresh")]
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=True):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             sleep=lambda _: None)
        field.fill.assert_called_once_with("fresh", timeout=3000)
        self.assertEqual(action.click.call_count, 1)
        self.assertEqual(client.create_image_task.call_count, 2)
        self.assertEqual([r["event"] for r in rows if r["event"] == "captcha_auto_retry"],
                         ["captcha_auto_retry"])

    def test_missing_balance_keeps_manual_pause_without_paid_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.return_value = b"synthetic-image"
        client.get_balance.return_value = BalanceResult(True, Decimal("0"))
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_pre_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows),
                                             telemetry=rows.append)
        client.create_image_task.assert_not_called()
        field.fill.assert_not_called(); action.click.assert_not_called()

    def test_recognized_hh_captcha_with_ambiguous_submit_logs_reason_without_paid_task(self):
        rows = []
        structure = {"captcha_type": "image_to_text", "classification_confidence": "structural"}
        with patch("anti_captcha.capture_challenge_structure", return_value=structure), \
             patch("anti_captcha.image_challenge_controls", return_value=None), \
             patch("anti_captcha.image_control_failure_reason",
                   return_value="captcha_submit_ambiguous"), \
             patch("anti_captcha.AntiCaptchaClient") as client, \
             patch.object(config, "ANTI_CAPTCHA_ENABLED", True), \
             patch.object(config, "ANTI_CAPTCHA_API_KEY", "DUMMY_KEY"), \
             patch("hh_web.safe_captcha_path", return_value="/vacancy/123"), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="application_post_submit", run_id="e" * 32,
                vacancy_id="123", telemetry=rows.append,
                stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows))
        self.assertEqual(rows[1]["event"], "captcha_auto_fallback")
        self.assertEqual(rows[1]["reason"], "captcha_submit_ambiguous")
        client.return_value.create_image_task.assert_not_called()
        self.assertNotIn("DUMMY_KEY", json.dumps(rows))

    def test_unaccepted_image_answer_stays_on_same_vacancy_without_second_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.return_value = b"synthetic-image"
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "wrong", Decimal("0.001"))
        registry.claim.return_value = True
        rows, now = [], [0]
        def sleep(seconds):
            now[0] += seconds
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=False), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: now[0] >= 15, sleep=sleep,
                                             telemetry=rows.append)
        self.assertEqual(action.click.call_count, 1)
        client.create_image_task.assert_called_once()
        self.assertEqual(now[0], 15.5)
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_hh_verification_result")["verified"],
                         False)
        self.assertEqual(rows[-2]["reason"], "hh_not_verified")
        self.assertEqual(rows[0]["vacancy_id"], "123")

    def test_new_image_without_explicit_rejection_stays_manual(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"first"] * 3 + [b"second"] * 40
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "wrong"),
                                                      ImageSolution("ready", "correct")]
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=False), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows),
                                             telemetry=rows.append,
                                             sleep=lambda _: None)
        self.assertEqual(client.create_image_task.call_count, 1)
        self.assertEqual(action.click.call_count, 1)
        self.assertEqual([row["attempt"] for row in rows if row["event"] == "captcha_auto_started"], [1])
        self.assertEqual(next(row for row in rows if row["event"] == "captcha_hh_verification_result")["state"],
                         "new_challenge")
        self.assertEqual(next(row for row in rows if row["event"] == "captcha_auto_fallback")["reason"],
                         "hh_not_verified")

    def test_delayed_error_structure_is_saved_once_without_retry_or_private_text(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"first"] * 3 + [b"second"] * 40
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "PRIVATE_ANSWER")
        registry.claim.return_value = True
        rows, now = [], [0.0]
        def sleep(seconds):
            now[0] += seconds
        def structure(_):
            return {"status": "ok", "field_aria_invalid": "true" if now[0] >= 6 else None,
                    "exact_message_total_count": 1 if now[0] >= 6 else 0,
                    "nearby": [], "exact_message_nodes": []}
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=False), \
             patch("anti_captcha.hh_wrong_image_answer_visible", return_value=False), \
             patch("anti_captcha.hh_error_structure_snapshot", side_effect=structure), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows),
                                             telemetry=rows.append, clock=lambda: now[0], sleep=sleep)
        traces = [r for r in rows if r["event"] == "captcha_hh_error_structure"]
        self.assertEqual(len(traces), 1)
        samples = traces[0]["samples"]
        self.assertLessEqual(len(samples), 8)
        self.assertEqual(samples[0]["elapsed_ms"], 0)
        self.assertTrue(any(sample["elapsed_ms"] >= 6000 and
                            sample["structure"]["field_aria_invalid"] == "true" for sample in samples))
        self.assertEqual(client.create_image_task.call_count, 1)
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(rows))

    def test_preflight_refresh_does_not_consume_paid_retry_after_rejection(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = ([b"old", b"new"] + [b"new"] * 3 +
                                        [b"third"] * 12)
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "wrong"),
                                                      ImageSolution("ready", "correct")]
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("anti_captcha.hh_wrong_image_answer_visible", return_value=True), \
             patch("hh_web.captcha_resolution_verified", side_effect=[False, True, True]):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             sleep=lambda _: None)
        preflight = next(r for r in rows if r.get("reason") == "preflight_challenge_changed")
        self.assertEqual(preflight["paid_tasks"], 0)
        self.assertEqual([r["attempt"] for r in rows if r["event"] == "captcha_auto_started"], [1, 2])
        self.assertEqual(client.create_image_task.call_count, 2)

    def test_preflight_refresh_limit_stops_without_paid_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [bytes([n]) for n in range(8)]
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows),
                                             telemetry=rows.append, sleep=lambda _: None)
        self.assertEqual(sum(r.get("reason") == "preflight_challenge_changed" for r in rows), 3)
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_auto_fallback")["reason"],
                         "preflight_refresh_limit")
        client.create_image_task.assert_not_called()

    def test_image_loads_two_seconds_after_modal_without_paid_preflight_attempt(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.return_value = b"loaded-image"
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "answer")
        registry.claim.return_value = True
        rows, now, task_times = [], [0.0], []
        image.evaluate.side_effect = lambda *_: now[0] >= 2
        client.create_image_task.side_effect = lambda *_: (task_times.append(now[0]) or (7, "created"))
        def sleep(seconds):
            now[0] += seconds
        patches = self._auto_image_patches(image, field, action, client, registry)
        page = MagicMock()
        with patch("anti_captcha.capture_challenge_structure",
                   side_effect=lambda _: ({"captcha_type": "unknown", "probe_status": "ok"}
                                          if now[0] < 2 else {"captcha_type": "image_to_text"})), \
             patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_present", return_value=True), \
             patch("hh_web.captcha_resolution_verified", return_value=True):
            run_web._wait_for_manual_captcha(page, operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             clock=lambda: now[0], sleep=sleep)
        self.assertEqual(len(task_times), 1)
        self.assertGreaterEqual(task_times[0], 2.5)
        self.assertNotIn("preflight_challenge_changed", [r.get("reason") for r in rows])
        self.assertEqual(action.click.call_count, 1)
        page.goto.assert_not_called()
        page.reload.assert_not_called()
        self.assertNotIn("loaded-image", json.dumps(rows))
        self.assertNotIn("answer", json.dumps(rows))

    def test_stop_during_initial_image_loading_creates_no_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.evaluate.return_value = False
        rows, stopped = [], [False]
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: stopped[0], telemetry=rows.append,
                                             sleep=lambda _: stopped.__setitem__(0, True))
        client.create_image_task.assert_not_called()
        action.click.assert_not_called()

    def test_explicit_hh_rejection_and_new_image_retries_once(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"first"] * 3 + [b"second"] * 12
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "wrong"),
                                                      ImageSolution("ready", "correct")]
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("anti_captcha.hh_wrong_image_answer_visible", return_value=True), \
             patch("hh_web.captcha_resolution_verified", side_effect=[False, True, True]):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             sleep=lambda _: None)
        self.assertEqual(client.create_image_task.call_count, 2)
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_hh_verification_result")["state"],
                         "explicit_rejected")
        self.assertEqual(rows[-1]["event"], "captcha_wait_resolved")

    def test_async_hh_rejection_after_five_seconds_starts_second_captcha_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"first"] * 3 + [b"second"] * 40
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "wrong"),
                                                      ImageSolution("ready", "correct")]
        registry.claim.return_value = True
        rows, now = [], [0.0]
        def sleep(seconds):
            now[0] += seconds
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("anti_captcha.hh_wrong_image_answer_visible",
                   side_effect=lambda _: action.click.call_count == 1 and now[0] >= 6), \
             patch("hh_web.captcha_resolution_verified",
                   side_effect=lambda *_: action.click.call_count == 2):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             clock=lambda: now[0], sleep=sleep)
        self.assertGreaterEqual(now[0], 6)
        self.assertLess(now[0], run_web.CAPTCHA_HH_VERIFY_SECONDS + 2)
        self.assertEqual(client.create_image_task.call_count, 2)
        self.assertEqual(action.click.call_count, 2)
        self.assertEqual([r["attempt"] for r in rows if r["event"] == "captcha_auto_started"], [1, 2])
        self.assertEqual(rows[-1]["event"], "captcha_wait_resolved")

    def test_three_rejected_challenges_return_to_manual_without_fourth_task(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = lambda **_: f"image-{action.click.call_count}".encode()
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.side_effect = [(7, "created"), (8, "created"), (9, "created")]
        client.wait_for_image_solution.side_effect = [ImageSolution("ready", "wrong"),
                                                      ImageSolution("ready", "wrong-again"),
                                                      ImageSolution("ready", "still-wrong")]
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("anti_captcha.hh_wrong_image_answer_visible", return_value=True), \
             patch("hh_web.captcha_resolution_verified", return_value=False), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: any(r["event"] == "captcha_auto_fallback" for r in rows),
                                             telemetry=rows.append, sleep=lambda _: None)
        self.assertEqual(client.create_image_task.call_count, 3)
        self.assertEqual(action.click.call_count, 3)
        self.assertEqual([r["attempt"] for r in rows if r["event"] == "captcha_auto_started"], [1, 2, 3])
        self.assertEqual(next(r for r in rows if r["event"] == "captcha_auto_fallback")["reason"],
                         "hh_not_verified")

    def test_late_hh_success_wins_over_early_new_image(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.side_effect = [b"first"] * 3 + [b"second"] * 12
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "answer")
        registry.claim.return_value = True
        rows = []
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", side_effect=[False, False, True, True]):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: False, telemetry=rows.append,
                                             sleep=lambda _: None)
        client.create_image_task.assert_called_once()
        self.assertNotIn("captcha_auto_retry", [r["event"] for r in rows])
        self.assertEqual(rows[-1]["event"], "captcha_wait_resolved")

    def test_stop_interrupts_hh_verification_wait(self):
        image, field, action, client, registry = [MagicMock() for _ in range(5)]
        image.screenshot.return_value = b"first"
        client.get_balance.return_value = BalanceResult(True, Decimal("1"))
        client.create_image_task.return_value = (7, "created")
        client.wait_for_image_solution.return_value = ImageSolution("ready", "answer")
        registry.claim.return_value = True
        rows, slept = [], [False]
        patches = self._auto_image_patches(image, field, action, client, registry)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
             patch("hh_web.captcha_resolution_verified", return_value=False), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(MagicMock(), operation="application_post_submit",
                                             run_id="e" * 32, vacancy_id="123",
                                             stopped=lambda: slept[0], telemetry=rows.append,
                                             sleep=lambda _: slept.__setitem__(0, action.click.call_count > 0))
        client.create_image_task.assert_called_once()
        self.assertEqual(rows[-1]["event"], "captcha_wait_stopped")

    def _base_patches(self, browser, rows):
        return (
            patch("run_web.has_session", return_value=True),
            patch("run_web._RunControl"),
            patch("run_web.browser_context", return_value=browser),
            patch("run_web._get_resume", return_value="Synthetic resume facts"),
            patch("decide.hard_filters", return_value=None),
            patch("decide.score", return_value=dict(test_safe_mvp.RESULT, score=10,
                                                      capability_score=10)),
            patch("decide.cover_letter", return_value="Synthetic letter"),
            patch("run_web._log", side_effect=rows.append),
        )

    def test_detail_captcha_resumes_same_vacancy_without_manual_outcome(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows, calls = [], []
        vacancy = dict(test_safe_mvp.VAC, id="101")

        def load(_page, current, *, diagnostics, **_kwargs):
            calls.append(current["id"])
            if len(calls) == 1:
                diagnostics.update(page_state_markers={"captcha_marker_present": True})
                raise ValueError("vacancy_description_not_found")
            return current

        patches = self._base_patches(browser, rows)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as score, \
             patches[6], patches[7], \
             patch("hh_web.search_page", return_value=[vacancy]), \
             patch("hh_web.load_full", side_effect=load), \
             patch("run_web._wait_for_manual_captcha") as wait:
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(calls, ["101", "101"])
        wait.assert_called_once()
        self.assertEqual(wait.call_args.kwargs["operation"], "vacancy_detail")
        self.assertEqual(wait.call_args.kwargs["vacancy_id"], "101")
        score.assert_called_once()
        outcomes = [row for row in rows if row.get("vacancy_id") and not row.get("event")]
        self.assertEqual([(row["vacancy_id"], row["decision"]) for row in outcomes],
                         [("101", "rejected_score")])
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual(final["counts"]["manual_review"], 0)
        self.assertEqual(final["counts"]["applied_confirmed"], 0)

    def test_detail_captcha_stop_does_not_advance_to_next_vacancy(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows, opened = [], []
        vacancies = [dict(test_safe_mvp.VAC, id="101"), dict(test_safe_mvp.VAC, id="102")]

        def load(_page, vacancy, *, diagnostics, **_kwargs):
            opened.append(vacancy["id"])
            diagnostics.update(page_state_markers={"captcha_marker_present": True})
            raise ValueError("vacancy_description_not_found")

        patches = self._base_patches(browser, rows)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as score, \
             patches[6] as letter, patches[7], \
             patch("hh_web.search_page", return_value=vacancies), \
             patch("hh_web.load_full", side_effect=load), \
             patch("run_web._wait_for_manual_captcha", side_effect=LLMStopped()):
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(opened, ["101"])
        score.assert_not_called(); letter.assert_not_called()
        self.assertFalse(any(row.get("vacancy_id") and not row.get("event") for row in rows))
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "user_stop"))

    def test_search_captcha_resumes_same_page(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows, pages = [], []
        vacancy = dict(test_safe_mvp.VAC, id="101")

        def search(_page, *, page_num, diagnostics, **_kwargs):
            pages.append(page_num)
            if len(pages) == 1:
                diagnostics.update(outcome="captcha_detected", captcha_marker_present=True,
                                   raw_card_count=0, parsed_vacancy_count=0)
                return []
            diagnostics.update(outcome="vacancies_parsed", captcha_marker_present=False,
                               raw_card_count=1, parsed_vacancy_count=1)
            return [vacancy]

        patches = self._base_patches(browser, rows)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patches[6], patches[7], patch("hh_web.search_page", side_effect=search), \
             patch("hh_web.load_full", side_effect=lambda _p, vac, **_kw: vac), \
             patch("run_web._wait_for_manual_captcha") as wait:
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(pages, [0, 0])
        self.assertEqual(wait.call_args.kwargs["operation"], "search")
        outcome = next(row for row in rows if row.get("vacancy_id") and not row.get("event"))
        self.assertEqual(outcome["decision"], "rejected_score")

    def test_search_captcha_stop_is_not_generic_empty_result(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows = []

        def search(_page, *, diagnostics, **_kwargs):
            diagnostics.update(outcome="captcha_detected", captcha_marker_present=True,
                               raw_card_count=0, parsed_vacancy_count=0)
            return []

        patches = self._base_patches(browser, rows)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as score, \
             patches[6], patches[7], patch("hh_web.search_page", side_effect=search), \
             patch("hh_web.load_full") as load, \
             patch("run_web._wait_for_manual_captcha", side_effect=LLMStopped()):
            self.assertEqual(run_web.main(), 0)

        score.assert_not_called(); load.assert_not_called()
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "user_stop"))

    def test_wait_requires_explicit_continue_and_verified_page(self):
        rows, now = [], [0.0]
        page = MagicMock()

        def sleep(seconds):
            now[0] += seconds
            if now[0] == 2:
                run_web._captcha_resume_requested.set()

        with patch("hh_web.captcha_resolution_verified", return_value=True) as verified:
            run_web._wait_for_manual_captcha(
                page, operation="vacancy_detail", run_id="a" * 32,
                stopped=lambda: False, telemetry=rows.append,
                clock=lambda: now[0], sleep=sleep, timeout_seconds=120, poll_seconds=1)

        self.assertEqual([row["event"] for row in rows],
                         ["captcha_wait_started", "captcha_auto_skipped", "captcha_wait_resolved"])
        self.assertIn(rows[1]["reason"], {"integration_disabled", "api_key_missing", "unsupported_or_unknown_type"})
        verified.assert_called_once()
        page.goto.assert_not_called()
        self.assertEqual(now[0], 2)

    def test_pause_records_structural_type_and_dashboard_shows_it(self):
        for kind in ("turnstile", "image_to_text"):
            with self.subTest(kind=kind):
                rows = []
                structure = {"captcha_type": kind, "classification_confidence": "structural",
                             "visible_iframe_count": int(kind == "turnstile"),
                             "recognized_marker_count": int(kind == "turnstile"),
                             "image_candidate_dialog_count": int(kind == "image_to_text"),
                             "image_probe_status": "ok",
                             "probe_status": "ok"}
                with patch("anti_captcha.capture_challenge_structure", return_value=structure), \
                     self.assertRaises(LLMStopped):
                    run_web._wait_for_manual_captcha(
                        MagicMock(), operation="vacancy_detail", run_id="e" * 32,
                        vacancy_id="123", stopped=lambda: True, telemetry=rows.append,
                        sleep=lambda _: self.fail("Stop did not interrupt pause"))
                self.assertEqual(rows[0]["event"], "captcha_wait_started")
                self.assertEqual(rows[0]["captcha_type"], kind)
                self.assertEqual(rows[0]["vacancy_id"], "123")
                self.assertEqual(webapp._active_captcha_type(rows[::-1]), "unknown")
                self.assertEqual(webapp._active_captcha_type(rows[:1]), kind)
                self.assertEqual(rows[-1]["event"], "captcha_wait_stopped")

    def test_unknown_type_keeps_manual_pause_and_private_data_out_of_event(self):
        rows = []
        page = MagicMock()
        page.locator.return_value.evaluate_all.return_value = {
            "visible": 0, "markers": [], "private_text": "secret@example.test"}
        with self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(
                page, operation="search", run_id="f" * 32,
                stopped=lambda: True, telemetry=rows.append,
                sleep=lambda _: self.fail("Stop did not interrupt pause"))
        self.assertEqual(rows[0]["captcha_type"], "unknown")
        self.assertNotIn("secret@example.test", json.dumps(rows))

    def test_user_stop_exits_wait_without_sleeping(self):
        rows = []
        with patch("hh_web.captcha_present", return_value=True), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="search", run_id="b" * 32,
                stopped=lambda: True, telemetry=rows.append,
                sleep=lambda _seconds: self.fail("wait did not stop promptly"))
        self.assertEqual(rows[-1]["event"], "captcha_wait_stopped")

    def test_expired_challenge_keeps_browser_open_until_stop(self):
        rows, now = [], [0.0]

        def sleep(seconds):
            now[0] += seconds

        with self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="search", run_id="c" * 32,
                stopped=lambda: now[0] >= 4, telemetry=rows.append,
                clock=lambda: now[0], sleep=sleep, timeout_seconds=2, poll_seconds=1)
        self.assertEqual(now[0], 4)
        self.assertEqual([row["event"] for row in rows],
                         ["captcha_wait_started", "captcha_auto_skipped",
                          "captcha_wait_expired", "captcha_wait_stopped"])

    def test_dashboard_state_tracks_only_active_captcha_wait(self):
        self.assertTrue(webapp._captcha_waiting([
            {"event": "captcha_wait_started"}, {"event": "run_started"}]))
        self.assertFalse(webapp._captcha_waiting([
            {"event": "captcha_wait_resolved"}, {"event": "captcha_wait_started"}]))
        self.assertTrue(webapp._captcha_waiting([
            {"event": "captcha_wait_expired"}, {"event": "captcha_wait_started"}]))

    def test_disappeared_marker_without_verified_page_does_not_resume(self):
        rows, now = [], [0]
        def sleep(seconds):
            now[0] += seconds
            if now[0] in (1, 2):
                run_web._captcha_resume_requested.set()
        with patch("hh_web.captcha_resolution_verified", side_effect=[False, True]) as verify:
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="vacancy_detail", run_id="d" * 32,
                vacancy_id="123", stopped=lambda: False, telemetry=rows.append,
                clock=lambda: now[0], sleep=sleep, poll_seconds=1)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual([r["event"] for r in rows],
                         ["captcha_wait_started", "captcha_auto_skipped",
                          "captcha_resume_rejected", "captcha_wait_resolved"])

    def test_captcha_continue_requires_active_authenticated_worker(self):
        state = {"pid": 123, "run_id": "a" * 32, "control_token": "[not printed]"}
        with patch("webapp._read_run_state", return_value=state), \
             patch("webapp._pid_alive", return_value=True), \
             patch("webapp._control_request", return_value={"ok": True,
                   "status": "captcha_continue_requested"}) as control:
            response = webapp.app.test_client().post("/api/captcha/continue")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(control.call_args.args, (state, "captcha_continue"))
        self.assertNotIn("control_token", response.get_data(as_text=True))
        with patch("webapp._read_run_state", return_value=None):
            self.assertEqual(webapp.app.test_client().post("/api/captcha/continue").status_code, 409)

    def test_resume_requires_expected_page_structure_for_search_and_detail(self):
        page = MagicMock(); page.url = "https://hh.ru/search/vacancy?private=query"
        cards = [0]
        def locator(selector):
            found = MagicMock()
            found.count.return_value = 0 if "captcha" in selector else cards[0]
            return found
        page.locator.side_effect = locator
        self.assertFalse(hh_web.captcha_resolution_verified(page, "search"))
        cards[0] = 2
        self.assertTrue(hh_web.captcha_resolution_verified(page, "search"))
        page.url = "https://hh.ru/vacancy/123?private=query"
        self.assertTrue(hh_web.captcha_resolution_verified(page, "vacancy_detail", "123"))
        self.assertFalse(hh_web.captcha_resolution_verified(page, "vacancy_detail", "456"))
        page.url = "https://hh.ru/captcha?private=query"
        self.assertFalse(hh_web.captcha_resolution_verified(page, "search"))
        self.assertEqual(hh_web.safe_captcha_path(page), "/captcha")

    def test_form_captcha_before_submit_never_arms_request(self):
        page = MagicMock(); page.url = "https://hh.ru/captcha?token=private-value"
        journal = MagicMock()
        with patch.object(config, "DRY_RUN", False), \
             patch("hh_web.captcha_present", return_value=True), \
             patch("application.form_diagnostics", return_value={}):
            result = application.apply(page, test_safe_mvp.VAC, "Synthetic letter",
                                       resume_id="aaaa", dry_run=False, before_submit=journal)
        self.assertEqual(result["reason"], "captcha_before_submit")
        self.assertEqual(result["application_status"], "needs_manual_review")
        self.assertFalse(result["submit_click_attempted"] if "submit_click_attempted" in result else False)
        journal.assert_not_called()
        self.assertNotIn("private-value", json.dumps(result))

    def test_form_early_manual_exit_routes_detected_captcha_to_pause(self):
        for path in ("/vacancy/123", "/applicant/vacancy_response"):
            with self.subTest(path=path):
                page = MagicMock(); page.url = "https://hh.ru" + path
                form = MagicMock()
                form.locator.return_value.count.return_value = 1  # unsupported control
                journal = MagicMock()
                with patch.object(config, "DRY_RUN", False), \
                     patch("application._signal", return_value=False), \
                     patch("application._response_entry", return_value=MagicMock()), \
                     patch("application._wait_response_form", return_value=(form, None)), \
                     patch("application.form_diagnostics", return_value={}), \
                     patch("hh_web.captcha_present", side_effect=[False, True]):
                    result = application.apply(page, test_safe_mvp.VAC, "Synthetic letter",
                                               resume_id="aaaa", dry_run=False,
                                               before_submit=journal)
                self.assertEqual(result["reason"], "captcha_before_submit")
                self.assertEqual(result["stage"], "apply_questions")
                self.assertEqual(result["application_status"], "needs_manual_review")
                journal.assert_not_called()

    def test_form_early_manual_exit_without_captcha_stays_manual(self):
        page = MagicMock(); page.url = "https://hh.ru/vacancy/123"
        form = MagicMock(); form.locator.return_value.count.return_value = 1
        with patch.object(config, "DRY_RUN", False), \
             patch("application._signal", return_value=False), \
             patch("application._response_entry", return_value=MagicMock()), \
             patch("application._wait_response_form", return_value=(form, None)), \
             patch("application.form_diagnostics", return_value={}), \
             patch("hh_web.captcha_present", return_value=False):
            result = application.apply(page, test_safe_mvp.VAC, "Synthetic letter",
                                       resume_id="aaaa", dry_run=False,
                                       before_submit=MagicMock())
        self.assertEqual(result["reason"], "unsupported_custom_control")

    def test_blocked_route_registration_is_identified_before_submit(self):
        page = MagicMock(); stages = []; journal = MagicMock()
        page.context.route.side_effect = RuntimeError("synthetic route setup stall")
        with patch.object(config, "DRY_RUN", False), self.assertRaises(RuntimeError):
            application.apply(page, test_safe_mvp.VAC, "Synthetic letter",
                              resume_id="aaaa", dry_run=False, before_submit=journal,
                              on_stage=stages.append)
        self.assertEqual(stages, ["application_route_setup"])
        journal.assert_not_called()

    def test_application_captcha_before_submit_retries_only_same_vacancy(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows, calls = [], []
        def apply(_page, vacancy, _letter, **_kwargs):
            calls.append(vacancy["id"])
            return {"application_status": "needs_manual_review",
                    "reason": "captcha_before_submit" if len(calls) == 1 else "selected_resume_not_verified"}
        patches = self._base_patches(browser, rows)
        with patch.object(config, "DRY_RUN", False), patches[0], patches[1], patches[2], \
             patches[3], patches[4], patches[6], patches[7], \
             patch("decide.score", return_value=test_safe_mvp.RESULT), \
             patch("hh_web.search_page", return_value=[test_safe_mvp.VAC]), \
             patch("hh_web.load_full", side_effect=lambda _p, vac, **_kw: vac), \
             patch("hh_web.apply", side_effect=apply), \
             patch("run_web._wait_for_manual_captcha") as wait:
            self.assertEqual(run_web.main(), 0)
        self.assertEqual(calls, [test_safe_mvp.VAC["id"]] * 2)
        self.assertEqual(wait.call_args.kwargs["operation"], "application_pre_submit")
        self.assertFalse(any(r.get("event") == "apply_submit_started" for r in rows))

    def test_application_captcha_after_submit_never_retries_and_keeps_ledger(self):
        page = MagicMock(); browser = MagicMock(); browser.__enter__.return_value = (None, page)
        rows, calls = [], []
        def apply(_page, vacancy, _letter, **kwargs):
            calls.append(vacancy["id"])
            kwargs["before_submit"]()
            return {"application_status": "submission_unknown", "reason": "captcha_after_possible_submit"}
        patches = self._base_patches(browser, rows)
        with patch.object(config, "DRY_RUN", False), patches[0], patches[1], patches[2], \
             patches[3], patches[4], patches[6], patches[7], \
             patch("decide.score", return_value=test_safe_mvp.RESULT), \
             patch("hh_web.search_page", return_value=[test_safe_mvp.VAC]), \
             patch("hh_web.load_full", side_effect=lambda _p, vac, **_kw: vac), \
             patch("hh_web.apply", side_effect=apply), \
             patch("run_web._wait_for_manual_captcha") as wait:
            self.assertEqual(run_web.main(), 0)
        self.assertEqual(calls, [test_safe_mvp.VAC["id"]])
        self.assertEqual(wait.call_args.kwargs["operation"], "application_post_submit")
        self.assertEqual(json.loads(Path(config.APPLICATION_SAFETY_FILE).read_text())["vacancies"]
                         [test_safe_mvp.VAC["id"]], "submission_unknown")


if __name__ == "__main__":
    unittest.main()
