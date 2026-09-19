"""Synthetic connections/HTTP only. No real storage, credentials or provider requests."""
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import MagicMock, patch

import test_safe_mvp
import config
import llm
import llm_connections as store
from llm_connections import LLMConnection as Connection
from llm_errors import LLMUnavailable, classify_http
import run_web
from requests import Response

DAILY = 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'
RPM = 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'
TPM = 'GenerateContentInputTokensPerModelPerMinute-FreeTier'


def response(status=200, qid=None, delay=None, body=None):
    r = Response(); r.status_code = status
    r.url = 'https://example.test/?key=SECRET_A'
    if body is None:
        if status == 200:
            body = {'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}
        else:
            details = []
            if qid:
                details.append({'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': qid}]})
            if delay is not None:
                details.append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': delay})
            body = {'error': {'status': 'RESOURCE_EXHAUSTED', 'message': 'SECRET_B private prompt', 'details': details}}
    r._content = json.dumps(body).encode()
    return r


def connection(index=1, provider='gemini', model='model-x', enabled=True, folder_id=''):
    return Connection(f'{index:032x}', provider, model, enabled, f'SECRET_{chr(64+index)}', folder_id)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name).resolve()/'private'/'llm_connections.json'
        p = patch.object(store, 'STORAGE_PATH', self.path); p.start(); self.addCleanup(p.stop)
        p = patch.multiple(config, LLM_PROVIDER='gemini', LLM_MODEL='model-x', LLM_API_KEY='SECRET_A', YANDEX_FOLDER_ID='', ACCESS_TOKEN='')
        p.start(); self.addCleanup(p.stop)
        p = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        p.start(); self.addCleanup(p.stop)
        llm.reset_runtime(); self.addCleanup(llm.reset_runtime)

    def test_legacy_success(self):
        with patch('llm.requests.post', return_value=response()) as post:
            self.assertEqual(llm.generate('professional facts'), 'OK')
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs['params']['key'], 'SECRET_A')
        self.assertFalse(self.path.exists())

    def test_active_success_does_not_call_b(self):
        router = llm.Router([connection(), connection(2)])
        with patch('llm.requests.post', return_value=response()) as post:
            router.generate('facts'); router.generate('facts')
            self.assertEqual([c.kwargs['params']['key'] for c in post.call_args_list], ['SECRET_A']*2)

    def test_daily_zero_retries_and_sticky_failover(self):
        router = llm.Router([connection(), connection(2)], sleep=MagicMock())
        with patch('llm.requests.post', side_effect=[response(429, DAILY), response(), response()]) as post:
            router.generate('facts'); router.generate('facts')
            self.assertEqual([c.kwargs['params']['key'] for c in post.call_args_list], ['SECRET_A','SECRET_B','SECRET_B'])
        router.sleep.assert_not_called()

    def test_same_combination_first_then_other_provider(self):
        router = llm.Router([connection(), connection(4, 'openai', 'other'), connection(2), connection(3)])
        openai = response(body={'choices':[{'message':{'content':'OK'}}]})
        with patch('llm.requests.post', side_effect=[response(429, DAILY)]*3+[openai]) as post:
            self.assertEqual(router.generate('facts'), 'OK')
            self.assertEqual([c.kwargs.get('params',{}).get('key') for c in post.call_args_list], ['SECRET_A','SECRET_B','SECRET_C',None])
            self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer SECRET_D')

    def test_third_same_model_recovers(self):
        router=llm.Router([connection(),connection(2),connection(3)])
        with patch('llm.requests.post',side_effect=[response(429,DAILY),response(429,DAILY),response()]) as post:
            self.assertEqual(router.generate('facts'),'OK'); self.assertEqual(post.call_count,3)

    def test_disabled_and_incomplete_yandex_skipped(self):
        router=llm.Router([connection(enabled=False),connection(2,'yandex'),connection(3)])
        with patch('llm.requests.post',return_value=response()) as post:
            router.generate('facts'); self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.kwargs['params']['key'],'SECRET_C')

    def test_rate_recovery_and_exhaustion(self):
        for qid in (RPM, TPM, None):
            for recover in (True,False):
                with self.subTest(qid=qid,recover=recover):
                    sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
                    replies=[response(429,qid),response()] if recover else [response(429,qid),response(429,qid),response()]
                    with patch('llm.requests.post',side_effect=replies) as post:
                        router.generate('facts')
                        self.assertEqual(post.call_count,2 if recover else 3)
                        self.assertEqual(router.active.id,connection().id if recover else connection(2).id)
                    sleep.assert_called_once_with(2.0)

    def test_retry_delay_bounds(self):
        for delay, expected in [('7.5s',7.5),('0s',1.0),('31s',None)]:
            with self.subTest(delay=delay):
                sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
                with patch('llm.requests.post',side_effect=[response(429,RPM,delay),response()]):
                    router.generate('facts')
                if expected is None:
                    sleep.assert_not_called(); self.assertEqual(router.active.id,connection(2).id)
                else: sleep.assert_called_once_with(expected)

    def test_terminal_no_further_calls_and_safe_exception(self):
        router=llm.Router([connection(),connection(2),connection(3)])
        with patch('llm.requests.post',side_effect=[response(429,DAILY)]*3) as post:
            for _ in range(4):
                try: router.generate('facts')
                except LLMUnavailable as error:
                    output=repr(error)+str(error)+json.dumps(run_web._exception_details(error,'question'))+traceback.format_exc()
                    for key in ('SECRET_A','SECRET_B','SECRET_C','private prompt','?key='):
                        self.assertNotIn(key,output)
                    self.assertIsNone(error.__context__)
                else: self.fail('terminal required')
            self.assertEqual(post.call_count,3)

    def test_provider_failures_no_rate_retries(self):
        for status,category in [(401,'auth/provider_access'),(403,'auth/provider_access'),(503,'provider_server_error'),(400,'provider_http_error')]:
            with self.subTest(status=status):
                sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
                with patch('llm.requests.post',side_effect=[response(status),response()]): router.generate('facts')
                self.assertEqual(router.failures[0]['provider_error_category'],category); sleep.assert_not_called()

    def test_all_provider_request_contracts(self):
        fixtures={'anthropic':{'content':[{'text':'OK'}]},'openai':{'choices':[{'message':{'content':'OK'}}]},
                  'yandex':{'result':{'alternatives':[{'message':{'text':'OK'}}]}}}
        for provider,body in fixtures.items():
            with self.subTest(provider=provider),patch('llm.requests.post',return_value=response(body=body)) as post:
                router=llm.Router([connection(provider=provider,folder_id='folder')])
                self.assertEqual(router.generate('facts',650,'question'),'OK')
                self.assertEqual(post.call_args.kwargs['timeout'],60)

    def test_storage_migration_public_enable_and_secret_safety(self):
        import webapp
        client=webapp.app.test_client()
        env={'LLM_PROVIDER':'gemini','LLM_MODEL':'model-x','LLM_API_KEY':'SECRET_A'}
        with patch('webapp.envfile.read_env',return_value=env):
            self.assertEqual(client.get('/api/llm-connections').json['connections'][0]['id'],'legacy')
            result=client.post('/api/llm-connections',json={'provider':'gemini','model':'model-x','credential':'SECRET_B'})
            self.assertEqual(result.status_code,200)
            rows=result.json['connections']; self.assertEqual(len(rows),2)
            self.assertTrue(all(row['credential_configured'] for row in rows))
            cid=rows[1]['id']
            self.assertEqual(client.patch('/api/llm-connections/'+cid,json={'enabled':False}).status_code,200)
            self.assertFalse(store.load_connections()[1].enabled)
            self.assertEqual(store.load_connections()[0].credential,'SECRET_A')
            public=client.get('/api/llm-connections').get_data(as_text=True)+repr(store.load_connections())
            for secret in ('SECRET_A','SECRET_B','SECRET_C'): self.assertNotIn(secret,public)
            self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
            router=llm.Router(store.load_connections())
            with patch('llm.requests.post',return_value=response()) as post:
                router.generate('facts'); self.assertEqual(post.call_args.kwargs['params']['key'],'SECRET_A')
            self.assertEqual(client.patch('/api/llm-connections/'+cid,json={'credential':''}).status_code,200)
            self.assertEqual(store.load_connections()[1].credential,'SECRET_B')

    def test_secret_in_any_prompt_or_public_metadata_is_blocked(self):
        from privacy import sanitize_candidate_text, PrivacyError
        store.save_change({'provider':'gemini','model':'model-x','credential':'SECRET_B'})
        store.save_change({'provider':'gemini','model':'model-x','credential':'SECRET_C','enabled':False})
        for secret in ('SECRET_A','SECRET_B','SECRET_C'):
            with self.assertRaises(PrivacyError): sanitize_candidate_text('facts '+secret)
            with self.assertRaises(store.ConnectionConfigError):
                store.save_change({'provider':'gemini','model':secret,'credential':'OTHER_KEY'})
        self.assertEqual(len(store.load_connections()),3)

    def test_runtime_connections_are_frozen_before_first_task(self):
        llm.start_runtime()
        store.save_change({'provider':'gemini','model':'model-x','credential':'SECRET_B'})
        with patch('llm.requests.post',return_value=response(429,DAILY)) as post:
            with self.assertRaises(LLMUnavailable): llm.generate('facts')
            self.assertEqual(post.call_count,1)
        llm.reset_runtime()
        with patch('llm.requests.post',side_effect=[response(429,DAILY),response()]) as post:
            self.assertEqual(llm.generate('facts'),'OK'); self.assertEqual(post.call_count,2)

    def test_runtime_does_not_disable_persistence(self):
        store.save_change({'provider':'gemini','model':'model-x','credential':'SECRET_B'})
        with patch('llm.requests.post',side_effect=[response(429,DAILY)]*2):
            with self.assertRaises(LLMUnavailable): llm.generate('facts')
        self.assertTrue(all(c.enabled for c in store.load_connections()))
        llm.reset_runtime()
        with patch('llm.requests.post',return_value=response()) as post:
            llm.generate('facts'); self.assertEqual(post.call_args.kwargs['params']['key'],'SECRET_A')

    def test_empty_explicit_list_never_restores_legacy(self):
        self.path.parent.mkdir(); self.path.write_text('{"version":1,"connections":[]}')
        with patch('llm.requests.post') as post:
            with self.assertRaises(LLMUnavailable): llm.generate('facts')
            post.assert_not_called()

    def test_output_cannot_return_any_configured_secret(self):
        from privacy import PrivacyError
        for key in ('SECRET_A','SECRET_B','SECRET_C'):
            router=llm.Router([connection(),connection(2),connection(3,enabled=False)])
            with patch('llm.requests.post',return_value=response(body={'candidates':[{'content':{'parts':[{'text':key}]}}]})):
                with self.assertRaises(PrivacyError): router.generate('facts')

    def test_invalid_matching_json_does_not_trigger_provider_fallback(self):
        import decide
        router=llm.Router([connection(),connection(2)])
        with patch.object(llm,'_router',router),patch('llm.requests.post',return_value=response()) as post:
            with self.assertRaisesRegex(ValueError,'matching_invalid_json'):
                decide.score(test_safe_mvp.VAC, 'facts', resume_title='Project Manager')
            self.assertEqual(post.call_count,1)

    def test_other_model_fallback_and_missing_credentials(self):
        router=llm.Router([Connection('legacy','gemini','model-x'),connection(),connection(2,model='model-y')])
        with patch('llm.requests.post',side_effect=[response(429,DAILY),response()]) as post:
            router.generate('facts'); self.assertEqual(post.call_count,2)
            self.assertIn('model-y',post.call_args.args[0])

    def test_invalid_storage_becomes_safe_terminal(self):
        self.path.parent.mkdir(); self.path.write_text('{broken')
        with patch('llm.requests.post') as post:
            with self.assertRaises(LLMUnavailable) as caught: llm.generate('facts')
            post.assert_not_called()
        self.assertIn('connection_config_invalid',str(caught.exception.failures))
        run_web._exception_details(caught.exception,'matching')

    def test_foreign_origin_write_is_rejected(self):
        import webapp
        with patch('webapp.envfile.read_env',return_value={}),patch('llm_connections.save_change') as save:
            result=webapp.app.test_client().post('/api/llm-connections',json={'provider':'gemini','credential':'SECRET_B'},headers={'Origin':'https://other.test'})
        self.assertEqual(result.status_code,400); save.assert_not_called()

    def test_public_state_endpoint_never_returns_keys(self):
        import webapp
        env={'LLM_PROVIDER':'gemini','LLM_MODEL':'model-x','LLM_API_KEY':'SECRET_A'}
        with patch('webapp.envfile.read_env',return_value=env),patch('webapp._current_run_rows',return_value=(None,[])),patch('webapp._read_resumes',return_value=[]),patch('webapp._running',return_value=False),patch('webapp._any_run_alive',return_value=False),patch('webapp._login_running',return_value=False),patch('webapp._resumes_running',return_value=False):
            text=webapp.app.test_client().get('/api/state').get_data(as_text=True)
        self.assertNotIn('SECRET_A',text)

    def test_malformed_storage_and_symlink_fail_closed(self):
        self.path.parent.mkdir(); self.path.write_text('{broken')
        with self.assertRaises(store.ConnectionConfigError): store.load_connections()
        self.path.unlink(); self.path.symlink_to(self.path.parent/'target')
        with self.assertRaises(store.ConnectionConfigError): store.save_change({'provider':'gemini','credential':'SECRET_B'})


