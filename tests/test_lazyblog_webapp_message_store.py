from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lazyblog_webapp  # noqa: E402


class MessageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.chat_root = self.root / "content" / "chat"
        self.patchers = [
            patch.object(lazyblog_webapp, "ROOT_DIR", self.root),
            patch.object(lazyblog_webapp, "CHAT_ROOT", self.chat_root),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.chat_root.mkdir(parents=True, exist_ok=True)
        self.app = lazyblog_webapp.LazyBlogStudio.__new__(lazyblog_webapp.LazyBlogStudio)
        self.app.message_lock = threading.RLock()
        self.app.message_store_lock = threading.RLock()
        self.app.chat_queue_lock = threading.Lock()
        self.app.event_lock = threading.Condition()
        self.app.event_seq = 0
        self.app.events = deque(maxlen=100)

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / "content" / lazyblog_webapp.MESSAGE_STORE_FILENAME)
        connection.row_factory = sqlite3.Row
        return connection

    def test_create_edit_and_unsend_keep_an_append_only_history(self) -> None:
        session = self.app.create_session()
        path = self.app.append_message(session["id"], "user", "First thought")
        self.app.edit_message(session["id"], path.stem, "Revised thought")
        self.app.unsend_message(session["id"], path.stem)

        with self.connect() as connection:
            message = connection.execute("SELECT * FROM messages WHERE message_id = ?", (path.stem,)).fetchone()
            events = connection.execute(
                "SELECT event_type, snapshot_json FROM message_events WHERE message_id = ? ORDER BY event_id",
                (path.stem,),
            ).fetchall()

        self.assertEqual(message["content"], "Revised thought")
        self.assertIsNotNone(message["deleted_at"])
        self.assertEqual([row["event_type"] for row in events], ["created", "edited", "unsent"])
        self.assertIn("First thought", events[0]["snapshot_json"])
        self.assertIn("Revised thought", events[-1]["snapshot_json"])

    def test_startup_reconciles_existing_markdown_without_duplicate_events(self) -> None:
        session_id = "20260904-120000-backfill"
        message_path = self.chat_root / session_id / "messages" / "20260904T120000Z-old-user.md"
        lazyblog_webapp.write_markdown(
            message_path,
            {
                "kind": "lazyblog-chat-message",
                "session_id": session_id,
                "role": "user",
                "created_at": "2026-09-04T12:00:00Z",
            },
            "Existing Markdown memory",
        )

        self.app.initialize_message_store()
        self.app._message_store_ready = ""
        self.app.initialize_message_store()

        with self.connect() as connection:
            message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            event_count = connection.execute("SELECT COUNT(*) FROM message_events").fetchone()[0]

        self.assertEqual(message_count, 1)
        self.assertEqual(event_count, 1)

    def test_plain_note_uses_fast_path_without_action_model(self) -> None:
        with patch.object(self.app, "run_codex_tool") as run_tool, patch.object(
            self.app, "deterministic_action_hint"
        ) as deterministic_hint:
            routed = self.app.route_chat_action("unused-session", "Our bodies disappear, so writing preserves memory.")

        run_tool.assert_not_called()
        deterministic_hint.assert_not_called()
        self.assertEqual(routed["action"], "no_op")
        self.assertEqual(routed["routing_lane"], "fast-chat")

    def test_negated_publish_words_do_not_trigger_control_lane(self) -> None:
        with patch.object(self.app, "run_codex_tool") as run_tool, patch.object(
            self.app, "deterministic_action_hint"
        ) as deterministic_hint:
            routed = self.app.route_chat_action(
                "unused-session",
                "Store this memory and reply briefly; do not draft or publish it.",
            )

        run_tool.assert_not_called()
        deterministic_hint.assert_not_called()
        self.assertEqual(routed["action"], "no_op")
        self.assertEqual(routed["routing_lane"], "fast-chat")

    def test_queue_lane_separates_notes_from_controlled_post_work(self) -> None:
        self.assertEqual(self.app.chat_lane_for_message("A note about today's work."), "fast-chat")
        self.assertEqual(self.app.chat_lane_for_message("Draft a new post from today's notes."), "controlled-task")
        self.assertEqual(
            self.app.chat_lane_for_message("https://blog.example.test/?p=123"),
            "controlled-task",
        )
        self.assertEqual(
            self.app.chat_lane_for_message("Do not draft or publish this note."),
            "fast-chat",
        )

    def test_each_worker_claims_only_its_queue_lane(self) -> None:
        session = self.app.create_session()
        controlled = {
            "id": "20260904T120000Z-controlled",
            "session_id": session["id"],
            "status": "queued",
            "created_at": "2026-09-04T12:00:00Z",
            "message": "Draft a new post.",
            "lane": "controlled-task",
        }
        fast = {
            "id": "20260904T120001Z-fast",
            "session_id": session["id"],
            "status": "queued",
            "created_at": "2026-09-04T12:00:01Z",
            "message": "Remember this note.",
            "lane": "fast-chat",
        }
        self.app.write_chat_queue_item(controlled)
        self.app.write_chat_queue_item(fast)

        self.assertEqual(self.app.next_chat_queue_item("fast-chat")["id"], fast["id"])
        self.assertEqual(self.app.next_chat_queue_item("controlled-task")["id"], controlled["id"])


if __name__ == "__main__":
    unittest.main()
