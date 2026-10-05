"""Offline CAPTCHA pause/resume regression tests; no HH or external LLM."""
import unittest
from unittest.mock import MagicMock, patch

import config
from llm_errors import LLMStopped
import run_web
import test_safe_mvp
import webapp


class CaptchaPauseTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

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

    def test_detail_captcha_timeout_stops_before_next_vacancy_or_llm(self):
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
             patch("run_web._wait_for_manual_captcha", side_effect=run_web.HHCaptchaTimeout()):
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(opened, ["101"])
        score.assert_not_called(); letter.assert_not_called()
        self.assertFalse(any(row.get("vacancy_id") and not row.get("event") for row in rows))
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "hh_captcha_timeout"))

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

    def test_search_captcha_timeout_is_not_generic_empty_result(self):
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
             patch("run_web._wait_for_manual_captcha", side_effect=run_web.HHCaptchaTimeout()):
            self.assertEqual(run_web.main(), 0)

        score.assert_not_called(); load.assert_not_called()
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "hh_captcha_timeout"))

    def test_wait_polls_until_solved_without_navigation_and_emits_lifecycle(self):
        rows, now = [], [0.0]
        page = MagicMock()

        def sleep(seconds):
            now[0] += seconds

        with patch("hh_web.captcha_present", side_effect=[True, True, False]):
            run_web._wait_for_manual_captcha(
                page, operation="vacancy_detail", run_id="a" * 32,
                stopped=lambda: False, telemetry=rows.append,
                clock=lambda: now[0], sleep=sleep, timeout_seconds=120, poll_seconds=1)

        self.assertEqual([row["event"] for row in rows],
                         ["captcha_wait_started", "captcha_wait_resolved"])
        page.goto.assert_not_called()
        self.assertEqual(now[0], 2)

    def test_user_stop_exits_wait_without_sleeping(self):
        rows = []
        with patch("hh_web.captcha_present", return_value=True), \
             self.assertRaises(LLMStopped):
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="search", run_id="b" * 32,
                stopped=lambda: True, telemetry=rows.append,
                sleep=lambda _seconds: self.fail("wait did not stop promptly"))
        self.assertEqual(rows[-1]["event"], "captcha_wait_stopped")

    def test_wait_timeout_uses_fake_clock_and_canonical_reason(self):
        rows, now = [], [0.0]

        def sleep(seconds):
            now[0] += seconds

        with patch("hh_web.captcha_present", return_value=True), \
             self.assertRaisesRegex(run_web.HHCaptchaTimeout, "hh_captcha_timeout"):
            run_web._wait_for_manual_captcha(
                MagicMock(), operation="search", run_id="c" * 32,
                stopped=lambda: False, telemetry=rows.append,
                clock=lambda: now[0], sleep=sleep, timeout_seconds=2, poll_seconds=1)
        self.assertEqual(now[0], 2)
        self.assertEqual([row["event"] for row in rows],
                         ["captcha_wait_started", "captcha_wait_timeout"])

    def test_dashboard_state_tracks_only_active_captcha_wait(self):
        self.assertTrue(webapp._captcha_waiting([
            {"event": "captcha_wait_started"}, {"event": "run_started"}]))
        self.assertFalse(webapp._captcha_waiting([
            {"event": "captcha_wait_resolved"}, {"event": "captcha_wait_started"}]))


if __name__ == "__main__":
    unittest.main()
