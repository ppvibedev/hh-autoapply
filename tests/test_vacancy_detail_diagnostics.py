"""Offline vacancy-detail diagnostics; no HH, LLM or private configuration."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_safe_mvp  # Load isolated config before project modules.
import config
import hh_web
import run_web


class _Leaf:
    def __init__(self, count=0, *, counts=None, text=""):
        self._count = count
        self._counts = list(counts) if counts is not None else None
        self._text = text
        self.first = self

    def count(self):
        if self._counts is not None:
            return self._counts.pop(0) if self._counts else self._count
        return self._count

    def inner_text(self):
        return self._text


class _DataQaElement:
    def __init__(self, identifier):
        self.identifier = identifier

    def get_attribute(self, name):
        if name != "data-qa":
            raise AssertionError(f"unexpected attribute read: {name}")
        return self.identifier

    def inner_text(self):
        raise AssertionError("diagnostic must not read visible text")

    def text_content(self):
        raise AssertionError("diagnostic must not read DOM text")

    def input_value(self):
        raise AssertionError("diagnostic must not read form values")


class _DataQaElements:
    def __init__(self, identifiers):
        self.identifiers = list(identifiers)

    def count(self):
        return len(self.identifiers)

    def nth(self, index):
        return _DataQaElement(self.identifiers[index])


class _Page:
    def __init__(self, *, description_counts=(0, 0, 0, 0),
                 url="https://hh.ru/vacancy/123456789?tracking=private#fragment",
                 status=200, title_count=1, captcha_count=0, data_qa=()):
        self.url = url
        self.status = status
        self.description = _Leaf(counts=description_counts, text="Synthetic description")
        self.title = _Leaf(title_count)
        self.captcha = _Leaf(captcha_count)
        self.response_control = _Leaf(1)
        self.modal = _Leaf(0)
        self.work_format = _Leaf(1, text="Remote")
        self.data_qa = _DataQaElements(data_qa)
        self.waits = []

    def goto(self, *_args, **_kwargs):
        return None if self.status is None else SimpleNamespace(status=self.status)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def locator(self, selector):
        mapping = {
            hh_web.SEL["vac_desc"]: self.description,
            hh_web.SEL["vac_title"]: self.title,
            '[data-qa*="captcha"]': self.captcha,
            hh_web.SEL["respond_btn"]: self.response_control,
            '[role="dialog"], [data-qa="modal"]': self.modal,
            "[data-qa]": self.data_qa,
            '[data-qa="vacancy-view-work-format"]': self.work_format,
        }
        if selector in mapping:
            return mapping[selector]
        raise AssertionError(f"unexpected selector: {selector}")


class VacancyDetailDiagnosticTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def vacancy(self, **changes):
        result = dict(test_safe_mvp.VAC, salary={"from": 200000, "to": None, "currency": "RUR"})
        result.update(changes)
        return result

    def missing(self, page, vacancy=None, *, stopped=None):
        diagnostics = {}
        with patch("hh_web._pause"):
            with self.assertRaisesRegex(ValueError, "vacancy_description_not_found"):
                hh_web.load_full(page, vacancy or self.vacancy(), diagnostics=diagnostics,
                                 stopped=stopped or (lambda: False))
        return diagnostics

    def test_normal_vacancy_does_not_create_diagnostic(self):
        diagnostics = {}
        page = _Page(description_counts=(1,))
        with patch("hh_web._pause"):
            result = hh_web.load_full(page, self.vacancy(), diagnostics=diagnostics)
        self.assertEqual(result["description"], "Synthetic description")
        self.assertEqual(diagnostics, {})
        self.assertEqual(page.waits, [])

    def test_missing_description_records_safe_paths_status_counts_and_markers(self):
        page = _Page(data_qa=("vacancy-title", "vacancy-title", "vacancy-response-link-top"))
        diagnostic = self.missing(page)
        self.assertEqual(diagnostic["requested_path"], "/vacancy/123456789")
        self.assertEqual(diagnostic["final_path"], "/vacancy/123456789")
        self.assertTrue(diagnostic["same_vacancy_path"])
        self.assertEqual(diagnostic["navigation_status"], 200)
        self.assertEqual(diagnostic["title_selector_count"], 1)
        self.assertEqual(diagnostic["description_selector_count"], 0)
        self.assertTrue(diagnostic["page_state_markers"]["vacancy_content_marker_present"])
        self.assertFalse(diagnostic["page_state_markers"]["login_marker_present"])
        self.assertEqual(diagnostic["limited_safe_data_qa_snapshot"], [
            {"data_qa": "vacancy-response-link-top", "count": 1},
            {"data_qa": "vacancy-title", "count": 2},
        ])

    def test_external_paths_and_absent_navigation_response_are_not_persisted(self):
        private_url = "https://private.example.test/secret/path?token=SECRET#fragment"
        page = _Page(url=private_url, status=None)
        diagnostic = self.missing(page, self.vacancy(url=private_url))
        self.assertEqual(diagnostic["requested_path"], "[external]")
        self.assertEqual(diagnostic["final_path"], "[external]")
        self.assertIsNone(diagnostic["navigation_status"])
        encoded = json.dumps(diagnostic)
        for forbidden in ("private.example.test", "secret/path", "token", "SECRET", "fragment"):
            self.assertNotIn(forbidden, encoded)

    def test_known_login_and_captcha_states_are_structural(self):
        login = self.missing(_Page(url="https://hh.ru/account/login?from=vacancy"))
        self.assertTrue(login["page_state_markers"]["login_marker_present"])
        captcha = self.missing(_Page(url="https://hh.ru/captcha?key=private", captcha_count=1))
        self.assertTrue(captcha["page_state_markers"]["captcha_marker_present"])
        self.assertEqual(captcha["page_state_markers"]["captcha_marker_count"], 1)
        self.assertNotIn("key=private", json.dumps(captcha))

    def test_data_qa_snapshot_is_names_only_deduplicated_bounded_and_private_free(self):
        identifiers = [f"marker-{index}" for index in range(90)] + [
            "marker-1", "private@example.test", "vacancy-123456789",
            "resume-option-1b02537dff0fa9736d0039ed1f394b6d45544d",
        ]
        diagnostic = self.missing(_Page(data_qa=identifiers))
        snapshot = diagnostic["limited_safe_data_qa_snapshot"]
        self.assertLessEqual(len(snapshot), 75)
        self.assertEqual(len({item["data_qa"] for item in snapshot}), len(snapshot))
        self.assertTrue(diagnostic["data_qa_snapshot_truncated"])
        encoded = json.dumps(snapshot)
        for forbidden in ("private@example.test", "123456789", "1b02537dff0fa9736d0039ed1f394b6d45544d",
                          "textContent", "innerHTML", "outerHTML", "input_value"):
            self.assertNotIn(forbidden, encoded)

    def test_delayed_description_is_observed_but_outcome_remains_failure(self):
        page = _Page(description_counts=(0, 0, 1))
        diagnostic = self.missing(page)
        self.assertTrue(diagnostic["description_appeared_during_diagnostic_wait"])
        self.assertEqual(diagnostic["description_appeared_after_ms"], 300)
        self.assertEqual(diagnostic["diagnostic_wait_elapsed_ms"], 300)
        self.assertEqual(page.waits, [100, 200])

    def test_description_that_never_appears_is_recorded_with_bounded_wait(self):
        page = _Page(description_counts=(0, 0, 0, 0))
        diagnostic = self.missing(page)
        self.assertFalse(diagnostic["description_appeared_during_diagnostic_wait"])
        self.assertIsNone(diagnostic["description_appeared_after_ms"])
        self.assertEqual(diagnostic["diagnostic_wait_elapsed_ms"], 600)
        self.assertEqual(page.waits, [100, 200, 300])

    def test_diagnostic_wait_honors_existing_stop_signal(self):
        page = _Page()
        diagnostic = self.missing(page, stopped=lambda: True)
        self.assertTrue(diagnostic["diagnostic_wait_stopped"])
        self.assertEqual(diagnostic["diagnostic_wait_elapsed_ms"], 0)
        self.assertEqual(page.waits, [])

    def test_consecutive_failure_counter_resets_after_success(self):
        browser = MagicMock()
        browser.__enter__.return_value = (None, MagicMock())
        vacancies = [dict(test_safe_mvp.VAC, id=str(identifier))
                     for identifier in (101, 102, 103, 104, 105)]
        failures = {"101", "102", "103", "105"}
        rows = []

        def load_full(_page, vacancy, *, diagnostics, stopped):
            if vacancy["id"] in failures:
                diagnostics.update(description_selector_count=0,
                                   requested_path=f'/vacancy/{vacancy["id"]}',
                                   final_path=f'/vacancy/{vacancy["id"]}')
                raise ValueError("vacancy_description_not_found")
            return vacancy

        with patch.object(config, "PAGES_PER_RUN", 1), \
             patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=browser), \
             patch("run_web._get_resume", return_value="Synthetic facts"), \
             patch("hh_web.search_page", return_value=vacancies), \
             patch("hh_web.load_full", side_effect=load_full), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", return_value=test_safe_mvp.RESULT), \
             patch("decide.cover_letter", return_value="Synthetic letter"), \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)

        diagnostics = [row for row in rows if row.get("event") == "vacancy_detail_diagnostic"]
        self.assertEqual([row["vacancy_id"] for row in diagnostics], ["101", "102", "103", "105"])
        self.assertEqual([row["consecutive_description_not_found"] for row in diagnostics],
                         [1, 2, 3, 1])
        self.assertTrue(all("Synthetic facts" not in json.dumps(row)
                            and "Synthetic letter" not in json.dumps(row)
                            for row in diagnostics))


if __name__ == "__main__":
    unittest.main()
