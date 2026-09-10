"""Additional question branch over existing LIVE fixtures; no HH or LLM."""
import itertools
import json
import unittest
from unittest.mock import patch
import test_live
import test_safe_mvp
import test_warnings_resume
import application

RADIO=dict(test_live.Q,type='radio',question='Есть ли у вас опыт работы в дилерском центре?',
           options=[{'value':'yes','label':'Да'},{'value':'no','label':'Нет'},{'value':'other','label':'Свой вариант'}])
TEXT=dict(test_live.Q,index=1,question='Расскажите о релевантном проекте')

class OptionalQuestionTests(unittest.TestCase):
    setUp=test_safe_mvp.SafeTests.setUp
    fixture=test_live.LiveTests.fixture

    def test_no_questions_keeps_normal_flow(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed')
        result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['application_status'],'applied_confirmed')
        self.assertEqual(result['questions'],[])
        model.assert_not_called(); fill.assert_not_called()

    def test_radio_and_text_before_resume_letter_submit_with_optional_warning(self):
        real_wait=application._wait_response_form
        for warning_text in (None,test_warnings_resume.COUNTRY,test_warnings_resume.LANGUAGE):
            with self.subTest(warning=warning_text is not None):
                page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed',questions=[RADIO,TEXT])
                stages=[]
                fill.side_effect=lambda *args: stages.append('question')
                letter.fill.side_effect=lambda *args,**kw:stages.append('letter')
                def selected(*args): stages.append('resume'); return True
                with patch('application._choose_resume',side_effect=selected), patch('application.time.monotonic',side_effect=itertools.count()):
                    if warning_text:
                        warningpage,dialog,unused,button=test_warnings_resume.WarningResumeTests.warning_flow(self,warning_text)
                        warningpage.locator(application.FORM_SELECTOR).first=form
                        def wait(page,warnings,blocked,stopped,diagnostics=None):
                            stages.append('warning')
                            return real_wait(warningpage,warnings,blocked,stopped)
                    else:
                        def wait(*args): return form,None
                    with patch('application._wait_response_form',side_effect=wait):
                        result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
                self.assertEqual(result['application_status'],'applied_confirmed')
                self.assertEqual(stages,(['warning'] if warning_text else [])+['question','question','resume','letter'])
                submit.click.assert_called_once()

    def test_honest_no_allowed_only_for_experience_question(self):
        raw=json.dumps({'answer':'no','basis':'experience_not_confirmed','evidence':''})
        with patch('llm.generate',return_value=raw):
            result=application.answer_question(RADIO,test_live.VAC,test_live.FACT)
            self.assertEqual(result['answer'],'no')
            with self.assertRaises(ValueError):
                application.answer_question(dict(RADIO,question='Есть ли у вас разрешение на работу?'),test_live.VAC,test_live.FACT)
        with patch('llm.generate',return_value=json.dumps({'answer':'yes','basis':'experience_not_confirmed','evidence':''})), self.assertRaises(ValueError):
            application.answer_question(RADIO,test_live.VAC,test_live.FACT)

    def test_contact_question_never_sent_to_llm(self):
        with patch('llm.generate') as model, self.assertRaisesRegex(ValueError,'contact_answer_unavailable'):
            application.answer_question(dict(TEXT,question='Укажите ваш email'),test_live.VAC,test_live.FACT)
        model.assert_not_called()

    def test_unknown_exact_fact_blocks_and_wrong_resume_still_blocks(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(questions=[TEXT])
        model.side_effect=ValueError('question_answer_unknown')
        result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['application_status'],'needs_manual_review')
        submit.click.assert_not_called()
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(chosen=False,questions=[TEXT])
        result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        fill.assert_called_once()
        self.assertEqual(result['reason'],'selected_resume_not_verified')
        submit.click.assert_not_called()

    def test_optional_unknown_skips_and_contact_questions_never_use_llm(self):
        real_answer = application.answer_question
        for contact in (False,True):
            for required in (False,True):
                q=dict(TEXT,required=required,question='Укажите email' if contact else TEXT['question'])
                page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed',questions=[q])
                with patch('application.answer_question',side_effect=real_answer), patch('llm.generate',return_value=None) as llm, patch('application._clear_optional_question',return_value=True) as clear:
                    result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal,resume_text=test_live.FACT)
                if contact: llm.assert_not_called()
                else: llm.assert_called_once()
                if required:
                    self.assertEqual(result['application_status'],'needs_manual_review')
                    submit.click.assert_not_called(); clear.assert_not_called()
                else:
                    self.assertEqual(result['application_status'],'applied_confirmed')
                    self.assertEqual(result['questions'][0]['fill_result'],'optional_question_skipped')
                    clear.assert_called_once_with(form,q)
                fill.assert_not_called()

    def test_unknown_requiredness_blocks_on_missing_answer(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(questions=[dict(TEXT,required=None)])
        model.side_effect=ValueError('question_answer_unknown')
        result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['reason'],'question_not_safely_answered')
        submit.click.assert_not_called()

    def test_no_predefined_or_local_contact_source(self):
        for name in ('load_known_answers','KNOWN_ANSWERS_PATH','_contact_kind','_local_contact'):
            self.assertFalse(hasattr(application,name))
        # A professional answer requires no file reads: only the supplied run context.
        with patch('pathlib.Path.read_text',side_effect=AssertionError('unexpected file read')), patch('llm.generate',return_value=json.dumps({'answer':test_live.FACT,'evidence':test_live.FACT})) as model:
            result=application.answer_question(TEXT,test_live.VAC,test_live.FACT)
            self.assertEqual(result['source'],'llm')
            model.assert_called_once()

    def test_unclear_optional_field_cannot_keep_unverified_value(self):
        q=dict(TEXT,required=False)
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed',questions=[q])
        model.side_effect=ValueError('question_evidence_missing')
        with patch('application._clear_optional_question',return_value=False):
            result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['reason'],'optional_field_not_cleared')
        submit.click.assert_not_called()

    def test_failed_question_records_only_safe_error_code(self):
        for error,code in [('question_label_unknown','question_text_not_found'),
                           ('question_llm_invalid','question_llm_invalid_json'),
                           ('question_evidence_missing','question_evidence_rejected'),
                           ('question_choice_invalid','question_option_not_found'),
                           ('secret@example.test','question_pipeline_error')]:
            page,form,letter,submit,route,journal,order,fill,model=self.fixture(questions=[TEXT])
            model.side_effect=ValueError(error)
            result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
            self.assertEqual(result['questions'][0]['error_code'],code)
            self.assertNotIn('secret@example.test',str(result))
            submit.click.assert_not_called()
