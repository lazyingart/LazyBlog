from __future__ import annotations

import sys
import hashlib
import hmac
import http.client
import threading
import urllib.parse
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lazyblog_webapp import request_auth_mode, studio_cookie_attributes  # noqa: E402
import lazyblog_webapp as web  # noqa: E402


class RequestAuthModeTests(unittest.TestCase):
    def test_public_bootstrap_paths_remain_public(self) -> None:
        for path in ("/api/health", "/api/login", "/login", "/service-worker.js"):
            self.assertEqual(request_auth_mode(path, cookie_only=True), "public")

    def test_cookie_only_mode_protects_codex_and_translation_routes(self) -> None:
        for path in ("/api/codex/respond", "/api/codex/jobs", "/api/translate/jobs"):
            self.assertEqual(request_auth_mode(path, cookie_only=True), "studio")

    def test_api_compatible_mode_preserves_bearer_api_routes(self) -> None:
        for path in ("/api/codex/respond", "/api/codex/jobs", "/api/translate/jobs"):
            self.assertEqual(request_auth_mode(path, cookie_only=False), "api")

    def test_regular_application_routes_require_studio_login(self) -> None:
        for path in ("/", "/api/chat", "/api/posts", "/api/events"):
            self.assertEqual(request_auth_mode(path, cookie_only=True), "studio")

    def test_public_https_cookie_is_secure_and_http_only(self) -> None:
        attributes = studio_cookie_attributes(secure=True)
        self.assertIn("HttpOnly", attributes)
        self.assertIn("SameSite=Lax", attributes)
        self.assertIn("Secure", attributes)

    def test_local_http_cookie_can_omit_secure_attribute(self) -> None:
        self.assertNotIn("Secure", studio_cookie_attributes(secure=False))


class PersistentLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(web.os.environ, {
            "LAZYBLOG_STUDIO_USERNAME": "writer",
            "LAZYBLOG_STUDIO_LOGIN_TOKEN": "unit-test-only",
            "LAZYBLOG_STUDIO_AUTH_DISABLED": "0",
            "LAZYBLOG_STUDIO_COOKIE_ONLY": "1",
            "LAZYBLOG_STUDIO_SECURE_COOKIE": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def cookie(self, *, remember=True):
        return f"{web.STUDIO_AUTH_COOKIE}={web.make_studio_cookie('writer', remember=remember)}"

    def test_remembered_cookie_lasts_90_days_and_renews_after_one_day(self):
        with patch.object(web.time, "time", return_value=1000):
            cookie = self.cookie()
            self.assertEqual(web.studio_cookie_details(cookie), ("writer", 1000 + 90 * 86400, True))
            self.assertIsNone(web.renewed_studio_cookie(cookie))
        with patch.object(web.time, "time", return_value=1000 + 86400):
            renewed = web.renewed_studio_cookie(cookie)
            self.assertIn("Max-Age=7776000", renewed)
            self.assertIn("Secure", renewed)
            self.assertTrue(web.verify_studio_cookie(renewed))

    def test_unchecked_cookie_has_no_browser_persistence_or_sliding_renewal(self):
        self.assertNotIn("Max-Age", studio_cookie_attributes(remember=False))
        with patch.object(web.time, "time", return_value=1000):
            cookie = self.cookie(remember=False)
            self.assertEqual(web.studio_cookie_details(cookie), ("writer", 87400, False))
        with patch.object(web.time, "time", return_value=5000):
            self.assertTrue(web.verify_studio_cookie(cookie))
            self.assertIsNone(web.renewed_studio_cookie(cookie))
        with patch.object(web.time, "time", return_value=87400):
            self.assertFalse(web.verify_studio_cookie(cookie))

    def test_legacy_cookie_is_accepted_and_upgraded(self):
        with patch.object(web.time, "time", return_value=1000):
            message = f"writer:{1000 + 30 * 86400}"
            signature = hmac.new(b"unit-test-only", message.encode(), hashlib.sha256).hexdigest()
            cookie = f"{web.STUDIO_AUTH_COOKIE}=" + urllib.parse.quote(f"{message}:{signature}", safe="")
            self.assertTrue(web.verify_studio_cookie(cookie))
            self.assertIn("%3Ap%3A", web.renewed_studio_cookie(cookie))

    def test_tampering_expiry_signature_mode_or_username_is_rejected(self):
        cookie = self.cookie()
        for invalid in (cookie.replace("%3Ap%3A", "%3As%3A"), cookie.replace("writer", "admin"), cookie + "a", cookie.rsplit("%3A", 1)[0] + "%3A中文", "invalid"):
            self.assertFalse(web.verify_studio_cookie(invalid))
        with patch.dict(web.os.environ, {"LAZYBLOG_STUDIO_LOGIN_TOKEN": "rotated-key"}):
            self.assertFalse(web.verify_studio_cookie(cookie))

    def test_http_login_reload_redirect_and_logout_contract(self):
        app = SimpleNamespace(codex_profile=lambda _: {"model": "test", "reasoning": "low"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address)
        try:
            connection.request("POST", "/api/login", '{"username":"writer","token":"unit-test-only","remember":true}', {"Content-Type": "application/json"})
            response = connection.getresponse()
            cookie = response.getheader("Set-Cookie")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertIn("HttpOnly", cookie)
            response.read()
            connection.request("GET", "/login", headers={"Cookie": cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            self.assertEqual(response.getheader("Location"), "/")
            response.read()
            connection.request("GET", "/", headers={"Cookie": cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"__STUDIO_THEME__", response.read())
            connection.request("POST", "/api/logout", "{}", {"Cookie": cookie, "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
            self.assertEqual(len([x for x in response.getheaders() if x[0].lower() == "set-cookie"]), 1)
            response.read()
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            self.assertIn(b"rememberLogin", response.read())
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
