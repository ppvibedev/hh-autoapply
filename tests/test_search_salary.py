"""Only mocked browser calls; no HH, LLM or private configuration."""
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, parse_qs
import test_safe_mvp
import hh_web
import decide
import config

class SearchSalaryTests(unittest.TestCase):
    def query(self, area='', salary=200000):
        page=MagicMock()
        page.locator.return_value.count.return_value=0
        with patch('hh_web._pause'):
            hh_web.search_page(page,text='Project Manager',area=area,salary_min=salary,
                               work_format_codes=['REMOTE'],page_num=0)
        return parse_qs(urlparse(page.goto.call_args.args[0]).query)

    def test_worldwide_rubles_and_optional_salary(self):
        params=self.query()
        self.assertNotIn('area',params)
        self.assertEqual(params['salary'],['200000'])
        self.assertEqual(params['currency'],['RUR'])
        self.assertNotIn('only_with_salary',params)
        self.assertEqual(params['work_format'],['REMOTE'])
        self.assertEqual(self.query(area='2')['area'],['2'])
        self.assertNotIn('currency',self.query(salary=0))

    def test_hard_salary_does_not_compare_foreign_or_unknown_currency(self):
        with patch.multiple(config,SALARY_MIN=200000,STOPWORDS=[],BLACKLIST_EMPLOYERS=[]):
            for text in ('3 000 – 4 000 USD','3 000 – 4 000 EUR','от 150 000 ₽','150 000',''):
                self.assertIsNone(decide.hard_filters({'salary':hh_web._parse_salary(text)}),text)
            for text in ('100 000 – 150 000 ₽','до 150 000 RUB'):
                self.assertIsNotNone(decide.hard_filters({'salary':hh_web._parse_salary(text)}),text)
            self.assertIsNone(decide.hard_filters({'salary':{'from':3000,'to':4000,'currency':'USD'}}))
