"""Recovery diagnostics only, no external data or services."""
import unittest
import test_safe_mvp
import application as app

class RecoveryMetadataTests(unittest.TestCase):
    def test_failure_reasons_without_source_text(self):
        query='в команде разрабатывал клиентские проекты на Bitrix и Laravel'
        cases=[('короткий запрос',('Source',),'too_few_tokens'),
            (query,('Полностью постороннее предложение',),'no_candidate'),
            (query,('В команде НЕ разрабатывал клиентские проекты на Bitrix и Laravel.',),'disallowed_extra_tokens'),
            (query,('В команде разрабатывал клиентские проекты на Bitrix и также и также Laravel.',),'coverage_below_threshold'),
            (query,('В команде разрабатывал клиентские проекты на Laravel и Bitrix.',),'token_order_failed'),
            (query,('В команде разрабатывал клиентские проекты на Bitrix и Laravel.','В команде разрабатывал клиентские проекты на Bitrix и также Laravel.'),'ambiguous_best_match')]
        for evidence,sources,reason in cases:
            with self.subTest(reason=reason):
                diag={};recovered,result=app._recover_evidence(evidence,sources,diag)
                self.assertIsNone(recovered)
                self.assertEqual(diag['recovery_failure_reason'],reason)
                self.assertIn('recovery_best_coverage',diag)
                self.assertIn('recovery_order_preserved',diag)
                for source in sources:self.assertNotIn(source,str(diag))
