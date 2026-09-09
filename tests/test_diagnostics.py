"""Targeted offline diagnostics checks, sharing isolated config fixtures."""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import test_safe_mvp
import run_web
from privacy import sanitize_candidate_text
import config

# Reuse isolation setup only, not the full MVP suite.
import unittest
class DiagnosticsTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def test_timestamp_and_run_id_preserved(self):
        stamp = datetime.fromisoformat('2026-09-08T11:30:12.123456+00:00')
        rid = '12345678901234567890123456789012'
        with patch('run_web.datetime') as clock:
            clock.now.return_value = stamp
            run_web._log({'run_id': rid, 'stage':'browser_init'})
        row = json.loads(Path(config.DECISIONS_LOG).read_text())
        self.assertEqual(row['ts'],stamp.isoformat())
        self.assertEqual(row['run_id'],rid)

    def test_candidate_contacts_removed(self):
        cleaned = sanitize_candidate_text('Project manager\nperson@example.test\n+7 (999) 123-45-67')
        self.assertNotIn('person@example.test',cleaned)
        self.assertNotIn('999',cleaned)
        self.assertIn('Project manager',cleaned)

    def test_exception_stage_type_frames_without_private_content(self):
        def broken_browser(*, on_stage):
            on_stage('browser_context_init')
            private_local = 'PRIVATE_RESUME_TEXT'
            raise RuntimeError("Browser.new_context: Permission denied\nAPI_KEY=artificial-secret\n" + private_local + ' person@example.test')
        with patch('run_web.has_session',return_value=True), patch('run_web.load_candidate_context',return_value=('','')), patch('run_web.browser_context',side_effect=broken_browser), patch('builtins.print') as output:
            self.assertEqual(run_web.main(),1)
        rows = [json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()]
        error = next(r for r in rows if r.get('event')=='run_error')
        self.assertEqual(error['stage'],'browser_context_init')
        self.assertEqual(error['error_type'],'builtins.RuntimeError')
        self.assertEqual(error['run_id'],rows[0]['run_id'])
        self.assertIn('Permission denied',error['error_message'])
        frame = error['traceback_frames'][-1]
        self.assertEqual(frame['filename'],'test_diagnostics.py')
        self.assertEqual(frame['function_name'],'broken_browser')
        self.assertIsInstance(frame['line_number'],int)
        text = json.dumps(rows)+str(output.call_args_list)
        for private in ('PRIVATE_RESUME_TEXT','artificial-secret','person@example.test','private_local'):
            self.assertNotIn(private,text)
