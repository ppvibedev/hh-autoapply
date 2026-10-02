"""Offline durable worker-stop tests; no HH, LLM, or host process signals."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import test_safe_mvp
import config
import run_web
import webapp
from llm_errors import LLMStopped


class RunLifecycleTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def setUp(self):
        test_safe_mvp.SafeTests.setUp(self)
        with webapp._stop_attempts_lock:
            webapp._stop_attempts.clear()
        self.control_path = f"/tmp/hh-run-{os.getpid()}-{id(self)}.sock"
        setting = patch.object(config, "RUN_CONTROL_SOCKET", self.control_path)
        setting.start(); self.addCleanup(setting.stop)
        self.addCleanup(lambda: Path(self.control_path).unlink(missing_ok=True))

    def state(self, *, pid=4242, token="a" * 43):
        return {"version": 2, "pid": pid, "run_id": "b" * 32, "control_token": token}

    def write_state(self, state):
        Path(config.RUN_PID_FILE).write_text(json.dumps(state), encoding="utf-8")

    def test_current_in_memory_worker_still_terminates(self):
        proc = MagicMock(); proc.poll.return_value = None
        with patch.object(webapp, "_proc", proc):
            result = webapp._kill_all_runs()
        self.assertEqual(result["status"], "in_memory_stop_requested")
        self.assertEqual(result["delivery_path"], "in_memory_process")
        self.assertTrue(result["delivery_accepted"])
        self.assertTrue(result["stopped"])
        proc.terminate.assert_called_once()

    def test_recovered_authenticated_worker_receives_stop(self):
        attempt_id = "stop-attempt-recovered-1234"
        state = self.state(token="t" * 43)
        self.write_state(state)
        run_web._stop_requested = False
        previous = (run_web._active_run_id, run_web._active_stage,
                    run_web._worker_stop_ack_logged, run_web._worker_stop_observed_logged,
                    run_web._active_stop_attempt_id)
        self.addCleanup(lambda: setattr(run_web, "_active_run_id", previous[0]))
        self.addCleanup(lambda: setattr(run_web, "_active_stage", previous[1]))
        self.addCleanup(lambda: setattr(run_web, "_worker_stop_ack_logged", previous[2]))
        self.addCleanup(lambda: setattr(run_web, "_worker_stop_observed_logged", previous[3]))
        self.addCleanup(lambda: setattr(run_web, "_active_stop_attempt_id", previous[4]))
        run_web._active_run_id = state["run_id"]
        run_web._active_stage = "matching"
        run_web._worker_stop_ack_logged = False
        run_web._worker_stop_observed_logged = False
        run_web._active_stop_attempt_id = None
        captured = []
        control = run_web._RunControl(config.RUN_CONTROL_SOCKET, state["control_token"])
        control.start(); self.addCleanup(control.close)
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=True), \
             patch("webapp.os.kill") as kill, patch("run_web._log", side_effect=captured.append):
            result = webapp._kill_all_runs(attempt_id)
            self.assertTrue(run_web._observe_stop())
        self.assertEqual(result["status"], "recovered_stop_requested")
        self.assertEqual(result["delivery_path"], "authenticated_control_socket")
        self.assertTrue(result["stopped"])
        self.assertTrue(run_web._stop_requested)
        stop_events = [row for row in captured if row.get("event", "").startswith("worker_stop_")]
        self.assertEqual([row["event"] for row in stop_events],
                         ["worker_stop_acknowledged", "worker_stop_observed"])
        self.assertTrue(all(row["stop_attempt_id"] == attempt_id for row in stop_events))
        kill.assert_not_called()

    def test_dead_state_is_cleaned_without_signal_and_no_longer_blocks(self):
        self.write_state(self.state())
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=False), patch("webapp._control_request") as control, patch("webapp.os.kill") as kill:
            result = webapp._kill_all_runs()
            self.assertIsNone(webapp._run_pid())
        self.assertEqual(result["status"], "already_stopped")
        self.assertFalse(Path(config.RUN_PID_FILE).exists())
        control.assert_not_called(); kill.assert_not_called()
        with patch.object(webapp, "_proc", None), patch("webapp.os.path.exists", return_value=True), patch("webapp._read_decisions", return_value=[]), patch("webapp.envfile.read_env", return_value={"RUN_MODE": "SAFE", "RESUME_URL": "https://hh.ru/resume/aaaa"}), patch("webapp._spawn") as spawn:
            response = webapp.app.test_client().post("/api/run", json={"mode": "SAFE"})
        self.assertEqual(response.status_code, 200)
        spawn.assert_called_once_with("run_web.py")

    def test_identity_mismatch_never_signals_live_pid(self):
        state = self.state(token="t" * 43)
        self.write_state(state)
        control = run_web._RunControl(config.RUN_CONTROL_SOCKET, "different-token-value-which-is-long-enough-123")
        control.start(); self.addCleanup(control.close)
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=True), patch("webapp.os.kill") as kill:
            result = webapp._kill_all_runs()
        self.assertFalse(result["ok"]); self.assertEqual(result["status"], "worker_identity_not_verified")
        self.assertTrue(Path(config.RUN_PID_FILE).exists())
        kill.assert_not_called()
        Path(config.RUN_PID_FILE).write_text("4242", encoding="utf-8")
        with patch("webapp._pid_alive", return_value=False):
            self.assertIsNone(webapp._run_pid())
        self.assertFalse(Path(config.RUN_PID_FILE).exists())

    def test_legacy_or_invalid_pid_file_never_signals(self):
        for raw in ("not-json", json.dumps({"version": 2, "pid": 4242})):
            with self.subTest(raw=raw):
                Path(config.RUN_PID_FILE).write_text(raw, encoding="utf-8")
                with patch.object(webapp, "_proc", None), patch("webapp.os.kill") as kill:
                    result = webapp._kill_all_runs()
                self.assertTrue(result["ok"]); self.assertEqual(result["status"], "already_stopped")
                self.assertFalse(Path(config.RUN_PID_FILE).exists())
                kill.assert_not_called()
        Path(config.RUN_PID_FILE).write_text("4242", encoding="utf-8")
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=True), patch("webapp.os.kill") as kill:
            result = webapp._kill_all_runs()
            self.assertEqual(webapp._run_pid(), 4242)
        self.assertFalse(result["ok"]); self.assertEqual(result["status"], "worker_identity_not_verified")
        self.assertTrue(Path(config.RUN_PID_FILE).exists())
        kill.assert_not_called()

    def test_worker_disappearing_after_identity_check_is_safe(self):
        self.write_state(self.state())
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", side_effect=[True, False]), patch("webapp._control_request", return_value={}), patch("webapp.os.kill") as kill:
            result = webapp._kill_all_runs()
        self.assertTrue(result["ok"]); self.assertEqual(result["status"], "already_stopped")
        self.assertFalse(Path(config.RUN_PID_FILE).exists())
        kill.assert_not_called()

    def test_repeated_authenticated_stop_is_safe(self):
        state = self.state(token="t" * 43)
        self.write_state(state)
        run_web._stop_requested = False
        control = run_web._RunControl(config.RUN_CONTROL_SOCKET, state["control_token"])
        control.start(); self.addCleanup(control.close)
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=True):
            first = webapp._kill_all_runs()
            second = webapp._kill_all_runs()
        self.assertTrue(first["stopped"]); self.assertTrue(second["stopped"])
        self.assertTrue(run_web._stop_requested)

    def test_stop_api_reports_unverified_worker_honestly(self):
        with patch("webapp._kill_all_runs", return_value={"ok": False, "stopped": False, "status": "worker_identity_not_verified"}):
            response = webapp.app.test_client().post("/api/stop")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["status"], "worker_identity_not_verified")
        self.assertFalse(response.get_json()["stopped"])

    def test_stop_api_persists_safe_receipt_and_delivery_events(self):
        attempt_id = "stop-attempt-safe-12345678"
        state = self.state(token="TOP_SECRET_CONTROL_TOKEN_12345678901234567890")
        self.write_state(state)
        proc = MagicMock(); proc.poll.return_value = None
        with patch.object(webapp, "_proc", proc):
            response = webapp.app.test_client().post("/api/stop", json={"stop_attempt_id": attempt_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["stop_attempt_id"], attempt_id)
        self.assertTrue(response.get_json()["telemetry_recorded"])
        rows = [json.loads(line) for line in Path(config.DECISIONS_LOG).read_text(encoding="utf-8").splitlines()]
        events = [row for row in rows if row.get("event", "").startswith("stop_")]
        self.assertEqual([row["event"] for row in events], [
            "stop_requested_by_user", "stop_api_received", "stop_delivery_path", "stop_delivery_result"])
        self.assertTrue(events[1]["in_memory_process_exists"])
        self.assertTrue(events[-1]["delivery_accepted"])
        self.assertTrue(all(row["stop_attempt_id"] == attempt_id for row in events))
        self.assertNotIn("TOP_SECRET_CONTROL_TOKEN", json.dumps(events))

    def test_telemetry_failure_never_blocks_stop_delivery_or_leaks_details(self):
        attempt_id = "stop-attempt-telemetry-failure"
        result = {"ok": True, "stopped": True, "status": "in_memory_stop_requested",
                  "delivery_path": "in_memory_process", "delivery_accepted": True}
        with patch("webapp._log_stop_telemetry", side_effect=OSError("PRIVATE_TOKEN_VALUE")), \
             patch("webapp._kill_all_runs", return_value=result) as deliver:
            response = webapp.app.test_client().post("/api/stop", json={"stop_attempt_id": attempt_id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["telemetry_recorded"])
        deliver.assert_called_once_with(attempt_id)
        self.assertNotIn("PRIVATE_TOKEN_VALUE", response.get_data(as_text=True))

    def test_same_stop_attempt_is_delivered_only_once(self):
        attempt_id = "stop-attempt-idempotent-1234"
        result = {"ok": True, "stopped": True, "status": "in_memory_stop_requested",
                  "delivery_path": "in_memory_process", "delivery_accepted": True}
        with patch("webapp._kill_all_runs", return_value=result) as deliver:
            first = webapp.app.test_client().post("/api/stop", json={"stop_attempt_id": attempt_id})
            second = webapp.app.test_client().post("/api/stop", json={"stop_attempt_id": attempt_id})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])
        deliver.assert_called_once_with(attempt_id)

    def test_invalid_stop_attempt_id_never_reaches_delivery(self):
        with patch("webapp._kill_all_runs") as deliver:
            response = webapp.app.test_client().post(
                "/api/stop", json={"stop_attempt_id": "secret=value@example.test"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["status"], "invalid_stop_attempt_id")
        deliver.assert_not_called()

    def test_worker_acknowledgement_and_observation_are_durable(self):
        captured = []
        previous = (run_web._active_run_id, run_web._active_stage,
                    run_web._worker_stop_ack_logged, run_web._worker_stop_observed_logged,
                    run_web._stop_requested, run_web._active_stop_attempt_id)
        self.addCleanup(lambda: setattr(run_web, "_active_run_id", previous[0]))
        self.addCleanup(lambda: setattr(run_web, "_active_stage", previous[1]))
        self.addCleanup(lambda: setattr(run_web, "_worker_stop_ack_logged", previous[2]))
        self.addCleanup(lambda: setattr(run_web, "_worker_stop_observed_logged", previous[3]))
        self.addCleanup(lambda: setattr(run_web, "_stop_requested", previous[4]))
        self.addCleanup(lambda: setattr(run_web, "_active_stop_attempt_id", previous[5]))
        run_web._active_run_id = "b" * 32; run_web._active_stage = "matching"
        run_web._worker_stop_ack_logged = False; run_web._worker_stop_observed_logged = False
        run_web._stop_requested = False
        with patch("run_web._log", side_effect=captured.append):
            run_web._request_stop(stop_attempt_id="stop-attempt-worker-1234")
            self.assertTrue(run_web._observe_stop())
        self.assertEqual([row["event"] for row in captured], ["worker_stop_acknowledged", "worker_stop_observed"])
        self.assertTrue(all(row["run_id"] == "b" * 32 for row in captured))
        self.assertTrue(all(row["stop_attempt_id"] == "stop-attempt-worker-1234" for row in captured))

    def test_graceful_stop_finishes_run_before_any_vacancy_starts(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        rows = []
        attempt_id = "stop-attempt-main-12345678"

        def request_stop(*_args, **_kwargs):
            run_web._request_stop(stop_attempt_id=attempt_id)
            return [dict(test_safe_mvp.VAC), dict(test_safe_mvp.VAC, id="987654321")]

        with patch("run_web.has_session", return_value=True), \
             patch("run_web.browser_context", return_value=browser), \
             patch("run_web._get_resume", return_value="Current resume facts"), \
             patch("hh_web.search_page", side_effect=request_stop), \
             patch("hh_web.load_full") as load_full, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)
        load_full.assert_not_called()
        self.assertIn("worker_stop_acknowledged", [row.get("event") for row in rows])
        self.assertIn("worker_stop_observed", [row.get("event") for row in rows])
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]), ("STOPPED", "user_stop"))
        self.assertEqual(final["stop_attempt_id"], attempt_id)

    def test_llm_stop_does_not_start_next_vacancy(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        rows = []
        vacancies = [dict(test_safe_mvp.VAC, id="123"), dict(test_safe_mvp.VAC, id="456")]
        with patch("run_web.has_session", return_value=True), \
             patch("run_web.browser_context", return_value=browser), \
             patch("run_web._get_resume", return_value="Current resume facts"), \
             patch("hh_web.search_page", return_value=vacancies), \
             patch("hh_web.load_full", side_effect=lambda _page, vacancy, **_kwargs: vacancy) as load_full, \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", side_effect=LLMStopped()), \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)
        self.assertEqual(load_full.call_count, 1)
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual((final["status"], final["stop_reason"]), ("STOPPED", "user_stop"))

    def test_stop_ui_reports_request_not_completion(self):
        source = Path("web/index.html").read_text(encoding="utf-8")
        self.assertIn("Остановка запрошена; ожидаю завершения", source)
        self.assertIn("Запрос остановки не доставлен", source)
        self.assertNotIn("postAction('/api/stop','Прогон остановлен')", source)
