"""Regression tests for documented CURRENT behavior; no HH or external LLM."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import test_safe_mvp
import config
import decide
import question_sources
import run_web


class CurrentProductSemanticsTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def _run(self, vacancies, matching_results, *, pages=1, search=None):
        browser = MagicMock()
        browser.__enter__.return_value = (None, MagicMock())
        rows = []
        results = iter(matching_results)
        search_mock = search or MagicMock(return_value=vacancies)
        with patch.object(config, "PAGES_PER_RUN", pages), \
             patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=browser), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("hh_web.search_page", search_mock), \
             patch("hh_web.load_full", side_effect=lambda _page, vacancy, **_kw: vacancy), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", side_effect=lambda *_a, **_kw: next(results)), \
             patch("decide.cover_letter", return_value="Synthetic letter") as letter, \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)
        return rows, letter

    def test_stopwords_apply_only_to_title_case_insensitive_substring_or(self):
        with patch.dict(os.environ, {"STOPWORDS": "JUNIOR, продажи"}):
            words = config._list("STOPWORDS")
        with patch.object(config, "STOPWORDS", words), \
             patch.object(config, "BLACKLIST_EMPLOYERS", []), \
             patch.object(config, "SALARY_MIN", 0):
            self.assertEqual(decide.hard_filters({"name": "Junior Project Manager"}),
                             "stopword:junior")
            self.assertEqual(decide.hard_filters({"name": "Руководитель отдела продажи рекламы"}),
                             "stopword:продажи")
            self.assertIsNone(decide.hard_filters({"name": "Менеджер по продажам"}))
            self.assertEqual(decide.hard_filters({
                "name": "Project Manager",
                "description": "В подчинении Junior-разработчики и продажи",
            }), None)

    def test_employer_blacklist_is_case_insensitive_substring_or(self):
        with patch.dict(os.environ, {"BLACKLIST_EMPLOYERS": "ACME, Ромашка"}):
            blocked = config._list("BLACKLIST_EMPLOYERS")
        with patch.object(config, "STOPWORDS", []), \
             patch.object(config, "BLACKLIST_EMPLOYERS", blocked), \
             patch.object(config, "SALARY_MIN", 0):
            self.assertEqual(decide.hard_filters({
                "name": "Project Manager", "employer": {"name": "ACME Digital"},
            }), "blacklist:acme")
            self.assertEqual(decide.hard_filters({
                "name": "Project Manager", "employer": {"name": "ООО РОМАШКА Групп"},
            }), "blacklist:ромашка")
            self.assertIsNone(decide.hard_filters({
                "name": "Project Manager", "employer": {"name": "Example"},
                "description": "Проект для ACME",
            }))

    def test_definite_remote_rejection_then_score_then_remote_uncertainty(self):
        cases = (
            ("unclear low mismatch", "unclear", 10, "functional_mismatch",
             "rejected_score", "score_rejected"),
            ("unclear low aligned", "unclear", 10, "aligned",
             "rejected_score", "score_rejected"),
            ("ineligible low", "ineligible", 10, "aligned",
             "rejected_remote", "hard_rejected"),
            ("ineligible high", "ineligible", 90, "aligned",
             "rejected_remote", "hard_rejected"),
            ("unclear high", "unclear", 90, "aligned",
             "needs_manual_review", "manual_review"),
        )
        for name, remote, score, role, expected, counter in cases:
            with self.subTest(name=name):
                result = dict(test_safe_mvp.RESULT, score=score, capability_score=score,
                              remote_status=remote, remote_reason="Remote format result",
                              role_alignment=role)
                rows, letter = self._run([dict(test_safe_mvp.VAC)], [result])
                outcome = next(row for row in rows if row.get("vacancy_id"))
                final = next(row for row in rows if row.get("event") == "run_finished")
                self.assertEqual(outcome["decision"], expected)
                self.assertEqual(final["counts"][counter], 1)
                self.assertEqual(final["counts"]["score_rejected"],
                                 1 if expected == "rejected_score" else 0)
                self.assertEqual(final["counts"]["manual_review"],
                                 1 if expected == "needs_manual_review" else 0)
                letter.assert_not_called()

    def test_configured_page_budget_finishes_with_pages_exhausted(self):
        pages = [[dict(test_safe_mvp.VAC, id="101")],
                 [dict(test_safe_mvp.VAC, id="102")]]
        search = MagicMock(side_effect=pages)
        low = dict(test_safe_mvp.RESULT, score=10, capability_score=10)
        rows, _ = self._run([], [low, low], pages=2, search=search)
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["stop_reason"], "pages_exhausted")

    def test_raw_cards_without_parsed_vacancies_use_current_unclassified_terminal_reason(self):
        def search(_page, *, diagnostics, **_kwargs):
            diagnostics.update(raw_card_count=3, parsed_vacancy_count=0,
                               outcome="raw_cards_parse_failed")
            return []
        rows, _ = self._run([], [], pages=3, search=MagicMock(side_effect=search))
        diagnostic = next(row for row in rows if row.get("event") == "search_page_diagnostic")
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual(diagnostic["outcome"], "raw_cards_parse_failed")
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["stop_reason"], "no_results_or_page_not_recognized")

    def test_normal_resume_refresh_removes_telegram_cache_fallback(self):
        private = self.root / "private"
        private.mkdir()
        question = {"question": "Как найти вас в Telegram?", "type": "text",
                    "required": True, "options": []}
        with patch.object(question_sources, "PRIVATE", private), \
             patch("application_profile.telegram", return_value=""), \
             patch("hh_web.load_resume_text", return_value=(
                 "Project delivery experience\nTelegram: @synthetic_user")):
            refreshed = run_web._get_resume(None, "aaaa")
            self.assertNotIn("@synthetic_user", refreshed)
            with self.assertRaisesRegex(ValueError, "contact_answer_unavailable"):
                question_sources.resolve(question, "aaaa")

    def test_search_login_redirect_keeps_current_unclassified_run_terminal_reason(self):
        def search(_page, *, diagnostics, **_kwargs):
            diagnostics.update(raw_card_count=0, parsed_vacancy_count=0,
                               final_path="/account/login", redirect_observed=True,
                               login_marker_present=True, outcome="login_page_detected")
            return []
        rows, _ = self._run([], [], search=MagicMock(side_effect=search))
        diagnostic = next(row for row in rows if row.get("event") == "search_page_diagnostic")
        final = next(row for row in rows if row.get("event") == "run_finished")
        self.assertEqual(diagnostic["outcome"], "login_page_detected")
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["stop_reason"], "no_results_or_page_not_recognized")

    def test_description_failure_without_captcha_remains_vacancy_local(self):
        browser = MagicMock()
        browser.__enter__.return_value = (None, MagicMock())
        first = dict(test_safe_mvp.VAC, id="101")
        second = dict(test_safe_mvp.VAC, id="102")
        rows = []

        def load(_page, vacancy, *, diagnostics, **_kwargs):
            if vacancy["id"] == "101":
                diagnostics.update(page_state_markers={"captcha_marker_present": False})
                raise ValueError("vacancy_description_not_found")
            return vacancy

        low = dict(test_safe_mvp.RESULT, score=10, capability_score=10)
        with patch("run_web.has_session", return_value=True), \
             patch("run_web._RunControl"), \
             patch("run_web.browser_context", return_value=browser), \
             patch("run_web._get_resume", return_value="Synthetic resume facts"), \
             patch("hh_web.search_page", return_value=[first, second]), \
             patch("hh_web.load_full", side_effect=load), \
             patch("decide.hard_filters", return_value=None), \
             patch("decide.score", return_value=low), \
             patch("run_web._log", side_effect=rows.append):
            self.assertEqual(run_web.main(), 0)
        outcomes = [row for row in rows if row.get("vacancy_id") and not row.get("event")]
        self.assertEqual([(row["vacancy_id"], row["decision"]) for row in outcomes],
                         [("101", "needs_manual_review"), ("102", "rejected_score")])
        diagnostic = next(row for row in rows if row.get("event") == "vacancy_detail_diagnostic")
        self.assertFalse(diagnostic["page_state_markers"]["captcha_marker_present"])


if __name__ == "__main__":
    unittest.main()
