"""Local dashboard credential checks; no Anti-Captcha network or HH."""
from decimal import Decimal
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import anti_captcha
import envfile
import webapp


class AntiCaptchaDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_text("# existing comment\nRUN_MODE=SAFE\nSEARCH_TEXT=synthetic\n")
        read, write = envfile.read_env, envfile.update_env
        self.read = lambda: read(str(self.path))
        self.read_patch = patch("webapp.envfile.read_env", side_effect=self.read)
        self.write_patch = patch("webapp.envfile.update_env",
                                 side_effect=lambda updates: write(updates, str(self.path)))
        self.read_patch.start(); self.write_patch.start()
        self.addCleanup(self.read_patch.stop); self.addCleanup(self.write_patch.stop)
        webapp._anti_captcha_client = None
        webapp._anti_captcha_client_key = None
        self.client = webapp.app.test_client()
        self.origin = {"Origin": "http://localhost"}

    def test_save_check_replace_disable_and_remove_without_key_reflection(self):
        provider = Mock()
        provider.get_balance.return_value = anti_captcha.BalanceResult(True, Decimal("3.50"))
        with patch("webapp.anti_captcha.AntiCaptchaClient", return_value=provider):
            first = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "SYNTHETIC_FIRST_KEY"}, headers=self.origin)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["status"], "connected")
            self.assertEqual(first.json["balance_usd"], "3.50")
            self.assertNotIn("SYNTHETIC_FIRST_KEY", first.get_data(as_text=True))
            self.assertEqual(first.headers["Cache-Control"], "no-store")
            status = self.client.get("/api/anti-captcha")
            self.assertEqual(status.json["status"], "connected")
            self.assertNotIn("SYNTHETIC_FIRST_KEY", status.get_data(as_text=True))
            self.assertEqual(self.read()["RUN_MODE"], "SAFE")
            self.assertEqual(self.read()["SEARCH_TEXT"], "synthetic")
            self.assertTrue(self.path.read_text().startswith("# existing comment\n"))
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            second = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "SYNTHETIC_SECOND_KEY"}, headers=self.origin)
            self.assertEqual(second.json["status"], "connected")
            self.assertNotIn("SYNTHETIC_FIRST_KEY", self.path.read_text())
            self.assertNotIn("SYNTHETIC_SECOND_KEY", second.get_data(as_text=True))
            disabled = self.client.post("/api/anti-captcha", json={"enabled": False}, headers=self.origin)
            self.assertEqual(disabled.json["status"], "disabled")
            self.assertEqual(self.read()["ANTI_CAPTCHA_API_KEY"], "SYNTHETIC_SECOND_KEY")
            cleared = self.client.post("/api/anti-captcha", json={
                "enabled": False, "clear_key": True}, headers=self.origin)
            self.assertEqual(cleared.json["status"], "not_configured")
            self.assertEqual(self.read()["ANTI_CAPTCHA_API_KEY"], "")

    def test_failed_balance_and_invalid_or_foreign_request(self):
        provider = Mock()
        provider.get_balance.return_value = anti_captcha.BalanceResult(False, reason="balance_service_error")
        with patch("webapp.anti_captcha.AntiCaptchaClient", return_value=provider):
            bad_origin = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "SYNTHETIC_FIRST_KEY"},
                headers={"Origin": "https://foreign.test"})
            self.assertEqual(bad_origin.status_code, 403)
            self.assertNotIn("ANTI_CAPTCHA_API_KEY", self.read())
            missing_origin = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "SYNTHETIC_FIRST_KEY"})
            self.assertEqual(missing_origin.status_code, 403)
            invalid = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "PRIVATE\nKEY"}, headers=self.origin)
            self.assertEqual(invalid.status_code, 400)
            saved = self.client.post("/api/anti-captcha", json={
                "enabled": True, "api_key": "SYNTHETIC_FIRST_KEY"}, headers=self.origin)
            self.assertEqual(saved.json["status"], "connection_error")
            self.assertNotIn("SYNTHETIC_FIRST_KEY", saved.get_data(as_text=True))

    def test_dashboard_secret_field_is_isolated_from_general_settings(self):
        html = (Path(webapp.__file__).parent / "web" / "index.html").read_text()
        self.assertIn('id="antiCaptchaKey" type="password"', html)
        self.assertIn("!el.id.startsWith('antiCaptcha')", html)
        self.assertNotIn("localStorage", html)


if __name__ == "__main__":
    unittest.main()
