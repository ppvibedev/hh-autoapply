"""Question metadata only, with synthetic LLM responses and no browser/network."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_safe_mvp
import test_live
import application
import config
import run_web


class QuestionDiagnosticsTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp
    fixture = test_live.LiveTests.fixture

    def saved_diagnostics(self, raw, kind='textarea'):
        real_answer = application.answer_question
        field = dict(test_live.Q, type=kind, required=None,
                     options=[{'value':'yes','label':'Да'},{'value':'no','label':'Нет'}] if kind=='radio' else [])
        page,form,letter,submit,route,journal,order,fill,model = self.fixture(questions=[field])
        model.side_effect = real_answer
        with patch('llm.generate',return_value=raw):
            result=application.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,
                                     resume_text='Synthetic source',before_submit=journal)
        submit.click.assert_not_called()
        run_web._log(result)
        row=json.loads(Path(config.DECISIONS_LOG).read_text().splitlines()[-1])
        self.assertNotIn('Synthetic source',json.dumps(row))
        return row['questions'][0]['diagnostics'],row

    def test_evidence_reasons_saved_without_answer_or_sources(self):
        for evidence,reason in ((None,'evidence_missing'),(['private evidence'],'evidence_not_string'),
                                ('ab','evidence_too_short'),('private evidence','evidence_literal_match_failed')):
            with self.subTest(reason=reason):
                diag,row=self.saved_diagnostics(json.dumps({'answer':'Private proposed text','evidence':evidence}))
                self.assertEqual(diag['evidence_validation_reason'],reason)
                self.assertEqual(diag['answer_type'],'string')
                self.assertTrue(diag['answer_present'])
                self.assertEqual(diag['answer_length'],len('Private proposed text'))
                self.assertIn('evidence_type',diag)
                self.assertNotIn('Private proposed text',json.dumps(row))
                self.assertNotIn('private evidence',json.dumps(row))

    def test_parsed_shapes_and_radio_options_saved(self):
        cases=[('[]','array','top_level_not_object'),('"yes"','string','top_level_not_object'),
               ('null','null','top_level_not_object'),('{"response":"yes"}','object','answer_key_missing'),
               ('{"answer":["yes"]}','object','answer_not_string'),('not json',None,'invalid_json')]
        for raw,kind,reason in cases:
            with self.subTest(raw=raw):
                diag,row=self.saved_diagnostics(raw,'radio')
                self.assertEqual(diag['parser_reason'],reason)
                if kind:self.assertEqual(diag['parsed_top_level_type'],kind)
                self.assertEqual(diag['options'],[{'value':'yes','label':'Да'},{'value':'no','label':'Нет'}])
                if 'response' in raw:
                    self.assertEqual(diag['parsed_keys'],['response'])
                    self.assertFalse(diag['answer_key_present'])
                if '["yes"]' in raw:
                    self.assertTrue(diag['answer_key_present'])
                    self.assertEqual(diag['answer_type'],'array')
                    self.assertEqual(diag['answer_length'],1)

    def test_question_prompt_requires_exact_evidence_and_accepts_paraphrased_answer(self):
        source='Работал с Bitrix и Laravel.'
        answer='Есть опыт работы с Laravel и Bitrix.'
        field=dict(test_live.Q,type='textarea',question='С какими CMS/фреймворками вы работали?',options=[])
        with patch('llm.generate',return_value=json.dumps({'answer':answer,'evidence':source})) as model:
            result=application.answer_question(field,test_live.VAC,source)
        self.assertEqual(result['answer'],answer)
        prompt=model.call_args.args[0]
        for rule in ('ДОСЛОВНЫЙ непрерывный фрагмент ОДНОГО источника','plain string',
                     'запрещены paraphrase/summary','"Resume:" / "Evidence:"','верни evidence=""'):
            self.assertIn(rule,prompt)
        with patch('llm.generate',return_value=json.dumps({'answer':answer,'evidence':answer})):
            with self.assertRaisesRegex(ValueError,'question_evidence_missing'):
                application.answer_question(field,test_live.VAC,source)
