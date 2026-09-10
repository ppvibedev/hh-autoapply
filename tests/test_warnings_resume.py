"""User-observed warning texts, fake dialogs only."""
import unittest
from unittest.mock import MagicMock,patch
import test_safe_mvp
import application

COUNTRY='Вы откликаетесь на вакансию в другой стране. Если это не удалённая работа и вы не указали, что хотите переехать, скорее всего, будет отказ'
LANGUAGE='Такой отклик может получить отказ. Уровень английского языка в резюме ниже уровня работодателя.'

class WarningResumeTests(unittest.TestCase):
    def warning_flow(self,text):
        page=MagicMock(); dialog=MagicMock(); form=MagicMock(); dialogs=MagicMock(); forms=MagicMock()
        forms.count.return_value=1; forms.first=form; form.is_visible.return_value=True
        state={'open':True}
        dialogs.count.side_effect=lambda:int(state['open'])
        dialogs.nth.return_value=dialog
        dialog.inner_text.return_value=text
        dialog.locator.return_value.count.return_value=0
        button=dialog.get_by_role.return_value
        button.count.return_value=1; button.is_enabled.return_value=True
        button.click.side_effect=lambda **kw:state.update(open=False)
        empty=MagicMock(); empty.count.return_value=0
        page.locator.side_effect=lambda selector: forms if selector==application.FORM_SELECTOR else dialogs if selector=='[role="dialog"]:visible, [data-qa="modal"]:visible' else empty
        return page,dialog,form,button

    def test_country_warning_then_form(self):
        page,dialog,form,button=self.warning_flow(COUNTRY)
        warnings=[]
        result,reason=application._wait_response_form(page,warnings,lambda:False,lambda:False)
        self.assertIs(result,form); self.assertIsNone(reason)
        self.assertEqual(warnings,[{'kind':'country_warning','action':'confirmed'}])
        button.click.assert_called_once()
        self.assertTrue(dialog.get_by_role.call_args.kwargs['name'].fullmatch('Все равно откликнуться'))

    def test_language_warning_then_form(self):
        page,dialog,form,button=self.warning_flow(LANGUAGE)
        warnings=[]
        self.assertIs(application._wait_response_form(page,warnings,lambda:False,lambda:False)[0],form)
        self.assertEqual(warnings[0]['kind'],'language_warning')
        self.assertEqual(dialog.get_by_role.call_args.kwargs['name'],'Откликнуться')
        button.click.assert_called_once()

    def test_unknown_warning_and_new_fields_not_bypassed(self):
        for text in ('Подтвердите согласие на передачу персональных данных',COUNTRY):
            page,dialog,form,button=self.warning_flow(text)
            if text==COUNTRY: dialog.locator.return_value.count.return_value=1
            result,reason=application._wait_response_form(page,[],lambda:False,lambda:False)
            self.assertIsNone(result); self.assertEqual(reason,'unsupported_form')
            button.click.assert_not_called()

    def test_missing_form_and_blocked_quick_apply_are_distinct(self):
        page=MagicMock(); page.locator.return_value.count.return_value=0
        for blocked,reason in ((False,'response_form_not_found'),(True,'quick_apply_unverified')):
            with patch('application.time.monotonic',side_effect=[0,11]):
                self.assertEqual(application._wait_response_form(page,[],lambda:blocked,lambda:False),(None,reason))

    def test_exact_selected_identifier_required(self):
        form=MagicMock()
        form.evaluate.return_value={'verified':True,'selected_count':1,'expected_selected':True,'other_selected':False}
        self.assertTrue(application._selected_resume(form,'aaaa'))
        self.assertEqual(form.evaluate.call_args.args[1],'aaaa')
        form.evaluate.return_value={'verified':False,'selected_count':1,'expected_selected':False,'other_selected':True}
        self.assertFalse(application._selected_resume(form,'aaaa'))

    def test_linked_radio_candidate_is_checked_then_reverified(self):
        form=MagicMock(); form.locator.return_value.count.return_value=0
        form.evaluate.side_effect=[{'verified':False,'candidates':[2]}, {'verified':True}]
        self.assertTrue(application._choose_resume(form,'aaaa'))
        form.evaluate_handle.assert_called_once_with("(form,i) => (form.elements || form.querySelectorAll('input,select,textarea,button,fieldset,output'))[i]",2)
        form.evaluate_handle.return_value.as_element.return_value.check.assert_called_once()
