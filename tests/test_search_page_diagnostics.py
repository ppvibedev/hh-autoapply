"""Offline structural search-page diagnostics; no real HH or candidate data."""
from types import SimpleNamespace
import unittest

import test_safe_mvp  # Load the isolated test config before project modules.
import hh_web


class _Leaf:
    def __init__(self, count=0, text="", href=None):
        self._count = count
        self._text = text
        self._href = href
        self.first = self

    def count(self):
        return self._count

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._href if name == "href" else None


class _Card:
    def __init__(self, *, parsed, vacancy_id):
        self.title = _Leaf(1 if parsed else 0, "Synthetic vacancy",
                           f"https://hh.ru/vacancy/{vacancy_id}?from=test")
        self.employer = _Leaf(1, "Synthetic employer")
        self.salary = _Leaf()

    def locator(self, selector):
        return {
            hh_web.SEL["card_title"]: self.title,
            hh_web.SEL["card_employer"]: self.employer,
            hh_web.SEL["card_salary"]: self.salary,
        }[selector]


class _Cards:
    def __init__(self, cards):
        self.cards = cards

    def count(self):
        return len(self.cards)

    def nth(self, index):
        return self.cards[index]


class _Page:
    def __init__(self, cards, *, url="https://hh.ru/search/vacancy?page=0", status=200,
                 captcha=False):
        self.url = url
        self.cards = _Cards(cards)
        self.status = status
        self.captcha = captcha

    def goto(self, *_args, **_kwargs):
        return SimpleNamespace(status=self.status)

    def locator(self, selector):
        if selector == hh_web.SEL["card"]:
            return self.cards
        if selector == '[data-qa*="captcha"]':
            return _Leaf(1 if self.captcha else 0)
        raise AssertionError(f"unexpected selector: {selector}")


class SearchPageDiagnosticTests(unittest.TestCase):
    def diagnostic(self, **changes):
        values = dict(
            requested_path="/search/vacancy",
            final_url="https://hh.ru/search/vacancy?page=0",
            navigation_http_status=200,
            raw_card_count=0,
            parsed_vacancy_count=0,
            captcha_marker_present=False,
        )
        values.update(changes)
        return hh_web._search_page_diagnostic(**values)

    def test_supported_page_outcomes_and_redirect(self):
        cases = (
            (dict(raw_card_count=3, parsed_vacancy_count=2), "vacancies_parsed"),
            (dict(raw_card_count=3), "raw_cards_parse_failed"),
            (dict(), "empty_unclassified"),
            (dict(final_url="https://hh.ru/account/login"), "login_page_detected"),
            (dict(captcha_marker_present=True), "captcha_detected"),
            (dict(final_url="https://hh.ru/captcha"), "captcha_detected"),
            (dict(navigation_http_status=503), "hh_error_detected"),
        )
        for changes, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.diagnostic(**changes)["outcome"], expected)
        redirected = self.diagnostic(final_url="https://hh.ru/account/login?token=not-persisted")
        self.assertTrue(redirected["redirect_observed"])
        self.assertEqual(redirected["final_path"], "/account/login")
        self.assertNotIn("token", str(redirected))

    def test_search_page_measures_raw_and_parsed_counts_separately(self):
        page = _Page([_Card(parsed=True, vacancy_id="123"),
                      _Card(parsed=False, vacancy_id="456")])
        diagnostics = {}
        original_pause = hh_web._pause
        try:
            hh_web._pause = lambda: None
            vacancies = hh_web.search_page(
                page, text="Project Manager", area="", salary_min=0,
                work_format_codes=["REMOTE"], page_num=0, diagnostics=diagnostics)
        finally:
            hh_web._pause = original_pause
        self.assertEqual(len(vacancies), 1)
        self.assertEqual(diagnostics["raw_card_count"], 2)
        self.assertEqual(diagnostics["parsed_vacancy_count"], 1)
        self.assertEqual(diagnostics["outcome"], "vacancies_parsed")
        self.assertEqual(diagnostics["empty_results_marker"], "unknown")
        self.assertEqual(diagnostics["next_page_marker"], "unknown")


if __name__ == "__main__":
    unittest.main()