class ClassificationTests(unittest.TestCase):
    def test_categories(self):
        for qid,category in [(DAILY,'quota_daily'),(RPM,'rate_limit_requests'),(TPM,'rate_limit_tokens'),(None,'rate_limit_unknown'),('SECRET_C','rate_limit_unknown')]:
            with self.subTest(qid=qid):
                failure=classify_http('gemini',response(429,qid)); self.assertEqual(failure.category,category)
                self.assertNotIn('SECRET_C',repr(failure))

    def test_malformed_and_message_alone_are_unknown(self):
        for body in ([],[],{'error':[]},{'error':{'details':{}}},{'error':{'details':[None,4,{'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[None]}]}},{'error':{'message':DAILY}}):
            self.assertEqual(classify_http('gemini',response(429,body=body)).category,'rate_limit_unknown')
        r=response(429); r._content=b'not json'
        self.assertEqual(classify_http('gemini',r).category,'rate_limit_unknown')

    def test_daily_wins_over_retry_info_and_other_quotas(self):
        r=response(429,DAILY,'12s'); body=r.json()
        body['error']['details'][0]['violations'].append({'quotaId':RPM})
        r._content=json.dumps(body).encode()
        self.assertEqual(classify_http('gemini',r).category,'quota_daily')

    def test_retry_after_and_retry_info_take_later_delay(self):
        r=response(429,RPM,'7s'); r.headers['Retry-After']='12'
        self.assertEqual(classify_http('gemini',r).retry_delay,12)
        for value in ('NaN','inf','-1','SECRET_A'):
            r=response(429); r.headers['Retry-After']=value
            self.assertIsNone(classify_http('gemini',r).retry_delay)

    def test_non_gemini_daily_text_does_not_prove_daily_quota(self):
        self.assertEqual(classify_http('openai',response(429,DAILY)).category,'rate_limit_unknown')


class RunTerminalTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp
    def test_terminal_matching_and_letter_stop_before_second_vacancy(self):
        for stage in ('matching','cover_letter'):
            with self.subTest(stage=stage):
                browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
                vacancies=[dict(test_safe_mvp.VAC,id=str(i)) for i in (123,456,789)]
                rows=[]
                with patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value='facts'),patch('hh_web.search_page',return_value=vacancies),patch('hh_web.load_full',side_effect=lambda p,v:v) as full,patch('decide.hard_filters',return_value=None),patch('decide.score',side_effect=LLMUnavailable() if stage=='matching' else None,return_value=test_safe_mvp.RESULT),patch('decide.cover_letter',side_effect=LLMUnavailable()),patch('run_web._log',side_effect=rows.append),patch('hh_web.apply') as apply:
                    run_web.main()
                self.assertEqual(full.call_count,1); apply.assert_not_called()
                self.assertEqual(rows[-1]['status'],'STOPPED')
                self.assertEqual(rows[-1]['stop_reason'],'llm_infrastructure_unavailable')

    def test_question_terminal_survives_answer_boundary(self):
        import application
        question={'question':'Какие проекты вы вели?','type':'text','required':False,'options':[]}
        with patch('question_sources.resolve',return_value=None),patch('llm.generate',side_effect=LLMUnavailable()):
            with self.assertRaises(LLMUnavailable): application.answer_question(question,test_safe_mvp.VAC,'facts')

    def test_terminal_through_real_question_application_and_run_boundaries(self):
        import test_live
        import application
        real_answer = application.answer_question
        for required in (True, False):
            with self.subTest(required=required):
                page,form,letter,submit,route,journal,order,fill,model = test_live.LiveTests.fixture(self, questions=[dict(test_live.Q, required=required)])
                browser=MagicMock(); browser.__enter__.return_value=(None,page)
                rows=[]
                with patch('application.answer_question',side_effect=real_answer),patch('llm.generate',side_effect=LLMUnavailable()),patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value=test_live.FACT),patch('hh_web.search_page',return_value=[dict(test_live.VAC),dict(test_live.VAC,id='999')]),patch('hh_web.load_full',side_effect=lambda p,v:v) as full,patch('decide.hard_filters',return_value=None),patch('decide.score',return_value=test_safe_mvp.RESULT),patch('decide.cover_letter',return_value='Prepared letter'),patch('run_web._log',side_effect=rows.append):
                    run_web.main()
                submit.click.assert_not_called(); fill.assert_not_called()
                self.assertEqual(full.call_count,1)
                self.assertEqual(rows[-1]['stop_reason'],'llm_infrastructure_unavailable')
                self.assertEqual(rows[-2]['stage'],'application')

    def test_actual_router_daily_terminal_stops_vacancy_loop(self):
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        rows=[]
        with patch.multiple(config,LLM_PROVIDER='gemini',LLM_MODEL='model-x',LLM_API_KEY='SECRET_A'),patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value='facts'),patch('hh_web.search_page',return_value=[dict(test_safe_mvp.VAC,id=str(i)) for i in range(100,131)]),patch('hh_web.load_full',side_effect=lambda p,v:v) as full,patch('decide.hard_filters',return_value=None),patch('run_web._log',side_effect=rows.append),patch('llm.requests.post',return_value=response(429,DAILY)) as post:
            run_web.main()
        self.assertEqual(post.call_count,1); self.assertEqual(full.call_count,1)
        self.assertEqual(rows[-1]['stop_reason'],'llm_infrastructure_unavailable')
        self.assertEqual(rows[-2]['llm_failures'][0]['provider_error_category'],'quota_daily')
