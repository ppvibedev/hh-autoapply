"""Offline browser checks for Stop UI; all HTTP is locally fulfilled."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


class StopUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.html = Path("web/index.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_init_script("window.setInterval=()=>0")
        page = context.new_page()
        initial_state = {
            "running": True, "login_running": False, "dry_run": False, "mode": "LIVE",
            "has_session": True, "config": {}, "resumes": [], "resumes_refreshing": False,
            "run_id": "a" * 32, "run_log": "", "counts": {}, "decisions": [], "last_run": None,
        }

        def route(request):
            path = urlparse(request.request.url).path
            if request.request.resource_type == "document":
                request.fulfill(status=200, content_type="text/html", body=self.html)
            elif path == "/api/state":
                request.fulfill(status=200, content_type="application/json", body=json.dumps(initial_state))
            elif path == "/api/areas":
                request.fulfill(status=200, content_type="application/json", body='{"regions":[]}')
            elif path == "/api/llm-connections":
                request.fulfill(status=200, content_type="application/json", body='{"connections":[]}')
            else:
                request.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        context.route("**/*", route)
        page.goto("http://offline.test/")
        page.wait_for_function("lastRunRunning === true")
        page.evaluate("""state => {
          window.__stopRequests=[];
          window.__stopPending=[];
          window.__running=true;
          window.fetch=(url,options={})=>{
            const path=new URL(String(url),location.href).pathname;
            if(path==='/api/stop'){
              window.__stopRequests.push({method:options.method,body:JSON.parse(options.body)});
              return new Promise((resolve,reject)=>window.__stopPending.push({resolve,reject}));
            }
            if(path==='/api/state'){
              return Promise.resolve(new Response(JSON.stringify({...state,running:window.__running}),
                {status:200,headers:{'Content-Type':'application/json'}}));
            }
            return Promise.resolve(new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}}));
          };
        }""", initial_state)
        return page

    def test_both_buttons_share_handler_and_each_posts_once(self):
        for selector in ("#runBannerStopBtn", "#stopBtn"):
            with self.subTest(selector=selector):
                page = self.page()
                self.assertEqual(page.get_attribute(selector, "onclick"), "stop()")
                page.click(selector)
                self.assertEqual(page.evaluate("__stopRequests.length"), 1)
                request = page.evaluate("__stopRequests[0]")
                self.assertEqual(request["method"], "POST")
                self.assertRegex(request["body"]["stop_attempt_id"], r"^[A-Za-z0-9_-]{8,64}$")

    def test_pending_is_immediate_persistent_and_idempotent(self):
        page = self.page()
        page.click("#stopBtn")
        self.assertEqual(page.locator("#runState").inner_text(), "Остановка запрошена; ожидаю завершения…")
        self.assertTrue(page.locator("#stopBtn").is_disabled())
        self.assertTrue(page.locator("#runBannerStopBtn").is_disabled())
        page.evaluate("stop()")
        self.assertEqual(page.evaluate("__stopRequests.length"), 1)

        page.evaluate("""() => __stopPending[0].resolve(new Response(JSON.stringify({
          ok:true,status:'in_memory_stop_requested',delivery_accepted:true,telemetry_recorded:true
        }),{status:200,headers:{'Content-Type':'application/json'}}))""")
        page.wait_for_function("stopUiState === 'accepted'")
        self.assertEqual(page.locator("#runState").inner_text(), "Остановка запрошена; ожидаю завершения…")
        page.evaluate("window.__running=false; refresh()")
        page.wait_for_function("stopUiState === 'idle'")

    def test_network_failure_is_persistent_and_allows_new_attempt(self):
        page = self.page()
        page.click("#stopBtn")
        first_id = page.evaluate("__stopRequests[0].body.stop_attempt_id")
        page.evaluate("() => __stopPending[0].reject(new TypeError('offline failure'))")
        page.wait_for_function("stopUiState === 'failed'")
        self.assertIn("не доставлен", page.locator("#runState").inner_text())
        self.assertFalse(page.locator("#stopBtn").is_disabled())
        self.assertFalse(page.locator("#runBannerStopBtn").is_disabled())
        page.click("#runBannerStopBtn")
        self.assertEqual(page.evaluate("__stopRequests.length"), 2)
        self.assertNotEqual(first_id, page.evaluate("__stopRequests[1].body.stop_attempt_id"))

    def test_non_2xx_is_persistent_delivery_failure(self):
        page = self.page()
        page.click("#runBannerStopBtn")
        page.evaluate("""() => __stopPending[0].resolve(new Response(
          JSON.stringify({ok:false,error:'delivery rejected'}),
          {status:409,headers:{'Content-Type':'application/json'}}))""")
        page.wait_for_function("stopUiState === 'failed'")
        self.assertIn("прогон может продолжаться", page.locator("#runState").inner_text())
        self.assertFalse(page.locator("#stopBtn").is_disabled())


if __name__ == "__main__":
    unittest.main()
