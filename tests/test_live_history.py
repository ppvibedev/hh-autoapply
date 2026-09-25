"""Offline tests for strict, bounded LIVE history preflight."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_safe_mvp
import history_store

config = test_safe_mvp.config
run_web = test_safe_mvp.run_web


def _row(**fields):
    return json.dumps(fields or {"application_status": "not_attempted"})


class LiveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def test_valid_first_read_and_empty_history_are_unchanged(self):
        sleep_calls = []
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(run_web._read_live_history(
            self.path, sleep=sleep_calls.append), [])

        expected = [{"application_status": "applied_confirmed", "vacancy_id": "123"},
                    {"application_status": "submission_unknown", "vacancy_id": "456"}]
        self.path.write_text("\n".join(json.dumps(item) for item in expected) + "\n",
                             encoding="utf-8")
        self.assertEqual(run_web._read_live_history(
            self.path, sleep=sleep_calls.append), expected)
        self.assertEqual(sleep_calls, [])

    def test_unicode_line_separators_remain_inside_one_jsonl_record(self):
        for separator in ("\u2028", "\u2029"):
            with self.subTest(codepoint=f"U+{ord(separator):04X}"):
                sleep_calls = []
                expected = {"title": f"Before{separator}After", "vacancy_id": "123"}
                serialized = json.dumps(expected, ensure_ascii=False) + "\n"
                self.assertIn(separator, serialized)
                self.assertEqual(serialized.count("\n"), 1)
                self.assertEqual(json.loads(serialized.rstrip("\n")), expected)
                self.path.write_text(serialized, encoding="utf-8")

                self.assertEqual(
                    run_web._read_live_history(self.path, sleep=sleep_calls.append),
                    [expected],
                )
                self.assertEqual(sleep_calls, [])

    def test_malformed_final_line_recovers_after_one_complete_reread(self):
        private_fragment = "person@example.test"
        self.path.write_text(_row(application_status="applied_confirmed", vacancy_id="123")
                             + "\n" + '{"private":"' + private_fragment,
                             encoding="utf-8")
        events = []

        def complete_append(_delay):
            self.path.write_text(_row(application_status="applied_confirmed", vacancy_id="123")
                                 + "\n" + _row(application_status="not_attempted") + "\n",
                                 encoding="utf-8")

        records = run_web._read_live_history(
            self.path, sleep=complete_append, telemetry=events.append)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "history_read_recovered")
        self.assertEqual(event["initial_failure"]["attempt"], "initial")
        self.assertEqual(event["initial_failure"]["line_number"], 2)
        self.assertTrue(event["initial_failure"]["is_last_line"])
        self.assertFalse(event["initial_failure"]["ends_with_newline"])
        self.assertIsInstance(event["initial_failure"]["error_position"], int)
        self.assertNotIn(private_fragment, json.dumps(event))

    def test_recovery_telemetry_is_included_before_atomic_compaction(self):
        self.path.write_text(_row(vacancy_id="1") + "\n{partial", encoding="utf-8")
        def complete_append(_delay):
            self.path.write_text(_row(vacancy_id="1", run_id="run-1") + "\n"
                                 + _row(event="run_finished", run_id="run-1") + "\n",
                                 encoding="utf-8")
        run_web._read_live_history(
            self.path, sleep=complete_append,
            telemetry=lambda event: history_store.append_jsonl(
                self.path, dict(event, run_id="current-run")),
        )
        latest = run_web._read_live_history(self.path)
        history_store.compact_history(self.path, latest, active_run_id="current-run")
        parsed = [json.loads(line) for line in self.path.read_text().split("\n") if line]
        self.assertEqual(parsed[-1]["event"], "history_read_recovered")

    def test_malformed_middle_line_recovers_only_from_fully_valid_reread(self):
        self.path.write_text(_row(vacancy_id="1") + "\n{broken}\n" + _row(vacancy_id="3") + "\n",
                             encoding="utf-8")

        def replace_with_valid(_delay):
            self.path.write_text(_row(vacancy_id="1") + "\n" + _row(vacancy_id="2")
                                 + "\n" + _row(vacancy_id="3") + "\n", encoding="utf-8")

        records = run_web._read_live_history(self.path, sleep=replace_with_valid)
        self.assertEqual([item["vacancy_id"] for item in records], ["1", "2", "3"])

    def test_stable_malformed_final_line_is_never_skipped(self):
        self.path.write_text(_row(vacancy_id="1") + "\n{broken", encoding="utf-8")
        with self.assertRaisesRegex(run_web.LiveHistoryCorrupt, "live_history_corrupt") as caught:
            run_web._read_live_history(self.path, sleep=lambda _delay: None)
        diagnostics = caught.exception.history_diagnostics
        self.assertEqual([item["attempt"] for item in diagnostics["attempts"]],
                         ["initial", "reread"])
        self.assertTrue(all(item["is_last_line"] for item in diagnostics["attempts"]))
        self.assertFalse(any(item["ends_with_newline"] for item in diagnostics["attempts"]))

    def test_stable_malformed_middle_line_is_never_skipped(self):
        private_fragment = "contact@example.test"
        self.path.write_text(_row(vacancy_id="1") + "\n{" + private_fragment + "}\n"
                             + _row(vacancy_id="3") + "\n", encoding="utf-8")
        with self.assertRaises(run_web.LiveHistoryCorrupt) as caught:
            run_web._read_live_history(self.path, sleep=lambda _delay: None)
        diagnostics = caught.exception.history_diagnostics
        self.assertEqual([item["line_number"] for item in diagnostics["attempts"]], [2, 2])
        self.assertTrue(all(not item["is_last_line"] for item in diagnostics["attempts"]))
        self.assertNotIn(private_fragment, json.dumps(diagnostics))
        for item in diagnostics["attempts"]:
            self.assertIn("line_length_chars", item)
            self.assertIn("line_length_bytes", item)
            self.assertIn("error_position", item)
            self.assertIn("snapshot_size_bytes", item)
            self.assertIn("file_size", item)
            self.assertIn("mtime_ns", item)

    def test_stable_corruption_stops_live_before_browser_or_vacancy_work(self):
        self.path.write_text('{"application_status":', encoding="utf-8")
        rows = []
        with patch.multiple(config, DRY_RUN=False, DECISIONS_LOG=str(self.path),
                            RESUME_URL="https://hh.ru/resume/aaaa", PAGES_PER_RUN=1,
                            PAUSE_SECONDS=0, TARGET_APPLICATIONS_PER_RUN=1,
                            SCORE_THRESHOLD=80,
                            RUN_PID_FILE=str(Path(self.tmp.name) / "run.pid"),
                            RUN_CONTROL_SOCKET=str(Path(self.tmp.name) / "run.sock")), \
             patch("run_web.has_session", return_value=True), \
             patch("run_web.selected_resume_title", return_value="Project Manager"), \
             patch("run_web.load_candidate_context", return_value=("", "")), \
             patch("run_web.llm.reset_runtime"), patch("run_web.llm.start_runtime"), \
             patch("run_web.LIVE_HISTORY_REREAD_DELAY_SECONDS", 0), \
             patch("run_web.browser_context") as browser, \
             patch("hh_web.search_page") as search, patch("decide.score") as score, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 1)
        browser.assert_not_called()
        search.assert_not_called()
        score.assert_not_called()
        error = next(item for item in rows if item.get("event") == "run_error")
        self.assertEqual(error["reason_code"], "live_history_corrupt")
        self.assertEqual(error["stage"], "resume_metadata_preflight")
        self.assertEqual(len(error["history_diagnostics"]["attempts"]), 2)

    def test_safe_error_mapper_exposes_specific_history_reason(self):
        error = run_web.LiveHistoryCorrupt({"attempts": []})
        self.assertEqual(run_web._error_code(error), "live_history_corrupt")
        details = run_web._exception_details(error, "resume_metadata_preflight")
        self.assertEqual(details["history_diagnostics"], {"attempts": []})


if __name__ == "__main__":
    unittest.main()
