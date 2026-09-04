from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lazyblog_webapp import request_auth_mode, studio_cookie_attributes  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
