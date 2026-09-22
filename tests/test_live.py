"""Controlled form fixtures only; no browser, HH session, LLM or real network."""
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest
import test_safe_mvp
import application
import config
import decide
import run_web
import webapp

Q = {'index':0,'question':'Опишите опыт проектов','type':'textarea','required':True,'options':[]}
FACT = 'Я руководил запуском проекта Alpha.'
VAC = dict(test_safe_mvp.VAC)

class LiveTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def question(self, kind='textarea', options=None, **kw):
        return dict(Q,type=kind,options=options or [],**kw)

    def test_llm_open_question_with_evidence(self):
        with patch('llm.generate',return_value=json.dumps({'answer':FACT,'evidence':FACT})) as model:
            answer=application.answer_question(Q,VAC,FACT)
            self.assertEqual(answer['answer'],FACT)
            self.assertEqual(answer['source'],'llm')
            self.assertIn(VAC['description'],model.call_args.args[0])

    def test_all_professional_types_use_llm(self):
        for kind, value, options in [('text','Через неделю',[]),('textarea','Опыт',[]),
            ('radio','yes',[{'value':'yes','label':'Да'},{'value':'no','label':'Нет'}]),
            ('select-one','remote',[{'value':'remote','label':'Удалённо'}]),
            ('number','3',[]),('checkbox','true',[])]:
            with self.subTest(kind=kind), patch('llm.generate',return_value=json.dumps({'answer':value,'evidence':'Опыт 3 года'})) as model:
                answer=application.answer_question(self.question(kind,options),VAC,'Опыт 3 года', 'Кейс', 'Инструкции')
                self.assertEqual(answer['answer'],value)
                self.assertEqual(answer['source'],'llm')
                model.assert_called_once()
                prompt=model.call_args.args[0]
                for text in (Q['question'],VAC['description'],'Опыт 3 года','Кейс','Инструкции'):
                    self.assertIn(text,prompt)
                if kind in ('radio','select-one'):
                    # Raw DOM values are employer-controlled.  The prompt receives aliases
                    # and answer_question restores the exact value only after validation.
                    for i, option in enumerate(options):
                        self.assertIn('__hh_option_'+str(i),prompt)
                        self.assertNotIn('"value": "'+option['value']+'"',prompt)
                else:
                    for option in options: self.assertIn(option['value'],prompt)

    def test_unknown_required_answer_and_llm_errors(self):
        for raw in (None, 'garbage', '{}', '{"answer":null}',json.dumps({'answer':'10','evidence':'invented'})):
            with patch('llm.generate',return_value=raw), self.assertRaises(ValueError):
                application.answer_question(Q,VAC,FACT)
        with patch('llm.generate',side_effect=RuntimeError('fake')), self.assertRaisesRegex(ValueError,'llm_error'):
            application.answer_question(Q,VAC,FACT)

    def test_unsupported_question_and_invalid_number_or_choice(self):
        for kind,value in [('file','x'),('number','someday'),('radio','unlisted')]:
            with patch('llm.generate',return_value=json.dumps({'answer':value,'evidence':FACT})), self.assertRaises(ValueError):
                application.answer_question(self.question(kind),VAC,FACT)

    def test_request_must_contain_exact_resume_and_vacancy(self):
        req=SimpleNamespace(url='https://hh.ru/applicant/vacancy_response',method='POST',post_data='vacancyId=123456789&resumeId=aaaa')
        self.assertTrue(application._request_identity(req,VAC['id'],'aaaa'))
        self.assertFalse(application._request_identity(req,VAC['id'],'bbbb'))
        req.url += '?resumeId=bbbb'
        self.assertFalse(application._request_identity(req,VAC['id'],'aaaa'))
        req.url = 'https://hh.ru/applicant/vacancy_response'
        req.post_data += '&resumeId=bbbb'
        self.assertFalse(application._request_identity(req,VAC['id'],'aaaa'))

    def fixture(self, *, chosen=True, signal=None, failure=False, questions=None, stop=False, send=True, fill_error=False):
        page=MagicMock(); page.url=VAC['url']
        form=MagicMock(); form.count.return_value=1; form.first=form
        letters=MagicMock(); letters.count.return_value=1; letters.input_value.return_value='Prepared letter'
        submit=MagicMock(); submit.count.return_value=1; submit.is_enabled.return_value=True
        none=MagicMock(); none.count.return_value=0
        button=MagicMock(); button.count.return_value=1
        fail=MagicMock(); fail.count.side_effect=lambda:int(failure and 'request' in order); fail.is_visible.return_value=True; fail.inner_text.return_value='Отклик не отправлен'
        def page_locator(selector):
            if selector==application.FORM_SELECTOR: return form
            if selector=='[data-qa="vacancy-response-link-top"]': return button
            if selector=='[data-qa="vacancy-response-error"]': return fail
            return none
        page.locator.side_effect=page_locator
        def form_locator(selector):
            if selector=='[data-qa="vacancy-response-popup-form-letter-input"]': return letters
            if selector==application.SUBMIT_SELECTOR: return submit
            return none
        form.locator.side_effect=form_locator
        form.evaluate.side_effect=lambda js,*args: questions or [] if js==application.INSPECT_FORM else True
        route=MagicMock(); route.request=SimpleNamespace(url='https://hh.ru/applicant/vacancy_response',method='POST',post_data='vacancyId=123456789&resumeId=aaaa')
        order=[]
        def click(**kwargs):
            if send:
                guard=page.context.route.call_args.args[1]
                route.continue_.side_effect=lambda:order.append('request')
                guard(route)
        submit.click.side_effect=click
        def journal(): order.append('journal')
        stack=ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(config,'DRY_RUN',False))
        stack.enter_context(patch('application._wait_response_form',return_value=(form,None)))
        stack.enter_context(patch('application._transient_error',return_value=False))
        stack.enter_context(patch('application._resume_diagnostics',return_value={'verified':chosen}))
        stack.enter_context(patch('application.form_diagnostics',return_value={}))
        stack.enter_context(patch('application._choose_resume',return_value=chosen))
        stack.enter_context(patch('application._selected_resume',return_value=chosen))
        def get_signal(*args,**kwargs):
            if kwargs.get('before'): return 'already_applied' if signal=='already_applied' else None
            return 'applied_confirmed' if signal=='applied_confirmed' and 'request' in order else None
        stack.enter_context(patch('application._signal',side_effect=get_signal))
        stack.enter_context(patch('application.time.monotonic',side_effect=[0,0,9,10]))
        fill=stack.enter_context(patch('application._fill_question',side_effect=ValueError('fake') if fill_error else None))
        model=stack.enter_context(patch('application.answer_question',return_value=dict(question=Q['question'],type='textarea',required=True,answer=FACT,source='llm',fill_result='verified')))
        return page,form,letters,submit,route,journal,order,fill,model

    def apply_fixture(self,**kw):
        page,form,letters,submit,route,journal,order,fill,model=self.fixture(**kw)
        result=application.apply(page,VAC,'Prepared letter',resume_id='aaaa',dry_run=False,resume_text=FACT,
                                 before_submit=journal,stopped=lambda:kw.get('stop',False))
        return result,letters,submit,route,order,fill

    def test_normal_form_letter_and_explicit_success(self):
        result,letter,submit,route,order,_=self.apply_fixture(signal='applied_confirmed')
        self.assertEqual(result['application_status'],'applied_confirmed')
        letter.fill.assert_called_once_with('Prepared letter',timeout=3000)
        self.assertEqual(order,['journal','request'])

    def test_resume_missing_no_send(self):
        result,_,submit,_,order,_=self.apply_fixture(chosen=False)
        self.assertEqual(result['application_status'],'needs_manual_review')
        submit.click.assert_not_called(); self.assertEqual(order,[])

    def test_text_question_fill(self):
        result,_,_,_,_,fill=self.apply_fixture(questions=[Q],signal='applied_confirmed')
        self.assertEqual(result['application_status'],'applied_confirmed')
        fill.assert_called_once(); self.assertEqual(len(result['questions']),1)

    def test_question_fill_failure_no_submit(self):
        result,_,submit,_,_,_=self.apply_fixture(questions=[Q],fill_error=True)
        self.assertEqual(result['application_status'],'needs_manual_review')
        submit.click.assert_not_called()

    def test_explicit_failure(self):
        self.assertEqual(self.apply_fixture(failure=True)[0]['application_status'],'apply_failed')

    def test_unknown_after_submit(self):
        self.assertEqual(self.apply_fixture()[0]['application_status'],'submission_unknown')

    def test_already_applied(self):
        result,_,submit,_,_,_=self.apply_fixture(signal='already_applied')
        self.assertEqual(result['application_status'],'already_applied'); submit.click.assert_not_called()

    def test_stop_before_submit(self):
        result,_,submit,_,order,_=self.apply_fixture(stop=True)
        self.assertEqual(result['application_status'],'needs_manual_review'); submit.click.assert_not_called()
        self.assertEqual(order,[])

    def test_apply_validate_records_each_predicate_and_blocks_submit(self):
        for resume_ok, form_ok, expected in (
            (True, True, None),
            (False, True, 'resume_invalid_at_validation'),
            (True, False, 'form_invalid_at_validation'),
            (False, False, 'resume_and_form_invalid_at_validation'),
        ):
            with self.subTest(resume_ok=resume_ok, form_ok=form_ok):
                page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed')
                def evaluate(js,*args):
                    if js == application.INSPECT_FORM: return []
                    if 'form.checkValidity' in js: return form_ok
                    return True
                form.evaluate.side_effect=evaluate
                with patch('application._selected_resume',return_value=resume_ok), \
                     patch('application._validation_diagnostics',return_value={'invalid_fields':[]}):
                    result=application.apply(page,VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
                if expected is None:
                    self.assertEqual(result['application_status'],'applied_confirmed')
                else:
                    self.assertEqual(result['reason_code'],'required_fields_or_resume_invalid')
                    self.assertEqual(result['validation_reason_code'],expected)
                    self.assertEqual(result['validation_predicates'],{
                        'resume_matches_at_validation':resume_ok,'form_check_validity':form_ok})
                    submit.click.assert_not_called()

    def test_safe_does_not_touch_browser(self):
        page=MagicMock()
        result=application.apply(page,VAC,'letter',resume_id='aaaa',dry_run=True)
        self.assertEqual(result['application_status'],'safe_not_sent'); self.assertEqual(page.mock_calls,[])

    def test_native_fill_types(self):
        for kind,value in [('text','hello'),('textarea','hello'),('number','3'),('select-one','yes'),('checkbox','true'),('radio','yes')]:
            form=MagicMock(); control=MagicMock()
            form.evaluate_handle.return_value.as_element.return_value=control
            control.input_value.return_value=value; control.is_checked.return_value=True
            control.get_attribute.return_value='question'
            form.locator.return_value.count.return_value=1
            radio=form.locator.return_value.nth.return_value
            radio.get_attribute.side_effect=lambda attr:'question' if attr=='name' else value
            radio.is_checked.return_value=True
            answer={'answer':value}
            application._fill_question(form,self.question(kind),answer)
            self.assertEqual(answer['fill_result'],'verified')

    def test_live_route_requires_per_run_confirmation(self):
        with patch('webapp._running',return_value=False), patch('webapp._any_run_alive',return_value=False), patch('webapp.os.path.exists',return_value=True), patch('webapp.envfile.read_env',return_value={'RUN_MODE':'LIVE','RESUME_URL':'https://hh.ru/resume/aaaa'}), patch('webapp._spawn') as spawn:
            self.assertEqual(webapp.app.test_client().post('/api/run',json={'mode':'LIVE'}).status_code,400)
            spawn.assert_not_called()

    def test_selected_resume_control_verified(self):
        form=MagicMock()
        radios=form.locator.return_value
        radios.count.return_value=1
        el=radios.nth.return_value
        el.get_attribute.side_effect=lambda attr:'resumeId' if attr=='name' else 'aaaa'
        form.evaluate.return_value={'verified':True}
        self.assertTrue(application._choose_resume(form,'aaaa'))
        el.check.assert_called_once()
        form.evaluate.return_value={'verified':False}
        self.assertFalse(application._selected_resume(form,'bbbb'))

    def test_signal_requires_exact_success_and_vacancy(self):
        page=MagicMock(); page.url=VAC['url']
        marker=page.locator.return_value.nth.return_value
        page.locator.return_value.count.return_value=1
        marker.is_visible.return_value=True
        marker.inner_text.return_value='Отклик отправлен'
        marker.locator.return_value.count.return_value=0
        self.assertEqual(application._signal(page,VAC['id']),'applied_confirmed')
        page.url='https://hh.ru/vacancy/999'
        self.assertIsNone(application._signal(page,VAC['id']))
        page.url=VAC['url']; marker.inner_text.return_value='Подождите'
        self.assertIsNone(application._signal(page,VAC['id']))

    def test_first_click_cannot_submit(self):
        page,form,letter,submit,route,journal,order,_,_=self.fixture(signal='applied_confirmed')
        button=page.locator('[data-qa="vacancy-response-link-top"]')
        def first_click(**kw):
            page.context.route.call_args.args[1](route)
        button.click.side_effect=first_click
        application.apply(page,VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertGreaterEqual(route.abort.call_count,1)
        self.assertEqual(order,['journal','request'])

    def test_target_and_dedup(self):
        from pathlib import Path
        previous={'event':'apply_submit_started','vacancy_id':'111','application_status':'submission_unknown'}
        confirmed={'vacancy_id':'222','application_status':'applied_confirmed'}
        Path(config.DECISIONS_LOG).write_text(json.dumps(previous)+'\n'+json.dumps(confirmed)+'\n')
        vacs=[dict(VAC,id='111'),dict(VAC,id='222'),dict(VAC,id='333'),dict(VAC,id='444')]
        browser=MagicMock(); browser.__enter__.return_value=(None,MagicMock())
        with patch.object(config,'DRY_RUN',False), patch('run_web.browser_context',return_value=browser), patch('run_web.has_session',return_value=True), patch('run_web.load_candidate_context',return_value=('','')), patch('run_web._get_resume',return_value=FACT), patch('hh_web.search_page',return_value=vacs), patch('hh_web.load_full',side_effect=lambda page,vac:vac), patch('decide.hard_filters',return_value=None), patch('decide.score',return_value=test_safe_mvp.RESULT), patch('decide.cover_letter',return_value='Prepared letter'), patch('hh_web.apply',return_value={'application_status':'applied_confirmed','reason':'fixture'}) as apply:
            self.assertEqual(run_web.main(),0)
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[1]['id'],'333')
        rows=[json.loads(x) for x in Path(config.DECISIONS_LOG).read_text().splitlines()]
        self.assertEqual(rows[-1]['stop_reason'],'target_reached')
        self.assertEqual(rows[-1]['counts']['applied_confirmed'],1)

    def test_old_live_setting_does_not_arm_worker(self):
        import os,sys,types
        from pathlib import Path
        source=Path('config.py').read_text()
        for authorized in (False,True):
            env={'RUN_MODE':'LIVE'}
            if authorized: env['HH_LIVE_CONFIRMED_FOR_RUN']='1'
            with patch.dict(os.environ,env,clear=True), patch.dict(sys.modules,{'dotenv':types.SimpleNamespace(load_dotenv=lambda **kw:None)}):
                values={}
                exec(compile(source,'config.py','exec'),values)
                self.assertEqual(values['DRY_RUN'],not authorized)

    def test_failed_journal_blocks_request(self):
        page,form,letter,submit,route,_,order,_,_=self.fixture()
        def fail_journal(): raise OSError('artificial disk failure')
        result=application.apply(page,VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=fail_journal)
        route.continue_.assert_not_called()
        submit.click.assert_not_called()
        self.assertEqual(result['reason_code'],'submission_journal_failed')
        self.assertEqual(result['application_status'],'needs_manual_review')
