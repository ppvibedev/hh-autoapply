"""Bounded LLM scheduling on synthetic clocks/providers only; no network."""
import json
import threading
import time
import unittest
from unittest.mock import patch
import llm
from llm_errors import LLMUnavailable, LLMStopped, ProviderFailure, classify_http
from test_llm_connections import FakeClock, connection, response


class DeadlineTests(unittest.TestCase):
    def router(self, pool):
        clock=FakeClock();events=[]
        return llm.Router(pool,clock=clock,sleep=clock.advance,telemetry=events.append),clock,events

    def test_timeout_checks_alternative_before_wait(self):
        router,clock,events=self.router([connection(),connection(2)])
        with patch('llm.execute',side_effect=[(None,ProviderFailure('provider_transport_timeout')),('OK',None)]) as call:
            self.assertEqual(router.generate('PRIVATE_PROMPT'),'OK')
        self.assertEqual([c.args[0].id for c in call.call_args_list],[connection().id,connection(2).id])
        self.assertEqual(clock(),0)
        self.assertNotIn('PRIVATE_PROMPT',json.dumps(events))

    def test_recovery_can_succeed_after_more_than_one_extra_attempt(self):
        router,clock,events=self.router([connection(1,'zai','glm-4.7-flash')])
        rate=ProviderFailure('rate_limit_unknown',429)
        with patch.object(llm,'RATE_RETRIES',0), patch('llm.execute',side_effect=[(None,rate)]*3+[('OK',None)]) as call:
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual(call.call_count,4)
        self.assertEqual(len([e for e in events if e['event']=='llm_rate_limit_recovery_started']),2)
        self.assertLessEqual(clock(),llm.OPERATION_DEADLINE)

    def test_slow_timeouts_stop_at_operation_deadline(self):
        router,clock,events=self.router([connection(i) for i in range(1,5)])
        def request(*args,**kwargs):
            clock.advance(25)
            return None,ProviderFailure('provider_transport_timeout')
        with patch('llm.execute',side_effect=request) as call, self.assertRaises(LLMUnavailable):
            router.generate('facts')
        self.assertEqual(call.call_count,5)
        self.assertEqual(events[-1]['exhaustion_reason'],'operation_deadline_exhausted')
        self.assertLessEqual(call.call_count,llm.MAX_OPERATION_ATTEMPTS)
        # Fake provider deliberately violates its supplied timeout; late result is rejected.
        self.assertEqual(events[-1]['outcome'],'unavailable')

    def test_actual_hung_call_is_bounded_and_never_overlapped(self):
        release=threading.Event();completed=threading.Event();events=[]
        router=llm.Router([connection()],telemetry=events.append)
        def request(*args,**kwargs):
            release.wait(2)
            completed.set()
            return 'LATE_PRIVATE_RESPONSE',None
        try:
            with patch.object(llm,'OPERATION_DEADLINE',0.08),patch('llm.execute',side_effect=request) as call:
                start=time.monotonic()
                with self.assertRaises(LLMUnavailable):router.generate('PRIVATE_PROMPT')
                self.assertLess(time.monotonic()-start,0.5)
                self.assertEqual(events[-1]['exhaustion_reason'],'operation_deadline_exhausted')
                with self.assertRaises(LLMUnavailable):router.generate('later')
                self.assertEqual(call.call_count,1)
                self.assertEqual(events[-1]['exhaustion_reason'],'previous_provider_request_in_flight')
        finally:
            release.set();completed.wait(1)
        self.assertNotIn('PRIVATE',json.dumps(events))

    def test_attempt_cap_and_metrics(self):
        router,clock,events=self.router([connection()])
        def request(*args,**kwargs):
            clock.advance(0.1)
            return None,ProviderFailure('provider_server_error',503,retry_delay=0)
        with patch.object(llm,'MAX_OPERATION_ATTEMPTS',6),patch('llm.execute',side_effect=request) as call, self.assertRaises(LLMUnavailable):
            router.generate('facts')
        self.assertEqual(call.call_count,6)
        summary=events[-1]
        self.assertEqual(summary['exhaustion_reason'],'operation_attempt_limit_exhausted')
        self.assertEqual(summary['provider_attempt_time_ms'],600)
        self.assertGreater(summary['wait_time_ms'],0)
        self.assertIn('started_at',summary);self.assertIn('finished_at',summary)
        failures=[e for e in events if e['event']=='llm_attempt_finished']
        self.assertTrue(all(e['retry_delay_source']=='provider' for e in failures))

    def test_413_once_with_later_recovery(self):
        a,b=connection(1,'groq','openai/gpt-oss-120b'),connection(2)
        router,clock,events=self.router([a,b]);calls=[]
        def request(c,*args,**kwargs):
            calls.append(c.id)
            return (None,ProviderFailure('request_too_large',413)) if c.id==a.id else (None,ProviderFailure('provider_server_error',503))
        with patch('llm.execute',side_effect=request),self.assertRaises(LLMUnavailable):router.generate('facts')
        self.assertEqual(calls.count(a.id),1)
        self.assertEqual(router.unavailable,set())
        self.assertTrue(any(e['event']=='llm_connection_excluded' and e['reason_category']=='request_too_large' for e in events))

    def test_503_retry_after_honored_and_delay_source_recorded(self):
        reply=response(503);reply.headers['Retry-After']='7.5'
        failure=classify_http('gemini',reply)
        self.assertEqual(failure.retry_delay,7.5)
        router,clock,events=self.router([connection()])
        with patch('llm.execute',side_effect=[(None,failure),('OK',None)]):
            self.assertEqual(router.generate('facts'),'OK')
        self.assertEqual(clock(),7.5)
        self.assertEqual(next(e for e in events if e['event']=='llm_attempt_finished')['retry_delay_source'],'provider')

    def test_stop_interrupts_pending_provider_without_new_requests(self):
        release=threading.Event();entered=threading.Event();done=threading.Event();stop={'value':False}
        router=llm.Router([connection()],stopped=lambda:stop['value'])
        def request(*args,**kwargs):
            entered.set();release.wait(2);done.set();return 'LATE',None
        def stop_soon():
            entered.wait(1);stop['value']=True
        stopper=threading.Thread(target=stop_soon);stopper.start()
        try:
            with patch('llm.execute',side_effect=request) as call,self.assertRaises(LLMStopped):router.generate('facts')
            self.assertEqual(call.call_count,1)
        finally:
            release.set();done.wait(1);stopper.join(1)
