"""Save/read mode with an artificial config file; never launch a worker."""
import unittest
from unittest.mock import patch
import test_safe_mvp
import envfile
import webapp

class ModePersistenceTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def test_live_save_reload_still_requires_run_confirmation(self):
        path = str(self.root / 'settings.env')
        read, write = envfile.read_env, envfile.update_env
        with patch('webapp.envfile.read_env',side_effect=lambda:read(path)), patch('webapp.envfile.update_env',side_effect=lambda updates:write(updates,path)), patch('webapp._current_run_rows',return_value=(None,[])), patch('webapp._read_resumes',return_value=[]), patch('webapp._running',return_value=False), patch('webapp._any_run_alive',return_value=False), patch('webapp._spawn') as spawn:
            client = webapp.app.test_client()
            self.assertEqual(client.get('/api/state').get_json()['config']['RUN_MODE'],'SAFE')
            self.assertEqual(client.post('/api/config',json={'RUN_MODE':'LIVE','RESUME_URL':'https://hh.ru/resume/aaaa'}).status_code,200)
            self.assertEqual(read(path)['RUN_MODE'],'LIVE')
            self.assertEqual(client.get('/api/state').get_json()['config']['RUN_MODE'],'LIVE')
            spawn.assert_not_called()
            with patch('webapp.os.path.exists',return_value=True):
                for body in ({'mode':'LIVE'}, {'mode':'LIVE','confirm_live':False}):
                    self.assertEqual(client.post('/api/run',json=body).status_code,400)
            spawn.assert_not_called()
