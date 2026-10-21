"""Synthetic connections/HTTP only. No real storage, credentials or provider requests."""
import errno
import json
from pathlib import Path
import socket
import tempfile
import traceback
import unittest
from unittest.mock import MagicMock, patch
import requests
from urllib3 import exceptions as urllib3_exceptions

import test_safe_mvp
import config
import llm
import llm_connections as store
import llm_provider
from llm_connections import LLMConnection as Connection
from llm_errors import LLMUnavailable, LLMStopped, ProviderFailure, classify_http
from llm_provider import classify_transport_failure
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


def chat_response(status=200, text='OK'):
    return response(status, body={'choices': [{'message': {'content': text}}]})


def posted_json(call):
    if 'json' in call.kwargs:
        return call.kwargs['json']
    return json.loads(call.kwargs['data'].decode('utf-8'))


def connection(index=1, provider='gemini', model='model-x', enabled=True, folder_id=''):
    return Connection(f'{index:032x}', provider, model, enabled, f'SECRET_{chr(64+index)}', folder_id)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


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

    def test_stop_before_operation_starts_no_provider_attempt(self):
        router = llm.Router([connection()], stopped=lambda: True)
        with patch('llm.requests.post') as post, self.assertRaises(LLMStopped):
            router.generate('facts')
        post.assert_not_called()

    def test_stop_after_active_provider_call_prevents_retry_and_failover(self):
        stopped = {'value': False}
        def request(*_args, **_kwargs):
            stopped['value'] = True
            return response(503)
        router = llm.Router([connection(), connection(2)], stopped=lambda: stopped['value'])
        with patch('llm.requests.post', side_effect=request) as post, self.assertRaises(LLMStopped):
            router.generate('facts')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(router.unavailable, set())

    def test_retry_wait_is_interrupted_without_retry_or_failover(self):
        stopped = {'value': False}
        def sleep(_delay):
            stopped['value'] = True
        router = llm.Router([connection(), connection(2)], sleep=sleep,
                            stopped=lambda: stopped['value'])
        with patch('llm.requests.post', return_value=response(503)) as post, self.assertRaises(LLMStopped):
            router.generate('facts')
        self.assertEqual(post.call_count, 1)

    def test_first_attempt_success_has_operation_and_attempt_timing(self):
        clock=FakeClock(); events=[]
        router=llm.Router([connection()],clock=clock,telemetry=events.append,
                          operation_id_factory=lambda:'operation-1')
        private_response='PRIVATE_MODEL_RESPONSE'
        def request(*_args,**_kwargs):
            clock.advance(0.125)
            return response(body={'candidates':[{'content':{'parts':[{'text':private_response}]}}]})
        with patch('llm.requests.post',side_effect=request):
            self.assertEqual(router.generate('PRIVATE_RESUME_TEXT',task='matching'),private_response)
        self.assertEqual([event['event'] for event in events],[
            'llm_operation_started','llm_attempt_started','llm_attempt_finished','llm_operation_finished'])
        attempt=events[2]; finished=events[3]
        self.assertEqual(attempt['duration_ms'],125)
        self.assertEqual(attempt['outcome'],'success')
        self.assertEqual(attempt['model'],'model-x')
        self.assertEqual(finished['duration_ms'],125)
        self.assertEqual(finished['total_provider_attempt_count'],1)
        self.assertEqual(finished['final_connection_id'],connection().id)
        public=json.dumps(events)
        for private in ('PRIVATE_RESUME_TEXT',private_response,'SECRET_A','?key='):
            self.assertNotIn(private,public)

    def test_503_retry_timing_separates_provider_and_wait(self):
        clock=FakeClock(); events=[]; replies=[response(503),response()]
        def request(*_args,**_kwargs):
            clock.advance(0.1 if len(replies)==2 else 0.2)
            return replies.pop(0)
        def sleep(delay):
            self.assertEqual(delay,2.0)
            clock.advance(2.25)
        router=llm.Router([connection()],clock=clock,sleep=sleep,telemetry=events.append,
                          operation_id_factory=lambda:'operation-2')
        with patch('llm.requests.post',side_effect=request):
            self.assertEqual(router.generate('facts'),'OK')
        attempts=[event for event in events if event['event']=='llm_attempt_finished']
        self.assertEqual([(event['attempt'],event['outcome'],event['duration_ms']) for event in attempts],
                         [(1,'failure',100),(2,'success',200)])
        self.assertTrue(attempts[0]['retry_allowed'])
        started=next(event for event in events if event['event']=='llm_retry_wait_started')
        finished=next(event for event in events if event['event']=='llm_retry_wait_finished')
        self.assertEqual(started['requested_delay_ms'],2000)
        self.assertEqual(finished['requested_delay_ms'],2000)
        self.assertEqual(finished['actual_duration_ms'],2250)
        operation=next(event for event in events if event['event']=='llm_operation_finished')
        self.assertEqual(operation['duration_ms'],2550)
        self.assertEqual(operation['total_provider_attempt_count'],2)

    def test_timeout_retry_timing_distinguishes_transport_phase(self):
        cases=((requests.ConnectTimeout(),'connect','connect_timeout'),
               (requests.ReadTimeout(),'read','read_timeout'),
               (requests.Timeout(),'generic','unknown_timeout'))
        for error,phase,detail in cases:
            with self.subTest(phase=phase):
                clock=FakeClock(); events=[]; calls=0
                def request(*_args,**_kwargs):
                    nonlocal calls
                    calls+=1; clock.advance(0.3)
                    if calls==1: raise error
                    return response()
                router=llm.Router([connection()],clock=clock,sleep=lambda delay:clock.advance(delay),
                                  telemetry=events.append)
                with patch('llm.requests.post',side_effect=request):
                    self.assertEqual(router.generate('facts'),'OK')
                attempts=[event for event in events if event['event']=='llm_attempt_finished']
                self.assertEqual(len(attempts),2)
                self.assertEqual(attempts[0]['provider_error_category'],'provider_transport_timeout')
                self.assertEqual(attempts[0]['transport_phase'],phase)
                self.assertEqual(attempts[0]['transport_failure_detail'],detail)
                self.assertTrue(attempts[0]['retry_allowed'])
                self.assertEqual(attempts[1]['outcome'],'success')

    def test_safe_transport_failure_subtypes_from_types_and_errno(self):
        dns = urllib3_exceptions.NameResolutionError(
            'SECRET_HOST', None, socket.gaierror(socket.EAI_NONAME, 'SECRET_DNS'))
        cases = (
            (requests.ConnectTimeout('SECRET_CONNECT'), 'connect_timeout'),
            (requests.ReadTimeout('SECRET_READ'), 'read_timeout'),
            (requests.ConnectionError('SECRET_GENERIC'), 'unknown_connection'),
            (requests.ConnectionError(dns), 'dns'),
            (requests.ConnectionError(ConnectionRefusedError(errno.ECONNREFUSED,
                                                              'SECRET_REFUSED')), 'tcp_refused'),
            (requests.ConnectionError(ConnectionResetError(errno.ECONNRESET,
                                                            'SECRET_RESET')), 'tcp_reset'),
            (requests.ConnectionError(OSError(errno.ENETUNREACH,
                                               'SECRET_NETWORK')), 'network_unreachable'),
            (requests.exceptions.SSLError('SECRET_TLS'), 'tls'),
            (requests.exceptions.ProxyError('SECRET_PROXY'), 'proxy'),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_transport_failure(error), expected)

    def test_transport_telemetry_excludes_arbitrary_exception_message(self):
        private = 'SECRET_TEST_VALUE https://private-provider.example/?key=PRIVATE_KEY'
        events=[]; sleep=MagicMock()
        router=llm.Router([connection()],sleep=sleep,telemetry=events.append)
        with patch('llm.requests.post',side_effect=requests.ConnectionError(private)) as post, \
             self.assertRaises(LLMUnavailable) as caught:
            router.generate('facts',task='matching')
        self.assertEqual(post.call_count,4)
        self.assertEqual(sleep.call_count,3)
        attempts=[event for event in events if event['event']=='llm_attempt_finished']
        self.assertEqual([event['transport_failure_detail'] for event in attempts],
                         ['unknown_connection']*4)
        self.assertEqual([event['router_round'] for event in attempts],[1,1,2,2])
        public=json.dumps(events)+json.dumps(caught.exception.failures)
        self.assertNotIn(private,public)
        self.assertNotIn('private-provider.example',public)
        self.assertNotIn('PRIVATE_KEY',public)

    def test_timing_records_exhaustion_failover_and_success(self):
        clock=FakeClock(); events=[]; replies=[response(503),response(503),response()]
        def request(*_args,**_kwargs):
            clock.advance(0.05)
            return replies.pop(0)
        router=llm.Router([connection(),connection(2)],clock=clock,
                          sleep=lambda delay:clock.advance(delay),telemetry=events.append)
        with patch('llm.requests.post',side_effect=request) as post:
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],
                         ['SECRET_A','SECRET_A','SECRET_B'])
        failover=next(event for event in events if event['event']=='llm_failover')
        self.assertEqual(failover['previous_connection_id'],connection().id)
        self.assertEqual(failover['next_connection_id'],connection(2).id)
        self.assertEqual(failover['reason_category'],'provider_server_error')
        operation=next(event for event in events if event['event']=='llm_operation_finished')
        self.assertEqual(operation['outcome'],'success')
        self.assertEqual(operation['total_provider_attempt_count'],3)
        self.assertEqual(operation['final_connection_id'],connection(2).id)

    def test_timing_all_connections_exhausted_finishes_unavailable(self):
        clock=FakeClock(); events=[]
        router=llm.Router([connection(),connection(2)],clock=clock,
                          sleep=lambda delay:clock.advance(delay),telemetry=events.append)
        with patch('llm.requests.post',side_effect=lambda *_args,**_kwargs:(clock.advance(0.05),response(503))[1]):
            with self.assertRaises(LLMUnavailable):
                router.generate('facts')
        exhausted=next(event for event in events if event['event']=='llm_connections_exhausted')
        self.assertEqual(exhausted['last_connection_id'],connection(2).id)
        self.assertEqual(exhausted['reason_category'],'provider_server_error')
        operation=next(event for event in events if event['event']=='llm_operation_finished')
        self.assertEqual(operation['outcome'],'unavailable')
        self.assertEqual(operation['total_provider_attempt_count'],8)
        self.assertEqual(operation['final_connection_id'],connection(2).id)
        self.assertEqual(len([event for event in events if event['event']=='llm_failover']),2)
        self.assertEqual(operation['router_rounds_started'],2)
        round_two=next(event for event in events if event['event']=='llm_router_round_started')
        self.assertEqual(round_two['router_round'],2)
        self.assertEqual(exhausted['rounds_completed'],2)

    def test_telemetry_failure_does_not_change_provider_success(self):
        def broken_telemetry(_event):
            raise OSError('diagnostic write failed')
        router=llm.Router([connection()],telemetry=broken_telemetry)
        with patch('llm.requests.post',return_value=response()) as post:
            self.assertEqual(router.generate('facts'),'OK')
        post.assert_called_once()

    def test_blocked_private_prompt_finishes_without_persisting_content(self):
        from privacy import PrivacyError
        events=[]; router=llm.Router([connection()],telemetry=events.append,
                                    operation_id_factory=lambda:'operation-private')
        private='API_KEY=artificial-private-value'
        with patch('llm.requests.post') as post, self.assertRaises(PrivacyError):
            router.generate(private,task='question')
        post.assert_not_called()
        self.assertEqual([event['event'] for event in events],
                         ['llm_operation_started','llm_operation_finished'])
        self.assertEqual(events[-1]['outcome'],'deterministic_failure')
        self.assertEqual(events[-1]['total_provider_attempt_count'],0)
        self.assertNotIn(private,json.dumps(events))

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

    def test_503_retries_same_connection_then_recovers(self):
        sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
        with patch('llm.requests.post',side_effect=[response(503),response(),response()]) as post:
            self.assertEqual(router.generate('facts'),'OK')
            self.assertEqual(post.call_count,2)
            self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],['SECRET_A']*2)
            self.assertNotIn(connection().id,router.unavailable)
            self.assertEqual(router.generate('facts'),'OK')
            self.assertEqual(post.call_args.kwargs['params']['key'],'SECRET_A')
        sleep.assert_called_once_with(2.0)
        self.assertEqual(router.failures[0]['provider_error_category'],'provider_server_error')
        self.assertTrue(router.failures[0]['retry_scheduled'])
        self.assertEqual(router.failures[0]['retry_delay'],2.0)
        event=router.consume_routing_events()[0]
        self.assertEqual(event['recovery'],'same_connection_retry')
        self.assertEqual(event['final_connection_id'],connection().id)

    def test_503_retry_exhaustion_fails_over_to_second_connection(self):
        sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
        with patch('llm.requests.post',side_effect=[response(503),response(503),response()]) as post:
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],['SECRET_A','SECRET_A','SECRET_B'])
        self.assertEqual(router.active.id,connection(2).id)
        self.assertEqual([item['attempt'] for item in router.failures],[1,2])
        self.assertEqual([item['retry_scheduled'] for item in router.failures],[True,False])
        sleep.assert_called_once_with(2.0)
        event=router.consume_routing_events()[0]
        self.assertEqual(event['recovery'],'failover_connection')
        self.assertEqual(event['final_connection_id'],connection(2).id)

    def test_second_connection_retries_after_first_503_exhaustion(self):
        sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
        with patch('llm.requests.post',side_effect=[response(503),response(503),response(503),response()]) as post:
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],['SECRET_A','SECRET_A','SECRET_B','SECRET_B'])
        self.assertEqual(router.active.id,connection(2).id)
        self.assertEqual([item['attempt'] for item in router.failures],[1,2,1])
        sleep.assert_has_calls([unittest.mock.call(2.0),unittest.mock.call(2.0)])

    def test_all_503_connections_exhaust_after_one_retry_each_per_round(self):
        sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
        with patch('llm.requests.post',side_effect=[response(503)]*8) as post:
            with self.assertRaises(LLMUnavailable) as caught:
                router.generate('facts')
        self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],
                         ['SECRET_A','SECRET_A','SECRET_B','SECRET_B']*2)
        self.assertEqual([(item['connection_id'],item['attempt']) for item in caught.exception.failures],[
            (connection().id,1),(connection().id,2),(connection(2).id,1),(connection(2).id,2),
            (connection().id,1),(connection().id,2),(connection(2).id,1),(connection(2).id,2)])
        self.assertEqual([item['router_round'] for item in caught.exception.failures],
                         [1]*4+[2]*4)
        self.assertEqual([item['retry_scheduled'] for item in caught.exception.failures],
                         [True,False,True,False]*2)
        self.assertEqual(sleep.call_count,6)
        text=repr(caught.exception)+json.dumps(caught.exception.failures)
        for private in ('SECRET_A','SECRET_B','private prompt','?key='):
            self.assertNotIn(private,text)

    def test_four_recoverable_connections_reach_round_two_first_success(self):
        connections=[connection(index) for index in range(1,5)]
        failure=ProviderFailure('provider_transport_error',
                                transport_failure_detail='unknown_transport')
        events=[]
        router=llm.Router(connections,telemetry=events.append)
        with patch('llm.execute',side_effect=[(None,failure)]*4+[('RECOVERED',None)]) as execute:
            self.assertEqual(router.generate('facts',task='matching'),'RECOVERED')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [item.id for item in connections]+[connections[0].id])
        attempts=[event for event in events if event['event']=='llm_attempt_finished']
        self.assertEqual([event['router_round'] for event in attempts],[1,1,1,1,2])
        self.assertEqual(router.consume_routing_events()[0]['recovery'],
                         'second_router_round')

    def test_second_connection_recovers_and_later_round_two_connections_are_skipped(self):
        connections=[connection(index) for index in range(1,5)]
        failure=ProviderFailure('provider_transport_error')
        router=llm.Router(connections)
        with patch('llm.execute',side_effect=[(None,failure)]*5+[('OK',None)]) as execute:
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [item.id for item in connections]
                         +[connections[0].id,connections[1].id])

    def test_daily_quota_is_run_permanent_and_never_reaches_round_two(self):
        item=connection(); events=[]
        router=llm.Router([item],telemetry=events.append)
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        with patch('llm.execute',return_value=(None,daily)) as execute:
            with self.assertRaises(LLMUnavailable): router.generate('facts')
            with self.assertRaises(LLMUnavailable): router.generate('later facts')
        self.assertEqual(execute.call_count,1)
        self.assertEqual(router.unavailable,{item.id})
        self.assertFalse(any(event['event']=='llm_router_round_started' for event in events))
        failure_event=next(event for event in events
                           if event['event']=='llm_attempt_finished')
        self.assertEqual(failure_event['failure_scope'],'run_permanent')
        self.assertTrue(failure_event['connection_unavailable_after_attempt'])

    def test_mixed_permanent_and_recoverable_pool_only_blacklists_permanent(self):
        a,b,c,d=[connection(index) for index in range(1,5)]
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        recoverable=ProviderFailure('provider_transport_error')
        router=llm.Router([a,b,c,d])
        with patch('llm.execute',side_effect=[(None,daily),(None,recoverable),
                                              (None,recoverable),('OK',None),
                                              ('NEXT',None)]) as execute:
            self.assertEqual(router.generate('facts'),'OK')
            self.assertEqual(router.generate('later facts'),'NEXT')
        self.assertEqual(router.unavailable,{a.id})
        self.assertEqual({item.id for item in router._candidates()},{b.id,c.id,d.id})
        self.assertEqual(execute.call_args_list[-1].args[0].id,d.id)

    def test_http_413_is_operation_scoped_and_eligible_in_future_operation(self):
        groq=connection(1,'groq','openai/gpt-oss-120b')
        backup=connection(2,'zai','glm-4.7-flash')
        too_large=ProviderFailure('request_too_large',413)
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        router=llm.Router([groq,backup])
        with patch('llm.execute',side_effect=[(None,too_large),('FIRST',None),
                                              (None,daily),('SECOND',None)]) as execute:
            self.assertEqual(router.generate('large facts'),'FIRST')
            self.assertEqual(router.generate('small facts'),'SECOND')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [groq.id,backup.id,backup.id,groq.id])
        self.assertNotIn(groq.id,router.unavailable)
        first_failure=router.failures[0]
        self.assertEqual(first_failure['http_status'],413)
        self.assertEqual(first_failure['failure_scope'],'operation_scoped')
        self.assertFalse(first_failure['retry_scheduled'])

    def test_identical_groq_413_is_not_repeated_but_other_connections_remain_independent(self):
        groq_a=connection(1,'groq','openai/gpt-oss-120b')
        groq_b=connection(2,'groq','openai/gpt-oss-120b')
        backup=connection(3,'zai','glm-4.7-flash')
        too_large=ProviderFailure('request_too_large',413)
        events=[]
        with patch('llm.execute',side_effect=[(None,too_large),(None,too_large),
                                              ('OK',None)]) as execute:
            self.assertEqual(llm.Router([groq_a,groq_b,backup],
                                        telemetry=events.append).generate('same payload'),'OK')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [groq_a.id,groq_b.id,backup.id])
        self.assertNotIn('llm_router_round_started',
                         [event['event'] for event in events])

        events=[]
        with patch('llm.execute',return_value=(None,too_large)) as execute, \
             self.assertRaises(LLMUnavailable):
            llm.Router([groq_a],telemetry=events.append).generate('same payload')
        self.assertEqual(execute.call_count,1)
        skipped=next(event for event in events
                     if event['event']=='llm_router_round_connection_skipped')
        self.assertEqual(skipped['reason_category'],
                         'identical_payload_request_too_large')

    def test_zai_transient_429_can_recover_again_in_second_router_round(self):
        zai=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        events=[]; sleep=MagicMock(); router=llm.Router([zai],sleep=sleep,
                                                        telemetry=events.append)
        with patch('llm.execute',side_effect=[(None,rate),('FIRST',None),
                                              (None,rate),(None,rate),('SECOND',None)]) as execute:
            self.assertEqual(router.generate('facts'),'FIRST')
            self.assertEqual(router.generate('later facts'),'SECOND')
        self.assertEqual(execute.call_count,5)
        self.assertNotIn(zai.id,router.unavailable)
        second_operation_rounds=[event['router_round'] for event in events
                                 if event['event']=='llm_attempt_finished'][2:]
        self.assertEqual(second_operation_rounds,[1,1,2])
        self.assertEqual(sleep.call_count,3)

    def test_transient_429_uses_one_bounded_recovery_cycle_then_succeeds(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        clock=FakeClock(); events=[]
        router=llm.Router([item],clock=clock,sleep=clock.advance,
                          telemetry=events.append)
        with patch.object(llm,'RATE_RETRIES',0), \
             patch('llm.execute',side_effect=[(None,rate),(None,rate),('OK',None)]) as execute:
            self.assertEqual(router.generate('facts',task='matching'),'OK')
        self.assertEqual(execute.call_count,3)
        self.assertEqual(router.recovery_wait_used,2.0)
        self.assertEqual(len([event for event in events
                              if event['event']=='llm_rate_limit_recovery_started']),1)
        wait=next(event for event in events
                  if event['event']=='llm_rate_limit_recovery_wait_started')
        self.assertEqual(wait['requested_delay_ms'],2000)
        self.assertEqual(router.consume_routing_events()[0]['recovery'],
                         'bounded_rate_limit_recovery')

    def test_transient_429_recovery_exhaustion_has_explicit_terminal_reason(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        clock=FakeClock(); events=[]
        router=llm.Router([item],clock=clock,sleep=clock.advance,
                          telemetry=events.append)
        with patch.object(llm,'RATE_RETRIES',0), \
             patch('llm.execute',return_value=(None,rate)) as execute, \
             self.assertRaises(LLMUnavailable) as caught:
            router.generate('PRIVATE_RESUME_TEXT')
        self.assertEqual(execute.call_count,3)
        exhausted=next(event for event in events
                       if event['event']=='llm_connections_exhausted')
        self.assertEqual(exhausted['reason_category'],
                         'transient_rate_limit_recovery_exhausted')
        self.assertEqual(exhausted['recovery_attempt_count'],1)
        self.assertEqual(exhausted['operation_recovery_wait_used_ms'],2000)
        public=json.dumps(events)+json.dumps(caught.exception.failures)
        for private in ('PRIVATE_RESUME_TEXT','SECRET_A'):
            self.assertNotIn(private,public)

    def test_recovery_terminal_reason_preserves_non_rate_limit_failure(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        cases=(ProviderFailure('auth/provider_access',401),
               ProviderFailure('request_too_large',413))
        for terminal in cases:
            with self.subTest(category=terminal.category):
                clock=FakeClock(); events=[]
                router=llm.Router([item],clock=clock,sleep=clock.advance,
                                  telemetry=events.append)
                with patch.object(llm,'RATE_RETRIES',0), \
                     patch('llm.execute',side_effect=[(None,rate),(None,rate),
                                                      (None,terminal)]), \
                     self.assertRaises(LLMUnavailable):
                    router.generate('facts')
                exhausted=next(event for event in events
                               if event['event']=='llm_connections_exhausted')
                self.assertEqual(exhausted['reason_category'],terminal.category)

    def test_bounded_recovery_honors_retry_after(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429,retry_delay=7.5)
        clock=FakeClock(); events=[]
        router=llm.Router([item],clock=clock,sleep=clock.advance,
                          telemetry=events.append)
        with patch.object(llm,'RATE_RETRIES',0), \
             patch('llm.execute',side_effect=[(None,rate),(None,rate),('OK',None)]):
            self.assertEqual(router.generate('facts'),'OK')
        waits=[event['requested_delay_ms'] for event in events
               if event['event']=='llm_rate_limit_recovery_wait_started']
        self.assertEqual(waits,[7500])

    def test_run_recovery_wait_budget_caps_multiple_operations(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        clock=FakeClock(); events=[]
        router=llm.Router([item],clock=clock,sleep=clock.advance,
                          telemetry=events.append)
        with patch.object(llm,'RATE_RETRIES',0), \
             patch.object(llm,'RECOVERY_RUN_WAIT_LIMIT',4.0), \
             patch('llm.execute',return_value=(None,rate)) as execute:
            for _ in range(3):
                with self.assertRaises(LLMUnavailable):
                    router.generate('facts')
        self.assertEqual(execute.call_count,8)
        self.assertEqual(router.recovery_wait_used,4.0)
        exhausted=[event for event in events
                   if event['event']=='llm_connections_exhausted']
        self.assertEqual(exhausted[-1]['reason_category'],
                         'recovery_wait_budget_exhausted')
        self.assertEqual(exhausted[-1]['recovery_attempt_count'],0)

    def test_stop_interrupts_bounded_recovery_wait(self):
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        stopped={'value':False}; in_recovery={'value':False}; events=[]
        def telemetry(event):
            events.append(event)
            if event['event']=='llm_rate_limit_recovery_wait_started':
                in_recovery['value']=True
        def sleep(_delay):
            if in_recovery['value']:
                stopped['value']=True
        router=llm.Router([item],sleep=sleep,telemetry=telemetry,
                          stopped=lambda:stopped['value'])
        with patch.object(llm,'RATE_RETRIES',0), \
             patch('llm.execute',return_value=(None,rate)) as execute, \
             self.assertRaises(LLMStopped):
            router.generate('facts')
        self.assertEqual(execute.call_count,2)
        self.assertEqual(events[-1]['event'],'llm_operation_finished')
        self.assertEqual(events[-1]['outcome'],'user_stop')

    def test_two_recoverable_rounds_exhaust_without_third_round(self):
        a,b=connection(),connection(2)
        failure=ProviderFailure('provider_transport_error')
        events=[]; router=llm.Router([a,b],telemetry=events.append)
        with patch('llm.execute',return_value=(None,failure)) as execute, \
             self.assertRaises(LLMUnavailable):
            router.generate('facts')
        self.assertEqual(execute.call_count,4)
        attempts=[event for event in events if event['event']=='llm_attempt_finished']
        self.assertEqual([(event['router_round'],event['connection_id']) for event in attempts],
                         [(1,a.id),(1,b.id),(2,a.id),(2,b.id)])
        exhausted=next(event for event in events
                       if event['event']=='llm_connections_exhausted')
        self.assertEqual(exhausted['rounds_completed'],2)
        self.assertEqual(router.unavailable,set())

    def test_permanent_pool_exhaustion_skips_empty_second_round(self):
        a,b=connection(),connection(2)
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        auth=ProviderFailure('auth/provider_access',401)
        events=[]; router=llm.Router([a,b],telemetry=events.append)
        with patch('llm.execute',side_effect=[(None,daily),(None,auth)]) as execute, \
             self.assertRaises(LLMUnavailable):
            router.generate('facts')
        self.assertEqual(execute.call_count,2)
        self.assertEqual(router.unavailable,{a.id,b.id})
        self.assertFalse(any(event['event']=='llm_router_round_started' for event in events))
        exhausted=next(event for event in events
                       if event['event']=='llm_connections_exhausted')
        self.assertEqual(exhausted['rounds_completed'],1)

    def test_recoverable_exhaustion_is_eligible_in_future_operation(self):
        item=connection(); failure=ProviderFailure('provider_transport_error')
        router=llm.Router([item])
        with patch('llm.execute',side_effect=[(None,failure),(None,failure),
                                              ('RECOVERED',None)]) as execute:
            with self.assertRaises(LLMUnavailable): router.generate('facts')
            self.assertEqual(router.generate('later facts'),'RECOVERED')
        self.assertEqual(execute.call_count,3)
        self.assertEqual(router.unavailable,set())

    def test_transport_retry_failover_and_future_operation_recovery(self):
        a,b=connection(),connection(2)
        timeout=ProviderFailure('provider_transport_timeout',transport_phase='read',
                                transport_failure_detail='read_timeout')
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        sleep=MagicMock(); router=llm.Router([a,b],sleep=sleep)
        with patch('llm.execute',side_effect=[(None,timeout),(None,timeout),('FIRST',None),
                                              (None,daily),('SECOND',None)]) as execute:
            self.assertEqual(router.generate('facts'),'FIRST')
            self.assertEqual(router.generate('later facts'),'SECOND')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [a.id,a.id,b.id,b.id,a.id])
        self.assertNotIn(a.id,router.unavailable)
        sleep.assert_called_once_with(2.0)

    def test_stop_before_round_two_prevents_new_attempt(self):
        stopped={'value':False}; events=[]
        def telemetry(event):
            events.append(event)
            if (event['event']=='llm_attempt_finished'
                    and event['router_round']==1):
                stopped['value']=True
        router=llm.Router([connection()],telemetry=telemetry,
                          stopped=lambda:stopped['value'])
        failure=ProviderFailure('provider_transport_error')
        with patch('llm.execute',return_value=(None,failure)) as execute, \
             self.assertRaises(LLMStopped):
            router.generate('facts')
        self.assertEqual(execute.call_count,1)
        self.assertFalse(any(event['event']=='llm_router_round_started' for event in events))
        self.assertEqual(events[-1]['event'],'llm_operation_finished')
        self.assertEqual(events[-1]['outcome'],'user_stop')

    def test_stop_interrupts_provider_retry_wait_in_round_two(self):
        stopped={'value':False}; events=[]; round_two_wait={'value':False}
        def telemetry(event):
            events.append(event)
            if event['event']=='llm_router_round_wait_started':
                round_two_wait['value']=True
        def sleep(_delay):
            if round_two_wait['value']: stopped['value']=True
        transient=ProviderFailure('provider_server_error',503)
        router=llm.Router([connection()],sleep=sleep,telemetry=telemetry,
                          stopped=lambda:stopped['value'])
        with patch('llm.execute',side_effect=[(None,transient),(None,transient)]) as execute, \
             self.assertRaises(LLMStopped):
            router.generate('facts')
        self.assertEqual(execute.call_count,2)
        self.assertTrue(any(event['event']=='llm_router_round_started' for event in events))
        self.assertEqual(len([event for event in events
                              if event['event']=='llm_retry_wait_started']),1)
        self.assertEqual(len([event for event in events
                              if event['event']=='llm_router_round_wait_started']),1)
        self.assertEqual(events[-1]['outcome'],'user_stop')

    def test_retry_delay_over_bound_skips_round_two_without_run_blacklist(self):
        item=connection(); events=[]; sleep=MagicMock()
        delayed=ProviderFailure('rate_limit_unknown',429,retry_delay=31)
        router=llm.Router([item],sleep=sleep,telemetry=events.append)
        with patch('llm.execute',side_effect=[(None,delayed),('RECOVERED',None)]) as execute:
            with self.assertRaises(LLMUnavailable): router.generate('facts')
            self.assertEqual(router.generate('later facts'),'RECOVERED')
        self.assertEqual(execute.call_count,2)
        sleep.assert_not_called()
        self.assertEqual(router.unavailable,set())
        skipped=next(event for event in events
                     if event['event']=='llm_router_round_connection_skipped')
        self.assertEqual(skipped['router_round'],2)
        self.assertEqual(skipped['reason_category'],'retry_delay_exceeds_bound')

    def test_synthetic_mixed_lifecycle_recovers_on_b_in_round_two(self):
        a,b,c,d=[connection(index) for index in range(1,5)]
        daily=ProviderFailure('quota_daily',429,quota_types=('daily_requests',))
        transient=ProviderFailure('provider_server_error',503)
        too_large=ProviderFailure('request_too_large',413)
        transport=ProviderFailure('provider_transport_timeout',transport_phase='read',
                                  transport_failure_detail='read_timeout')
        events=[]; router=llm.Router([a,b,c,d],sleep=MagicMock(),
                                     telemetry=events.append)
        replies=[(None,daily),(None,transient),(None,transient),(None,too_large),
                 (None,transport),(None,transport),('RECOVERED',None)]
        with patch('llm.execute',side_effect=replies) as execute:
            self.assertEqual(router.generate('PRIVATE_PROMPT',task='matching'),'RECOVERED')
        self.assertEqual([call.args[0].id for call in execute.call_args_list],
                         [a.id,b.id,b.id,c.id,d.id,d.id,b.id])
        self.assertEqual(router.unavailable,{a.id})
        failed=[event for event in events if event['event']=='llm_attempt_finished'
                and event['outcome']=='failure']
        self.assertEqual([event['failure_scope'] for event in failed],
                         ['run_permanent']+['operation_scoped']*5)
        self.assertEqual([event['router_round'] for event in failed],[1]*6)
        self.assertEqual(next(event for event in events
                              if event['event']=='llm_router_round_started')['router_round'],2)
        public=json.dumps(events)+json.dumps(router.consume_routing_events())
        for private in ('PRIVATE_PROMPT','SECRET_A','SECRET_B','SECRET_C','SECRET_D'):
            self.assertNotIn(private,public)

    def test_transient_transport_retry_and_generic_transport_failover(self):
        for first,second,expected_category,expected_calls in [
                (requests.ReadTimeout(),response(),'provider_transport_timeout',['SECRET_A','SECRET_A']),
                (requests.ConnectionError(),requests.ReadTimeout(),'provider_transport_connection',['SECRET_A','SECRET_A','SECRET_B']),
                (requests.exceptions.InvalidURL('bad'),response(),'provider_transport_error',['SECRET_A','SECRET_B'])]:
            with self.subTest(category=expected_category):
                sleep=MagicMock(); router=llm.Router([connection(),connection(2)],sleep=sleep)
                replies=[first,second] if len(expected_calls)==2 else [first,second,response()]
                with patch('llm.requests.post',side_effect=replies) as post:
                    self.assertEqual(router.generate('facts'),'OK')
                self.assertEqual([call.kwargs['params']['key'] for call in post.call_args_list],expected_calls)
                self.assertEqual(router.failures[0]['provider_error_category'],expected_category)
                if expected_category == 'provider_transport_timeout':
                    self.assertEqual(router.failures[0]['transport_phase'],'read')
                elif expected_category == 'provider_transport_connection':
                    self.assertEqual(router.failures[0]['transport_phase'],'connection')
                if expected_category == 'provider_transport_error':
                    sleep.assert_not_called()
                else:
                    sleep.assert_called_once_with(2.0)

    def test_deterministic_provider_failures_do_not_retry(self):
        for status,category in [(401,'auth/provider_access'),(403,'auth/provider_access'),(400,'provider_http_error')]:
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

    def test_groq_and_zai_openai_compatible_contracts(self):
        cases = {
            'groq': ('openai/gpt-oss-120b', 'https://api.groq.com/openai/v1/chat/completions',
                     {'reasoning_effort': 'low', 'include_reasoning': False},
                     'max_completion_tokens'),
            'zai': ('glm-4.7-flash', 'https://api.z.ai/api/paas/v4/chat/completions',
                    {'thinking': {'type': 'disabled'}}, 'max_tokens'),
        }
        for index, (provider, (model, url, extra, limit_field)) in enumerate(cases.items(), 1):
            with self.subTest(provider=provider), patch('llm.requests.post', return_value=chat_response(text=' FINAL ')) as post:
                item = connection(index, provider, model)
                self.assertEqual(llm.Router([item]).generate('neutral facts', 123, 'question'), 'FINAL')
                request = post.call_args
                payload = posted_json(request)
                self.assertEqual(request.args[0], url)
                self.assertEqual(request.kwargs['headers']['Authorization'], f'Bearer SECRET_{chr(64+index)}')
                self.assertEqual(payload['model'], model)
                self.assertEqual(payload[limit_field], 123)
                self.assertNotIn('max_tokens' if limit_field == 'max_completion_tokens'
                                 else 'max_completion_tokens', payload)
                self.assertEqual(payload['messages'], [{'role': 'user', 'content': 'neutral facts'}])
                for key, value in extra.items():
                    self.assertEqual(payload[key], value)
                self.assertEqual(request.kwargs['timeout'], 60)

    def test_groq_transport_uses_semantically_equal_compact_utf8_json(self):
        item = connection(provider='groq', model='openai/gpt-oss-120b')
        prompt = 'Кириллица, emoji 😀, quote " and slash \\.' * 200
        payload = llm_provider._openai_payload(item, prompt, 1800)
        legacy_body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
        compact_body = llm_provider._groq_request_body(item, prompt, 1800)
        prepared = requests.Request(
            'POST', llm_provider.OPENAI_COMPATIBLE['groq']['url'],
            headers={'Content-Type': 'application/json'}, data=compact_body).prepare()

        self.assertEqual(json.loads(compact_body.decode('utf-8')), payload)
        self.assertLess(len(compact_body), len(legacy_body))
        self.assertNotIn(b'\\u041a', compact_body)
        self.assertEqual(prepared.body, compact_body)
        self.assertEqual(prepared.headers['Content-Type'], 'application/json')
        self.assertEqual(int(prepared.headers['Content-Length']), len(compact_body))

        with patch('llm_provider.requests.post', return_value=chat_response()) as post:
            text, failure = llm_provider.execute(item, prompt, 1800)
        self.assertEqual(text, 'OK')
        self.assertIsNone(failure)
        self.assertEqual(post.call_args.kwargs['data'], compact_body)
        self.assertNotIn('json', post.call_args.kwargs)

    def test_groq_reasoning_only_length_response_fails_closed(self):
        real_shape = {'choices': [{'finish_reason': 'length', 'message': {
            'role': 'assistant', 'content': ''}}], 'usage': {
            'completion_tokens': 16,
            'completion_tokens_details': {'reasoning_tokens': 9}}}
        with patch('llm_provider.requests.post', return_value=response(body=real_shape)):
            text, failure = llm_provider.execute(
                connection(provider='groq', model='openai/gpt-oss-120b'), 'neutral', 16)
        self.assertIsNone(text)
        self.assertEqual(failure.category, 'provider_invalid_response')

    def test_groq_never_returns_reasoning_as_final_output(self):
        body = {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': ' CONNECTED ',
            'reasoning': 'PRIVATE_CHAIN_OF_THOUGHT'}}]}
        with patch('llm_provider.requests.post', return_value=response(body=body)):
            text, failure = llm_provider.execute(
                connection(provider='groq', model='openai/gpt-oss-120b'), 'neutral', 128)
        self.assertEqual(text, 'CONNECTED')
        self.assertIsNone(failure)
        self.assertNotIn('PRIVATE_CHAIN_OF_THOUGHT', text)

    def test_groq_and_zai_429_are_safely_normalized(self):
        for provider, model in (('groq', 'openai/gpt-oss-120b'), ('zai', 'glm-4.7-flash')):
            with self.subTest(provider=provider), patch('llm.requests.post', return_value=response(429)):
                text, failure = llm_provider.execute(connection(provider=provider, model=model), 'neutral', 8)
                self.assertIsNone(text)
                self.assertEqual(failure.category, 'rate_limit_unknown')
                self.assertEqual(failure.http_status, 429)

    def test_groq_413_is_classified_with_size_only_diagnostics(self):
        body = {'error': {'type': 'invalid_request_error', 'code': 'request_too_large',
                          'message': 'PRIVATE_RESUME_TEXT SECRET_A'}}
        prompt = 'PRIVATE_RESUME_TEXT ' + ('факты ' * 40)
        with patch('llm_provider.requests.post', return_value=response(413, body=body)):
            text, failure = llm_provider.execute(
                connection(provider='groq', model='openai/gpt-oss-120b'), prompt, 1800)
        self.assertIsNone(text)
        self.assertEqual(failure.category, 'request_too_large')
        self.assertEqual(failure.http_status, 413)
        self.assertEqual(failure.provider_error_type, 'invalid_request_error')
        self.assertEqual(failure.provider_error_code, 'request_too_large')
        self.assertEqual(failure.prompt_char_count, len(prompt))
        self.assertGreater(failure.estimated_input_tokens, 0)
        self.assertGreater(failure.request_body_bytes, len(prompt))
        self.assertEqual(failure.requested_output_tokens, 1800)
        public = json.dumps(failure.public())
        for private in ('PRIVATE_RESUME_TEXT', 'SECRET_A', 'факты'):
            self.assertNotIn(private, public)

        events = []
        with patch('llm_provider.requests.post', return_value=response(413, body=body)):
            with self.assertRaises(LLMUnavailable):
                llm.Router([connection(provider='groq', model='openai/gpt-oss-120b')],
                           telemetry=events.append).generate(prompt, 1800, 'matching')
        attempt = next(event for event in events
                       if event['event'] == 'llm_attempt_finished')
        self.assertEqual(attempt['provider_error_category'], 'request_too_large')
        self.assertEqual(attempt['request_body_bytes'], failure.request_body_bytes)
        self.assertEqual(attempt['requested_output_tokens'], 1800)
        self.assertNotIn('PRIVATE_RESUME_TEXT', json.dumps(events))

    def test_groq_uses_each_real_task_budget_without_affecting_other_providers(self):
        for task, limit in (('matching', 1800), ('cover_letter', 900), ('question', 650)):
            with self.subTest(task=task), patch(
                    'llm_provider.requests.post', return_value=chat_response()) as post:
                router = llm.Router([
                    connection(provider='groq', model='openai/gpt-oss-120b')])
                self.assertEqual(router.generate('synthetic facts', limit, task), 'OK')
                payload = posted_json(post.call_args)
                self.assertEqual(payload['max_completion_tokens'], limit)
                self.assertNotIn('max_tokens', payload)

        zai = connection(2, 'zai', 'glm-4.7-flash')
        with patch('llm_provider.requests.post', return_value=chat_response()) as post:
            self.assertEqual(llm.Router([zai]).generate('facts', 650, 'question'), 'OK')
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 650)

    def test_daily_gemini_and_unavailable_zai_fail_over_to_working_groq(self):
        gemini_a, gemini_b = connection(), connection(2)
        zai = connection(3, 'zai', 'glm-4.7-flash')
        groq = connection(4, 'groq', 'openai/gpt-oss-120b')
        for pool, replies in (
                ([gemini_a, gemini_b, groq],
                 [response(429, DAILY), response(429, DAILY), chat_response()]),
                ([zai, groq], [response(503), response(503), chat_response()])):
            with self.subTest(first=pool[0].provider), patch(
                    'llm_provider.requests.post', side_effect=replies):
                router = llm.Router(pool, sleep=MagicMock())
                self.assertEqual(router.generate('facts', 650, 'question'), 'OK')
                self.assertEqual(router.active.provider, 'groq')

    def test_gemini_groq_zai_failover_and_saved_order(self):
        gemini = connection(1)
        groq = connection(2, 'groq', 'openai/gpt-oss-120b')
        zai = connection(3, 'zai', 'glm-4.7-flash')
        sleep = MagicMock()
        router = llm.Router([gemini, groq, zai], sleep=sleep)
        replies = [response(503), response(503), response(503), response(503), chat_response()]
        with patch('llm.requests.post', side_effect=replies) as post:
            self.assertEqual(router.generate('facts'), 'OK')
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            'https://generativelanguage.googleapis.com/v1beta/models/model-x:generateContent',
            'https://generativelanguage.googleapis.com/v1beta/models/model-x:generateContent',
            'https://api.groq.com/openai/v1/chat/completions',
            'https://api.groq.com/openai/v1/chat/completions',
            'https://api.z.ai/api/paas/v4/chat/completions'])
        self.assertEqual(router.active.id, zai.id)
        self.assertEqual(sleep.call_count, 2)

        # Saved order remains authoritative when there is no active connection.
        ordered = llm.Router([zai, groq])
        with patch('llm.requests.post', return_value=chat_response()) as post:
            self.assertEqual(ordered.generate('facts'), 'OK')
        self.assertEqual(post.call_args.args[0], 'https://api.z.ai/api/paas/v4/chat/completions')

    def test_new_provider_503_exhaustion_and_disabled_skip(self):
        groq = connection(1, 'groq', 'openai/gpt-oss-120b')
        zai = connection(2, 'zai', 'glm-4.7-flash')
        router = llm.Router([groq, zai], sleep=MagicMock())
        with patch('llm.requests.post', side_effect=[response(503)] * 8) as post:
            with self.assertRaises(LLMUnavailable) as caught:
                router.generate('facts')
        self.assertEqual(post.call_count, 8)
        self.assertEqual([(row['connection_id'], row['attempt']) for row in caught.exception.failures],
                         [(groq.id, 1), (groq.id, 2), (zai.id, 1), (zai.id, 2)]*2)

        router = llm.Router([connection(1, 'groq', enabled=False), zai])
        with patch('llm.requests.post', return_value=chat_response()) as post:
            self.assertEqual(router.generate('facts'), 'OK')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], 'https://api.z.ai/api/paas/v4/chat/completions')

        # Z.ai receives its bounded retry, then fails over like every provider.
        openai = connection(3, 'openai', 'gpt-4o-mini')
        router = llm.Router([zai, openai], sleep=MagicMock())
        with patch('llm.requests.post', side_effect=[response(503), response(503), chat_response()]) as post:
            self.assertEqual(router.generate('facts'), 'OK')
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            'https://api.z.ai/api/paas/v4/chat/completions',
            'https://api.z.ai/api/paas/v4/chat/completions',
            'https://api.openai.com/v1/chat/completions'])

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

    def test_groq_and_zai_storage_defaults_and_safe_connection_test(self):
        import webapp
        client = webapp.app.test_client()
        env = {'LLM_PROVIDER': 'gemini', 'LLM_MODEL': 'model-x', 'LLM_API_KEY': 'SECRET_A'}
        with patch('webapp.envfile.read_env', return_value=env):
            groq_result = client.post('/api/llm-connections', json={
                'provider': 'groq', 'model': '', 'credential': 'SECRET_B'})
            zai_result = client.post('/api/llm-connections', json={
                'provider': 'zai', 'model': '', 'credential': 'SECRET_C'})
            self.assertEqual((groq_result.status_code, zai_result.status_code), (200, 200))
            rows = zai_result.json['connections']
            self.assertEqual([(row['provider'], row['model']) for row in rows[1:]], [
                ('groq', 'openai/gpt-oss-120b'), ('zai', 'glm-4.7-flash')])
            public = groq_result.get_data(as_text=True) + zai_result.get_data(as_text=True)
            for secret in ('SECRET_A', 'SECRET_B', 'SECRET_C'):
                self.assertNotIn(secret, public)

            target = rows[1]['id']
            with patch('llm_provider.execute', return_value=('CONNECTED', None)) as execute:
                tested = client.post(f'/api/llm-connections/{target}/test', json={})
            self.assertEqual(tested.json, {'ok': True, 'status': 'CONNECTED',
                                           'readiness': 'smoke_test_only'})
            item, prompt, limit = execute.call_args.args
            self.assertEqual((item.provider, item.model, limit),
                             ('groq', 'openai/gpt-oss-120b', 128))
            self.assertEqual(prompt, 'Reply with exactly the single word CONNECTED.')
            for private in ('SECRET_A', 'SECRET_B', 'SECRET_C', 'resume', 'vacancy', 'candidate', 'hh'):
                self.assertNotIn(private, prompt.lower())
                self.assertNotIn(private, tested.get_data(as_text=True))

            failure = ProviderFailure('provider_server_error', 503)
            with patch('llm_provider.execute', return_value=(None, failure)):
                tested = client.post(f'/api/llm-connections/{target}/test', json={})
            self.assertEqual(tested.json, {'ok': False, 'status': 'HTTP 503',
                                           'category': 'provider_server_error', 'http_status': 503})

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
    def test_rate_limit_recovery_continues_once_without_repeating_application(self):
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        item=connection(1,'zai','glm-4.7-flash')
        rate=ProviderFailure('rate_limit_unknown',429)
        clock=FakeClock(); router=llm.Router([item],clock=clock,sleep=clock.advance)
        rows=[]

        def score(*_args, **_kwargs):
            self.assertEqual(router.generate('facts',task='matching'),'OK')
            return test_safe_mvp.RESULT

        outcome={'application_status':'applied_confirmed'}
        with patch.object(config,'DRY_RUN',False), \
             patch.object(llm,'_router',router),patch('llm.reset_runtime'), \
             patch.object(llm,'RATE_RETRIES',0), \
             patch('llm.execute',side_effect=[(None,rate),(None,rate),('OK',None)]), \
             patch('run_web.browser_context',return_value=browser), \
             patch('run_web.has_session',return_value=True), \
             patch('run_web.load_candidate_context',return_value=('','')), \
             patch('run_web._get_resume',return_value='facts'), \
             patch('hh_web.search_page',return_value=[dict(test_safe_mvp.VAC)]), \
             patch('hh_web.load_full',side_effect=lambda p,v,**_kwargs:v), \
             patch('decide.hard_filters',return_value=None), \
             patch('decide.score',side_effect=score), \
             patch('decide.cover_letter',return_value='Prepared letter'), \
             patch('hh_web.apply',return_value=outcome) as apply, \
             patch('run_web._log',side_effect=rows.append):
            self.assertEqual(run_web.main(),0)
        apply.assert_called_once()
        self.assertEqual(rows[-1]['counts']['applied_confirmed'],1)

    def test_recovered_route_is_persisted_without_private_data(self):
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        router=llm.Router([connection(),connection(2)],sleep=MagicMock())
        rows=[]

        def score(*_args, **_kwargs):
            self.assertEqual(llm.generate('facts',task='matching'),'OK')
            return test_safe_mvp.RESULT

        with patch.object(llm,'_router',router),patch('llm.reset_runtime'),patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value='facts'),patch('hh_web.search_page',return_value=[dict(test_safe_mvp.VAC)]),patch('hh_web.load_full',side_effect=lambda p,v,**_kwargs:v),patch('decide.hard_filters',return_value=None),patch('decide.score',side_effect=score),patch('decide.cover_letter',return_value='Prepared letter'),patch('run_web._log',side_effect=rows.append),patch('llm.requests.post',side_effect=[response(503),response()]):
            self.assertEqual(run_web.main(),0)
        entry=next(row for row in rows if row.get('vacancy_id'))
        event=entry['llm_routing'][0]
        self.assertEqual(event['event'],'llm_routing_recovered')
        self.assertEqual(event['recovery'],'same_connection_retry')
        self.assertEqual(event['failures'][0]['retry_delay'],2.0)
        timing=[row for row in rows if row.get('event','').startswith('llm_')]
        self.assertEqual([row['event'] for row in timing],[
            'llm_operation_started','llm_attempt_started','llm_attempt_finished',
            'llm_retry_wait_started','llm_retry_wait_finished','llm_attempt_started',
            'llm_attempt_finished','llm_operation_finished'])
        self.assertTrue(all(row['run_id']==entry['run_id'] for row in timing))
        self.assertTrue(all(row['vacancy_context_id']==test_safe_mvp.VAC['id'] for row in timing))
        self.assertTrue(all('vacancy_id' not in row for row in timing))
        public=json.dumps(rows)
        for private in ('SECRET_A','SECRET_B','private prompt','?key='):
            self.assertNotIn(private,public)

    def test_terminal_matching_and_letter_stop_before_second_vacancy(self):
        for stage in ('matching','cover_letter'):
            with self.subTest(stage=stage):
                browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
                vacancies=[dict(test_safe_mvp.VAC,id=str(i)) for i in (123,456,789)]
                rows=[]
                with patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value='facts'),patch('hh_web.search_page',return_value=vacancies),patch('hh_web.load_full',side_effect=lambda p,v,**_kwargs:v) as full,patch('decide.hard_filters',return_value=None),patch('decide.score',side_effect=LLMUnavailable() if stage=='matching' else None,return_value=test_safe_mvp.RESULT),patch('decide.cover_letter',side_effect=LLMUnavailable()),patch('run_web._log',side_effect=rows.append),patch('hh_web.apply') as apply:
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
                with patch('application.answer_question',side_effect=real_answer),patch('llm.generate',side_effect=LLMUnavailable()),patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value=test_live.FACT),patch('hh_web.search_page',return_value=[dict(test_live.VAC),dict(test_live.VAC,id='999')]),patch('hh_web.load_full',side_effect=lambda p,v,**_kwargs:v) as full,patch('decide.hard_filters',return_value=None),patch('decide.score',return_value=test_safe_mvp.RESULT),patch('decide.cover_letter',return_value='Prepared letter'),patch('run_web._log',side_effect=rows.append):
                    run_web.main()
                submit.click.assert_not_called(); fill.assert_not_called()
                self.assertEqual(full.call_count,1)
                self.assertEqual(rows[-1]['stop_reason'],'llm_infrastructure_unavailable')
                self.assertEqual(rows[-2]['stage'],'application')

    def test_actual_router_daily_terminal_stops_vacancy_loop(self):
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        rows=[]
        with patch.multiple(config,LLM_PROVIDER='gemini',LLM_MODEL='model-x',LLM_API_KEY='SECRET_A'),patch('run_web.browser_context',return_value=browser),patch('run_web.has_session',return_value=True),patch('run_web.load_candidate_context',return_value=('','')),patch('run_web._get_resume',return_value='facts'),patch('hh_web.search_page',return_value=[dict(test_safe_mvp.VAC,id=str(i)) for i in range(100,131)]),patch('hh_web.load_full',side_effect=lambda p,v,**_kwargs:v) as full,patch('decide.hard_filters',return_value=None),patch('run_web._log',side_effect=rows.append),patch('llm.requests.post',return_value=response(429,DAILY)) as post:
            run_web.main()
        self.assertEqual(post.call_count,1); self.assertEqual(full.call_count,1)
        self.assertEqual(rows[-1]['stop_reason'],'llm_infrastructure_unavailable')
        self.assertEqual(rows[-2]['llm_failures'][0]['provider_error_category'],'quota_daily')
