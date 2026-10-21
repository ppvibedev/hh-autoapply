"""Offline HH navigation resilience tests; no HH, LLM, or application requests."""
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import test_safe_mvp
import config
import history_store
from llm_errors import LLMStopped
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import run_web
import webapp


DISCONNECTED = RuntimeError("Page.goto; net::ERR_INTERNET_DISCONNECTED")


class NetworkResilienceTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def browser(self):
        browser = MagicMock()
        browser.__enter__.return_value = (None, MagicMock())
        return browser

    def test_temporary_navigation_error_retries_then_succeeds(self):
        call = MagicMock(side_effect=[DISCONNECTED, "ok"])
        events = []
        result = run_web._safe_navigation(
            call, operation="search", run_id="a" * 32,
            stopped=lambda: False, telemetry=events.append,
            delays=(0, 0), sleep=lambda _seconds: None)

        self.assertEqual(result, "ok")
        self.assertEqual(call.call_count, 2)
        self.assertEqual([row["event"] for row in events], [
            "hh_navigation_retry_wait_started",
            "hh_navigation_retry_wait_finished",
        ])
        self.assertEqual(events[0]["network_error_code"], "internet_disconnected")
        self.assertNotIn("ERR_INTERNET_DISCONNECTED", json.dumps(events))

    def test_repeated_navigation_errors_stop_after_bounded_attempts(self):
        call = MagicMock(side_effect=[DISCONNECTED, DISCONNECTED, DISCONNECTED, DISCONNECTED])
        events = []
        with self.assertRaises(run_web.HHNetworkUnavailable) as caught:
            run_web._safe_navigation(
                call, operation="search", run_id="a" * 32,
                stopped=lambda: False, telemetry=events.append,
                delays=(0, 0), sleep=lambda _seconds: None)

        self.assertEqual(call.call_count, 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(events[-1]["event"], "hh_navigation_retries_exhausted")
        self.assertEqual(events[-1]["attempt"], 3)

    def test_only_playwright_goto_timeout_is_a_retryable_timeout(self):
        error = PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded")
        self.assertEqual(run_web._navigation_error_code(error), "navigation_timeout")
        self.assertIsNone(run_web._navigation_error_code(
            RuntimeError("Page.goto: Timeout 30000ms exceeded")))

    def test_stop_interrupts_retry_wait_before_another_navigation(self):
        call = MagicMock(side_effect=DISCONNECTED)
        events = []
        observations = iter((False, True))
        with self.assertRaises(LLMStopped):
            run_web._safe_navigation(
                call, operation="vacancy_detail", run_id="a" * 32,
                vacancy_id="123", stopped=lambda: next(observations),
                telemetry=events.append, delays=(1, 2), sleep=lambda _seconds: None)

        self.assertEqual(call.call_count, 1)
        self.assertEqual([row["event"] for row in events], [
            "hh_navigation_retry_wait_started",
            "hh_navigation_retry_wait_stopped",
        ])

    def test_isolated_vacancy_navigation_error_recovers_without_manual_review(self):
        first = dict(test_safe_mvp.VAC, id="101")
        second = dict(test_safe_mvp.VAC, id="102")
        load = MagicMock(side_effect=[DISCONNECTED, first, second])
        rows = []
        low = dict(test_safe_mvp.RESULT, score=10, capability_score=10)
        with patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=self.browser()), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("run_web._sleep_with_stop"), \
             patch("hh_web.search_page", return_value=[first, second]), \
             patch("hh_web.load_full", side_effect=load), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", return_value=low), \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        outcomes = [row for row in rows if row.get("vacancy_id") and not row.get("event")]
        self.assertEqual([row["decision"] for row in outcomes],
                         ["rejected_score", "rejected_score"])
        self.assertEqual(load.call_count, 3)
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual(final["counts"]["manual_review"], 0)

    def test_search_page_navigation_retries_then_continues_normal_pipeline(self):
        vacancy = dict(test_safe_mvp.VAC, id="101")
        search = MagicMock(side_effect=[DISCONNECTED, [vacancy]])
        rows = []
        low = dict(test_safe_mvp.RESULT, score=10, capability_score=10)
        with patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=self.browser()), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("run_web._sleep_with_stop"), \
             patch("hh_web.search_page", side_effect=search), \
             patch("hh_web.load_full", side_effect=lambda _page, item, **_kw: item), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", return_value=low), \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(search.call_count, 2)
        outcome = next(row for row in rows if row.get("vacancy_id") == "101")
        self.assertEqual(outcome["decision"], "rejected_score")
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("COMPLETED", "pages_exhausted"))

    def test_search_page_outage_stops_with_specific_reason(self):
        search = MagicMock(side_effect=[DISCONNECTED] * 4)
        rows = []
        with patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=self.browser()), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("run_web._sleep_with_stop"), \
             patch("hh_web.search_page", side_effect=search), \
             patch("hh_web.load_full") as load, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(search.call_count, 3)
        load.assert_not_called()
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "hh_network_unavailable"))
        self.assertEqual(final["counts"]["manual_review"], 0)

    def test_persistent_outage_stops_without_mass_manual_reviews(self):
        vacancies = [dict(test_safe_mvp.VAC, id=str(100 + index)) for index in range(20)]
        rows = []
        load = MagicMock(side_effect=[DISCONNECTED] * 10)
        with patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=self.browser()), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("run_web._sleep_with_stop"), \
             patch("hh_web.search_page", return_value=vacancies), \
             patch("hh_web.load_full", side_effect=load), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score") as score, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        self.assertEqual(load.call_count, 3)
        score.assert_not_called()
        outcomes = [row for row in rows if row.get("vacancy_id") and not row.get("event")]
        self.assertEqual(outcomes, [])
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "hh_network_unavailable"))
        self.assertEqual(final["counts"]["manual_review"], 0)
        self.assertEqual(final["counts"]["scanned"], 0)
        error = next(row for row in rows if row.get("event") == "run_error")
        self.assertEqual(error["reason_code"], "hh_network_unavailable")
        self.assertEqual(error["network_error_code"], "internet_disconnected")
        self.assertEqual(error["attempts"], 3)

    def test_confirmed_and_unknown_safety_records_survive_later_network_stop(self):
        unknown = dict(test_safe_mvp.VAC, id="111")
        confirmed = dict(test_safe_mvp.VAC, id="222")
        history_store.record_safety(config.APPLICATION_SAFETY_FILE, "111", "submission_unknown")
        search = MagicMock(side_effect=[
            [unknown, confirmed], DISCONNECTED, DISCONNECTED, DISCONNECTED,
        ])
        rows = []
        with patch.multiple(config, DRY_RUN=False, PAGES_PER_RUN=2,
                            TARGET_APPLICATIONS_PER_RUN=2), \
             patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=self.browser()), \
             patch("run_web.load_candidate_context", return_value=("", "")), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("run_web._sleep_with_stop"), \
             patch("hh_web.search_page", side_effect=search), \
             patch("hh_web.load_full", side_effect=lambda _page, vacancy, **_kw: vacancy), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", return_value=test_safe_mvp.RESULT), \
             patch("decide.cover_letter", return_value="Synthetic letter"), \
             patch("hh_web.apply", return_value={
                 "application_status": "applied_confirmed", "reason": "fixture",
             }) as apply, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[1]["id"], "222")
        safety = history_store.load_safety(config.APPLICATION_SAFETY_FILE)
        self.assertEqual(safety["111"], "submission_unknown")
        self.assertEqual(safety["222"], "applied_confirmed")
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]),
                         ("STOPPED", "hh_network_unavailable"))
        self.assertEqual(final["counts"]["applied_confirmed"], 1)
        self.assertEqual(final["counts"]["submission_unknown"], 0)

    def test_dashboard_exposes_specific_network_stop_reason(self):
        final = {"event": "run_finished", "run_id": "a" * 32,
                 "status": "STOPPED", "stop_reason": "hh_network_unavailable"}
        with patch("webapp._current_run_rows", return_value=("a" * 32, [final])), \
             patch("webapp._running", return_value=False), \
             patch("webapp._any_run_alive", return_value=False), \
             patch("webapp._read_resumes", return_value=[]), \
             patch("webapp.envfile.read_env", return_value={}):
            response = webapp.app.test_client().get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["last_run"]["stop_reason"],
                         "hh_network_unavailable")
        source = Path("web/index.html").read_text(encoding="utf-8")
        self.assertIn("${s.last_run.status}: ${s.last_run.stop_reason}", source)


if __name__ == "__main__":
    unittest.main()
