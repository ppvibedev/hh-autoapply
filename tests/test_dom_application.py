"""Offline HTML only: no HH session, network or external LLM."""
import json
import unittest
from unittest.mock import patch
import test_safe_mvp
import application as app
from playwright.sync_api import sync_playwright

class DOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)

    def test_local_question_text_and_existing_llm_evidence(self):
        self.page.set_content('''<form>
          <div><div>С какими CMS вы работали?</div><div><textarea></textarea></div></div>
          <div><p>Какие проекты вы вели?</p><textarea aria-required="false"></textarea></div>
          <button type="submit">Откликнуться</button></form>''')
        fields = self.page.locator('form').evaluate(app.INSPECT_FORM)
        self.assertEqual([x['question'] for x in fields], ['С какими CMS вы работали?', 'Какие проекты вы вели?'])
        self.assertIsNone(fields[0]['required'])
        self.assertIs(fields[1]['required'], False)
        with patch('llm.generate',return_value=json.dumps({'answer':'Работал с CMS.', 'evidence':'Работал с CMS.'})) as generate:
            answer = app.answer_question(fields[0],test_safe_mvp.VAC,'Работал с CMS.')
        self.assertIn('С какими CMS вы работали?', generate.call_args.args[0])
        app._fill_question(self.page.locator('form'),fields[0],answer)
        self.assertEqual(answer['fill_result'],'verified')

    def test_dropdown_exact_id_and_reverification(self):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div role="button" aria-controls="choices" onclick="document.getElementById('choices').hidden=false">
          <a href="https://hh.ru/resume/bbbb">Другое</a></div>
          <div role="listbox" id="choices" hidden>
          <div role="option" onclick="document.querySelector('[role=button] a').href='https://hh.ru/resume/aaaa';this.parentElement.hidden=true">
          <a href="https://hh.ru/resume/aaaa">Нужное</a></div></div></section></form>''')
        form=self.page.locator('form'); diagnostic={}
        self.assertFalse(app._selected_resume(form,'aaaa'))
        self.assertTrue(app._choose_resume(form,'aaaa',diagnostic))
        self.assertEqual(diagnostic['before']['selected_ids'],['bbbb'])
        self.assertEqual(diagnostic['after']['selected_ids'],['aaaa'])
        self.assertTrue(app._selected_resume(form,'aaaa'))
        self.assertFalse(app._choose_resume(form,'cccc',{}))

    def test_no_questions_native_resume(self):
        self.page.set_content('''<form><input name="resumeId" type="hidden" value="aaaa">
          <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea></form>''')
        form=self.page.locator('form')
        self.assertEqual(form.evaluate(app.INSPECT_FORM),[])
        self.assertTrue(app._choose_resume(form,'aaaa'))

    def test_real_letter_toggle(self):
        self.page.set_content('''<form><div data-qa="vacancy-response-letter-toggle" onclick="document.querySelector('textarea').hidden=false">Добавить</div>
          <textarea hidden data-qa="vacancy-response-popup-form-letter-input"></textarea></form>''')
        self.assertTrue(app._fill_letter(self.page.locator('form'),'Короткое письмо'))
        self.assertEqual(self.page.locator('textarea').input_value(),'Короткое письмо')

    def test_diagnostics_exclude_values_and_resume_text(self):
        self.page.set_content('''<div role="dialog" data-qa="modal">
          Вы откликаетесь на вакансию в другой стране.
          <form><div data-qa="resume">PRIVATE RESUME</div>
          <textarea>private@example.test</textarea></form>
          <button>Все равно откликнуться</button></div>''')
        diag=app._modal_diagnostics(self.page.locator('[role=dialog]'),1)
        self.assertIn('другой стране',diag['text'])
        self.assertNotIn('PRIVATE RESUME',str(diag))
        self.assertNotIn('private@example.test',str(diag))
        snapshot=app.form_diagnostics(self.page)
        self.assertEqual(len(snapshot['dialogs']),1)
        self.assertNotIn('private@example.test',str(snapshot))

    def test_conflicting_native_id_cannot_be_overridden(self):
        self.page.set_content('''<form><input type="hidden" name="resumeId" value="bbbb">
          <section><div>Резюме для отклика</div><div role="button">
          <a href="https://hh.ru/resume/aaaa">Нужное</a></div></section></form>''')
        form=self.page.locator('form')
        self.assertFalse(app._selected_resume(form,'aaaa'))
        self.assertFalse(app._choose_resume(form,'aaaa',{}))

