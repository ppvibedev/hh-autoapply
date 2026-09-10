"""Focused local source resolution; no network, real profiles or browser."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_safe_mvp
import test_live
import application as app
import question_sources as sources
import config


class SourceTests(unittest.TestCase):
    setUp=test_safe_mvp.SafeTests.setUp

    def q(self,text,kind='textarea',options=None):
        return dict(test_live.Q,question=text,type=kind,required=None,options=options or [])

    def test_current_run_salary_and_format(self):
        with patch('llm.generate') as model:
            for salary in (200000,300000):
                with patch.object(config,'SALARY_MIN',salary):
                    a=app.answer_question(self.q('Какие зарплатные ожидания?'),test_live.VAC,'Synthetic')
                    self.assertIn(f'{salary:,}'.replace(',',' '),a['answer'])
            for codes,expected in ((['REMOTE'],'Удалённый формат'),(['ON_SITE'],'Офис')):
                with patch.object(config,'WORK_FORMAT_CODES',codes):
                    a=app.answer_question(self.q('Какой формат работы предпочитаете?'),test_live.VAC,'Synthetic')
                    self.assertEqual(a['answer'],expected)
            model.assert_not_called()

    def test_preference_unknown_personal_and_ambiguous_options(self):
        with patch('question_sources._read',return_value={'start':'Готов приступить в течение недели.'}),patch('llm.generate') as model:
            a=app.answer_question(self.q('Когда готовы приступить?'),test_live.VAC,'Synthetic')
            self.assertIn('в течение недели',a['answer']);self.assertIsNone(a['required'])
            for text in ('Есть ли военный билет?','Есть ли карта Альфа-Банка?'):
                with self.assertRaisesRegex(ValueError,'personal_fact_unavailable'):
                    app.answer_question(self.q(text),test_live.VAC,'Synthetic')
            model.assert_not_called()
        with patch.object(config,'WORK_FORMAT_CODES',['REMOTE']):
            with self.assertRaisesRegex(ValueError,'question_choice_invalid'):
                sources.resolve(self.q('Какой формат работы предпочитаете?','radio',[{'value':'a','label':'Удалёнка'},{'value':'b','label':'Удалённо'}]))

    def test_professional_partial_keeps_exact_evidence(self):
        source='Вёл HR-проекты.'
        partial='Вёл HR-проекты. Опыт интеграции 1С:ЗУП в материалах не подтверждён.'
        q=self.q('Есть ли опыт HR-проектов и интеграций 1С:ЗУП?')
        with patch('llm.generate',return_value=json.dumps({'answer':partial,'evidence':source})) as model:
            a=app.answer_question(q,test_live.VAC,source)
            self.assertEqual(a['answer'],partial)
            self.assertIn('частичный ответ',model.call_args.args[0])
        with patch('llm.generate',return_value=json.dumps({'answer':'Есть опыт интеграций.','evidence':'Интегрировал 1С.'})):
            with self.assertRaisesRegex(ValueError,'question_evidence_missing'):app.answer_question(q,test_live.VAC,source)

    def test_local_contact_never_in_history_or_llm_and_numeric_option_preserved(self):
        real=app.answer_question
        owner=test_live.LiveTests();owner.setUp();self.addCleanup(owner.doCleanups)
        q=self.q('Укажите Telegram','text')
        page,form,letter,submit,route,journal,order,fill,model=owner.fixture(questions=[q],signal='applied_confirmed')
        model.side_effect=real
        contact='@synthetic_handle'
        with patch('question_sources._read',return_value={'resumes':{'aaaa':{'contacts':{'telegram':contact}}}}),patch('llm.generate') as cloud:
            outcome=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
            cloud.assert_not_called()
        self.assertEqual(outcome['application_status'],'applied_confirmed')
        self.assertNotIn(contact,json.dumps(outcome))
        self.assertEqual(fill.call_args.args[2]['answer'],contact)
        with patch.object(config,'WORK_FORMAT_CODES',['REMOTE']):
            value=sources.resolve(self.q('Какой формат работы предпочитаете?','radio',[{'value':'1234567890123456','label':'Удалёнка'}]))
            self.assertEqual(value['answer'],'1234567890123456')

    def test_professional_radio_alias_restores_exact_dom_value(self):
        source='Работал с CMS.'
        q=self.q('Есть ли опыт работы с CMS?','radio',[{'value':'1234567890123456','label':'Да'}])
        with patch('llm.generate',return_value=json.dumps({'answer':'__hh_option_0','evidence':source})) as model:
            answer=app.answer_question(q,test_live.VAC,source)
        self.assertEqual(answer['answer'],'1234567890123456')
        self.assertNotIn('1234567890123456',model.call_args.args[0])
        self.assertIn('__hh_option_0',model.call_args.args[0])
