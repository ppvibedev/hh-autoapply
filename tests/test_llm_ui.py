"""Existing UI against Flask test client; browser traffic intercepted locally."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
import test_safe_mvp
import application_profile
import webapp
import llm_connections as store


class ConnectionsUITests(unittest.TestCase):
    def test_add_disable_reload_and_no_secrets_in_public_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(store,'STORAGE_PATH',Path(tmp).resolve()/'connections.json'), patch.object(application_profile,'STORAGE_PATH',Path(tmp).resolve()/'application_profile.json'), patch('webapp.envfile.read_env',return_value={'LLM_PROVIDER':'gemini','LLM_MODEL':'model-x','LLM_API_KEY':'SECRET_A'}), patch('webapp.envfile.update_env'), patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')), patch('llm_provider.execute',return_value=('CONNECTED',None)) as connection_test:
            client=webapp.app.test_client()
            html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text()
            state=dict(mode='SAFE',has_session=False,running=False,login_running=False,last_run=None,counts={},resumes=[],config={},decisions=[],run_log='')
            public_responses=[]; forbidden_requests=[]
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                try:
                    page=browser.new_page(viewport={'width':1280,'height':1000})
                    errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
                    def route(request):
                        path=request.request.url.removeprefix('http://llm-ui.test')
                        if path=='/': request.fulfill(body=html,content_type='text/html')
                        elif path.startswith('/api/llm-connections'):
                            result=client.open(path,method=request.request.method,json=request.request.post_data_json if request.request.method!='GET' else None)
                            body=result.get_data(as_text=True); public_responses.append(body)
                            request.fulfill(status=result.status_code,body=body,content_type='application/json')
                        elif path=='/api/application-profile':
                            result=client.get(path); request.fulfill(status=result.status_code,
                                body=result.get_data(as_text=True),content_type='application/json')
                        elif path=='/api/config':
                            result=client.post(path,json=request.request.post_data_json)
                            request.fulfill(status=result.status_code,body=result.get_data(as_text=True),
                                            content_type='application/json')
                        elif path=='/api/state': request.fulfill(json=state)
                        elif path=='/api/areas': request.fulfill(json={'regions':[]})
                        else: forbidden_requests.append(path); request.abort()
                    page.route('**/*',route)
                    page.goto('http://llm-ui.test/')
                    page.get_by_text('1. gemini / model-x',exact=False).wait_for()
                    self.assertEqual(page.locator('#APPLICATION_TELEGRAM').input_value(),'')
                    page.get_by_role('button',name='Добавить подключение',exact=True).click()
                    page.locator('#connectionModel').fill('model-x')
                    page.locator('#connectionCredential').fill('SECRET_B')
                    page.get_by_role('button',name='Сохранить подключение',exact=True).click()
                    page.get_by_text('2. gemini / model-x',exact=False).wait_for()
                    self.assertEqual(page.locator('#connectionCredential').input_value(),'')

                    page.get_by_role('button',name='Добавить подключение',exact=True).click()
                    page.locator('#connectionProvider').select_option('groq')
                    self.assertEqual(page.locator('#connectionModel').get_attribute('placeholder'),'openai/gpt-oss-120b')
                    page.locator('#connectionCredential').fill('SECRET_C')
                    page.get_by_role('button',name='Сохранить подключение',exact=True).click()
                    page.get_by_text('3. groq / openai/gpt-oss-120b',exact=False).wait_for()
                    self.assertEqual(page.locator('#connectionCredential').input_value(),'')
                    page.get_by_role('button',name='Проверить подключение 3',exact=True).click()
                    page.get_by_text('CONNECTED',exact=True).wait_for()

                    page.get_by_role('button',name='Добавить подключение',exact=True).click()
                    page.locator('#connectionProvider').select_option('zai')
                    self.assertEqual(page.locator('#connectionModel').get_attribute('placeholder'),'glm-4.7-flash')
                    page.locator('#connectionCredential').fill('SECRET_D')
                    page.get_by_role('button',name='Сохранить подключение',exact=True).click()
                    page.get_by_text('4. zai / glm-4.7-flash',exact=False).wait_for()
                    self.assertEqual(page.locator('#connectionCredential').input_value(),'')

                    page.get_by_role('checkbox',name='Включить подключение 2',exact=True).uncheck()
                    page.wait_for_function("!document.querySelector('[aria-label=\"Включить подключение 2\"]').disabled")
                    page.reload()
                    page.get_by_text('2. gemini / model-x',exact=False).wait_for()
                    self.assertFalse(page.get_by_role('checkbox',name='Включить подключение 2',exact=True).is_checked())
                    self.assertFalse(store.load_connections()[1].enabled)
                    self.assertEqual(page.locator('#connectionCredential').input_value(),'')
                    public=page.content()+json.dumps(public_responses)+str(errors)
                    for secret in ('SECRET_A','SECRET_B','SECRET_C','SECRET_D'):
                        self.assertNotIn(secret,public)
                    tested_connection,test_prompt,test_limit=connection_test.call_args.args
                    self.assertEqual((tested_connection.provider,test_limit),('groq',128))
                    self.assertEqual(test_prompt,'Reply with exactly the single word CONNECTED.')
                    self.assertEqual(errors,[]); self.assertEqual(forbidden_requests,[])
                    page.locator('#llmConnections').scroll_into_view_if_needed()
                    if os.environ.get('HH_LLM_UI_SCREENSHOT'):
                        page.screenshot(path=os.environ['HH_LLM_UI_SCREENSHOT'])
                finally: browser.close()
