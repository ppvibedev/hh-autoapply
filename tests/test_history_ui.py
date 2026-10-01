"""Offline browser coverage for paginated, scrollable vacancy history."""
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


class HistoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.html = Path("web/index.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, records=None):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_init_script("window.setInterval=()=>0")
        self.run_id = "a" * 32
        self.records = records or [{
            "run_id": self.run_id, "vacancy_id": str(value), "vid": str(value),
            "decision": "needs_manual_review", "title": f"Vacancy {value}",
            "company": "Example", "url": f"https://hh.ru/vacancy/{value}",
            "score": value % 100, "reason_code": "fixture",
        } for value in range(135, 0, -1)]
        self.history_requests = []
        self.history_queries = []

        def route(request):
            parsed = urlparse(request.request.url)
            if request.request.resource_type == "document":
                request.fulfill(status=200, content_type="text/html", body=self.html)
            elif parsed.path == "/api/state":
                state = {
                    "running": True, "login_running": False, "dry_run": False, "mode": "LIVE",
                    "has_session": True, "config": {}, "resumes": [], "resumes_refreshing": False,
                    "run_id": self.run_id, "run_log": "", "counts": {"already_applied": 2},
                    "decisions": [], "last_run": None, "history_total": len(self.records),
                    "history_latest_vacancy_id": self.records[0]["vacancy_id"], "history_page_size": 75,
                }
                request.fulfill(status=200, content_type="application/json", body=json.dumps(state))
            elif parsed.path == "/api/history":
                query = parse_qs(parsed.query)
                cursor = query.get("cursor", [None])[0]
                limit = int(query.get("limit", ["75"])[0])
                status_filter = query.get("status", ["all"])[0]
                groups = {
                    "manual_review": {"needs_manual_review"},
                    "applied_confirmed": {"applied_confirmed"},
                    "already_applied": {"already_applied"},
                    "below_threshold": {"rejected_score"},
                    "filtered": {"rejected_hard_filter", "rejected_remote"},
                    "errors_unknown": {"apply_failed", "submission_unknown"},
                    "safe_ready": {"safe_ready"},
                }
                records = self.records if status_filter == "all" else [
                    row for row in self.records if row["decision"] in groups[status_filter]]
                start = 0 if cursor is None else next(
                    index + 1 for index, row in enumerate(records) if row["vacancy_id"] == cursor)
                items = records[start:start + limit]
                more = start + len(items) < len(records)
                self.history_requests.append(cursor)
                self.history_queries.append((status_filter, cursor))
                payload = {"run_id": self.run_id, "items": items, "total": len(records),
                           "filter": status_filter,
                           "order": "newest_first",
                           "next_cursor": items[-1]["vacancy_id"] if more and items else None}
                request.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            elif parsed.path in ("/api/areas", "/api/llm-connections"):
                body = '{"regions":[]}' if parsed.path == "/api/areas" else '{"connections":[]}'
                request.fulfill(status=200, content_type="application/json", body=body)
            else:
                request.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        context.route("**/*", route)
        page = context.new_page()
        page.goto("http://offline.test/")
        page.wait_for_function(f"historyItems.length === {min(len(self.records),75)}")
        return page

    def test_history_scroll_loads_every_card_once_and_keeps_compact_labels(self):
        page = self.page()
        feed = page.locator("#feed")
        style = feed.evaluate("node => ({overflowY:getComputedStyle(node).overflowY,maxHeight:getComputedStyle(node).maxHeight})")
        self.assertEqual(style["overflowY"], "auto")
        self.assertNotEqual(style["maxHeight"], "none")
        self.assertIn("Уже откликались", page.locator("#stats").inner_text())
        self.assertIn("Ошибка отправки", page.locator("#stats").inner_text())
        self.assertIn("Готово в SAFE", page.locator("#stats").inner_text())
        self.assertEqual(page.locator('.stat[title="Возможно отправлено — требуется проверка"]').count(), 1)

        feed.evaluate("node => { node.scrollTop=node.scrollHeight; node.dispatchEvent(new Event('scroll')); }")
        page.wait_for_function("historyItems.length === 135")
        ids = page.locator("#feed .item").evaluate_all(
            "nodes => nodes.map(node => node.dataset.vacancyId)")
        self.assertEqual(ids, [str(value) for value in range(135, 0, -1)])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(page.locator('#feed a[href="https://hh.ru/vacancy/135"]').count(), 1)
        self.assertEqual(self.history_requests, [None, "61"])

        feed.evaluate("node => { node.scrollTop=node.scrollHeight; node.dispatchEvent(new Event('scroll')); }")
        page.wait_for_timeout(100)
        self.assertEqual(self.history_requests, [None, "61"])

    def test_active_run_newest_page_adds_new_record_without_resetting_loaded_history(self):
        page = self.page()
        page.locator("#feed").evaluate(
            "node => { node.scrollTop=node.scrollHeight; node.dispatchEvent(new Event('scroll')); }")
        page.wait_for_function("historyItems.length === 135")
        self.records.insert(0, {
            "run_id": self.run_id, "vacancy_id": "136", "vid": "136",
            "decision": "applied_confirmed", "title": "Vacancy 136", "company": "Example",
            "url": "https://hh.ru/vacancy/136", "score": 90, "reason_code": "confirmed",
        })
        page.evaluate("refresh()")
        page.wait_for_function("historyItems.length === 136")
        ids = page.locator("#feed .item").evaluate_all(
            "nodes => nodes.map(node => node.dataset.vacancyId)")
        self.assertEqual(ids[0], "136")
        self.assertEqual(ids[-1], "1")
        self.assertEqual(len(ids), len(set(ids)))

    def test_filter_switches_server_sequence_and_restores_all(self):
        decisions = ["needs_manual_review", "rejected_score", "needs_manual_review",
                     "applied_confirmed", "already_applied", "apply_failed"]
        records = [{
            "run_id": "a" * 32, "vacancy_id": str(6-index), "vid": str(6-index),
            "decision": decision, "title": f"Vacancy {6-index}", "company": "Example",
            "url": f"https://hh.ru/vacancy/{6-index}",
        } for index,decision in enumerate(decisions)]
        page = self.page(records)
        page.get_by_role("button", name="Проверить вручную", exact=True).click()
        page.wait_for_function(
            "historyFilter==='manual_review' && !historyLoading && historyItems.length===2")
        self.assertEqual(page.locator("#feed .item").count(), 2)
        self.assertTrue(all(item["decision"] == "needs_manual_review"
                            for item in page.evaluate("historyItems")))
        self.assertEqual(self.history_queries[-1][0], "manual_review")

        page.get_by_role("button", name="Все", exact=True).click()
        page.wait_for_function("historyFilter==='all' && !historyLoading && historyItems.length===6")
        self.assertEqual(self.history_queries[-1][0], "all")

    def test_stale_old_filter_response_cannot_pollute_new_filter(self):
        page = self.page()
        page.evaluate("""() => {
          window.__pendingHistory = [];
          window.fetch = url => new Promise(resolve => window.__pendingHistory.push({url,resolve}));
          setHistoryFilter('manual_review');
          setHistoryFilter('applied_confirmed');
        }""")
        page.wait_for_function("window.__pendingHistory.length===2")
        page.evaluate("""() => window.__pendingHistory[1].resolve({ok:true,json:async()=>({
          run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',filter:'applied_confirmed',total:1,
          items:[{vacancy_id:'900',decision:'applied_confirmed',title:'Confirmed'}],
          next_cursor:null,order:'newest_first'})})""")
        page.wait_for_function(
            "!historyLoading && historyItems.length===1 && historyItems[0].vacancy_id==='900'")
        page.evaluate("""() => window.__pendingHistory[0].resolve({ok:true,json:async()=>({
          run_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',filter:'manual_review',total:1,
          items:[{vacancy_id:'901',decision:'needs_manual_review',title:'Stale'}],
          next_cursor:null,order:'newest_first'})})""")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("historyItems.map(item=>item.vacancy_id)"), ["900"])

    def test_active_run_adds_only_matching_cards_until_all_is_restored(self):
        records = [
            {"run_id":"a"*32,"vacancy_id":"3","decision":"needs_manual_review","title":"M3"},
            {"run_id":"a"*32,"vacancy_id":"2","decision":"rejected_score","title":"R2"},
            {"run_id":"a"*32,"vacancy_id":"1","decision":"needs_manual_review","title":"M1"},
        ]
        page = self.page(records)
        page.get_by_role("button", name="Проверить вручную", exact=True).click()
        page.wait_for_function("!historyLoading && historyItems.length===2")

        self.records.insert(0, {"run_id":"a"*32,"vacancy_id":"4",
                                "decision":"applied_confirmed","title":"A4"})
        page.evaluate("refresh()")
        page.wait_for_function("historySourceLatestVacancyId==='4' && !historyLoading")
        self.assertNotIn("4", page.evaluate("historyItems.map(item=>item.vacancy_id)"))

        self.records.insert(0, {"run_id":"a"*32,"vacancy_id":"5",
                                "decision":"needs_manual_review","title":"M5"})
        page.evaluate("refresh()")
        page.wait_for_function("historyItems.some(item=>item.vacancy_id==='5') && !historyLoading")
        self.assertEqual(page.evaluate("historyItems[0].vacancy_id"), "5")

        page.get_by_role("button", name="Все", exact=True).click()
        page.wait_for_function("historyFilter==='all' && !historyLoading && historyItems.length===5")
        self.assertIn("4", page.evaluate("historyItems.map(item=>item.vacancy_id)"))


if __name__ == "__main__":
    unittest.main()
