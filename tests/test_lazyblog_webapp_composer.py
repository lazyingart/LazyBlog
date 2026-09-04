from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lazyblog_webapp  # noqa: E402


class ComposerDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.chat_root = Path(self.temp_dir.name) / "chat"
        self.patch_chat_root = patch.object(lazyblog_webapp, "CHAT_ROOT", self.chat_root)
        self.patch_chat_root.start()
        self.app = lazyblog_webapp.LazyBlogStudio.__new__(lazyblog_webapp.LazyBlogStudio)
        self.app.composer_lock = threading.Lock()
        self.app.event_lock = threading.Condition()
        self.app.event_seq = 0
        self.app.events = deque(maxlen=500)

    def tearDown(self) -> None:
        self.patch_chat_root.stop()
        self.temp_dir.cleanup()

    def test_first_save_creates_session_and_persists_composer(self) -> None:
        result = self.app.save_composer("A durable thought", client_id="phone", base_version=0)

        self.assertTrue(result["created"])
        self.assertFalse(result["conflict"])
        composer = self.app.composer_payload(result["session"]["id"])
        self.assertEqual(composer["text"], "A durable thought")
        self.assertEqual(composer["version"], 1)
        self.assertEqual(composer["client_id"], "phone")

    def test_stale_other_device_is_preserved_as_conflict(self) -> None:
        initial = self.app.save_composer("Phone draft", client_id="phone", base_version=0)
        session_id = initial["session"]["id"]
        updated = self.app.save_composer("Phone draft continued", session_id, client_id="phone", base_version=1)

        conflict = self.app.save_composer("Tablet draft", session_id, client_id="tablet", base_version=1)

        self.assertTrue(conflict["conflict"])
        self.assertEqual(conflict["composer"]["text"], "Phone draft continued")
        snapshots = list((self.chat_root / session_id / "composer-conflicts").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertIn("Tablet draft", snapshots[0].read_text(encoding="utf-8"))
        self.assertEqual(updated["composer"]["version"], 2)

    def test_clear_after_send_is_synced(self) -> None:
        initial = self.app.save_composer("Ready to send", client_id="phone", base_version=0)
        session_id = initial["session"]["id"]

        cleared = self.app.save_composer("", session_id, client_id="phone", base_version=1)

        self.assertEqual(cleared["composer"]["text"], "")
        self.assertEqual(cleared["composer"]["version"], 2)


class PromptRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = lazyblog_webapp.LazyBlogStudio.__new__(lazyblog_webapp.LazyBlogStudio)
        self.app.args = SimpleNamespace(codex_timeout=60)

    @patch.object(lazyblog_webapp.shutil, "which", return_value="/usr/local/bin/agent-run")
    @patch.dict(
        lazyblog_webapp.os.environ,
        {
            "LAZYBLOG_CODEX_ACCOUNTS": "personal,company,lab",
            "LAZYBLOG_CODEX_FALLBACK_MODEL": "gpt-5.3-codex-spark",
            "LAZYBLOG_CODEX_FALLBACK_REASONING": "low",
        },
        clear=False,
    )
    def test_agentshell_accounts_are_tried_per_model_in_order(self, _which) -> None:
        routes = self.app.structured_prompt_routes("gpt-5.6-sol", "low")

        self.assertEqual(
            [(row["model"], row["account"]) for row in routes],
            [
                ("gpt-5.6-sol", "personal"),
                ("gpt-5.6-sol", "company"),
                ("gpt-5.6-sol", "lab"),
                ("gpt-5.3-codex-spark", "personal"),
                ("gpt-5.3-codex-spark", "company"),
                ("gpt-5.3-codex-spark", "lab"),
            ],
        )

    @patch.object(lazyblog_webapp.shutil, "which", return_value=None)
    @patch.dict(lazyblog_webapp.os.environ, {"LAZYBLOG_CODEX_ACCOUNTS": "personal,company,lab"}, clear=False)
    def test_plain_codex_is_used_when_agentshell_is_not_installed(self, _which) -> None:
        routes = self.app.structured_prompt_routes("gpt-5.6-sol", "low")

        self.assertTrue(all(row["account"] == "" for row in routes))

    @patch.object(lazyblog_webapp.shutil, "which", return_value=None)
    @patch.object(lazyblog_webapp.subprocess, "run")
    @patch.dict(lazyblog_webapp.os.environ, {"LAZYBLOG_CODEX_FALLBACK_MODEL": ""}, clear=False)
    def test_failed_routes_preserve_attempt_metadata(self, run, _which) -> None:
        run.return_value = SimpleNamespace(returncode=1, stdout="provider stdout", stderr="provider stderr")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "output.json"
            schema_path = Path(temp_dir) / "schema.json"
            schema_path.write_text('{"type":"object"}', encoding="utf-8")
            with self.assertRaises(lazyblog_webapp.PromptRouteError) as raised:
                self.app.run_structured_prompt(
                    full_prompt="test",
                    schema_path=schema_path,
                    output_path=output_path,
                    model="gpt-5.6-sol",
                    reasoning="low",
                    allow_aginti=False,
                )

        self.assertEqual(raised.exception.attempts[0]["status"], "failed")
        self.assertEqual(raised.exception.attempts[0]["model"], "gpt-5.6-sol")
        self.assertIn("provider stderr", raised.exception.stderr)


class StudioDefaultsTests(unittest.TestCase):
    def test_prompt_profiles_separate_fast_chat_and_deep_tasks(self) -> None:
        self.assertEqual(lazyblog_webapp.DEFAULT_PROFILE_SETTINGS["reply"], {"model": "gpt-5.6-sol", "reasoning": "low"})
        self.assertEqual(lazyblog_webapp.DEFAULT_PROFILE_SETTINGS["task"], {"model": "gpt-5.6-sol", "reasoning": "high"})
        self.assertEqual(lazyblog_webapp.DEFAULT_PROFILE_SETTINGS["action"], {"model": "gpt-5.6-sol", "reasoning": "medium"})

    def test_vendored_rendering_assets_exist(self) -> None:
        for filename, _content_type in lazyblog_webapp.VENDOR_ASSETS.values():
            self.assertTrue((lazyblog_webapp.VENDOR_ROOT / filename).is_file(), filename)
        fonts = list((lazyblog_webapp.VENDOR_ROOT / "katex-fonts").glob("*.woff2"))
        self.assertGreaterEqual(len(fonts), 20)


if __name__ == "__main__":
    unittest.main()
