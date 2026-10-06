from __future__ import annotations

import http.client
import json
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lazyblog_webapp as web


class StudioIconTests(unittest.TestCase):
    def test_pngs_are_opaque_correctly_sized_build_artifacts(self):
        for size in (192, 512):
            data = web.make_icon_png(size)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))
            self.assertEqual(data[25], 2)  # RGB, no transparent corners before OS masking.
            self.assertEqual(data, (web.APP_ICON_ROOT / f"lazyblog-{size}.png").read_bytes())
        with self.assertRaises(ValueError):
            web.make_icon_png(123)

    def test_manifest_shortcuts_and_apple_icon_share_version(self):
        self.assertEqual(web.PWA_MANIFEST["id"], web.PWA_MANIFEST["start_url"])
        for icon in [*web.PWA_MANIFEST["icons"], *web.PWA_MANIFEST["shortcuts"][0]["icons"]]:
            self.assertIn(f"?v={web.APP_ICON_VERSION}", icon["src"])
            self.assertEqual(web.request_auth_mode(urlparse(icon["src"]).path, cookie_only=True), "public")
        for page in (web.LOGIN_HTML, web.INDEX_HTML):
            self.assertIn('rel="apple-touch-icon" href="/icons/lazyblog-192.png?v=__ICON_VERSION__"', page)
        self.assertNotIn("__ICON_VERSION__", web.SERVICE_WORKER)
        self.assertIn('url.pathname === "/manifest.webmanifest"', web.SERVICE_WORKER)
        self.assertIn('cache: "no-cache"', web.SERVICE_WORKER)
        self.assertIn("#b83e2c", web.APP_ICON_SVG)
        self.assertIn("#fff8e7", web.APP_ICON_SVG)

    def test_versioned_icon_http_routes_and_manifest_revalidation(self):
        app = SimpleNamespace(codex_profile=lambda _: {"model": "test", "reasoning": "low"})
        with patch.dict(web.os.environ, {"LAZYBLOG_STUDIO_LOGIN_TOKEN": "test-only", "LAZYBLOG_STUDIO_AUTH_DISABLED": "0"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection(*server.server_address)
            try:
                connection.request("GET", "/manifest.webmanifest")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-cache")
                manifest = json.loads(response.read())
                for icon in manifest["icons"]:
                    connection.request("GET", icon["src"])
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), icon["type"] + ("; charset=utf-8" if icon["type"] == "image/svg+xml" else ""))
                    self.assertTrue(response.read())
                connection.request("GET", "/login")
                response = connection.getresponse()
                page = response.read().decode()
                self.assertEqual(response.status, 401)
                self.assertIn(f'/icons/lazyblog-192.png?v={web.APP_ICON_VERSION}', page)
                self.assertNotIn("__ICON_VERSION__", page)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
