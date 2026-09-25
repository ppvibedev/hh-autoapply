"""Offline retention/safety-ledger tests; no HH, candidate files or external services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_safe_mvp
import config
import history_store
import webapp


def completed(run_id, marker=None):
    return [
        {"event": "run_started", "run_id": run_id},
        {"run_id": run_id, "vacancy_id": str(marker or run_id), "decision": "safe_ready"},
        {"event": "run_finished", "run_id": run_id, "status": "COMPLETED"},
    ]


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / "decisions.jsonl"
        self.safety = self.root / "application_safety.json"

    def write(self, rows):
        self.journal.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                encoding="utf-8")

    def test_sixth_completed_run_prunes_only_oldest_and_keeps_active(self):
        rows = []
        for index in range(1, 7):
            rows.extend(completed(f"run-{index}"))
        rows.extend([{"event": "run_started", "run_id": "active"},
                     {"run_id": "active", "decision": "safe_ready"}])
        self.write(rows)
        retained = history_store.compact_history(self.journal, rows, active_run_id="active")
        ids = {row["run_id"] for row in retained}
        self.assertEqual(ids, {"run-2", "run-3", "run-4", "run-5", "run-6", "active"})
        parsed = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(parsed, retained)

    def test_unicode_separators_survive_and_stable_corruption_is_not_pruned(self):
        rows = completed("run-1")
        rows[1]["reason"] = "before\u2028middle\u2029after"
        self.write(rows)
        history_store.compact_history(self.journal, rows)
        self.assertEqual(json.loads(self.journal.read_text(encoding="utf-8").split("\n")[1]), rows[1])
        original = self.journal.read_bytes() + b"{broken\n"
        self.journal.write_bytes(original)
        with self.assertRaises(ValueError):
            history_store.compact_history(self.journal, rows)
        self.assertEqual(self.journal.read_bytes(), original)

    def test_safety_survives_retention_and_uses_exact_vacancy_id_only(self):
        old = completed("old")
        old.insert(1, {"run_id": "old", "vacancy_id": "111",
                       "application_status": "submission_unknown",
                       "title": "Private candidate person@example.test"})
        rows = old
        for index in range(1, 7):
            rows.extend(completed(f"run-{index}", 200 + index))
        self.write(rows)
        safety = history_store.merge_safety(self.safety, rows)
        self.assertEqual(safety, {"111": "submission_unknown"})
        history_store.compact_history(self.journal, rows)
        self.assertNotIn("old", {row["run_id"] for row in history_store.retained_records(rows)})
        self.assertEqual(history_store.load_safety(self.safety)["111"], "submission_unknown")
        self.assertNotIn("222", history_store.load_safety(self.safety))
        self.assertNotIn("person@example.test", self.safety.read_text(encoding="utf-8"))

    def test_reset_operational_history_does_not_delete_safety_ledger(self):
        history_store.record_safety(self.safety, "111", "applied_confirmed")
        self.write(completed("run-1"))
        with patch.object(config, "DECISIONS_LOG", str(self.journal)), \
             patch.object(config, "APPLICATION_SAFETY_FILE", str(self.safety)), \
             patch.object(config, "SEEN_FILE", str(self.root / "seen.json")), \
             patch.object(config, "RESUME_FILE", str(self.root / "resume.txt")), \
             patch("webapp._running", return_value=False), patch("webapp._any_run_alive", return_value=False):
            self.assertEqual(webapp.app.test_client().post("/api/reset").status_code, 200)
        self.assertFalse(self.journal.exists())
        self.assertEqual(history_store.load_safety(self.safety), {"111": "applied_confirmed"})

    def test_state_reader_tails_only_new_records_and_rebuilds_after_atomic_prune(self):
        rows = completed("run-1")
        self.write(rows)
        with patch.object(config, "DECISIONS_LOG", str(self.journal)):
            first = webapp._read_decisions(100)
            self.assertEqual(len(first), 3)
            history_store.append_jsonl(self.journal, {"event": "extra", "run_id": "run-1"})
            second = webapp._read_decisions(100)
            self.assertEqual(second[0]["event"], "extra")
            current = [json.loads(line) for line in self.journal.read_text().splitlines()]
            history_store.compact_history(self.journal, current, completed_limit=1)
            third = webapp._read_decisions(100)
            self.assertEqual(len(third), 4)


if __name__ == "__main__":
    unittest.main()
