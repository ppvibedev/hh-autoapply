"""Only synthetic professional evidence; no HH or external LLM."""
import json
import unittest
from unittest.mock import patch
import test_safe_mvp
import test_live
import application as app

class EvidenceRecoveryTests(unittest.TestCase):
    setUp=test_safe_mvp.SafeTests.setUp

    def answer(self,source,evidence,diagnostics):
        q=dict(test_live.Q,question='С какими CMS и фреймворками вы работали?',type='textarea',options=[])
        with patch('llm.generate',return_value=json.dumps({'answer':'Работал с Bitrix и Laravel.','evidence':evidence})):
            return app.answer_question(q,test_live.VAC,source,diagnostics=diagnostics)

    def test_exact_evidence_skips_recovery(self):
        source='Разрабатывал клиентские проекты на Bitrix и Laravel.';diag={}
        with patch('application._recover_evidence',side_effect=AssertionError('unneeded')):
            self.answer(source,source,diag)
        self.assertFalse(diag['evidence_recovery_attempted'])
        self.assertEqual(diag['evidence_recovery_result'],'exact_not_needed')

    def test_unique_source_recovered_and_literal_gate_passes(self):
        source='В команде разрабатывал клиентские проекты на Bitrix и также Laravel.'
        query='в команде разрабатывал клиентские проекты на Bitrix и Laravel'
        recovered,reason=app._recover_evidence(query,(source,''))
        self.assertEqual(recovered,source)
        self.assertIn(recovered,source)
        diag={};self.answer(source,query,diag)
        self.assertEqual(diag['evidence_validation_reason'],'accepted')
        self.assertEqual(diag['evidence_recovery_result'],'exact_source_recovered')
        self.assertNotIn(source,json.dumps(diag,ensure_ascii=False))

    def test_weak_ambiguous_and_invalid_recovery_rejected(self):
        query='в команде разрабатывал клиентские проекты на Bitrix и Laravel'
        for source,reason in (
            ('Разрабатывал проекты на неизвестной платформе.','below_threshold'),
            ('В команде разрабатывал клиентские проекты на Bitrix и Laravel.\nВ команде разрабатывал клиентские проекты на Bitrix и также Laravel.','ambiguous_match'),
            ('В команде НЕ разрабатывал клиентские проекты на Bitrix и Laravel.','below_threshold')):
            with self.subTest(reason=reason):
                diag={}
                with self.assertRaisesRegex(ValueError,'question_evidence_missing'):self.answer(source,query,diag)
                self.assertEqual(diag['evidence_recovery_result'],reason)
        with patch('application._recover_evidence',return_value=('Invented quote','exact_source_recovered')):
            with self.assertRaisesRegex(ValueError,'question_evidence_missing'):self.answer('Trusted source',query,{})
