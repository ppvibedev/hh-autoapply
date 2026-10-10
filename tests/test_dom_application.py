"""Offline HTML only: no HH session, network or external LLM."""
import json
import tempfile
import unittest
from pathlib import Path
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


    def test_response_container_without_form_preserves_all_stages(self):
        from playwright.sync_api import Route
        for question in (False, True):
            for toggle in ('add-cover-letter','vacancy-response-letter-toggle'):
                with self.subTest(question=question,toggle=toggle):
                    question_html = '<div><p>Какие проекты вы вели?</p><textarea required></textarea></div>' if question else ''
                    html = '''<meta charset="utf-8"><a data-qa="vacancy-response-link-top" onclick="document.querySelector('section').hidden=false">Откликнуться</a>
                    <section hidden>''' + question_html + '''<input type="hidden" name="resumeId" value="aaaa">
                    <button type="button" data-qa="''' + toggle + '''" onclick="document.getElementById('letter').hidden=false">Добавить</button>
                    <textarea id="letter" hidden data-qa="vacancy-response-popup-form-letter-input"></textarea>
                    <button type="submit" data-qa="vacancy-response-submit-popup" onclick="const xhr=new XMLHttpRequest();xhr.open('POST','/applicant/vacancy_response',false);xhr.send('vacancyId=123456789&resumeId=aaaa');document.body.innerHTML='<p>Отклик отправлен</p>'">Откликнуться</button></section>'''
                    submitted=[]
                    def offline_continue(route, **kwargs):
                        # Replace the transport itself: even the authorized POST never leaves this test.
                        if route.request.method == 'GET':route.fulfill(status=200,content_type='text/html',body=html)
                        else:submitted.append(True);route.fulfill(status=200,body='{}')
                    with patch.object(Route,'continue_',offline_continue), patch.object(app.config,'DRY_RUN',False), patch('llm.generate',return_value=json.dumps({'answer':'Вёл проекты Alpha.', 'evidence':'Вёл проекты Alpha.'})) as model:
                        result=app.apply(self.page,test_safe_mvp.VAC,'Письмо',resume_id='aaaa',dry_run=False,resume_text='Вёл проекты Alpha.',before_submit=lambda:None)
                    self.assertEqual(result['application_status'],'applied_confirmed',result)
                    self.assertEqual(len(submitted),1)
                    self.assertEqual(model.call_count,int(question))
                    if question:self.assertEqual(result['questions'][0]['fill_result'],'verified')

    def test_relocation_without_dialog_routes_immediately(self):
        self.page.set_content('''<div id="warning"><button data-qa="relocation-warning-confirm"
          onclick="document.getElementById('warning').remove();document.querySelector('section').hidden=false">Всё равно откликнуться</button>
          <button data-qa="relocation-warning-abort">Отмена</button></div>
          <section hidden><button data-qa="add-cover-letter">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></section>''')
        warnings=[]
        with patch.object(self.page,'wait_for_timeout',side_effect=AssertionError('recognized UI must not poll')):
            scope,reason=app._wait_response_form(self.page,warnings,lambda:False,lambda:False)
        self.assertIsNone(reason)
        self.assertEqual(scope.evaluate('(el)=>el.tagName'),'SECTION')
        self.assertEqual(warnings,[{'kind':'country_warning','action':'confirmed'}])

    def test_unknown_dialog_still_stops(self):
        self.page.set_content('<div role="dialog">Неизвестное предупреждение<button>Продолжить</button></div>')
        scope,reason=app._wait_response_form(self.page,[],lambda:False,lambda:False)
        self.assertIsNone(scope)
        self.assertEqual(reason,'unsupported_form')

    def test_radio_group_heading_and_placeholder_are_not_questions(self):
        self.page.set_content('''<form>
          <div><div>Есть ли у вас опыт с CMS?</div><div>
            <label><input type="radio" name="cms" value="yes">Да</label>
            <label><input type="radio" name="cms" value="no">Нет</label></div></div>
          <div><div>Какие интеграции вы выполняли?</div>
            <label>Писать тут<textarea placeholder="Писать тут" aria-label="Писать тут"></textarea></label></div>
          </form>''')
        fields=self.page.locator('form').evaluate(app.INSPECT_FORM)
        self.assertEqual([f['question'] for f in fields],['Есть ли у вас опыт с CMS?','Какие интеграции вы выполняли?'])
        self.assertEqual(fields[0]['options'],[{'value':'yes','label':'Да'},{'value':'no','label':'Нет'}])
        with patch('llm.generate',return_value=json.dumps({'answer':'yes','evidence':'Работал с CMS.'})) as model:
            answer=app.answer_question(fields[0],test_safe_mvp.VAC,'Работал с CMS.')
        self.assertIn('Есть ли у вас опыт с CMS?',model.call_args.args[0])
        app._fill_question(self.page.locator('form'),fields[0],answer)
        self.assertEqual(answer['fill_result'],'verified')

    def test_placeholder_alone_is_not_a_question(self):
        self.page.set_content('<form><label>Ваш ответ<textarea placeholder="Писать тут"></textarea></label></form>')
        question=self.page.locator('form').evaluate(app.INSPECT_FORM)[0]
        self.assertEqual(question['question'],'')
        with patch('llm.generate') as model, self.assertRaisesRegex(ValueError,'question_label_unknown'):
            app.answer_question(question,test_safe_mvp.VAC,'Опыт')
        model.assert_not_called()

    def test_dropdown_without_heading(self):
        self.page.set_content('''<div role="dialog"><div role="button" aria-controls="resumes"
          onclick="document.getElementById('resumes').hidden=false"><a href="https://hh.ru/resume/bbbb">Другое</a></div>
          <div role="listbox" id="resumes" hidden><div role="option"
          onclick="document.querySelector('[role=button] a').href='https://hh.ru/resume/aaaa';this.parentElement.hidden=true">
          <a href="https://hh.ru/resume/aaaa">Выбранное</a></div></div>
          <button data-qa="add-cover-letter">Добавить</button><button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        form=self.page.locator('[role=dialog]');diag={}
        self.assertTrue(app._choose_resume(form,'aaaa',diag))
        self.assertEqual(diag['after']['selected_ids'],['aaaa'])
        self.assertFalse(app._selected_resume(form,'cccc'))

    def test_application_controls_take_priority_over_warning_like_text(self):
        self.page.set_content('''<form><input name="search"></form><form></form>
          <div role="dialog">Уровень языка ниже, возможен отказ.
          <input type="hidden" name="resumeId" value="aaaa">
          <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        with patch.object(self.page,'wait_for_timeout',side_effect=AssertionError('must route immediately')):
            form,reason=app._wait_response_form(self.page,[],lambda:False,lambda:False)
        self.assertIsNone(reason)
        self.assertEqual(form.get_attribute('role'),'dialog')

    def test_separate_unknown_dialog_is_not_bypassed(self):
        self.page.set_content('''<div role="dialog"><textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>
          <div role="dialog">Неизвестное согласие<button>Продолжить</button></div>''')
        form,reason=app._wait_response_form(self.page,[],lambda:False,lambda:False)
        self.assertIsNone(form)
        self.assertEqual(reason,'unsupported_form')

    def test_zero_width_placeholders_and_unnamed_radio_groups(self):
        self.page.set_content('''<form><div><p>Есть ли опыт с CMS?</p><div>
          <label><input type="radio" value="yes">Да</label><label><input type="radio" value="no">Нет</label></div></div>
          <div><p>Есть ли опыт с CRM?</p><div>
          <label><input type="radio" value="yes">Да</label><label><input type="radio" value="no">Нет</label></div></div>
          <div><p>Какие проекты вы вели?</p><div>\u200b<textarea placeholder="Писать тут" aria-label="\u200b"></textarea></div></div></form>''')
        form=self.page.locator('form');fields=form.evaluate(app.INSPECT_FORM)
        self.assertEqual([q['question'] for q in fields],['Есть ли опыт с CMS?','Есть ли опыт с CRM?','Какие проекты вы вели?'])
        app._fill_question(form,fields[1],{'answer':'yes'})
        self.assertFalse(self.page.locator('input').nth(0).is_checked())
        self.assertTrue(self.page.locator('input').nth(2).is_checked())

    def test_hh_drop_base_radio_portal(self):
        self.page.set_content('''<div role="dialog" id="response"><div role="button" onclick="document.getElementById('popup').hidden=false">Выбрать</div>
          <button data-qa="add-cover-letter">Добавить</button></div>
          <div data-qa="drop-base" role="dialog" id="popup" hidden>
          <label><input type="radio" name="choice" value="bbbb" checked>Другое</label>
          <label><input type="radio" name="choice" value="aaaa" onchange="document.getElementById('popup').hidden=true">Нужное</label></div>''')
        root=self.page.locator('#response');diag={}
        self.assertTrue(app._choose_resume(root,'aaaa',diag))
        self.assertEqual(diag['after']['selected_ids'],['aaaa'])
        self.assertTrue(app._selected_resume(root,'aaaa'))
        self.assertFalse(app._selected_resume(root,'cccc'))

    def test_unknown_requiredness_with_valid_answer_is_filled(self):
        self.page.set_content('<section><p>Какие проекты вы вели?</p><textarea></textarea></section>')
        root=self.page.locator('section');question=root.evaluate(app.INSPECT_FORM)[0]
        self.assertIsNone(question['required'])
        with patch('llm.generate',return_value=json.dumps({'answer':'Вёл Alpha.', 'evidence':'Вёл Alpha.'})):
            answer=app.answer_question(question,test_safe_mvp.VAC,'Вёл Alpha.')
        app._fill_question(root,question,answer)
        self.assertEqual(root.locator('textarea').input_value(),'Вёл Alpha.')

    def test_disabled_submit_does_not_block_question_routing(self):
        self.page.set_content('''<div role="dialog"><p>Опыт?</p><textarea></textarea>
          <button disabled data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        with patch.object(self.page,'wait_for_timeout',side_effect=AssertionError('no wait for ready UI')):
            root,reason=app._wait_response_form(self.page,[],lambda:False,lambda:False)
        self.assertIsNone(reason)
        self.assertIsNotNone(root)

    def test_delayed_letter_appearance_and_verification_failure(self):
        self.page.set_content('''<div id="response"><button data-qa="add-cover-letter" onclick="setTimeout(()=>{this.insertAdjacentHTML('afterend','<textarea data-qa=vacancy-response-popup-form-letter-input></textarea>')},50)">Добавить</button></div>''')
        self.assertIsNotNone(app._fill_letter(self.page.locator('#response'),'Письмо'))
        self.assertEqual(self.page.locator('textarea').input_value(),'Письмо')
        self.page.locator('textarea').evaluate("el => el.addEventListener('input',()=>el.value='changed')")
        with self.assertRaisesRegex(ValueError,'cover_letter_readback_mismatch'):
            app._fill_letter(self.page.locator('#response'),'Письмо')

    def test_full_flow_warning_questions_portal_letter_async_confirmation(self):
        from playwright.sync_api import Route
        html = '''<meta charset="utf-8">
        <a data-qa="vacancy-response-link-top" onclick="document.getElementById('warning').hidden=false">Откликнуться</a>
        <div id="warning" hidden><button data-qa="relocation-warning-confirm" onclick="this.parentElement.remove();document.getElementById('response').hidden=false">Всё равно откликнуться</button></div>
        <div role="dialog" id="response" hidden>
          <div><p>Какие проекты вы вели?</p><div>\u200b<textarea placeholder="Писать тут"></textarea></div></div>
          <div role="button" onclick="document.getElementById('popup').hidden=false">Резюме</div>
          <button data-qa="add-cover-letter" onclick="document.getElementById('letter').hidden=false">Добавить</button>
          <textarea hidden id="letter" data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup" onclick="setTimeout(()=>fetch('/applicant/vacancy_response',{method:'POST',body:'vacancyId=123456789&resumeId='+document.querySelector('#popup input:checked').value}).then(()=>document.body.innerHTML='<p>Отклик отправлен</p>'),50)">Откликнуться</button>
        </div>
        <div role="dialog" data-qa="drop-base" id="popup" hidden>
          <label><input name="resume-choice" type="radio" value="bbbb" checked>Другое</label>
          <label><input name="resume-choice" type="radio" value="aaaa" onchange="document.getElementById('popup').hidden=true">Нужное</label>
        </div>'''
        journal=[];requests=[]
        def offline(route,**kwargs):
            if route.request.method == 'GET':route.fulfill(status=200,content_type='text/html',body=html)
            else:
                requests.append(route.request.post_data)
                self.assertEqual(self.page.locator('#letter').input_value(),'Prepared letter')
                self.assertEqual(self.page.locator('#response textarea').first.input_value(),'Вёл Alpha.')
                route.fulfill(status=200,body='{}')
        with patch.object(Route,'continue_',offline), patch.object(app.config,'DRY_RUN',False), patch('llm.generate',return_value=json.dumps({'answer':'Вёл Alpha.','evidence':'Вёл Alpha.'})) as model:
            result=app.apply(self.page,test_safe_mvp.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,resume_text='Вёл Alpha.',before_submit=lambda:journal.append(True))
        self.assertEqual(result['application_status'],'applied_confirmed',result)
        self.assertEqual(result['warnings'],[{'kind':'country_warning','action':'confirmed'}])
        self.assertEqual(journal,[True]);self.assertEqual(len(requests),1)
        self.assertIn('resumeId=aaaa',requests[0]);model.assert_called_once()

    def test_title_only_resume_never_verifies_identity(self):
        cases = [
            # Exact selected title in a unique local catalog entry.
            ('Target role', [{'resume_id':'aaaa','title':'Target role'}, {'resume_id':'bbbb','title':'Other role'}]),
            # A visible title that maps uniquely to another local resume.
            ('Other role', [{'resume_id':'aaaa','title':'Target role'}, {'resume_id':'bbbb','title':'Other role'}]),
            # An ambiguous local title.
            ('Target role', [{'resume_id':'aaaa','title':'Target role'}, {'resume_id':'bbbb','title':'Target role'}]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / 'resumes.json'
            with patch.object(app.config, 'RESUMES_FILE', str(catalog)):
                for visible_title, rows in cases:
                    catalog.write_text(json.dumps(rows), encoding='utf-8')
                    self.page.set_content(f'''<div id="response"><div role="button" aria-controls="choices"
                      onclick="document.getElementById('choices').hidden=false">{visible_title}</div>
                      <div role="listbox" id="choices" hidden><div role="option"
                      onclick="document.querySelector('[role=button]').textContent='Target role';this.parentElement.hidden=true">Target role</div></div></div>''')
                    root=self.page.locator('#response')
                    self.assertFalse(app._selected_resume(root,'aaaa'))
                    self.assertFalse(app._choose_resume(root,'aaaa',{}))
                    self.assertFalse(app._selected_resume(root,'aaaa'))

    def test_whole_native_form_not_just_resume_letter_footer(self):
        self.page.set_content('''<form><div><p>Какие проекты вы вели?</p><textarea required></textarea></div>
          <div><div>Резюме для отклика</div><input type="hidden" name="resumeId" value="aaaa">
          <button data-qa="add-cover-letter">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div></form>''')
        root,reason=app._wait_response_form(self.page,[],lambda:False,lambda:False)
        self.assertIsNone(reason);self.assertEqual(root.evaluate('(el)=>el.tagName'),'FORM')
        fields=root.evaluate(app.INSPECT_FORM)
        self.assertEqual(len(fields),1);self.assertEqual(fields[0]['question'],'Какие проекты вы вели?')

    def test_questions_after_resume_heading_are_not_skipped(self):
        self.page.set_content('''<form><div>Резюме для отклика</div><input type="hidden" name="resumeId" value="aaaa">
          <div><p>Какие проекты вы вели?</p><textarea required></textarea></div></form>''')
        fields=self.page.locator('form').evaluate(app.INSPECT_FORM)
        self.assertEqual(len(fields),1)
        self.assertEqual(fields[0]['question'],'Какие проекты вы вели?')

    def test_hidden_auxiliary_textarea_does_not_hide_radio_question(self):
        self.page.set_content('''<form><div><p>Есть ли у вас опыт?</p><div>
          <label><input type="radio" name="experience" value="yes">Да</label>
          <label><input type="radio" name="experience" value="no">Нет</label>
          <label><input type="radio" name="experience" value="other">Другое</label>
          <textarea hidden></textarea></div></div></form>''')
        fields=self.page.locator('form').evaluate(app.INSPECT_FORM)
        self.assertEqual(fields[0]['question'],'Есть ли у вас опыт?')
        self.assertEqual(len(fields[0]['options']),3)

    def test_fenced_question_json_preserves_evidence_validation(self):
        question={'question':'С какими CMS/фреймворками вы работали?', 'type':'textarea','required':None,'options':[]}
        with patch('llm.generate',return_value='```json\n'+json.dumps({'answer':'Работал с CMS.','evidence':'Работал с CMS.'})+'\n```'):
            self.assertEqual(app.answer_question(question,test_safe_mvp.VAC,'Работал с CMS.')['answer'],'Работал с CMS.')
        with patch('llm.generate',return_value='```json\n{"answer":"Invented","evidence":"Invented"}\n```'),self.assertRaisesRegex(ValueError,'question_evidence_missing'):
            app.answer_question(question,test_safe_mvp.VAC,'Работал с CMS.')

    def test_full_language_questions_safe_retry_and_no_retry_if_sent(self):
        from playwright.sync_api import Route
        for outcome in ('retry_success','maybe_sent','twice_failed'):
            with self.subTest(outcome=outcome):
                html='''<meta charset="utf-8"><script>let clicks=0;</script>
                <a data-qa="vacancy-response-link-top" onclick="document.getElementById('warning').hidden=false">Откликнуться</a>
                <div role="dialog" id="warning" hidden>Такой отклик может получить отказ. Уровень английского языка ниже уровня работодателя.
                  <button onclick="this.parentElement.remove();document.getElementById('response').hidden=false">Откликнуться</button></div>
                <form id="response" hidden onsubmit="return false"><div><p>С какими CMS вы работали?</p><textarea required></textarea></div>
                <input type="hidden" name="resumeId" value="aaaa">
                <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
                <div role="alert" id="error"></div>
                <button type="submit" data-qa="vacancy-response-submit-popup" onclick="clicks++;HANDLER">Откликнуться</button></form>'''
                send="const xhr=new XMLHttpRequest();xhr.open('POST','/applicant/vacancy_response/popup',false);xhr.send('vacancy_id=123456789&resume_hash=aaaa');"
                transient="document.getElementById('error').textContent='Произошла ошибка, попробуйте ещё раз';"
                if outcome=='retry_success': handler="if(clicks===1){"+transient+"}else{"+send+"document.body.innerHTML='<p>Отклик отправлен</p>'; }"
                elif outcome=='maybe_sent':handler=send+transient
                else:handler=transient
                html=html.replace('HANDLER',handler)
                released=[];journal=[]
                def offline(route,**kwargs):
                    if route.request.method=='GET':route.fulfill(status=200,content_type='text/html',body=html)
                    else:released.append(True);route.fulfill(status=200,body='{}')
                with patch.object(Route,'continue_',offline),patch.object(app.config,'DRY_RUN',False),patch('llm.generate',return_value=json.dumps({'answer':'Работал с CMS.','evidence':'Работал с CMS.'})):
                    result=app.apply(self.page,test_safe_mvp.VAC,'Письмо',resume_id='aaaa',dry_run=False,resume_text='Работал с CMS.',before_submit=lambda:journal.append(True))
                if outcome=='retry_success':
                    self.assertEqual(result['application_status'],'applied_confirmed',result)
                    self.assertEqual(result['retry_count'],1);self.assertEqual(released,[True]);self.assertEqual(journal,[True])
                elif outcome=='maybe_sent':
                    self.assertEqual(result['application_status'],'submission_unknown',result)
                    self.assertEqual(result['reason_code'],'submission_maybe_sent_no_retry');self.assertEqual(released,[True])
                    self.assertEqual(self.page.evaluate('clicks'),1)
                else:
                    self.assertEqual(result['reason_code'],'submit_transient_error_after_retry',result)
                    self.assertEqual(self.page.evaluate('clicks'),2);self.assertEqual(released,[])

    def test_disabled_question_then_unknown_live_request_and_success(self):
        from playwright.sync_api import Route
        html='''<meta charset="utf-8">
        <a data-qa="vacancy-response-link-top" onclick="document.querySelector('form').hidden=false">Откликнуться</a>
        <form hidden onsubmit="return false">
          <label>Какие проекты вы вели?<textarea required oninput="setTimeout(()=>document.querySelector('button').disabled=false,100)"></textarea></label>
          <input type="radio" name="resumeId" value="aaaa" checked hidden>
          <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button disabled data-qa="vacancy-response-submit-popup" onclick="fetch('/new-response-format',{method:'POST',body:'opaque'}).then(()=>document.body.innerHTML='<p>Отклик отправлен</p>')">Откликнуться</button>
        </form>'''
        requests=[]
        def offline(route,**kwargs):
            if route.request.method=='GET':route.fulfill(status=200,content_type='text/html',body=html)
            else:
                requests.append(route.request.url)
                self.assertEqual(self.page.locator('textarea').first.input_value(),'Вёл Alpha.')
                route.fulfill(status=200,body='{}')
        with patch.object(Route,'continue_',offline),patch.object(app.config,'DRY_RUN',False),patch('llm.generate',return_value=json.dumps({'answer':'Вёл Alpha.','evidence':'Вёл Alpha.'})):
            result=app.apply(self.page,test_safe_mvp.VAC,'Письмо',resume_id='aaaa',dry_run=False,resume_text='Вёл Alpha.',before_submit=lambda:None)
        self.assertEqual(result['application_status'],'applied_confirmed',result)
        self.assertEqual(len(requests),1)
        self.assertEqual(result['resume_verification'],'dom_verified')
        self.assertFalse(result['blocked_submission_request'])

    def test_validation_diagnostics_never_include_values(self):
        self.page.set_content('<form><input required><input type="email" value="private-invalid"><textarea required hidden></textarea></form>')
        info=app._validation_diagnostics(self.page.locator('form'))
        self.assertEqual(len(info['invalid_fields']),3)
        self.assertTrue(info['invalid_fields'][0]['value_missing'])
        self.assertTrue(info['invalid_fields'][1]['type_mismatch'])
        self.assertFalse(info['invalid_fields'][2]['visible'])
        self.assertNotIn('private',str(info))

    def test_form_scope_diagnostics_show_unparsed_question_like_controls_without_text(self):
        self.page.set_content('<form><fieldset><textarea required></textarea><input type="checkbox" required></fieldset><input name="resumeId" value="aaaa"></form>')
        info=app._form_scope_diagnostics(self.page.locator('form'), 0, 1)
        self.assertEqual(info['selected_scope_tag'],'form')
        self.assertEqual(info['parsed_question_count'],0)
        self.assertTrue(info['question_like_controls_unparsed'])
        self.assertEqual(info['required_control_count'],2)
        self.assertNotIn('aaaa',str(info))

    def test_known_warnings_text_add_letter_before_submit(self):
        from playwright.sync_api import Route
        for warning in ('language','country','none'):
            with self.subTest(warning=warning):
                intro=''
                if warning=='language':intro='Такой отклик может получить отказ. Уровень языка в резюме ниже требования работодателя.'
                country='''<div role="dialog" id="country" hidden>Вы откликаетесь на вакансию в другой стране
                  <button onclick="this.parentElement.remove();document.getElementById('response').hidden=false">Все равно откликнуться</button></div>'''
                target='country' if warning=='country' else 'response'
                html=f'''<meta charset="utf-8"><a data-qa="vacancy-response-link-top" onclick="document.getElementById('{target}').hidden=false">Откликнуться</a>
                {country if warning=='country' else ''}
                <form role="dialog" id="response" hidden onsubmit="return false">{intro}
                  <input type="radio" name="resumeId" value="aaaa" checked hidden>
                  <button type="button" onclick="document.getElementById('letter').hidden=false;this.remove()">Добавить сопроводительное</button>
                  <label>Сопроводительное письмо<textarea id="letter" hidden></textarea></label>
                  <button type="submit" onclick="if(document.getElementById('letter').value!=='Prepared letter')throw Error('missing letter');fetch('/applicant/vacancy_response',{{method:'POST',body:'opaque'}}).then(()=>document.body.innerHTML='<p>Отклик отправлен</p>')">Откликнуться</button>
                </form>'''
                released=[]
                def offline(route,**kwargs):
                    if route.request.method=='GET':route.fulfill(status=200,content_type='text/html',body=html)
                    else:
                        self.assertEqual(self.page.locator('#letter').input_value(),'Prepared letter')
                        released.append(True);route.fulfill(status=200,body='{}')
                with patch.object(Route,'continue_',offline),patch.object(app.config,'DRY_RUN',False):
                    result=app.apply(self.page,test_safe_mvp.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=lambda:None)
                self.assertEqual(result['application_status'],'applied_confirmed',result)
                self.assertEqual(released,[True])

    def test_resume_metadata_snapshots_exclude_private_text(self):
        self.page.set_content('''<form><magritte-select-layout><div role="button" tabindex="0" class="magritte-card" data-qa="resume-trigger" aria-expanded="false"
          aria-controls="choices" onclick="this.setAttribute('aria-expanded','true');document.getElementById('choices').hidden=false">PRIVATE_RESUME_TEXT</div></magritte-select-layout>
          <div data-qa="hidden-resume-warning">PRIVATE MODAL WARNING</div>
          <div hidden id="choices" data-qa="dropdown-unrecognised" class="dropdown-shell"><input value="TOP_SECRET_VALUE"><button>person@example.test PRIVATE_RESUME_TEXT</button></div></form>''')
        diag={}
        with patch('application._custom_resume_state',return_value={'selected_ids':['bbbb'],'popup_found':False,'options':[]}), \
             patch('application._resume_warning_snapshot') as warning_snapshot:
            self.assertFalse(app._custom_resume_choose(self.page.locator('form'),'aaaa',diag))
        warning_snapshot.assert_not_called()
        snapshots=diag['dom_snapshots']
        self.assertEqual([s['snapshot'] for s in snapshots],['before_click','immediately_after_click','100ms','500ms','1500ms','3000ms'])
        self.assertEqual(diag['reason_code'],'resume_popup_not_opened')
        self.assertEqual(diag['state_reason_code'],'chooser_not_opened')
        self.assertEqual(snapshots[0]['trigger']['aria_expanded'],'false')
        self.assertTrue(snapshots[0]['trigger']['visible'])
        self.assertTrue(snapshots[0]['trigger']['enabled'])
        self.assertEqual(snapshots[0]['magritte_select_layout_count'],1)
        self.assertEqual(snapshots[0]['resume_card_count'],1)
        self.assertIn('displayed_resume_id',snapshots[0])
        self.assertIn('navigation_or_reload_observed',snapshots[0])
        self.assertEqual(snapshots[-1]['trigger']['aria_expanded'],'true')
        self.assertTrue(snapshots[-1]['dom_changed'])
        self.assertEqual(snapshots[-1]['visible_counts']['qa_dropdown'],1)
        self.assertGreaterEqual(snapshots[-1]['elapsed_wait_ms'],2900)
        self.assertTrue(any(snapshot['mutation_event_count'] for snapshot in snapshots))
        self.assertTrue(any(event['type'] == 'attributes' for snapshot in snapshots for event in snapshot['mutation_events']))
        self.assertNotIn('PRIVATE_RESUME_TEXT',json.dumps(diag))
        self.assertNotIn('person@example.test',json.dumps(diag))
        self.assertNotIn('TOP_SECRET_VALUE',json.dumps(diag))
        self.assertNotIn('PRIVATE MODAL WARNING',json.dumps(diag))

    def test_resume_warning_visible_transition_uses_structural_mutation_only(self):
        snapshots=[{'mutation_events':[{'target':{'qa':'hidden-resume-warning'},
            'attribute_name':'style','state_value':{'display':'block','visibility':'visible',
                                                     'pointer_events':'auto'}}]}]
        self.assertTrue(app._resume_warning_was_visible(snapshots))
        snapshots[0]['mutation_events'][0]['state_value']['display']='none'
        self.assertFalse(app._resume_warning_was_visible(snapshots))

    def test_standalone_resume_identity_snapshot_keeps_only_safe_structure(self):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div role="button" data-qa="resume-trigger" data-testid="resume-control"
            data-resume-id="aaaa" data-note="PRIVATE_DATA_VALUE" title="PRIVATE_TITLE">
            PRIVATE_RESUME_TITLE<a href="https://hh.ru/resume/aaaa?token=PRIVATE_QUERY">PRIVATE LINK</a>
          </div><input type="hidden" name="resumeId" value="aaaa">
          <div data-id="aaaa"></div><div data-id="bbbb"></div><input name="contact" value="person@example.test">
          <div title="ARBITRARY_ATTRIBUTE_VALUE">UNRELATED PRIVATE TEXT</div></section></form>''')
        snapshot=app._resume_identity_snapshot(self.page.locator('form'),'aaaa','before_click')
        self.assertEqual(snapshot['phase'],'before_click')
        self.assertGreaterEqual(snapshot['exact_target_occurrence_count'],2)
        identifiers=[item for element in snapshot['elements'] for item in element['resume_identifiers']]
        self.assertIn({'attribute':'data-resume-id','resume_id':'aaaa'},identifiers)
        self.assertIn({'attribute':'data-id','resume_id':'aaaa'},identifiers)
        self.assertIn({'attribute':'href','resume_id':'aaaa'},identifiers)
        self.assertIn({'attribute':'value','resume_id':'aaaa'},identifiers)
        self.assertNotIn({'attribute':'data-id','resume_id':'bbbb'},identifiers)
        encoded=json.dumps(snapshot)
        for private in ('PRIVATE_RESUME_TITLE','PRIVATE LINK','PRIVATE_DATA_VALUE','PRIVATE_TITLE',
                        'PRIVATE_QUERY','person@example.test','UNRELATED PRIVATE TEXT','ARBITRARY_ATTRIBUTE_VALUE'):
            self.assertNotIn(private,encoded)
        self.assertNotIn('?token=',encoded)
        self.assertNotIn('contact',encoded)

    def test_standalone_resume_popup_failure_records_safe_identity_snapshots(self):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div role="button" data-qa="resume-trigger">PRIVATE RESUME TITLE</div>
          <input name="unrelated" value="TOP_SECRET_VALUE"></section></form>''')
        diagnostics={}
        with patch('application._safe_url_path',return_value='/applicant/vacancy_response'):
            self.assertFalse(app._custom_resume_choose(self.page.locator('form'),'aaaa',diagnostics))
        self.assertEqual(diagnostics['reason_code'],'resume_popup_not_opened')
        self.assertNotIn('resume_warning_snapshot',diagnostics)
        snapshots=diagnostics['resume_identity_snapshots']
        self.assertEqual([item['phase'] for item in snapshots],['before_click','after_click_wait'])
        self.assertTrue(all(item['exact_target_occurrence_count']==0 for item in snapshots))
        encoded=json.dumps(snapshots)
        self.assertNotIn('PRIVATE RESUME TITLE',encoded)
        self.assertNotIn('TOP_SECRET_VALUE',encoded)

    def test_resume_warning_snapshot_is_structural_private_and_exact(self):
        self.page.set_content('''<form><div data-qa="hidden-resume-warning" data-private="PRIVATE_DATA"
          data-testid="resume-warning" aria-label="PRIVATE_ARIA" aria-controls="warning-controls">
          PRIVATE_WARNING_TEXT
          <button id="action" type="button" data-qa="resume-warning-action" data-note="PRIVATE_NOTE"
            aria-label="PRIVATE_BUTTON" value="PRIVATE_VALUE" onclick="window.warningClicks++">PRIVATE_BUTTON_TEXT</button>
          <a id="target" href="https://hh.ru/resume/aaaa?token=PRIVATE_QUERY#PRIVATE_FRAGMENT">PRIVATE_TARGET_TITLE</a>
          <a id="other" href="https://hh.ru/resume/bbbb?token=OTHER_SECRET#fragment">PRIVATE_OTHER_TITLE</a>
          <a id="external" href="https://private.example/SECRET_PATH?token=SECRET">PRIVATE_EXTERNAL</a>
          <input type="radio" name="resumeId" value="bbbb" checked aria-label="PRIVATE_RADIO">
        </div></form><script>window.warningClicks=0</script>''')
        snapshot=app._resume_warning_snapshot(self.page.locator('form').first,'aaaa')
        self.assertTrue(snapshot['visible'])
        self.assertEqual(snapshot['root']['data_qa'],'hidden-resume-warning')
        self.assertIn('data-private',snapshot['root']['data_attribute_names'])
        controls={item['data_qa'] or item['href_path']:item for item in snapshot['controls']}
        self.assertEqual(controls['/resume/aaaa']['href_path'],'/resume/aaaa')
        self.assertEqual(controls['/resume/bbbb']['href_path'],'/resume/bbbb')
        self.assertEqual(controls['resume-warning-action']['type'],'button')
        self.assertEqual(controls['']['href_path'],'')
        identifiers=snapshot['exact_resume_identifiers']
        self.assertIn({'source_type':'href-path','resume_id':'aaaa','matches_expected':True},identifiers)
        self.assertIn({'source_type':'href-path','resume_id':'bbbb','matches_expected':False},identifiers)
        self.assertIn({'source_type':'resume-control-value','resume_id':'bbbb','matches_expected':False},identifiers)
        self.assertEqual(self.page.evaluate('warningClicks'),0)
        encoded=json.dumps(snapshot)
        for private in ('PRIVATE_DATA','PRIVATE_ARIA','PRIVATE_NOTE','PRIVATE_VALUE','PRIVATE_WARNING_TEXT',
                        'PRIVATE_BUTTON_TEXT','PRIVATE_TARGET_TITLE','PRIVATE_OTHER_TITLE','PRIVATE_EXTERNAL',
                        'PRIVATE_QUERY','PRIVATE_FRAGMENT','OTHER_SECRET','private.example','SECRET_PATH'):
            self.assertNotIn(private,encoded)
        self.assertNotIn('aria-label',encoded)
        self.assertNotIn('?token=',encoded)

    def test_resume_warning_visibility_diagnostics_are_structural(self):
        self.page.set_content('''<style>
          #clip {width:40px;height:20px;overflow:hidden;position:relative}
          #warning {width:200px;height:80px;opacity:0}
          #cover {position:absolute;inset:0;background:white;z-index:3}
          #outside {position:absolute;top:5000px}
          </style>
          <form><div id="clip"><div id="warning" data-qa="hidden-resume-warning"
            aria-hidden="true" inert><a href="https://hh.ru/resume/bbbb">PRIVATE OTHER</a></div>
            <div id="cover"></div></div></form>
          <div id="outside" data-qa="hidden-resume-warning">PRIVATE OUTSIDE</div><form id="other"></form>''')
        snapshot=app._resume_warning_snapshot(self.page.locator('form').first,'aaaa')
        self.assertEqual(snapshot['opacity'],0)
        self.assertIs(snapshot['aria_hidden'],True)
        self.assertIs(snapshot['inert'],True)
        self.assertGreater(snapshot['client_rect_count'],0)
        self.assertEqual(snapshot['clipping'],'clipped')
        self.assertEqual(snapshot['occlusion'],'occluded_at_center')
        self.assertEqual(snapshot['selected_resume_relation'],'other_resume')
        self.assertEqual(snapshot['actionable_control_count'],0)
        self.assertEqual(len(snapshot['exact_resume_identifiers']),1)
        self.assertNotIn('PRIVATE OUTSIDE',json.dumps(snapshot))
        self.assertIsNone(app._resume_warning_snapshot(self.page.locator('#other'),'aaaa'))
        self.page.locator('#warning a').evaluate('(el)=>el.remove()')
        snapshot=app._resume_warning_snapshot(self.page.locator('form').first,'aaaa')
        self.assertEqual(snapshot['selected_resume_relation'],'no_identifier')
        self.assertEqual(snapshot['actionable_control_count'],0)

    def test_resume_warning_outside_viewport_and_ambiguous_ids(self):
        self.page.set_content('''<form><div data-qa="hidden-resume-warning" style="position:absolute;top:5000px">
          <a href="https://hh.ru/resume/aaaa">PRIVATE TARGET</a>
          <a href="https://hh.ru/resume/bbbb">PRIVATE OTHER</a></div></form>''')
        snapshot=app._resume_warning_snapshot(self.page.locator('form'),'aaaa')
        self.assertFalse(snapshot['intersects_viewport'])
        self.assertEqual(snapshot['viewport_intersection_area'],0)
        self.assertEqual(snapshot['occlusion'],'unknown')
        self.assertEqual(snapshot['selected_resume_relation'],'ambiguous')

    def test_resume_warning_stabilization_records_transient_persistent_and_stop(self):
        self.page.set_content('''<form><div id="warning" data-qa="hidden-resume-warning">PRIVATE</div></form>''')
        form=self.page.locator('form')
        initial=app._resume_warning_snapshot(form,'aaaa')
        self.page.evaluate("setTimeout(()=>document.getElementById('warning').remove(),80)")
        transient=app._stabilize_resume_warning(form,'aaaa',initial,timeout_ms=150)
        self.assertTrue(transient['warning_disappeared'])
        self.assertFalse(transient['warning_persisted'])
        self.page.set_content('''<form><div data-qa="hidden-resume-warning">PRIVATE</div></form>''')
        form=self.page.locator('form'); initial=app._resume_warning_snapshot(form,'aaaa')
        persistent=app._stabilize_resume_warning(form,'aaaa',initial,timeout_ms=100)
        self.assertTrue(persistent['warning_persisted'])
        stopped=app._stabilize_resume_warning(form,'aaaa',initial,stopped=lambda:True,timeout_ms=400)
        self.assertTrue(stopped['stabilization_stopped'])
        self.assertLess(stopped['stabilization_elapsed_ms'],100)
        for data in (transient,persistent,stopped):
            self.assertNotIn('PRIVATE',json.dumps(data))

    def test_standalone_visible_resume_warning_gets_specific_diagnostic_without_action(self):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div id="trigger" role="button" data-qa="resume-trigger"
            onclick="document.getElementById('warning').hidden=false">PRIVATE RESUME TITLE</div>
          <div id="warning" data-qa="hidden-resume-warning" hidden>
            PRIVATE WARNING <button id="warning-action" type="button"
              data-qa="hidden-resume-warning-action" onclick="window.warningClicks++">PRIVATE ACTION</button>
            <a href="https://hh.ru/resume/aaaa?token=PRIVATE_QUERY">PRIVATE RESUME</a>
          </div></section></form><script>window.warningClicks=0</script>''')
        diagnostics={}
        with patch('application._safe_url_path',return_value='/applicant/vacancy_response'):
            self.assertFalse(app._custom_resume_choose(self.page.locator('form'),'aaaa',diagnostics))
        self.assertEqual(diagnostics['reason_code'],'resume_hidden_warning_requires_action')
        self.assertEqual(diagnostics['state_reason_code'],'hidden_resume_warning_visible')
        self.assertEqual(diagnostics['resume_warning_snapshot']['visibility_evidence'],'visible_after_click')
        self.assertEqual(diagnostics['resume_warning_snapshot']['exact_resume_identifiers'],[
            {'source_type':'href-path','resume_id':'aaaa','matches_expected':True}])
        self.assertEqual(diagnostics['resume_warning_snapshot']['selected_resume_relation'],'target_match')
        self.assertTrue(diagnostics['warning_visibility_transition_observed'])
        self.assertTrue(diagnostics['warning_persisted'])
        self.assertFalse(diagnostics['warning_disappeared'])
        self.assertIn('warning_initial_state',diagnostics)
        self.assertIn('warning_stabilized_state',diagnostics)
        chooser=diagnostics['chooser_failure_diagnostics']
        self.assertEqual(chooser['response_path_category'],'standalone')
        self.assertTrue(chooser['current_response_scope_established'])
        self.assertEqual(chooser['resume_trigger_count'],1)
        self.assertFalse(chooser['popup_appeared_transiently'])
        self.assertGreaterEqual(chooser['elapsed_wait_ms'],2900)
        self.assertEqual(self.page.evaluate('warningClicks'),0)
        encoded=json.dumps(diagnostics)
        for private in ('PRIVATE WARNING','PRIVATE ACTION','PRIVATE RESUME','PRIVATE_QUERY'):
            self.assertNotIn(private,encoded)

    def test_standalone_warning_stabilization_stop_keeps_safe_failure(self):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div role="button" data-qa="resume-trigger"
            onclick="document.getElementById('warning').hidden=false">PRIVATE RESUME</div>
          <div id="warning" data-qa="hidden-resume-warning" hidden>PRIVATE WARNING</div>
          <button type="submit" onclick="window.submitClicks++">PRIVATE SUBMIT</button>
          </section></form><script>window.submitClicks=0</script>''')
        diagnostics={}
        with patch('application._safe_url_path',return_value='/applicant/vacancy_response'):
            self.assertFalse(app._custom_resume_choose(
                self.page.locator('form'),'aaaa',diagnostics,stopped=lambda:True))
        self.assertEqual(diagnostics['reason_code'],'resume_hidden_warning_requires_action')
        self.assertTrue(diagnostics['stabilization_stopped'])
        self.assertEqual(self.page.evaluate('submitClicks'),0)

    def _real_resume_portal(self, update):
        self.page.set_content('''<form><section><div>Резюме для отклика</div>
          <div id="trigger" role="button" data-resume-id="bbbb"
            onclick="document.getElementById('portal').hidden=false">Synthetic</div>
          </section></form><div id="portal" role="dialog" data-qa="drop-base" hidden>
          <div role="listbox" data-qa="magritte-select-option-list">
          <label role="option" data-magritte-select-option data-qa="magritte-select-option-bbbb">Synthetic B<input type="radio"></label>
          <label id="target" role="option" data-magritte-select-option data-qa="magritte-select-option-aaaa">Synthetic A<input type="radio"></label>
          </div></div>''')
        self.page.locator('#target').evaluate('''(el, update)=>el.onclick=()=>{
          if(update)document.getElementById('trigger').setAttribute('data-resume-id','aaaa');
          document.getElementById('portal').hidden=true;
        }''',update)

    def _modal_resume_state(self, selected='aaaa', close_on_escape=True):
        escape = "document.addEventListener('keydown',event=>{if(event.key==='Escape')document.getElementById('portal').hidden=true})" if close_on_escape else ''
        self.page.set_content(f'''<meta charset="utf-8"><div role="dialog" id="response">
          <section><div>Резюме для отклика</div>
          <div id="trigger" role="button" onclick="document.getElementById('portal').hidden=false">Synthetic</div></section>
          <button data-qa="add-cover-letter" onclick="window.addClicks++">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup" onclick="window.submitClicks++">Откликнуться</button>
        </div><div role="dialog" data-qa="drop-base" id="portal" hidden>
          <div role="listbox" data-qa="magritte-select-option-list">
            <label id="other" role="option" data-magritte-select-option data-qa="magritte-select-option-bbbb" aria-selected="{'true' if selected == 'bbbb' else 'false'}">
              Other<input type="radio" {'checked' if selected == 'bbbb' else ''}></label>
            <label id="target" role="option" data-magritte-select-option data-qa="magritte-select-option-aaaa" aria-selected="{'true' if selected == 'aaaa' else 'false'}"
              onclick="if(event.target!==this)return;window.targetClicks++;document.getElementById('other').setAttribute('aria-selected','false');this.setAttribute('aria-selected','true');this.querySelector('input').checked=true;document.querySelector('#other input').checked=false">
              Target<input type="radio" {'checked' if selected == 'aaaa' else ''}></label>
          </div></div><script>window.targetClicks=0;window.addClicks=0;window.submitClicks=0;{escape}</script>''')
        return self.page.locator('#response')

    def test_modal_already_selected_is_not_clicked_and_chooser_closes(self):
        form=self._modal_resume_state('aaaa')
        diag={}; evidence={}
        self.assertTrue(app._choose_resume(form,'aaaa',diag,evidence,'123456789'))
        self.assertEqual(diag['selection_result'],'desired_resume_already_selected')
        self.assertEqual(diag['target_option_click_count'],0)
        self.assertTrue(diag['chooser_closed'])
        self.assertTrue(self.page.locator('#portal').is_hidden())
        self.assertEqual(self.page.evaluate('targetClicks'),0)
        self.assertTrue(evidence['valid'])

    def test_modal_wrong_resume_switches_once_then_closes(self):
        form=self._modal_resume_state('bbbb')
        diag={}; evidence={}
        self.assertTrue(app._choose_resume(form,'aaaa',diag,evidence,'123456789'))
        self.assertEqual(diag['selection_result'],'desired_resume_switched')
        self.assertEqual(diag['target_option_click_count'],1)
        self.assertEqual(self.page.evaluate('targetClicks'),1)
        self.assertTrue(self.page.locator('#portal').is_hidden())
        self.assertIsNotNone(app._reacquire_response_scope(self.page))

    def test_modal_chooser_that_cannot_close_fails_before_next_action(self):
        form=self._modal_resume_state('aaaa',close_on_escape=False)
        diag={}
        self.assertFalse(app._choose_resume(form,'aaaa',diag,{},'123456789'))
        self.assertEqual(diag['reason_code'],'resume_chooser_not_closed')
        self.assertEqual(diag['state_reason_code'],'chooser_not_closed')
        self.assertEqual(self.page.evaluate('[addClicks,submitClicks]'),[0,0])

    def test_scoped_resume_evidence_rejects_conflict_and_other_vacancy(self):
        self.page.set_content('<div id="scope"><div>Резюме для отклика</div><div role="button">Synthetic</div></div>')
        scope=self.page.locator('#scope'); evidence={}
        app._resume_evidence_record(evidence,'aaaa','111',self.page,'resume_chooser_exact_id')
        self.assertTrue(app._selected_resume(scope,'aaaa',evidence,'111'))
        self.assertFalse(app._selected_resume(scope,'aaaa',evidence,'222'))
        self.page.locator('#scope').evaluate("root=>root.insertAdjacentHTML('afterbegin','<input name=resumeId value=bbbb>')")
        self.assertFalse(app._selected_resume(scope,'aaaa',evidence,'111'))
        self.assertFalse(evidence['valid'])
        self.assertEqual(evidence['invalidated_reason'],'conflicting_resume_id')

    def test_cover_letter_existing_editor_skips_add(self):
        self.page.set_content('''<div role="dialog" id="response"><button data-qa="add-cover-letter" onclick="window.addClicks++">Добавить</button>
          <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div><script>window.addClicks=0</script>''')
        diag={}; scope,editor=app._ensure_cover_letter_editor(self.page,self.page.locator('#response'),'Letter',diag)
        self.assertEqual(diag['state'],'editor_already_visible')
        self.assertEqual(self.page.evaluate('addClicks'),0)
        self.assertEqual(editor.input_value(),'Letter')
        self.assertEqual(scope.get_attribute('id'),'response')

    def test_cover_letter_stays_inside_current_response_scope(self):
        self.page.set_content('''<textarea id="unrelated" data-qa="vacancy-response-popup-form-letter-input">Keep</textarea>
          <div role="dialog" id="response"><textarea id="letter" data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        diagnostics={}
        scope,editor=app._ensure_cover_letter_editor(
            self.page,self.page.locator('#response'),'Letter',diagnostics)
        self.assertEqual(scope.get_attribute('id'),'response')
        self.assertEqual(editor.get_attribute('id'),'letter')
        self.assertEqual(self.page.locator('#letter').input_value(),'Letter')
        self.assertEqual(self.page.locator('#unrelated').input_value(),'Keep')
        self.assertEqual([event['stage'] for event in diagnostics['events']],
                         ['fill_started','fill_completed','readback_started','readback_completed'])

    def test_cover_letter_reacquires_replaced_scope(self):
        self.page.set_content('''<div role="dialog" id="response"><button data-qa="add-cover-letter"
          onclick="this.parentElement.outerHTML='<div role=dialog id=response><textarea data-qa=vacancy-response-popup-form-letter-input></textarea><button type=submit data-qa=vacancy-response-submit-popup>Откликнуться</button></div>'">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        diag={}; scope,editor=app._ensure_cover_letter_editor(self.page,self.page.locator('#response'),'Letter',diag)
        self.assertEqual(diag['state'],'editor_opened')
        self.assertTrue(diag['response_scope_reacquired'])
        self.assertEqual(editor.input_value(),'Letter')
        self.assertTrue(scope.evaluate('(root)=>root.isConnected'))

    def test_cover_letter_editor_not_appeared_is_specific_failure(self):
        self.page.set_content('''<div role="dialog" id="response"><button data-qa="add-cover-letter">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>''')
        with self.assertRaisesRegex(ValueError,'cover_letter_editor_not_appeared'):
            app._ensure_cover_letter_editor(self.page,self.page.locator('#response'),'Letter',{})

    def _touch_regression_flow(self, conflicting_id=False):
        from playwright.sync_api import Route
        conflict = '<input type="hidden" name="resumeId" value="bbbb">' if conflicting_id else ''
        replacement = f'''<div role="dialog" id="response"><section><div>Резюме для отклика</div>
          <div role="button" onclick="document.getElementById('portal').hidden=false">Synthetic</div></section>{conflict}
          <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
          <button type="submit" data-qa="vacancy-response-submit-popup"
            onclick="fetch('/applicant/vacancy_response',{{method:'POST',body:'vacancyId=123456789&resumeId=aaaa'}}).then(()=>document.body.innerHTML='<p>Отклик отправлен</p>')">Откликнуться</button></div>'''
        replacement_json=json.dumps(replacement)
        html=f'''<meta charset="utf-8"><a data-qa="vacancy-response-link-top" onclick="document.getElementById('response').hidden=false">Откликнуться</a>
          <div role="dialog" id="response" hidden><section><div>Резюме для отклика</div>
          <div role="button" onclick="document.getElementById('portal').hidden=false">Synthetic</div></section>
          <button data-qa="add-cover-letter" onclick="replaceResponse()">Добавить</button>
          <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></div>
          <div role="dialog" data-qa="drop-base" id="portal" hidden><div role="listbox" data-qa="magritte-select-option-list">
          <label role="option" data-magritte-select-option data-qa="magritte-select-option-bbbb" aria-selected="false">Other<input type="radio"></label>
          <label role="option" data-magritte-select-option data-qa="magritte-select-option-aaaa" aria-selected="true">Target<input type="radio" checked></label>
          </div></div><script>function replaceResponse(){{document.getElementById('response').outerHTML={replacement_json}}}
          document.addEventListener('keydown',event=>{{if(event.key==='Escape')document.getElementById('portal').hidden=true}})</script>'''
        requests=[]
        def offline(route,**kwargs):
            if route.request.method == 'GET': route.fulfill(status=200,content_type='text/html',body=html)
            else: requests.append(route.request.post_data);route.fulfill(status=200,body='{}')
        with patch.object(Route,'continue_',offline),patch.object(app.config,'DRY_RUN',False):
            result=app.apply(self.page,test_safe_mvp.VAC,'Prepared letter',resume_id='aaaa',dry_run=False,before_submit=lambda:None)
        return result,requests

    def test_touch_transient_exact_id_survives_scope_replacement(self):
        result,requests=self._touch_regression_flow(False)
        self.assertEqual(result['application_status'],'applied_confirmed',result)
        self.assertEqual(result['resume_dropdown_diagnostics']['selection_result'],'desired_resume_already_selected')
        self.assertTrue(result['resume_dropdown_diagnostics']['chooser_closed'])
        self.assertEqual(result['cover_letter_diagnostics']['state'],'editor_opened')
        self.assertTrue(result['validation_predicates']['resume_matches_at_validation'])
        self.assertEqual(len(requests),1)

    def test_touch_later_conflicting_exact_id_invalidates_evidence(self):
        result,requests=self._touch_regression_flow(True)
        self.assertEqual(result['reason_code'],'required_fields_or_resume_invalid',result)
        self.assertEqual(result['validation_reason_code'],'resume_invalid_at_validation')
        self.assertFalse(result['resume_evidence_diagnostics']['valid'])
        self.assertEqual(result['resume_evidence_diagnostics']['invalidated_reason'],'conflicting_resume_id')
        self.assertEqual(requests,[])

    def test_real_resume_portal_exact_option_id(self):
        self._real_resume_portal(True)
        diag={}
        self.assertTrue(app._custom_resume_choose(self.page.locator('form'),'aaaa',diag))
        self.assertEqual(diag['after']['selected_ids'],['aaaa'])
        self.assertEqual(diag['reason_code'],'resume_verified')
        self.assertEqual(diag['trigger_diagnostics']['candidate_trigger_count'],1)
        self.assertEqual(diag['dom_snapshots'][-1]['exact_target_option_visible_count'],1)
        self.assertEqual(diag['dom_snapshots'][-1]['exact_target_option_location'],'page_portal')
        hit=diag['dom_snapshots'][0]['click_hit_test']
        self.assertEqual(hit['relation'],'trigger')
        self.assertGreater(hit['bounding_box']['width'],0)

    def test_real_resume_portal_click_without_changed_id_fails(self):
        self._real_resume_portal(False)
        diag={}
        self.assertFalse(app._custom_resume_choose(self.page.locator('form'),'aaaa',diag))
        self.assertEqual(diag['after']['selected_ids'],['bbbb'])
        self.assertEqual(diag['reason_code'],'resume_selection_not_changed')
