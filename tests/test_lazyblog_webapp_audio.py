from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import lazyblog_webapp


class AudioMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.patchers = [
            patch.object(lazyblog_webapp, "ROOT_DIR", self.root),
            patch.object(lazyblog_webapp, "CHAT_ROOT", self.root / "content/chat"),
            patch.object(lazyblog_webapp, "DRAFT_ROOT", self.root / "content/drafts"),
            patch.object(lazyblog_webapp, "UPLOAD_ROOT", self.root / "content/uploads/lazyblog-studio"),
            patch.object(lazyblog_webapp, "UPLOAD_MIRROR_ROOT", self.root / "content/upload-mirrors"),
            patch.object(lazyblog_webapp, "POST_PROJECT_ROOT", self.root / "content/studio-posts"),
        ]
        for patcher in self.patchers:
            patcher.start()
        for directory in (
            lazyblog_webapp.CHAT_ROOT,
            lazyblog_webapp.DRAFT_ROOT,
            lazyblog_webapp.UPLOAD_ROOT,
            lazyblog_webapp.UPLOAD_MIRROR_ROOT,
            lazyblog_webapp.POST_PROJECT_ROOT,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.app = lazyblog_webapp.LazyBlogStudio.__new__(lazyblog_webapp.LazyBlogStudio)
        self.app.message_lock = threading.Lock()
        self.app.audio_job_lock = threading.Lock()
        self.app.chat_queue_lock = threading.Lock()
        self.app.audio_job_event = threading.Event()
        self.app.chat_queue_event = threading.Event()
        self.app.event_lock = threading.Condition()
        self.app.event_seq = 0
        self.app.events = deque(maxlen=100)

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def headers(self, key: str = "audio-test-recording-0001") -> dict[str, str]:
        return {
            "Content-Length": "12",
            "Content-Type": "audio/webm",
            "X-Filename": "voice.webm",
            "X-Language": "auto",
            "X-Diarize": "auto",
            "X-Idempotency-Key": key,
        }

    def test_audio_upload_is_retained_and_idempotent(self) -> None:
        first = self.app.accept_audio_message(io.BytesIO(b"audio-bytes!"), self.headers())
        job = first["audio_job"]
        stored = self.app.find_audio_job(job["id"])[1]
        audio_path = self.root / stored["stored_path"]

        self.assertEqual(audio_path.read_bytes(), b"audio-bytes!")
        self.assertEqual(first["messages"][-1]["attachments"][0]["kind"], "audio")
        self.assertEqual(first["messages"][-1]["queue_status"], "transcribing")

        second = self.app.accept_audio_message(io.BytesIO(b"audio-bytes!"), self.headers())
        self.assertTrue(second["reused"])
        self.assertEqual(second["audio_job"]["id"], job["id"])
        self.assertEqual(len(self.app.audio_jobs()), 1)

    def test_completed_transcript_enters_existing_chat_queue(self) -> None:
        payload = self.app.accept_audio_message(io.BytesIO(b"audio-bytes!"), self.headers("audio-test-recording-0002"))
        _path, job = self.app.find_audio_job(payload["audio_job"]["id"])
        self.app.finalize_audio_job(
            job,
            {
                "model": "large-v3",
                "language": "zh",
                "duration": 4.2,
                "text": "这是一次语音测试。",
                "summary": "这是一次语音测试。",
                "segments": [],
                "diarization": {"status": "not-requested"},
            },
        )

        message_path = self.root / job["message_path"]
        self.app.update_message_queue_status(message_path, "running")
        self.app.update_message_queue_status(message_path, "succeeded")
        message = self.app.read_message(message_path)
        queue_files = list((lazyblog_webapp.CHAT_ROOT / job["session_id"] / "queue").glob("*.json"))
        queue = json.loads(queue_files[0].read_text(encoding="utf-8"))

        self.assertEqual(message["content"], "这是一次语音测试。")
        self.assertEqual(message["attachments"][0]["analysis_status"], "succeeded")
        self.assertEqual(message["attachments"][0]["language"], "zh")
        self.assertTrue(queue["attachments_preanalyzed"])
        self.assertEqual(queue["message"], "这是一次语音测试。")

    def test_permanent_failure_is_visible_and_retryable(self) -> None:
        payload = self.app.accept_audio_message(
            io.BytesIO(b"audio-bytes!"), self.headers("audio-test-recording-0003")
        )
        _path, job = self.app.find_audio_job(payload["audio_job"]["id"])
        self.app.fail_audio_job(job, "model could not decode this recording")

        failed = self.app.find_audio_job(job["id"])[1]
        message = self.app.read_message(self.root / job["message_path"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(message["queue_status"], "failed")
        self.assertEqual(message["attachments"][0]["analysis_status"], "failed")

        retried = self.app.retry_audio_job(job["id"])["audio_job"]
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["error"], "")

    def test_unreadable_audio_job_does_not_stop_job_discovery(self) -> None:
        job_dir = lazyblog_webapp.CHAT_ROOT / "session" / "audio-jobs"
        job_dir.mkdir(parents=True)
        (job_dir / "unreadable.json").write_text("{}", encoding="utf-8")

        with patch.object(lazyblog_webapp, "read_json", side_effect=OSError(5, "I/O error")):
            self.assertEqual(self.app.audio_jobs(), [])

    def test_unreadable_chat_queue_item_does_not_stop_worker_discovery(self) -> None:
        queue_dir = lazyblog_webapp.CHAT_ROOT / "session" / "queue"
        queue_dir.mkdir(parents=True)
        (queue_dir / "unreadable.json").write_text("{}", encoding="utf-8")

        with patch.object(self.app, "read_chat_queue_item", side_effect=OSError(5, "I/O error")):
            self.assertEqual(self.app.chat_queue_items(), [])


if __name__ == "__main__":
    unittest.main()
