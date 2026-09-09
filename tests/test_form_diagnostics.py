"""Read-only form discovery diagnostics, no actual browser or session."""
import unittest
from unittest.mock import MagicMock, patch
import test_safe_mvp
import application
import config

class FormDiagnosticsTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def test_missing_or_duplicate_entry_stops_before_click(self):
        for count in (0,2):
            page=MagicMock(); page.url=test_safe_mvp.VAC['url']
            button=page.locator.return_value; button.count.return_value=count
            with patch.object(config,'DRY_RUN',False), patch('application._signal',return_value=None), patch.object(application,'_form_diagnostic_recorded',False), patch('application.form_diagnostics',return_value={'form_count':0}) as capture:
                result=application.apply(page,test_safe_mvp.VAC,'letter',resume_id='aaaa',dry_run=False,before_submit=lambda:None)
                self.assertEqual(result['stage'],'apply_entry_lookup')
                self.assertEqual(result['response_entry_count'],count)
                self.assertEqual(result['application_status'],'needs_manual_review')
                button.click.assert_not_called()
                capture.assert_called_once()
                self.assertEqual(result['form_diagnostics'],{'form_count':0})

    def test_snapshot_does_not_capture_values_or_queries(self):
        page=MagicMock(); page.url='https://hh.ru/vacancy/123?token=private-value'
        page.evaluate.return_value={'elements':[{'tag':'input','qa':'resume-choice','name':'token_secret','type':'radio','visible':True,'button_text':''}], 'controls':{'input':1},'resume_block_present':True,'question_block_present':False,'form_count':1,'truncated':False}
        result=application.form_diagnostics(page)
        self.assertEqual(result['page_path'],'/vacancy/123')
        self.assertEqual(result['elements'][0]['name'],'[omitted]')
        self.assertNotIn('private-value',str(result))
        script=page.evaluate.call_args.args[0]
        self.assertNotIn('.value',script)
        self.assertNotIn('innerHTML',script)
        self.assertNotIn('outerHTML',script)
        self.assertNotIn('getAttribute(\'value\')',script)
        page.goto.assert_not_called()

    def test_missing_form_records_snapshot(self):
        page=MagicMock()
        with patch.object(config,'DRY_RUN',False), patch('application._signal',return_value=None), patch('application._response_entry',return_value=MagicMock()), patch('application._wait_response_form',return_value=(None,'response_form_not_found')), patch('application.form_diagnostics',return_value={'form_count':0}) as capture:
            result=application.apply(page,test_safe_mvp.VAC,'letter',resume_id='aaaa',dry_run=False,before_submit=lambda:None)
        self.assertEqual(result['reason'],'response_form_not_found')
        self.assertEqual(result['form_diagnostics'],{'form_count':0})
        capture.assert_called_once()
