"""Offline dashboard history pagination; no HH, candidate files or external services."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import config
import test_safe_mvp
import webapp


class DashboardHistoryTests(unittest.TestCase):
    def setUp(self):
        test_safe_mvp.SafeTests.setUp(self)
        webapp._decisions_cache.update(identity=None, offset=0, pending=b"", rows=[])
        webapp._pending_after_run = None
        webapp._display_run_id = None
        self.run_id = "a" * 32

    def write(self, rows):
        Path(config.DECISIONS_LOG).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")
        webapp._decisions_cache.update(identity=None, offset=0, pending=b"", rows=[])

    def outcomes(self, run_id, count, *, start=1, decision="needs_manual_review"):
        return [{
            "run_id": run_id,
            "vacancy_id": str(start + index),
            "vid": str(start + index),
            "decision": decision,
            "title": f"Vacancy {start + index}",
            "company": "Example",
            "url": f"https://hh.ru/vacancy/{start + index}",
            "score": index % 100,
            "reason_code": "fixture",
            "candidate_context": "PRIVATE_CONTEXT_MARKER",
            "resume_text": "PRIVATE_RESUME_MARKER",
            "questions": [{"answer": "PRIVATE_ANSWER_MARKER"}],
            "matching_result": {"private_source": "PRIVATE_MATCHING_MARKER"},
        } for index in range(count)]

    def test_more_than_one_hundred_outcomes_are_reachable_without_duplicates(self):
        rows = [{"event": "run_started", "run_id": self.run_id}]
        rows += self.outcomes(self.run_id, 135)
        rows += [{"event": "run_finished", "run_id": self.run_id, "status": "COMPLETED"}]
        self.write(rows)
        client = webapp.app.test_client()
        cursor = None
        found = []
        while True:
            query = f"/api/history?run_id={self.run_id}&limit=50"
            if cursor:
                query += f"&cursor={cursor}"
            response = client.get(query)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data["order"], "newest_first")
            self.assertEqual(data["total"], 135)
            found.extend(item["vacancy_id"] for item in data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(found, [str(value) for value in range(135, 0, -1)])
        self.assertEqual(len(found), len(set(found)))

    def test_history_parameters_fail_safely(self):
        self.write([{"run_id": self.run_id, "vacancy_id": "1", "decision": "safe_ready"}])
        client = webapp.app.test_client()
        for query in (
            "/api/history", f"/api/history?run_id=bad", f"/api/history?run_id={self.run_id}&limit=0",
            f"/api/history?run_id={self.run_id}&limit=101",
            f"/api/history?run_id={self.run_id}&limit=word",
            f"/api/history?run_id={self.run_id}&cursor=bad",
            f"/api/history?run_id={self.run_id}&cursor=999",
            f"/api/history?run_id={self.run_id}&status=unknown",
        ):
            with self.subTest(query=query):
                self.assertEqual(client.get(query).status_code, 400)
        self.assertEqual(client.get("/api/history?run_id=" + "b" * 32).status_code, 404)

    def test_status_groups_filter_before_page_limit(self):
        rows = [{"event": "run_started", "run_id": self.run_id}]
        # In newest-first API order the first 75 cards are not manual. The
        # manual cards must still fill the filtered page from deeper history.
        rows += self.outcomes(self.run_id, 25, start=1, decision="needs_manual_review")
        rows += self.outcomes(self.run_id, 75, start=26, decision="rejected_score")
        rows += [
            {"run_id": self.run_id, "vacancy_id": "101", "decision": "applied_confirmed"},
            {"run_id": self.run_id, "vacancy_id": "102", "decision": "already_applied"},
            {"run_id": self.run_id, "vacancy_id": "103", "decision": "rejected_hard_filter"},
            {"run_id": self.run_id, "vacancy_id": "104", "decision": "rejected_remote"},
            {"run_id": self.run_id, "vacancy_id": "105", "decision": "apply_failed"},
            {"run_id": self.run_id, "vacancy_id": "106", "decision": "submission_unknown"},
            {"run_id": self.run_id, "vacancy_id": "107", "decision": "safe_ready"},
        ]
        self.write(rows)
        client = webapp.app.test_client()

        all_page = client.get(f"/api/history?run_id={self.run_id}&limit=20").get_json()
        self.assertEqual(all_page["filter"], "all")
        self.assertEqual(all_page["total"], 107)
        self.assertEqual(len(all_page["items"]), 20)

        manual = client.get(
            f"/api/history?run_id={self.run_id}&limit=20&status=manual_review").get_json()
        self.assertEqual(manual["total"], 25)
        self.assertEqual(len(manual["items"]), 20)
        self.assertEqual({row["decision"] for row in manual["items"]}, {"needs_manual_review"})

        expected = {
            "below_threshold": {"rejected_score"},
            "applied_confirmed": {"applied_confirmed"},
            "already_applied": {"already_applied"},
            "filtered": {"rejected_hard_filter", "rejected_remote"},
            "errors_unknown": {"apply_failed", "submission_unknown"},
            "safe_ready": {"safe_ready"},
        }
        for status_filter, decisions in expected.items():
            with self.subTest(status_filter=status_filter):
                data = client.get(
                    f"/api/history?run_id={self.run_id}&status={status_filter}&limit=100").get_json()
                self.assertEqual({row["decision"] for row in data["items"]}, decisions)
                self.assertEqual(data["total"], len(data["items"]))

    def test_filtered_cursor_has_no_duplicates_or_run_leakage(self):
        other_run = "b" * 32
        rows = ([{"event": "run_started", "run_id": self.run_id}]
                + self.outcomes(self.run_id, 45, decision="needs_manual_review")
                + self.outcomes(self.run_id, 15, start=46, decision="rejected_score")
                + [{"event": "run_started", "run_id": other_run}]
                + self.outcomes(other_run, 5, start=100, decision="needs_manual_review"))
        self.write(rows)
        client = webapp.app.test_client()
        found = []
        cursor = None
        while True:
            query = f"/api/history?run_id={self.run_id}&status=manual_review&limit=20"
            if cursor:
                query += f"&cursor={cursor}"
            data = client.get(query).get_json()
            found.extend(row["vacancy_id"] for row in data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(found), 45)
        self.assertEqual(len(found), len(set(found)))
        self.assertFalse(set(found) & {str(value) for value in range(100, 105)})

    def test_retained_completed_and_active_runs_are_independently_paginated(self):
        completed = "b" * 32
        active = "c" * 32
        rows = ([{"event": "run_started", "run_id": completed}]
                + self.outcomes(completed, 3, start=10)
                + [{"event": "run_finished", "run_id": completed, "status": "COMPLETED"}]
                + [{"event": "run_started", "run_id": active}]
                + self.outcomes(active, 4, start=20))
        self.write(rows)
        client = webapp.app.test_client()
        self.assertEqual(client.get(f"/api/history?run_id={completed}").get_json()["total"], 3)
        self.assertEqual(client.get(f"/api/history?run_id={active}").get_json()["total"], 4)

    def test_state_is_lightweight_and_history_projection_excludes_private_payloads(self):
        rows = [{"event": "run_started", "run_id": self.run_id}]
        rows += self.outcomes(self.run_id, 135)
        rows += [{"event": "run_finished", "run_id": self.run_id, "status": "COMPLETED"}]
        self.write(rows)
        webapp._pending_after_run = "previous-run-boundary"
        webapp._display_run_id = self.run_id
        client = webapp.app.test_client()
        with patch("webapp.envfile.read_env", return_value={}), \
             patch("webapp._read_resumes", return_value=[]), \
             patch("webapp._running", return_value=False), \
             patch("webapp._any_run_alive", return_value=False), \
             patch("webapp._history_card", side_effect=AssertionError("state polling projected history")):
            state_response = client.get("/api/state")
        self.assertEqual(state_response.status_code, 200)
        state = state_response.get_json()
        self.assertEqual(state["decisions"], [])
        self.assertEqual(state["history_total"], 135)
        self.assertLess(len(state_response.data), 20_000)

        history_response = client.get(f"/api/history?run_id={self.run_id}&limit=75")
        self.assertEqual(history_response.status_code, 200)
        body = history_response.get_data(as_text=True)
        for private in ("PRIVATE_CONTEXT_MARKER", "PRIVATE_RESUME_MARKER",
                        "PRIVATE_ANSWER_MARKER", "PRIVATE_MATCHING_MARKER"):
            self.assertNotIn(private, body)
        self.assertEqual(len(history_response.get_json()["items"]), 75)


if __name__ == "__main__":
    unittest.main()
