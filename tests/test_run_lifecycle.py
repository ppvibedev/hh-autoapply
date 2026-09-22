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


class RunLifecycleTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def setUp(self):
        test_safe_mvp.SafeTests.setUp(self)
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
        self.assertTrue(result["stopped"])
        proc.terminate.assert_called_once()

    def test_recovered_authenticated_worker_receives_stop(self):
        state = self.state(token="t" * 43)
        self.write_state(state)
        run_web._stop_requested = False
        control = run_web._RunControl(config.RUN_CONTROL_SOCKET, state["control_token"])
        control.start(); self.addCleanup(control.close)
        with patch.object(webapp, "_proc", None), patch("webapp._pid_alive", return_value=True), patch("webapp.os.kill") as kill:
            result = webapp._kill_all_runs()
        self.assertEqual(result["status"], "recovered_stop_requested")
        self.assertTrue(result["stopped"])
        self.assertTrue(run_web._stop_requested)
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
