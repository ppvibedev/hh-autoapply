"""Offline fixtures only. No local .env, sessions or external services are read."""
import base64
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
from resume_metadata import selected_resume_title, write_resume_metadata

RESULT = dict(
    score=85, capability_score=85, decision='possible_match',
    matched=['Ведение проектов'], missing=[], risks=[], reason='Подтверждён опыт',
    remote_status='eligible', remote_reason='Полностью удалённо',
    vacancy_role=dict(primary_function='Управление проектами', functional_scope='Проекты',
                      seniority='middle', accountability='Сроки и результат проектов'),
    role_alignment='aligned', role_alignment_reason='Функция вакансии соответствует профилю.',
    role_evidence=['Вакансия описывает управление проектами.'],
)
VAC = dict(id='123456789', name='Project manager', employer={'name':'Example'}, description='Полностью удалённо. Управление проектами.', url='https://hh.ru/vacancy/123456789')

class SafeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'work')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control_socket = f"/tmp/hh-autoapply-{os.getpid()}-{id(self)}.sock"
        self.addCleanup(lambda: Path(self.control_socket).unlink(missing_ok=True))
        storage = patch('llm_connections.STORAGE_PATH', self.root/'llm_connections.json')
        storage.start(); self.addCleanup(storage.stop)
        llm.reset_runtime(); self.addCleanup(llm.reset_runtime)
        self.settings = patch.multiple(config, DRY_RUN=True, LLM_API_KEY='', ACCESS_TOKEN='',
            RESUMES_FILE=str(self.root/'resume-list.json'),
            RESUME_URL='https://hh.ru/resume/aaaa', RESUME_CACHE_DIR=str(self.root/'resumes'),
            DECISIONS_LOG=str(self.root/'history.jsonl'), RUN_PID_FILE=str(self.root/'run.pid'),
            APPLICATION_SAFETY_FILE=str(self.root/'application-safety.json'),
            RUN_CONTROL_SOCKET=self.control_socket,
            PAGES_PER_RUN=1, PAUSE_SECONDS=0, TARGET_APPLICATIONS_PER_RUN=1, SCORE_THRESHOLD=80)
        self.settings.start(); self.addCleanup(self.settings.stop)
        Path(config.RESUMES_FILE).write_text(
            json.dumps([{'title': 'Project Manager', 'url': config.RESUME_URL}]), encoding='utf-8')
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

    def test_selected_resume_refreshes_same_id_once_and_replaces_stale_cache(self):
        folder = Path(config.RESUME_CACHE_DIR); folder.mkdir()
        (folder/'aaaa.txt').write_text('Stale project manager')
        with patch('hh_web.load_resume_text', return_value='Current project manager') as read:
            self.assertEqual(run_web._get_resume(None, 'aaaa'), 'Current project manager')
            read.assert_called_once()
        self.assertEqual((folder/'aaaa.txt').read_text(), 'Current project manager')

    def test_resume_metadata_replacement_never_exposes_partial_final_file(self):
        catalog = Path(config.RESUMES_FILE)
        old = [{'title': 'Old title', 'url': config.RESUME_URL}]
        new = [{'title': 'New title', 'url': config.RESUME_URL}]
        catalog.write_text(json.dumps(old), encoding='utf-8')
        real_replace = os.replace

        def replace_after_reader(temporary, target):
            self.assertEqual(json.loads(catalog.read_text(encoding='utf-8')), old)
            self.assertEqual(Path(temporary).parent, catalog.parent)
            real_replace(temporary, target)

        with patch('resume_metadata.os.replace', side_effect=replace_after_reader):
            write_resume_metadata(new)
        self.assertEqual(json.loads(catalog.read_text(encoding='utf-8')), new)

    def test_missing_selected_resume_metadata_stops_before_scan_or_matching(self):
        Path(config.RESUMES_FILE).write_text('[]', encoding='utf-8')
        rows = []
        with patch('run_web.has_session', return_value=True), \
             patch('run_web.browser_context') as browser, \
             patch('hh_web.search_page') as search, \
             patch('decide.score') as score, \
             patch('run_web._log', side_effect=rows.append):
            self.assertEqual(run_web.main(), 1)
        browser.assert_not_called(); search.assert_not_called(); score.assert_not_called()
        error = next(row for row in rows if row.get('event') == 'run_error')
        self.assertEqual(error['stage'], 'resume_metadata_preflight')
        self.assertEqual(error['reason_code'], 'selected_resume_title_unavailable')

    def test_selected_resume_content_failure_stops_before_scan_or_matching(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        rows = []
        with patch('run_web.has_session', return_value=True), \
             patch('run_web.browser_context', return_value=browser), \
             patch('hh_web.load_resume_text', side_effect=ValueError('resume_content_empty')), \
             patch('hh_web.search_page') as search, \
             patch('decide.score') as score, \
             patch('run_web._log', side_effect=rows.append):
            self.assertEqual(run_web.main(), 1)
        search.assert_not_called(); score.assert_not_called()
        error = next(row for row in rows if row.get('event') == 'run_error')
        self.assertEqual(error['stage'], 'resume_refresh')
        self.assertEqual(error['reason_code'], 'resume_content_empty')

    def test_run_keeps_selected_metadata_snapshot_after_catalog_refresh(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())

        def refreshed_resume(*_args, **_kwargs):
            Path(config.RESUMES_FILE).write_text(json.dumps([
                {'title': 'Changed title', 'url': config.RESUME_URL}
            ]), encoding='utf-8')
            return 'Current resume facts'

        with patch('run_web.has_session', return_value=True), \
             patch('run_web.browser_context', return_value=browser), \
             patch('run_web._get_resume', side_effect=refreshed_resume), \
             patch('hh_web.search_page', return_value=[dict(VAC)]), \
             patch('hh_web.load_full', side_effect=lambda page, vac, **_kwargs: vac), \
             patch('decide.hard_filters', return_value=None), \
             patch('decide.score', return_value=RESULT) as score, \
             patch('decide.cover_letter', return_value='Letter'):
            self.assertEqual(run_web.main(), 0)
        self.assertEqual(score.call_args.kwargs['resume_title'], 'Project Manager')

    def test_selected_resume_refreshes_once_before_all_vacancies(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        second = dict(VAC, id='987654321')
        with patch('run_web.has_session', return_value=True), \
             patch('run_web.browser_context', return_value=browser), \
             patch('run_web._get_resume', return_value='Current resume facts') as resume, \
             patch('hh_web.search_page', return_value=[dict(VAC), second]), \
             patch('hh_web.load_full', side_effect=lambda page, vac, **_kwargs: vac), \
             patch('decide.hard_filters', return_value=None), \
             patch('decide.score', return_value=RESULT), \
             patch('decide.cover_letter', return_value='Letter'):
            self.assertEqual(run_web.main(), 0)
        resume.assert_called_once()

    def test_empty_search_diagnostic_precedes_finish_and_run_config_is_safe(self):
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        rows = []
        with patch.multiple(
                config, SEARCH_TEXT='Project Manager', SEARCH_QUERY='Project Manager', AREA='',
                PAGES_PER_RUN=3, SCORE_THRESHOLD=75, TARGET_APPLICATIONS_PER_RUN=12,
                WORK_FORMAT='remote_only', WORK_FORMAT_CODES=['REMOTE'], SALARY_MIN=200000,
                EXPERIENCE='between3And6', EMPLOYMENT='full', SCHEDULE='',
                SEARCH_PERIOD='7', ORDER_BY='publication_time',
                HH_SEARCH_PARAMS={'experience':'between3And6','employment':'full',
                                  'search_period':'7','order_by':'publication_time'}), \
             patch('run_web.has_session', return_value=True), \
             patch('run_web.browser_context', return_value=browser), \
             patch('run_web._get_resume', return_value='Current resume facts'), \
             patch('hh_web.search_page', return_value=[]), \
             patch('run_web._log', side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        events = [row.get('event') for row in rows]
        self.assertLess(events.index('search_page_diagnostic'), events.index('run_finished'))
        diagnostic = next(row for row in rows if row.get('event') == 'search_page_diagnostic')
        self.assertEqual(diagnostic['page_num'], 0)
        self.assertEqual(diagnostic['parsed_vacancy_count'], 0)
        self.assertEqual(diagnostic['new_unique_vacancy_count'], 0)
        self.assertEqual(diagnostic['outcome'], 'empty_unclassified')
        final = next(row for row in rows if row.get('event') == 'run_finished')
        self.assertEqual(final['stop_reason'], 'no_results_or_page_not_recognized')

        snapshot = next(row for row in rows if row.get('event') == 'run_started')['search_config']
        self.assertEqual(snapshot, {
            'search_text':'Project Manager', 'search_query':'Project Manager',
            'area':'', 'area_scope':'worldwide', 'pages_per_run':3,
            'score_threshold':75, 'target_confirmed_applications':12,
            'work_format':'remote_only', 'work_format_codes':['REMOTE'],
            'salary_min':200000, 'experience':'between3And6', 'employment':'full',
            'schedule':'', 'search_period':'7', 'order_by':'publication_time',
        })
        forbidden = {'api_key','token','cookie','session','credential','password',
                     'resume_text','candidate_context','vacancy_description'}
        def keys(value):
            if isinstance(value, dict):
                return {str(key).lower() for key in value} | set().union(
                    *(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()
        self.assertTrue(forbidden.isdisjoint(keys(diagnostic) | keys(snapshot)))

    def test_missing_context_matching(self):
        with patch.object(candidate_context, 'CONTEXT_DIR', self.root/'absent'):
            self.assertEqual(candidate_context.load_candidate_context(), ('', ''))
        with patch('llm.generate', return_value=json.dumps(RESULT)):
            self.assertEqual(decide.score(VAC, 'Ведение проектов', search_query='Project Manager',
                                          resume_title='Project Manager')['score'], 85)

    def test_matching_requires_selected_resume_metadata_title(self):
        with patch('llm.generate') as generate, self.assertRaisesRegex(ValueError, 'selected_resume_title_unavailable'):
            decide.score(VAC, 'Ведение проектов', search_query='Project Manager')
        generate.assert_not_called()

    def test_selected_resume_title_is_taken_only_from_exact_metadata_entry(self):
        self.assertEqual(selected_resume_title('aaaa'), 'Project Manager')
        Path(config.RESUMES_FILE).write_text(json.dumps([
            {'title': 'Project Manager', 'url': config.RESUME_URL},
            {'title': 'Commercial Director', 'url': config.RESUME_URL},
        ]), encoding='utf-8')
        self.assertIsNone(selected_resume_title('aaaa'))
        self.assertIsNone(selected_resume_title('bbbb'))

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
        cases = (
            ('API_KEY=artificial-secret', 'credential_assignment'),
            ('password: artificial', 'credential_assignment'),
            ('Authorization: Bearer abcdefghijk', 'credential_assignment'),
            ('{"cookies": []}', 'json_cookie_or_origin_array'),
            ('https://example.test/?token=artificial', 'credential_query_parameter'),
        )
        for text, rule_id in cases:
            with self.assertRaises(PrivacyError) as caught:
                sanitize_candidate_text(text)
            self.assertEqual(caught.exception.privacy_rule_id, rule_id)
        with self.assertRaises(PrivacyError) as caught:
            sanitize_candidate_text('contains artificial-key-value', ('artificial-key-value',))
        self.assertEqual(caught.exception.privacy_rule_id, 'configured_credential_value')

    def test_matching_privacy_diagnostic_has_source_and_rule_without_content(self):
        private_value = 'never-persist-this-value'
        source = 'API_KEY=' + private_value + '\nprivate surrounding vacancy material'
        vacancy = dict(VAC, description=source)
        with patch('llm.generate') as generate, self.assertRaises(PrivacyError) as caught:
            decide.score(vacancy, 'Safe resume', resume_title='Project Manager')
        generate.assert_not_called()
        details = run_web._exception_details(caught.exception, 'matching')
        self.assertEqual(details['stage'], 'matching')
        self.assertEqual(details['privacy_source_field'], 'full_vacancy')
        self.assertEqual(details['privacy_rule_id'], 'credential_assignment')
        encoded = json.dumps(details)
        self.assertNotIn(private_value, encoded)
        self.assertNotIn('private surrounding vacancy material', encoded)
        self.assertNotIn(source, encoded)

    def test_auth_scheme_match_shape_distinguishes_natural_and_basic_credentials(self):
        def shape(text):
            with self.assertRaises(PrivacyError) as caught:
                sanitize_candidate_text(text)
            self.assertEqual(caught.exception.privacy_rule_id,'auth_scheme')
            return caught.exception.privacy_match_shape

        self.assertEqual(sanitize_candidate_text('Basic knowledge'),'Basic knowledge')
        credential=base64.b64encode(b'synthetic-user:synthetic-pass').decode()
        credential_shape=shape('Basic '+credential)
        self.assertTrue(credential_shape['basic_base64_valid'])
        self.assertTrue(credential_shape['basic_decoded_has_colon'])
        no_colon=base64.b64encode(b'synthetic-payload').decode()
        self.assertEqual(sanitize_candidate_text('Basic '+no_colon),'Basic '+no_colon)

    def test_auth_scheme_match_shape_separator_length_and_characters(self):
        def shape(text):
            with self.assertRaises(PrivacyError) as caught:
                sanitize_candidate_text(text)
            return caught.exception.privacy_match_shape

        for separator,expected in [(' ','space'),('\t','tab'),('\n','newline'),(' \t','mixed_whitespace')]:
            with self.subTest(separator=expected):
                self.assertEqual(shape('Bearer'+separator+'knowledge')['separator'],expected)
        for length,expected in ((8,'8_11'),(11,'8_11'),(12,'12_23'),(23,'12_23'),
                                (24,'24_47'),(47,'24_47'),(48,'48_plus')):
            with self.subTest(length=length):
                self.assertEqual(shape('Bearer '+'A'*length)['suffix_length_bucket'],expected)
        opaque=shape('Bearer Abcd1234._-=')
        self.assertEqual(opaque['scheme'],'bearer')
        self.assertFalse(opaque['letters_only'])
        self.assertTrue(opaque['has_digit'])
        self.assertTrue(opaque['has_token_punctuation'])
        self.assertIsNone(opaque['basic_base64_valid'])
        self.assertIsNone(opaque['basic_decoded_has_colon'])
        bearer_words=shape('Bearer knowledge')
        self.assertTrue(bearer_words['letters_only'])
        self.assertFalse(bearer_words['has_digit'])
        self.assertFalse(bearer_words['has_token_punctuation'])

    def test_auth_scheme_diagnostic_persists_shape_without_matched_value(self):
        private_suffix='SYNTHETIC123._-='
        source='Professional vacancy\nBearer '+private_suffix+'\nPRIVATE SURROUNDING TEXT'
        vacancy=dict(VAC,description=source)
        with patch('llm.generate') as generate, self.assertRaises(PrivacyError) as caught:
            decide.score(vacancy,'Safe resume',resume_title='Project Manager')
        generate.assert_not_called()
        details=run_web._exception_details(caught.exception,'matching')
        self.assertEqual(details['privacy_source_field'],'full_vacancy')
        self.assertEqual(details['privacy_rule_id'],'auth_scheme')
        self.assertEqual(details['privacy_match_shape']['scheme'],'bearer')
        run_web._log(dict(event='run_error',reason_code='unsafe_content_blocked',**details))
        persisted=Path(config.DECISIONS_LOG).read_text(encoding='utf-8')
        for private in (private_suffix,source,'PRIVATE SURROUNDING TEXT'):
            self.assertNotIn(private,persisted)
        self.assertIn('privacy_match_shape',persisted)

    def test_full_context_sanitized_before_matching_and_letter(self):
        vacancy=dict(VAC, description='Профессиональные требования. '*500+'END_VACANCY')
        resume='Опыт работы. '*600+'END_RESUME\nperson@example.test\n+7 (999) 123-45-67'
        with patch('llm.generate', return_value=json.dumps(RESULT)) as generate:
            decide.score(vacancy, resume, 'Дополнительный кейс', 'Акцент на проекты',
                         search_query='Project Manager', resume_title='Project Manager')
            prompt=generate.call_args.args[0]
            for marker in ('END_VACANCY','END_RESUME','Дополнительный кейс','Акцент на проекты','Project Manager'): self.assertIn(marker,prompt)
            self.assertNotIn('person@example.test',prompt); self.assertNotIn('999',prompt)
        with patch('llm.generate', return_value='Здравствуйте! Мой опыт ведения проектов подтверждён описанными кейсами.') as generate:
            decide.cover_letter(vacancy,resume)
            self.assertIn('END_VACANCY',generate.call_args.args[0]); self.assertNotIn('person@example.test',generate.call_args.args[0])

    def test_invalid_scores_never_become_high_scores(self):
        for number in (-100,1000,'100',True,None,85.1):
            with patch('llm.generate', return_value=json.dumps(dict(RESULT,score=number))), self.assertRaises(ValueError):
                decide.score(VAC,'Реальный опыт', resume_title='Project Manager')
        for raw in ('100','garbage','{}','[]'):
            with patch('llm.generate', return_value=raw), self.assertRaises(ValueError):
                decide.score(VAC,'Опыт', resume_title='Project Manager')

    def test_invalid_role_contract_never_passes_matching(self):
        invalid = (
            dict(RESULT, role_alignment='sales_manager'),
            dict(RESULT, role_evidence=[]),
            dict(RESULT, vacancy_role=dict(RESULT['vacancy_role'], accountability='')),
            dict(RESULT, capability_score=84),
            dict(RESULT, capability_score=85.0),
        )
        for result in invalid:
            with patch('llm.generate', return_value=json.dumps(result)), self.assertRaises(ValueError):
                decide.score(VAC, 'Опыт', resume_title='Project Manager')

    def test_role_contract_covers_pm_cco_coo_and_growth_cases_with_one_call(self):
        cases = (
            ('Project Manager', 'Менеджер по продажам IT-оборудования', 'functional_mismatch'),
            ('Commercial Director / CCO', 'Руководитель отдела продаж', 'adjacent'),
            ('Operations Director / COO', 'Операционный директор e-commerce', 'aligned'),
            ('Growth Director', 'Head of Marketing', 'adjacent'),
        )
        for resume_title, vacancy_title, alignment in cases:
            with self.subTest(resume_title=resume_title, vacancy_title=vacancy_title):
                vacancy = dict(VAC, name=vacancy_title,
                               description='Полностью удалённо. Цели, KPI и ответственность роли.')
                response = dict(RESULT, role_alignment=alignment,
                                vacancy_role=dict(RESULT['vacancy_role'], primary_function=vacancy_title))
                with patch('llm.generate', return_value=json.dumps(response)) as generate:
                    result = decide.score(vacancy, 'Подтверждённый опыт',
                                          search_query='руководитель направления', resume_title=resume_title)
                self.assertEqual(result['role_alignment'], alignment)
                self.assertEqual(result['score'], result['capability_score'])
                self.assertEqual(generate.call_count, 1)
                prompt = generate.call_args.args[0]
                self.assertIn('"current_search_intent": "руководитель направления"', prompt)
                self.assertIn('"selected_resume_title": "'+resume_title+'"', prompt)

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
        with patch('run_web.browser_context',return_value=browser), patch('run_web.has_session',return_value=True), patch('run_web.load_candidate_context',return_value=('','')), patch('run_web._get_resume',return_value='Опыт'), patch('hh_web.search_page',return_value=[dict(VAC)]), patch('hh_web.load_full',side_effect=lambda page,vac,**_kwargs:vac), patch('decide.hard_filters',return_value=None), patch('decide.score',return_value=RESULT), patch('decide.cover_letter',return_value='Письмо'), patch('hh_web.apply') as apply:
            self.assertEqual(run_web.main(),0); self.assertEqual(run_web.main(),0)
            apply.assert_not_called()
        rows=[json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()]
        decisions=[r for r in rows if r.get('vacancy_id')]
        self.assertEqual(len(decisions),2); self.assertNotEqual(decisions[0]['run_id'],decisions[1]['run_id'])
        self.assertEqual(decisions[0]['url'],VAC['url']); self.assertEqual(decisions[0]['vacancy_id'],VAC['id'])
        self.assertTrue(all(r['application_status']=='safe_not_sent' for r in decisions))
        self.assertFalse(Path(config.RUN_PID_FILE).exists())
        self.assertFalse(Path(config.RUN_CONTROL_SOCKET).exists())

    def test_non_aligned_role_is_manual_and_keeps_original_matching_result(self):
        mismatch = dict(RESULT, role_alignment='functional_mismatch',
                        role_alignment_reason='Основная функция вакансии — продажи.',
                        role_evidence=['В обязанностях указан план продаж.'])
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        with patch('run_web.browser_context',return_value=browser), patch('run_web.has_session',return_value=True), patch('run_web.load_candidate_context',return_value=('','')), patch('run_web._get_resume',return_value='Опыт'), patch('hh_web.search_page',return_value=[dict(VAC)]), patch('hh_web.load_full',side_effect=lambda page,vac,**_kwargs:vac), patch('decide.hard_filters',return_value=None), patch('decide.score',return_value=mismatch), patch('decide.cover_letter') as letter, patch('hh_web.apply') as apply:
            self.assertEqual(run_web.main(), 0)
        letter.assert_not_called(); apply.assert_not_called()
        row = next(json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()
                   if 'vacancy_id' in line)
        self.assertEqual(row['decision'], 'needs_manual_review')
        self.assertEqual(row['reason_code'], 'role_functional_mismatch')
        self.assertEqual(row['matching_result']['reason'], RESULT['reason'])
        self.assertEqual(row['matching_result']['role_alignment'], 'functional_mismatch')

    def test_score_threshold_precedes_role_alignment_for_valid_matching_results(self):
        cases = (
            (15, 'functional_mismatch', 'rejected_score', False),
            (65, 'adjacent', 'rejected_score', False),
            (74, 'aligned', 'rejected_score', False),
            (75, 'aligned', 'safe_ready', True),
            (85, 'aligned', 'safe_ready', True),
            (85, 'adjacent', 'needs_manual_review', False),
            (85, 'functional_mismatch', 'needs_manual_review', False),
            (85, 'unclear', 'needs_manual_review', False),
        )
        for index, (score, alignment, expected, letter_expected) in enumerate(cases):
            with self.subTest(score=score, alignment=alignment):
                vacancy = dict(VAC, id=str(200000000 + index))
                result = dict(RESULT, score=score, capability_score=score,
                              role_alignment=alignment,
                              role_alignment_reason='Тестовая классификация роли.')
                browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
                with patch.object(config, 'SCORE_THRESHOLD', 75), \
                     patch('run_web.browser_context', return_value=browser), \
                     patch('run_web.has_session', return_value=True), \
                     patch('run_web.load_candidate_context', return_value=('', '')), \
                     patch('run_web._get_resume', return_value='Опыт'), \
                     patch('hh_web.search_page', return_value=[vacancy]), \
                     patch('hh_web.load_full', side_effect=lambda page, item, **_kwargs: item), \
                     patch('decide.hard_filters', return_value=None), \
                     patch('decide.score', return_value=result), \
                     patch('decide.cover_letter', return_value='Письмо') as letter:
                    self.assertEqual(run_web.main(), 0)
                row = [json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()
                       if 'vacancy_id' in line][-1]
                self.assertEqual(row['decision'], expected)
                self.assertEqual(letter.called, letter_expected)

    def test_threshold_and_manual_counters_are_disjoint_and_matching_failure_stays_manual(self):
        vacancies = [dict(VAC, id=str(300000000 + index)) for index in range(4)]
        results = [
            dict(RESULT, score=15, capability_score=15, role_alignment='functional_mismatch'),
            dict(RESULT, score=85, capability_score=85, role_alignment='adjacent'),
            dict(RESULT, score=85, capability_score=85, role_alignment='aligned'),
            ValueError('matching_invalid_json'),
        ]
        def score(*_args, **_kwargs):
            value = results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        browser = MagicMock(); browser.__enter__.return_value = (None, MagicMock())
        with patch.object(config, 'SCORE_THRESHOLD', 75), \
             patch('run_web.browser_context', return_value=browser), \
             patch('run_web.has_session', return_value=True), \
             patch('run_web.load_candidate_context', return_value=('', '')), \
             patch('run_web._get_resume', return_value='Опыт'), \
             patch('hh_web.search_page', return_value=vacancies), \
             patch('hh_web.load_full', side_effect=lambda page, item, **_kwargs: item), \
             patch('decide.hard_filters', return_value=None), \
             patch('decide.score', side_effect=score), \
             patch('decide.cover_letter', return_value='Письмо'):
            self.assertEqual(run_web.main(), 0)
        rows = [json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()]
        decisions = [row['decision'] for row in rows if row.get('vacancy_id')]
        self.assertEqual(decisions, ['rejected_score', 'needs_manual_review',
                                     'safe_ready', 'needs_manual_review'])
        final = next(row for row in reversed(rows) if row.get('event') == 'run_finished')
        self.assertEqual(final['counts']['score_rejected'], 1)
        self.assertEqual(final['counts']['manual_review'], 2)
        self.assertEqual(final['counts']['safe_ready'], 1)

    def test_role_gate_regression_matrix(self):
        cases = (
            ('PM → active B2B Sales', 'Project Manager', 'B2B Sales Manager',
             'Активные B2B-продажи, план продаж и CRM.', 'functional_mismatch', 95, False),
            ('PM → client-facing PM', 'Project Manager', 'Project Manager',
             'Ведение проектов, переговоры с клиентами и presale.', 'aligned', 85, True),
            ('CCO → Head of Sales', 'Commercial Director / CCO', 'Head of Sales',
             'План продаж, pipeline, CRM и личное выполнение продаж.', 'adjacent', 85, False),
            ('CCO → full CCO', 'Commercial Director / CCO', 'Commercial Director',
             'Коммерческая стратегия, продажи, маркетинг, P&L, GTM и leadership.', 'aligned', 85, True),
            ('CCO → Operations Director', 'Commercial Director / CCO', 'Operations Director',
             'Операционные процессы, service delivery и эффективность.', 'functional_mismatch', 85, False),
            ('CCO → Marketing Director', 'Commercial Director / CCO', 'Marketing Director',
             'Маркетинговая стратегия, performance-каналы и бренд.', 'adjacent', 85, False),
            ('CCO → commercial Growth Manager', 'Commercial Director / CCO', 'Growth Manager',
             'Revenue ownership, GTM, sales + marketing, unit economics и strategy.', 'aligned', 85, True),
            ('CCO → performance Growth Manager', 'Commercial Director / CCO', 'Growth Manager',
             'Performance campaigns, acquisition, SEO и advertising execution.', 'functional_mismatch', 85, False),
            ('CCO → unclear role', 'Commercial Director / CCO', 'Growth Manager',
             'Описание обязанностей не содержит данных о scope или ownership.', 'unclear', 85, False),
            ('mismatch score 95 threshold 75', 'Commercial Director / CCO', 'Operations Director',
             'Операционная ответственность и эффективность процессов.', 'functional_mismatch', 95, False),
        )
        for name, resume_title, vacancy_title, description, alignment, score, letter_expected in cases:
            with self.subTest(name=name):
                vacancy = dict(VAC, name=vacancy_title, description='Полностью удалённо. '+description)
                result = dict(RESULT, score=score, capability_score=score,
                              role_alignment=alignment,
                              role_alignment_reason='Тестовая классификация роли.',
                              role_evidence=[description])
                browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
                with patch.object(config, 'SCORE_THRESHOLD', 75 if name == 'mismatch score 95 threshold 75' else 80), \
                     patch('run_web.browser_context', return_value=browser), \
                     patch('run_web.has_session', return_value=True), \
                     patch('run_web.load_candidate_context', return_value=('', '')), \
                     patch('run_web._get_resume', return_value='Опыт'), \
                     patch('run_web.selected_resume_title', return_value=resume_title), \
                     patch('hh_web.search_page', return_value=[vacancy]), \
                     patch('hh_web.load_full', side_effect=lambda page, item, **_kwargs: item), \
                     patch('decide.hard_filters', return_value=None), \
                     patch('decide.score', return_value=result), \
                     patch('decide.cover_letter', return_value='Письмо') as letter, \
                     patch('hh_web.apply') as apply:
                    self.assertEqual(run_web.main(), 0)
                self.assertEqual(letter.called, letter_expected)
                apply.assert_not_called()
                row = [json.loads(line) for line in Path(config.DECISIONS_LOG).read_text().splitlines()
                       if 'vacancy_id' in line][-1]
                self.assertEqual(row['decision'], 'safe_ready' if letter_expected else 'needs_manual_review')
                self.assertEqual(row['role_alignment'], alignment)

    def test_growth_title_outside_search_query_can_pass_when_role_is_aligned(self):
        vacancy = dict(VAC, name='Growth Manager', description=(
            'Полностью удалённо. Revenue ownership, GTM, sales + marketing, unit economics, '
            'commercial strategy and cross-functional leadership.'))
        seen = {}
        def matching(*args, **kwargs):
            seen['resume_text'] = args[1]
            seen.update(kwargs)
            return RESULT
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        with patch.object(config, 'SEARCH_QUERY', 'Commercial Director OR CCO'), \
             patch('run_web.browser_context', return_value=browser), \
             patch('run_web.has_session', return_value=True), \
             patch('run_web.load_candidate_context', return_value=('', '')), \
             patch('run_web._get_resume', return_value='CCO подтверждённый опыт'), \
             patch('run_web.selected_resume_title', return_value='Commercial Director / CCO'), \
             patch('hh_web.search_page', return_value=[vacancy]), \
             patch('hh_web.load_full', side_effect=lambda page, item, **_kwargs: item), \
             patch('decide.hard_filters', return_value=None), \
             patch('decide.score', side_effect=matching), \
             patch('decide.cover_letter', return_value='Письмо') as letter:
            self.assertEqual(run_web.main(), 0)
        self.assertEqual(seen['search_query'], 'Commercial Director OR CCO')
        self.assertEqual(seen['resume_title'], 'Commercial Director / CCO')
        self.assertEqual(seen['resume_text'], 'CCO подтверждённый опыт')
        letter.assert_called_once()

    def test_history_and_errors_do_not_reveal_contacts(self):
        run_web._log(dict(reason='person@example.test +7 (999) 123-45-67'))
        text=Path(config.DECISIONS_LOG).read_text()
        self.assertNotIn('person@example.test',text); self.assertNotIn('999',text)
        self.assertEqual(run_web._error_code(RuntimeError('arbitrary_private_value')), 'RuntimeError')

    def test_matching_result_is_sanitized_before_persistence(self):
        result = dict(RESULT, role_alignment_reason='Связаться: person@example.test',
                      role_evidence=['Телефон +7 (999) 123-45-67 указан в вакансии.'])
        run_web._log(dict(matching_result=result))
        text = Path(config.DECISIONS_LOG).read_text()
        self.assertNotIn('person@example.test', text)
        self.assertNotIn('999', text)

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
