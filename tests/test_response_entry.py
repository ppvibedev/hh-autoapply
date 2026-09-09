"""Repeated response links from observed HH layout; no browser/network."""
import unittest
from unittest.mock import MagicMock
import test_safe_mvp
import application

class ResponseEntryTests(unittest.TestCase):
    def page(self):
        page=MagicMock(); page.url='https://hh.ru/vacancy/136785768'
        links=[MagicMock() for _ in range(3)]
        page.locator.return_value.count.return_value=3
        page.locator.return_value.nth.side_effect=lambda i:links[i]
        for i,el in enumerate(links):
            el.is_visible.return_value=i<2
            el.is_enabled.return_value=True
            el.inner_text.return_value='Откликнуться'
            el.get_attribute.side_effect=lambda key: '/applicant/vacancy_response?vacancyId=136785768' if key=='href' else None
        return page,links

    def test_two_visible_and_one_hidden_choose_first_equivalent(self):
        page,links=self.page()
        self.assertIs(application._response_entry(page,'136785768'),links[0])
        for el in links: el.click.assert_not_called()

    def test_disabled_first_choose_second(self):
        page,links=self.page(); links[0].is_enabled.return_value=False
        self.assertIs(application._response_entry(page,'136785768'),links[1])

    def test_different_targets_are_not_assumed_equivalent(self):
        page,links=self.page()
        links[1].get_attribute.side_effect=lambda key:'/applicant/vacancy_response?vacancyId=999' if key=='href' else None
        self.assertIsNone(application._response_entry(page,'136785768'))

    def test_no_visible_entry_does_not_click(self):
        page,links=self.page()
        for el in links: el.is_visible.return_value=False
        self.assertIsNone(application._response_entry(page,'136785768'))
