"""No HTTP: request objects and existing LIVE fixture only."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import test_safe_mvp
import test_live
import application as app

class RequestVariants(unittest.TestCase):
    setUp=test_safe_mvp.SafeTests.setUp
    fixture=test_live.LiveTests.fixture

    def req(self,path='/applicant/vacancy_response',body='vacancyId=123456789&resumeId=aaaa',content='application/x-www-form-urlencoded'):
        return SimpleNamespace(url='https://hh.ru'+path,method='POST',post_data=body,headers={'content-type':content})

    def test_alternative_endpoints_and_multipart(self):
        for path in ('/negotiations','/vacancy/123456789','/applicant/vacancy_response/popup/'):
            self.assertTrue(app._request_identity(self.req(path), '123456789','aaaa'))
        body='--boundary\r\nContent-Disposition: form-data; name="resumeId"\r\n\r\naaaa\r\n--boundary\r\nContent-Disposition: form-data; name="vacancyId"\r\n\r\n123456789\r\n--boundary--\r\n'
        self.assertTrue(app._request_identity(self.req(body=body,content='multipart/form-data; boundary=boundary'),'123456789','aaaa'))

    def test_letter_and_wrong_resume_blocked_without_payload_logging(self):
        request=self.req(body='vacancyId=123456789&resumeId=bbbb&letter=PRIVATE')
        info=app._request_details(request,'123456789','aaaa','Expected')
        self.assertFalse(info['verified']);self.assertEqual(info['reason_code'],'submit_payload_resume_mismatch')
        self.assertNotIn('PRIVATE',str(info))
        request.post_data='vacancyId=123456789&resumeId=aaaa&letter=WRONG'
        self.assertEqual(app._request_details(request,'123456789','aaaa','Expected')['reason_code'],'submit_payload_letter_mismatch')

    def test_payload_cannot_replace_dom_resume_verification(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture()
        def pending(root,rid,diag):
            diag.update(before={'selected_ids':[]},opened={'selected_ids':[]},reason_code='resume_target_not_found')
            return False
        with patch('application._choose_resume',side_effect=pending):
            result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['reason_code'],'resume_target_not_found')
        submit.click.assert_not_called();route.continue_.assert_not_called()

    def test_dom_verified_unknown_payload_and_endpoint_allowed(self):
        for path,body in (('/applicant/vacancy_response/popup','unrecognised=data'),
                          ('/new-response-endpoint','opaque'),
                          ('/applicant/vacancy_response','vacancyId=123456789&resumeId=aaaa&letter=different')):
            with self.subTest(path=path,body=body):
                page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed')
                route.request=self.req(path,body)
                result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
                self.assertEqual(result['application_status'],'applied_confirmed')
                self.assertEqual(result['resume_verification'],'dom_verified')
                route.continue_.assert_called_once();route.abort.assert_not_called()
                self.assertEqual(order,['journal','request'])

    def test_read_only_service_post_during_resume_selection(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed')
        service=MagicMock();service.request=self.req('/suggests','text=synthetic')
        def choose(*args):
            guard=page.context.route.call_args.args[1]
            guard(service)
            return True
        with patch('application._choose_resume',side_effect=choose):
            result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        service.continue_.assert_called_once();service.abort.assert_not_called()
        self.assertEqual(result['application_status'],'applied_confirmed')

    def test_submit_disabled_is_precise(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture()
        submit.is_enabled.return_value=False
        self.addCleanup(patch.stopall)
        patch('application._validation_diagnostics',return_value={'invalid_fields':[]}).start()
        result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['reason_code'],'submit_disabled_validation');submit.click.assert_not_called()

    def test_unknown_network_sent_no_confirmation_no_retry(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture()
        route.request=self.req('/unknown-response','opaque')
        result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['application_status'],'submission_unknown')
        self.assertEqual(result['reason_code'],'submission_sent_confirmation_missing')
        submit.click.assert_called_once();route.abort.assert_not_called()

    def test_language_warning_after_observed_submission_is_unknown_without_second_click(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture()
        dialogs=MagicMock(); dialogs.count.return_value=1
        dialog=dialogs.nth.return_value
        dialog.inner_text.return_value='Уровень языка в резюме ниже требования работодателя, возможен отказ.'
        dialog.locator.return_value.count.return_value=0
        confirm=dialog.get_by_role.return_value
        confirm.count.return_value=1; confirm.is_enabled.return_value=True
        original=page.locator.side_effect
        page.locator.side_effect=lambda selector: dialogs if selector=='[role="dialog"]:visible, [data-qa="modal"]:visible' else original(selector)
        result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['application_status'],'submission_unknown')
        self.assertEqual(result['reason_code'],'submission_sent_confirmation_missing')
        self.assertIn({'kind':'language_warning','action':'observed_after_possible_submission'},result['warnings'])
        submit.click.assert_called_once(); confirm.click.assert_not_called()

    def test_observer_exception_does_not_block_live(self):
        page,form,letter,submit,route,journal,order,fill,model=self.fixture(signal='applied_confirmed')
        with patch('application._request_details',side_effect=ValueError('synthetic')):
            result=app.apply(page,test_live.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=journal)
        self.assertEqual(result['application_status'],'applied_confirmed')
        route.abort.assert_not_called()
