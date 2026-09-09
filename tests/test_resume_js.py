"""Regression: Python multiline literal must not produce multiline JS quotes."""
import ast
from pathlib import Path
import re
import unittest

class ResumeJavaScriptTests(unittest.TestCase):
    def test_evaluate_has_no_raw_newlines_inside_quoted_strings(self):
        # Inspect the actual runtime string without importing config or opening HH.
        source = Path(__file__).resolve().parents[1] / 'hh_web.py'
        tree = ast.parse(source.read_text())
        loader = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'load_resume_text')
        call = next(n for n in ast.walk(loader) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == 'evaluate')
        script = ast.literal_eval(call.args[0])
        # All current JavaScript literals are single-quoted. Check CSS selector too.
        literals = re.findall(r"'(?:\\.|[^'\\])*'", script)
        self.assertTrue(any('data-qa*="contact"' in literal for literal in literals))
        for literal in literals:
            self.assertNotIn('\n', literal, 'Literal newline is invalid inside JS single quotes')
            self.assertNotIn('\r', literal)
        self.assertIn('[data-qa*="address"]', script)
        self.assertIn('node.remove()', script)
