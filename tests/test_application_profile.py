"""Offline tests for the one private Telegram application setting."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_safe_mvp
import application_profile as profile
import webapp


class ApplicationProfileTests(unittest.TestCase):
    setUp = test_safe_mvp.SafeTests.setUp

    def path(self):
        return self.root / "private" / "application_profile.json"

    def storage(self):
        return patch.object(profile, "STORAGE_PATH", self.path())

    def test_save_reload_change_clear_trim_and_preserve_unknown_fields(self):
        path = self.path()
        path.parent.mkdir()
        existing = {"version": 7, "unknown": {"keep": True},
                    "contacts": {"other": "preserved"},
                    "resumes": {"aaaa": {"contacts": {"email": "person@example.test"}}}}
        path.write_text(json.dumps(existing), encoding="utf-8")
        with self.storage():
            self.assertEqual(profile.save_telegram("  @first_user  "), "@first_user")
            self.assertEqual(profile.telegram(), "@first_user")
            self.assertEqual(profile.save_telegram("@second_user"), "@second_user")
            self.assertEqual(profile.telegram(), "@second_user")
            self.assertEqual(profile.save_telegram(""), "")
            self.assertEqual(profile.telegram(), "")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unknown"], existing["unknown"])
        self.assertEqual(saved["resumes"], existing["resumes"])
        self.assertEqual(saved["contacts"]["other"], "preserved")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_format_validation_is_backend_strict_and_does_not_overwrite(self):
        invalid = ("username", "https://t.me/username", "t.me/username", "+79999999999",
                   "@user", "@user-name", "@user name", "@юзер12345",
                   "@" + "a" * 33)
        with self.storage():
            profile.save_telegram("@valid_user")
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(profile.ApplicationProfileError):
                    profile.save_telegram(value)
                self.assertEqual(profile.telegram(), "@valid_user")
            for value in ("@username", "@georgiy123", "@my_username", "@" + "a" * 32):
                with self.subTest(value=value):
                    self.assertEqual(profile.save_telegram(value), value)

    def test_settings_api_loads_saves_clears_and_never_echoes_value_elsewhere(self):
        contact = "@private_contact"
        with self.storage(), patch("webapp.envfile.update_env") as env_save:
            client = webapp.app.test_client()
            saved = client.post("/api/config", json={"APPLICATION_TELEGRAM": contact})
            self.assertEqual(saved.status_code, 200)
            self.assertNotIn(contact, saved.get_data(as_text=True))
            loaded = client.get("/api/application-profile")
            self.assertEqual(loaded.get_json(), {"telegram": contact})
            self.assertEqual(loaded.headers["Cache-Control"], "no-store")
            self.assertNotIn(contact, client.get("/api/state").get_data(as_text=True))
            self.assertEqual(client.post("/api/config", json={"APPLICATION_TELEGRAM": ""}).status_code, 200)
            self.assertEqual(client.get("/api/application-profile").get_json(), {"telegram": ""})
            env_save.assert_called()

    def test_settings_api_rejects_invalid_and_foreign_origin_without_saving(self):
        with self.storage(), patch("webapp.envfile.update_env") as env_save:
            client = webapp.app.test_client()
            for value in ("username", "https://t.me/username", "@user", "@" + "a" * 33):
                response = client.post("/api/config", json={"APPLICATION_TELEGRAM": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Telegram должен", response.get_json()["error"])
            foreign = client.post("/api/config", json={"APPLICATION_TELEGRAM": "@valid_user"},
                                  headers={"Origin": "https://other.test"})
            self.assertEqual(foreign.status_code, 400)
            self.assertFalse(self.path().exists())
            env_save.assert_not_called()

    def test_dashboard_contains_load_save_clear_field_and_required_copy(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        for text in ("Telegram для откликов", 'placeholder="@username"',
                     "Укажите Telegram в формате @username.",
                     "Хранится локально и не передаётся в ИИ.",
                     "/api/application-profile", "APPLICATION_TELEGRAM"):
            self.assertIn(text, html)
        self.assertIn("if(!await postForm()) return", html)


if __name__ == "__main__":
    unittest.main()
