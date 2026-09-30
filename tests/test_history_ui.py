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

    def page(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_init_script("window.setInterval=()=>0")
        self.run_id = "a" * 32
        self.records = [{
            "run_id": self.run_id, "vacancy_id": str(value), "vid": str(value),
            "decision": "needs_manual_review", "title": f"Vacancy {value}",
            "company": "Example", "url": f"https://hh.ru/vacancy/{value}",
            "score": value % 100, "reason_code": "fixture",
        } for value in range(135, 0, -1)]
        self.history_requests = []

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
                start = 0 if cursor is None else next(
                    index + 1 for index, row in enumerate(self.records) if row["vacancy_id"] == cursor)
                items = self.records[start:start + limit]
                more = start + len(items) < len(self.records)
                self.history_requests.append(cursor)
                payload = {"run_id": self.run_id, "items": items, "total": len(self.records),
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
        page.wait_for_function("historyItems.length === 75")
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


if __name__ == "__main__":
    unittest.main()
