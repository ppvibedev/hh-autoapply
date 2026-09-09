"""Offline UI history isolation; never opens HH or actual project state."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_safe_mvp
import config
import webapp

class CurrentRunTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def test_restart_empty_then_user_run_only(self):
        rows = [dict(event='run_started',run_id='aaa'),
                dict(run_id='aaa',decision='safe_ready',title='Old'),
                dict(event='run_error',run_id='aaa',error_message='Old error'),
                dict(event='run_finished',run_id='aaa')]
        path = Path(config.DECISIONS_LOG)
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        original = path.read_bytes()
        with patch.object(webapp,'_pending_after_run',None), patch.object(webapp,'_display_run_id',None), patch('webapp.envfile.read_env',return_value={'RESUME_URL':'https://hh.ru/resume/aaaa'}), patch('webapp._read_resumes',return_value=[]), patch('webapp._running',return_value=False), patch('webapp._any_run_alive',return_value=False), patch('webapp._spawn') as spawn, patch.object(webapp,'_proc',None):
            client=webapp.app.test_client()
            def assert_empty():
                state=client.get('/api/state').get_json()
                self.assertIsNone(state['run_id'])
                self.assertTrue(all(n==0 for n in state['counts'].values()))
                self.assertEqual(state['decisions'],[])
                self.assertEqual(state['run_log'],'')
                self.assertIsNone(state['last_run'])
                self.assertEqual(client.get('/api/runlog').get_json()['log'],'')
            # A: fresh server, existing disk history.
            assert_empty()
            self.assertEqual(path.read_bytes(),original)
            # B: real route, but worker launch and session presence are mocked.
            with patch('webapp.os.path.exists',return_value=True):
                self.assertEqual(client.post('/api/run').status_code,200)
            spawn.assert_called_once_with('run_web.py')
            assert_empty()
            new_rows=[dict(event='run_started',run_id='bbb'),
                      dict(run_id='bbb',decision='safe_ready',title='New'),
                      dict(event='run_finished',run_id='bbb',status='COMPLETED')]
            with path.open('a') as f:
                for row in new_rows: f.write(json.dumps(row)+'\n')
            state=client.get('/api/state').get_json()
            self.assertEqual(state['run_id'],'bbb')
            self.assertEqual(state['counts']['safe_ready'],1)
            self.assertEqual([r['title'] for r in state['decisions']],['New'])
            self.assertEqual(state['last_run']['status'],'COMPLETED')
            self.assertNotIn('aaa',state['run_log'])
            self.assertNotIn('Old error',state['run_log'])
            saved=path.read_bytes()
            self.assertTrue(saved.startswith(original))
            # Another server restart hides completed results without changing disk.
            webapp._pending_after_run=None
            webapp._display_run_id=None
            assert_empty()
            self.assertEqual(path.read_bytes(),saved)
