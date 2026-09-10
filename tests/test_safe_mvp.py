"""Offline fixtures only. No local .env, sessions or external services are read."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

with patch.dict(sys.modules, {'dotenv': types.SimpleNamespace(load_dotenv=lambda **kw: None)}), patch.dict(os.environ, {}, clear=True):
    import config
sys.modules["config"] = config  # patch.dict restores sys.modules: keep the isolated module.
import candidate_context
import decide
import hh_web
from hh_client import HHClient
import llm
import run_web
from privacy import sanitize_candidate_text, PrivacyError

RESULT = dict(score=85, decision='possible_match', matched=['Ведение проектов'], missing=[], risks=[], reason='Подтверждён опыт', remote_status='eligible', remote_reason='Полностью удалённо')
VAC = dict(id='123456789', name='Project manager', employer={'name':'Example'}, description='Полностью удалённо. Управление проектами.', url='https://hh.ru/vacancy/123456789')

class SafeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'work')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = patch.multiple(config, DRY_RUN=True, LLM_API_KEY='', ACCESS_TOKEN='',
            RESUMES_FILE=str(self.root/'resume-list.json'),
            RESUME_URL='https://hh.ru/resume/aaaa', RESUME_CACHE_DIR=str(self.root/'resumes'),
            DECISIONS_LOG=str(self.root/'history.jsonl'), RUN_PID_FILE=str(self.root/'run.pid'),
            PAGES_PER_RUN=1, PAUSE_SECONDS=0, TARGET_APPLICATIONS_PER_RUN=1, SCORE_THRESHOLD=80)
        self.settings.start(); self.addCleanup(self.settings.stop)
        block = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        block.start(); self.addCleanup(block.stop)

    def test_safe_browser_barrier(self):
        page = MagicMock()
        self.assertEqual(hh_web.apply(page, VAC, 'letter', dry_run=False), 'safe_not_sent')
        with patch.object(config, 'DRY_RUN', False):
            self.assertEqual(hh_web.apply(page, VAC, 'letter', dry_run=True), 'safe_not_sent')
            self.assertEqual(hh_web.apply(page, VAC, 'letter', dry_run=False), 'needs_manual_review')
        self.assertEqual(page.mock_calls, [])

    def test_api_cannot_send(self):
        client = HHClient('artificial')
        with patch.object(client.s, 'post') as post:
            for safe in (True, False):
                with patch.object(config, 'DRY_RUN', safe), self.assertRaises(RuntimeError):
                    client.negotiate(vacancy_id='1', resume_id='aaaa', message='test')
            post.assert_not_called()

    def test_cache_is_by_resume_id(self):
        folder = Path(config.RESUME_CACHE_DIR); folder.mkdir()
        (folder/'bbbb.txt').write_text('Commercial director')
        with patch('hh_web.load_resume_text', return_value='Project manager') as read:
            self.assertEqual(run_web._get_resume(None, 'aaaa'), 'Project manager')
            read.assert_called_once()
        with patch.object(config, 'RESUME_URL', 'https://hh.ru/resume/bbbb'), patch('hh_web.load_resume_text') as read:
            self.assertEqual(run_web._get_resume(None, 'bbbb'), 'Commercial director')
            read.assert_not_called()
        self.assertEqual((folder/'aaaa.txt').read_text(), 'Project manager')

    def test_missing_context_matching(self):
        with patch.object(candidate_context, 'CONTEXT_DIR', self.root/'absent'):
            self.assertEqual(candidate_context.load_candidate_context(), ('', ''))
        with patch('llm.generate', return_value=json.dumps(RESULT)):
            self.assertEqual(decide.score(VAC, 'Ведение проектов')['score'], 85)

    def test_context_scope_and_recursion(self):
        root=self.root/'allowed'; root.mkdir(); (root/'nested').mkdir()
        (root/'INSTRUCTIONS.md').write_text('Только факты')
        (root/'nested'/'case.txt').write_text('Запуск проекта')
        (root/'ignore.json').write_text('not professional')
        (self.root/'outside.md').write_text('outside data')
        with patch.object(candidate_context, 'CONTEXT_DIR', root):
            instructions, text = candidate_context.load_candidate_context()
            self.assertEqual(instructions, 'Только факты')
            self.assertIn('Запуск проекта', text); self.assertNotIn('outside', text)
            (root/'linked.md').symlink_to(self.root/'outside.md')
            with self.assertRaises(ValueError): candidate_context.load_candidate_context()

    def test_contacts_removed_professional_context_retained(self):
        text = 'Project Manager, 2020-2024\nМосква, Россия, UTC+3\nЗапуск проектов\nEmail: person@example.test\n+7 (999) 123-45-67\nHome address: 10 Test Street\nhttps://portfolio.example/case'
        cleaned=sanitize_candidate_text(text)
        for item in ('person@example.test','999','10 Test Street'): self.assertNotIn(item, cleaned)
        for item in ('Project Manager', '2020-2024', 'Москва, Россия', 'UTC+3', 'Запуск проектов', 'https://portfolio.example/case'): self.assertIn(item, cleaned)

    def test_standard_phone_variants(self):
        for phone in ('+1 (415) 555-2671', '8 999 123 45 67', '(415) 555-2671', '89991234567'):
            self.assertNotIn(phone, sanitize_candidate_text(phone))

    def test_secrets_blocked(self):
        for text in ('API_KEY=artificial-secret', 'password: artificial', 'Authorization: Bearer abcdefghijk', '{"cookies": []}', 'https://example.test/?token=artificial'):
            with self.assertRaises(PrivacyError): sanitize_candidate_text(text)
        with self.assertRaises(PrivacyError): sanitize_candidate_text('contains artificial-key-value', ('artificial-key-value',))

    def test_full_context_sanitized_before_matching_and_letter(self):
        vacancy=dict(VAC, description='Профессиональные требования. '*500+'END_VACANCY')
        resume='Опыт работы. '*600+'END_RESUME\nperson@example.test\n+7 (999) 123-45-67'
        with patch('llm.generate', return_value=json.dumps(RESULT)) as generate:
            decide.score(vacancy, resume, 'Дополнительный кейс', 'Акцент на проекты')
            prompt=generate.call_args.args[0]
            for marker in ('END_VACANCY','END_RESUME','Дополнительный кейс','Акцент на проекты'): self.assertIn(marker,prompt)
            self.assertNotIn('person@example.test',prompt); self.assertNotIn('999',prompt)
        with patch('llm.generate', return_value='Здравствуйте! Мой опыт ведения проектов подтверждён описанными кейсами.') as generate:
            decide.cover_letter(vacancy,resume)
            self.assertIn('END_VACANCY',generate.call_args.args[0]); self.assertNotIn('person@example.test',generate.call_args.args[0])

    def test_invalid_scores_never_become_high_scores(self):
        for number in (-100,1000,'100',True,None,85.1):
            with patch('llm.generate', return_value=json.dumps(dict(RESULT,score=number))), self.assertRaises(ValueError):
                decide.score(VAC,'Реальный опыт')
        for raw in ('100','garbage','{}','[]'):
            with patch('llm.generate', return_value=raw), self.assertRaises(ValueError): decide.score(VAC,'Опыт')

    def test_letter_has_no_fallback(self):
        with patch('llm.generate', return_value=None), self.assertRaises(ValueError): decide.cover_letter(VAC,'Опыт')

    def test_llm_final_boundary(self):
        response=MagicMock(); response.json.return_value={'choices':[{'message':{'content':'result'}}]}
        with patch.multiple(config, LLM_PROVIDER='openai', LLM_API_KEY='artificial-key-value', LLM_MODEL='test'), patch('llm.requests.post',return_value=response) as post:
            llm.generate('Опыт\nperson@example.test\n+7 (999) 123-45-67')
            content=post.call_args.kwargs['json']['messages'][0]['content']
            self.assertNotIn('person@example.test',content); self.assertNotIn('999',content)
            post.reset_mock()
            with self.assertRaises(PrivacyError): llm.generate('artificial-key-value')
            post.assert_not_called()

    def test_two_runs_preserve_history_without_submission(self):
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        with patch('run_web.browser_context',return_value=browser), patch('run_web.has_session',return_value=True), patch('run_web.load_candidate_context',return_value=('','')), patch('run_web._get_resume',return_value='Опыт'), patch('hh_web.search_page',return_value=[dict(VAC)]), patch('hh_web.load_full',side_effect=lambda page,vac:vac), patch('decide.hard_filters',return_value=None), patch('decide.score',return_value=RESULT), patch('decide.cover_letter',return_value='Письмо'), patch('hh_web.apply') as apply:
            self.assertEqual(run_web.main(),0); self.assertEqual(run_web.main(),0)
            apply.assert_not_called()
        rows=[json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()]
        decisions=[r for r in rows if r.get('vacancy_id')]
        self.assertEqual(len(decisions),2); self.assertNotEqual(decisions[0]['run_id'],decisions[1]['run_id'])
        self.assertEqual(decisions[0]['url'],VAC['url']); self.assertEqual(decisions[0]['vacancy_id'],VAC['id'])
        self.assertTrue(all(r['application_status']=='safe_not_sent' for r in decisions))
        self.assertFalse(Path(config.RUN_PID_FILE).exists())

    def test_history_and_errors_do_not_reveal_contacts(self):
        run_web._log(dict(reason='person@example.test +7 (999) 123-45-67'))
        text=Path(config.DECISIONS_LOG).read_text()
        self.assertNotIn('person@example.test',text); self.assertNotIn('999',text)
        self.assertEqual(run_web._error_code(RuntimeError('arbitrary_private_value')), 'RuntimeError')

    def test_ui_config_and_history(self):
        import webapp
        with patch('webapp.envfile.update_env') as save:
            client = webapp.app.test_client()
            self.assertEqual(client.post('/api/config',json={'RUN_MODE':'UNSUPPORTED'}).status_code,400)
            self.assertEqual(client.post('/api/config',json={'SCORE_THRESHOLD':'-100'}).status_code,400)
            save.assert_not_called()
            self.assertEqual(client.post('/api/config',json={'RUN_MODE':'SAFE','RESUME_URL':'https://hh.ru/resume/aaaa'}).status_code,200)
            self.assertEqual(save.call_args.args[0]['WORK_FORMAT'],'remote_only')
        run_web._log(dict(vacancy_id='123456789', url=VAC['url'], reason='person@example.test'))
        rows=webapp._read_decisions()
        self.assertEqual(rows[0]['url'],VAC['url']); self.assertNotIn('person@example.test',rows[0]['reason'])

if __name__ == '__main__': unittest.main()
