"""Targeted Basic-auth privacy checks; synthetic data and no external services."""
import base64
import json
import unittest
from unittest.mock import patch

import test_safe_mvp  # Load isolated test configuration before project modules.
import decide
import llm
import run_web
from privacy import PrivacyError, sanitize_candidate_text


class BasicAuthPrivacyTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def assert_not_secret(self, text):
        self.assertEqual(sanitize_candidate_text(text), text)

    def assert_auth_secret(self, text):
        with self.assertRaises(PrivacyError) as caught:
            sanitize_candidate_text(text)
        self.assertEqual(caught.exception.privacy_rule_id, "auth_scheme")
        return caught.exception

    def token(self, value):
        return base64.b64encode(value).decode("ascii")

    def test_natural_language_basic_phrases_are_not_secrets(self):
        for text in (
            "Basic knowledge", "Basic understanding", "basic experience",
            "Basic architecture", "Basic requirements",
            "Basic architecturalrequirements", "Basic test",
        ):
            with self.subTest(text=text):
                self.assert_not_secret(text)

    def test_strict_basic_credentials_with_colon_are_secrets(self):
        examples = (
            "dXNlcjpwYXNzd29yZA==",  # synthetic user:password
            self.token(b"user:"),
            self.token(b":password"),
            self.token(b":"),
        )
        for token in examples:
            with self.subTest(token_length=len(token)):
                error = self.assert_auth_secret("Basic " + token)
                shape = error.privacy_match_shape
                self.assertTrue(shape["basic_base64_valid"])
                self.assertTrue(shape["basic_decoded_has_colon"])
                if token == "Og==":
                    self.assertEqual(shape["suffix_length_bucket"], "4_7")

    def test_strict_base64_without_colon_is_not_basic_credential(self):
        for payload in (b"synthetic-payload", b"ordinary bytes", b"architecture"):
            with self.subTest(payload_length=len(payload)):
                text = "Basic " + self.token(payload)
                self.assert_not_secret(text)

    def test_invalid_base64_and_malformed_padding_are_not_basic_credentials(self):
        examples = (
            "Basic dXNlcjpw$YXNz",
            "Basic dXNlcjpwYXNzd29yZA=",
            "Basic dXNlcjpwYXNzd29yZA===",
            "Basic not_base64_value",
            "Basic abcdefgh",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_not_secret(text)

    def test_scheme_case_and_supported_whitespace_remain_case_insensitive(self):
        token = self.token(b"synthetic-user:synthetic-password")
        for scheme in ("Basic", "basic", "BASIC"):
            for separator in (" ", "\t", "\n", " \t"):
                with self.subTest(scheme=scheme, separator=repr(separator)):
                    error = self.assert_auth_secret(scheme + separator + token)
                    self.assertEqual(error.privacy_match_shape["scheme"], "basic")

    def test_valid_credential_followed_by_prose_punctuation_is_still_protected(self):
        token = self.token(b"synthetic-user:synthetic-password")
        for text in ("Basic " + token + ", details",
                     "Basic " + token + ".",
                     "Basic " + token + " trailing text"):
            with self.subTest(suffix=text[-12:]):
                self.assert_auth_secret(text)
        self.assert_not_secret("Basic " + token + "$invalid-continuation")

    def test_bearer_detection_is_unchanged_and_does_not_require_base64(self):
        for text in ("Bearer knowledge", "bearer Abcd1234._-=", "BEARER opaque-token-123"):
            with self.subTest(text=text):
                error = self.assert_auth_secret(text)
                shape = error.privacy_match_shape
                self.assertEqual(shape["scheme"], "bearer")
                self.assertIsNone(shape["basic_base64_valid"])
                self.assertIsNone(shape["basic_decoded_has_colon"])

    def test_other_secret_rules_remain_fail_closed(self):
        cases = (
            ("API_KEY=synthetic-secret", "credential_assignment"),
            ("password: synthetic", "credential_assignment"),
            ('{"cookies": []}', "json_cookie_or_origin_array"),
            ("https://example.test/?token=synthetic", "credential_query_parameter"),
        )
        for text, rule in cases:
            with self.subTest(rule=rule), self.assertRaises(PrivacyError) as caught:
                sanitize_candidate_text(text)
            self.assertEqual(caught.exception.privacy_rule_id, rule)

    def test_natural_language_basic_continues_to_matching(self):
        vacancy = dict(test_safe_mvp.VAC,
                       description="Basic knowledge of project delivery is sufficient.")
        with patch("llm.generate", return_value=json.dumps(test_safe_mvp.RESULT)) as generate:
            result = decide.score(vacancy, "Synthetic professional facts",
                                  resume_title="Project Manager")
        self.assertEqual(result["score"], test_safe_mvp.RESULT["score"])
        generate.assert_called_once()

    def test_basic_diagnostic_never_contains_token_or_decoded_credential(self):
        token = self.token(b"synthetic-user:synthetic-password")
        error = self.assert_auth_secret("Basic " + token)
        error.with_source("full_vacancy")
        details = run_web._exception_details(error, "matching")
        encoded = json.dumps(details)
        self.assertEqual(details["privacy_match_shape"]["scheme"], "basic")
        self.assertTrue(details["privacy_match_shape"]["basic_base64_valid"])
        self.assertTrue(details["privacy_match_shape"]["basic_decoded_has_colon"])
        for forbidden in (token, "synthetic-user", "synthetic-password", "Basic "):
            self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()
