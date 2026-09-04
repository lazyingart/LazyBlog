#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import zlib
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lazyblog_sync import LazyBlogError, WPClient, html_to_markdown, make_client, markdown_to_html, require_auth
from lazyblog_translate import first_heading, load_env_file, split_front_matter


ROOT_DIR = Path(__file__).resolve().parents[1]
CHAT_ROOT = ROOT_DIR / "content" / "chat"
DRAFT_ROOT = ROOT_DIR / "content" / "drafts"
JOB_ROOT = ROOT_DIR / "content" / "codex-jobs"
UPLOAD_ROOT = ROOT_DIR / "content" / "uploads" / "lazyblog-studio"
UPLOAD_MIRROR_ROOT = ROOT_DIR / "content" / "upload-mirrors"
ARTIFACT_ROOT = ROOT_DIR / "content" / "studio-artifacts"
TRANSLATION_JOB_ROOT = ROOT_DIR / "content" / "translation-jobs"
TAXONOMY_ROOT = ROOT_DIR / "content" / "taxonomy"
CATEGORY_SNAPSHOT_PATH = TAXONOMY_ROOT / "categories.json"
POST_PROJECT_ROOT = ROOT_DIR / "content" / "studio-posts"
STUDIO_SETTINGS_PATH = ROOT_DIR / "content" / "studio-settings.json"
VENDOR_ROOT = ROOT_DIR / "web" / "vendor"
VENDOR_ASSETS = {
    "/assets/vendor/marked.js": ("marked-15.0.12.min.js", "application/javascript; charset=utf-8"),
    "/assets/vendor/dompurify.js": ("dompurify-3.2.6.min.js", "application/javascript; charset=utf-8"),
    "/assets/vendor/katex.js": ("katex-0.16.22.min.js", "application/javascript; charset=utf-8"),
    "/assets/vendor/katex-auto-render.js": ("katex-auto-render-0.16.22.min.js", "application/javascript; charset=utf-8"),
    "/assets/vendor/katex.css": ("katex-0.16.22.min.css", "text/css; charset=utf-8"),
}
CHAT_REPLY_PROMPT = ROOT_DIR / "prompts" / "web-chat-reply.txt"
CHAT_TASK_PROMPT = ROOT_DIR / "prompts" / "web-draft-task.txt"
CHAT_ACTION_PROMPT = ROOT_DIR / "prompts" / "web-action-router.txt"
GIT_COMMIT_PROMPT = ROOT_DIR / "prompts" / "web-git-commit-push.txt"
CODEX_RESPONSE_PROMPT = ROOT_DIR / "prompts" / "web-codex-response.txt"
ATTACHMENT_VISION_PROMPT = ROOT_DIR / "prompts" / "web-attachment-vision.txt"
CHAT_REPLY_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_chat_reply.schema.json"
CHAT_TASK_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_chat_task.schema.json"
CHAT_ACTION_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_action.schema.json"
CODEX_RESPONSE_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_codex_response.schema.json"
CODEX_TRANSLATION_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_web_translation.schema.json"
ATTACHMENT_VISION_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_attachment_vision.schema.json"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "low"
DEFAULT_ACTION_MODEL = "gpt-5.6-sol"
DEFAULT_ACTION_REASONING = "low"
DEFAULT_GIT_MODEL = "gpt-5.3-codex-spark"
DEFAULT_GIT_REASONING = "medium"
DEFAULT_MESSAGE_BATCH_SIZE = 10
MAX_COMPOSER_TEXT_CHARS = 250_000
MAX_ARTIFACT_TEXT_BYTES = 520_000
MAX_ARTIFACT_BINARY_BYTES = 8_000_000
ARTIFACT_ALLOWED_PREFIXES = (
    "content/uploads/lazyblog-studio",
    "content/upload-mirrors",
    "content/studio-artifacts",
    "content/codex-jobs",
    "content/studio-posts",
    "content/posts",
    "content/drafts",
    "out",
    "tmp",
)
ARTIFACT_SECRET_MARKERS = {
    ".env",
    ".env.local",
    ".git",
    ".ssh",
    ".npmrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
DEFAULT_PROFILE_SETTINGS = {
    "reply": {"model": "gpt-5.6-sol", "reasoning": "low"},
    "task": {"model": "gpt-5.6-sol", "reasoning": "low"},
    "action": {"model": "gpt-5.6-sol", "reasoning": "low"},
    "response": {"model": "gpt-5.6-sol", "reasoning": "low"},
    "translation": {"model": "gpt-5.6-sol", "reasoning": "low"},
}
REASONING_LEVELS = {"low", "medium", "high", "xhigh"}
STUDIO_AUTH_COOKIE = "lazyblog_studio_auth"
STUDIO_AUTH_TTL_SECONDS = 60 * 60 * 24 * 30


class WebAppError(RuntimeError):
    pass


class PromptRouteError(WebAppError):
    def __init__(
        self,
        message: str,
        *,
        attempts: list[dict[str, Any]] | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.attempts = attempts or []
        self.stdout = stdout
        self.stderr = stderr


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def studio_username() -> str:
    return os.environ.get("LAZYBLOG_STUDIO_USERNAME", "lachlan").strip() or "lachlan"


def studio_login_token() -> str:
    return os.environ.get("LAZYBLOG_STUDIO_LOGIN_TOKEN", "").strip()


def studio_auth_enabled() -> bool:
    return bool(studio_login_token()) and not bool_env("LAZYBLOG_STUDIO_AUTH_DISABLED", False)


def studio_cookie_only_auth_enabled() -> bool:
    return bool_env("LAZYBLOG_STUDIO_COOKIE_ONLY", False)


def studio_secure_cookie_enabled() -> bool:
    return bool_env("LAZYBLOG_STUDIO_SECURE_COOKIE", False)


def studio_cookie_attributes(*, secure: bool | None = None) -> str:
    use_secure = studio_secure_cookie_enabled() if secure is None else secure
    return f"Path=/; HttpOnly; SameSite=Lax; Max-Age={STUDIO_AUTH_TTL_SECONDS}" + (
        "; Secure" if use_secure else ""
    )


def request_auth_mode(path: str, *, cookie_only: bool | None = None) -> str:
    public_paths = {
        "/api/health",
        "/api/login",
        "/login",
        "/manifest.webmanifest",
        "/service-worker.js",
        "/icons/lazyblog.svg",
        "/icons/lazyblog-192.png",
        "/icons/lazyblog-512.png",
    }
    if path in public_paths:
        return "public"
    require_cookie = cookie_only if cookie_only is not None else studio_cookie_only_auth_enabled()
    if require_cookie:
        return "studio"
    if path.startswith("/api/translate/") or path.startswith("/api/codex/"):
        return "api"
    return "studio"


def studio_auth_secret() -> str:
    return studio_login_token() or os.environ.get("LAZYBLOG_API_TOKEN", "").strip()


def make_studio_cookie(username: str) -> str:
    expires = int(time.time()) + STUDIO_AUTH_TTL_SECONDS
    message = f"{username}:{expires}"
    signature = hmac.new(studio_auth_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.quote(f"{message}:{signature}", safe="")


def verify_studio_cookie(raw_cookie: str) -> bool:
    if not studio_auth_enabled():
        return True
    cookies: dict[str, str] = {}
    for chunk in raw_cookie.split(";"):
        name, separator, value = chunk.strip().partition("=")
        if separator:
            cookies[name] = value
    raw_value = cookies.get(STUDIO_AUTH_COOKIE, "")
    if not raw_value:
        return False
    try:
        username, expires_text, signature = urllib.parse.unquote(raw_value).split(":", 2)
        expires = int(expires_text)
    except ValueError:
        return False
    if username != studio_username() or expires < int(time.time()):
        return False
    message = f"{username}:{expires}"
    expected = hmac.new(studio_auth_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def slugify(value: str, fallback: str = "post") -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered, flags=re.UNICODE)
    lowered = re.sub(r"[\s_-]+", "-", lowered, flags=re.UNICODE).strip("-")
    return lowered or fallback


def safe_slug_token(value: str, fallback: str = "post") -> str:
    token = slugify(value, fallback=fallback)
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", token).strip("-")
    return token or fallback


def safe_session_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid session id")
    return value


def safe_job_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid job id")
    return value


def safe_post_project_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid post project id")
    return value


def safe_artifact_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid artifact id")
    return value


def stable_artifact_id(*parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def compact_preview(value: Any, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def artifact_kind_for(path: str = "", mime: str = "", kind: str = "") -> str:
    candidate = str(kind or "").strip().lower()
    safe_mime = str(mime or "").strip().lower()
    safe_path = str(path or "").strip().lower()
    suffix = Path(safe_path).suffix.lower()
    if candidate in {"image", "video", "pdf", "markdown", "text", "json", "diff", "file"}:
        if candidate != "file":
            return candidate
    if safe_mime.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic"}:
        return "image"
    if safe_mime.startswith("video/") or suffix in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
        return "video"
    if mime_is_pdf(safe_mime, safe_path):
        return "pdf"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in {".diff", ".patch"}:
        return "diff"
    if suffix in {".txt", ".log", ".tex", ".csv", ".yaml", ".yml"}:
        return "text"
    return "file" if candidate == "file" else "text"


def mime_for_artifact_path(path: str, fallback: str = "") -> str:
    guessed = mimetypes.guess_type(path)[0]
    if guessed:
        return guessed
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown; charset=utf-8"
    if suffix in {".txt", ".log", ".tex", ".csv", ".yaml", ".yml"}:
        return "text/plain; charset=utf-8"
    return fallback or "application/octet-stream"


def yaml_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def write_markdown(path: Path, front_matter: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in front_matter.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {yaml_quote(item)}")
        elif isinstance(value, (dict, bool, int, float)):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {yaml_quote(value)}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + body.strip() + "\n", encoding="utf-8")


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else dict(default)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def data_url_to_bytes(data_url: str) -> tuple[str, bytes]:
    text = str(data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        return "", b""
    header, payload = text.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    try:
        if ";base64" in header:
            return mime, base64.b64decode(payload)
        return mime, urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return mime, b""


def bytes_to_data_url(raw: bytes, mime: str) -> str:
    if not raw:
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def mime_is_pdf(mime: str, name: str = "") -> bool:
    safe_mime = str(mime or "").strip().lower()
    safe_name = str(name or "").strip().lower()
    return safe_mime == "application/pdf" or safe_name.endswith(".pdf")


def extension_for_mime(mime: str, name: str = "") -> str:
    suffix = Path(str(name or "")).suffix.strip()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(str(mime or "").strip().lower())
    if guessed:
        return guessed
    return ".bin"


def load_prompt(path: Path) -> str:
    if not path.exists():
        raise WebAppError(f"missing prompt template: {path}")
    return path.read_text(encoding="utf-8").strip()


def tail_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def list_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def extract_terms(text: str, limit: int = 12) -> list[str]:
    words = re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]{2,}", text.lower(), flags=re.UNICODE)
    ignored = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "about",
        "please",
        "could",
        "would",
        "write",
        "post",
        "blog",
    }
    seen: set[str] = set()
    terms: list[str] = []
    for word in words:
        if word in ignored or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= limit:
            break
    return terms


def front_matter_list(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    out: list[str] = []
    capture = False
    for line in lines:
        if capture:
            if line.startswith("  - "):
                value = line[4:].strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                out.append(value.replace("''", "'"))
                continue
            if line and not line.startswith((" ", "\t")):
                break
        if line.strip() == f"{key}:":
            capture = True
    return out


def extract_title(markdown: str, fallback: str) -> str:
    front_matter, body = split_front_matter(markdown)
    return front_matter.get("title") or first_heading(body) or fallback


def trim_snippet(text: str, terms: list[str], size: int = 420) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    lower = compact.lower()
    offsets = [lower.find(term.lower()) for term in terms if lower.find(term.lower()) >= 0]
    start = max(0, min(offsets) - 120) if offsets else 0
    snippet = compact[start : start + size]
    if start > 0:
        snippet = "..." + snippet
    if start + size < len(compact):
        snippet += "..."
    return snippet


def git_commit_push(paths: list[Path], message: str, branch: str, enabled: bool) -> None:
    if not enabled:
        return
    relative_paths = [str(path.relative_to(ROOT_DIR)) for path in paths if path.exists()]
    if not relative_paths:
        return
    lock_path = ROOT_DIR / ".git" / "lazyblog-webapp.lock"
    lock_path.parent.mkdir(exist_ok=True)
    script = f"""
set -euo pipefail
cd {json.dumps(str(ROOT_DIR))}
git add -f -- {' '.join(json.dumps(path) for path in relative_paths)}
if git diff --cached --quiet -- {' '.join(json.dumps(path) for path in relative_paths)}; then
  echo "No changes to commit for: {message}"
  exit 0
fi
git commit -m {json.dumps(message)}
for attempt in 1 2 3 4 5; do
  if git push origin HEAD:{json.dumps(branch)}; then
    exit 0
  fi
  git fetch origin {json.dumps(branch)} || true
  git rebase origin/{branch} || git rebase --abort || true
  sleep $((attempt * 2))
done
echo "Failed to push after retries: {message}" >&2
exit 1
"""
    subprocess.run(["flock", str(lock_path), "bash", "-lc", script], cwd=ROOT_DIR, check=True)


def git_commit_push_mixed(
    *,
    force_paths: list[Path] | None = None,
    tracked_paths: list[Path] | None = None,
    message: str,
    branch: str,
    enabled: bool,
) -> None:
    if not enabled:
        return
    force_relative = [str(path.relative_to(ROOT_DIR)) for path in (force_paths or []) if path.exists()]
    tracked_relative = [str(path.relative_to(ROOT_DIR)) for path in (tracked_paths or []) if path.exists()]
    all_relative = [*force_relative, *tracked_relative]
    if not all_relative:
        return
    lock_path = ROOT_DIR / ".git" / "lazyblog-webapp.lock"
    lock_path.parent.mkdir(exist_ok=True)
    force_add = f"git add -f -- {' '.join(json.dumps(path) for path in force_relative)}" if force_relative else ":"
    tracked_add = f"git add -u -- {' '.join(json.dumps(path) for path in tracked_relative)}" if tracked_relative else ":"
    path_args = " ".join(json.dumps(path) for path in all_relative)
    script = f"""
set -euo pipefail
cd {json.dumps(str(ROOT_DIR))}
{force_add}
{tracked_add}
if git diff --cached --quiet -- {path_args}; then
  echo "No changes to commit for: {message}"
  exit 0
fi
git commit -m {json.dumps(message)}
for attempt in 1 2 3 4 5; do
  if git push origin HEAD:{json.dumps(branch)}; then
    exit 0
  fi
  git fetch origin {json.dumps(branch)} || true
  git rebase origin/{branch} || git rebase --abort || true
  sleep $((attempt * 2))
done
echo "Failed to push after retries: {message}" >&2
exit 1
"""
    subprocess.run(["flock", str(lock_path), "bash", "-lc", script], cwd=ROOT_DIR, check=True)


def codex_git_commit_push_mixed(
    *,
    force_paths: list[Path] | None = None,
    tracked_paths: list[Path] | None = None,
    message: str,
    branch: str,
    enabled: bool,
    model: str = DEFAULT_GIT_MODEL,
    reasoning: str = DEFAULT_GIT_REASONING,
    timeout: int = 600,
) -> None:
    if not enabled:
        return
    force_relative = [str(path.relative_to(ROOT_DIR)) for path in (force_paths or []) if path.exists()]
    tracked_relative = [str(path.relative_to(ROOT_DIR)) for path in (tracked_paths or []) if path.exists()]
    all_relative = [*force_relative, *tracked_relative]
    if not all_relative:
        return

    run_dir = JOB_ROOT / "git-commit-push" / f"{stamp()}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = ROOT_DIR / ".git" / "lazyblog-webapp.lock"
    lock_path.parent.mkdir(exist_ok=True)
    force_add = f"git add -f -- {' '.join(json.dumps(path) for path in force_relative)}" if force_relative else ":"
    tracked_add = f"git add -u -- {' '.join(json.dumps(path) for path in tracked_relative)}" if tracked_relative else ":"
    path_args = " ".join(json.dumps(path) for path in all_relative)
    script_path = run_dir / "commit-push.sh"
    script = f"""#!/usr/bin/env bash
set -euo pipefail
cd {json.dumps(str(ROOT_DIR))}
{force_add}
{tracked_add}
if git diff --cached --quiet -- {path_args}; then
  echo "No changes to commit for: {message}"
  exit 0
fi
git commit -m {json.dumps(message)}
for attempt in 1 2 3 4 5; do
  if git push origin HEAD:{json.dumps(branch)}; then
    exit 0
  fi
  git fetch origin {json.dumps(branch)} || true
  git rebase origin/{branch} || git rebase --abort || true
  sleep $((attempt * 2))
done
echo "Failed to push after retries: {message}" >&2
exit 1
"""
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)
    payload = {
        "script_path": str(script_path),
        "root_dir": str(ROOT_DIR),
        "force_paths": force_relative,
        "tracked_paths": tracked_relative,
        "message": message,
        "branch": branch,
        "model": model,
        "reasoning": reasoning,
        "contract": "Run only bash script_path. The script contains the exact allowlisted git operation.",
    }
    write_json(run_dir / "input.json", payload)
    prompt = (
        load_prompt(GIT_COMMIT_PROMPT)
        + "\n\nInput JSON follows.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n"
    )
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    cmd = [
        "codex",
        "exec",
        "--ephemeral",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(ROOT_DIR),
        "--output-last-message",
        str(run_dir / "output.txt"),
        "-",
    ]
    proc = subprocess.run(
        ["flock", str(lock_path), *cmd],
        input=prompt,
        text=True,
        cwd=ROOT_DIR,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    (run_dir / "stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    write_json(
        run_dir / "run.json",
        {
            "model": model,
            "reasoning": reasoning,
            "returncode": proc.returncode,
            "paths": all_relative,
            "message": message,
            "branch": branch,
            "finished_at": now_iso(),
        },
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)


class LazyBlogStudio:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        CHAT_ROOT.mkdir(parents=True, exist_ok=True)
        DRAFT_ROOT.mkdir(parents=True, exist_ok=True)
        JOB_ROOT.mkdir(parents=True, exist_ok=True)
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        UPLOAD_MIRROR_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        TRANSLATION_JOB_ROOT.mkdir(parents=True, exist_ok=True)
        TAXONOMY_ROOT.mkdir(parents=True, exist_ok=True)
        POST_PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
        self.job_lock = threading.Lock()
        self.artifact_lock = threading.Lock()
        self.chat_queue_lock = threading.Lock()
        self.composer_lock = threading.Lock()
        self.chat_queue_event = threading.Event()
        self.event_lock = threading.Condition()
        self.event_seq = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.reset_stale_chat_queue_items()
        self.chat_queue_thread = threading.Thread(target=self.chat_queue_loop, daemon=True)
        self.chat_queue_thread.start()

    def new_session_id(self) -> str:
        return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def session_dir(self, session_id: str) -> Path:
        return CHAT_ROOT / safe_session_id(session_id)

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def load_session(self, session_id: str) -> dict[str, Any]:
        path = self.session_meta_path(session_id)
        if not path.exists():
            raise WebAppError(f"unknown session: {session_id}")
        return read_json(path)

    def save_session(self, session_id: str, meta: dict[str, Any]) -> None:
        meta["updated_at"] = now_iso()
        write_json(self.session_meta_path(session_id), meta)
        self.emit_event("session_updated", session_id, {"session_id": safe_session_id(session_id)})
        self.emit_event("sessions_changed", "", {"session_id": safe_session_id(session_id)})

    def composer_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "composer.json"

    def composer_payload(self, session_id: str) -> dict[str, Any]:
        safe_id = safe_session_id(session_id)
        self.load_session(safe_id)
        stored = read_json(self.composer_path(safe_id), {})
        return {
            "session_id": safe_id,
            "text": str(stored.get("text") or ""),
            "version": max(0, int(stored.get("version") or 0)),
            "updated_at": str(stored.get("updated_at") or ""),
            "client_id": str(stored.get("client_id") or ""),
        }

    def save_composer(
        self,
        text: str,
        session_id: str | None = None,
        *,
        client_id: str = "",
        base_version: int = 0,
    ) -> dict[str, Any]:
        clean_text = str(text or "")
        if len(clean_text) > MAX_COMPOSER_TEXT_CHARS:
            raise WebAppError(f"composer text exceeds {MAX_COMPOSER_TEXT_CHARS} characters")
        clean_client_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(client_id or "browser"))[:100] or "browser"
        safe_id = safe_session_id(session_id) if session_id else ""
        created = False
        if not safe_id:
            if not clean_text.strip():
                raise WebAppError("cannot create an empty chat composer")
            session = self.create_session(clean_text)
            safe_id = str(session["id"])
            created = True

        with self.composer_lock:
            session = self.load_session(safe_id)
            path = self.composer_path(safe_id)
            current = read_json(path, {})
            current_text = str(current.get("text") or "")
            current_version = max(0, int(current.get("version") or 0))
            current_client_id = str(current.get("client_id") or "")
            stale_other_client = (
                int(base_version or 0) < current_version
                and clean_client_id != current_client_id
                and clean_text != current_text
            )
            if stale_other_client:
                conflict_dir = self.session_dir(safe_id) / "composer-conflicts"
                write_json(
                    conflict_dir / f"{stamp()}-{uuid.uuid4().hex[:8]}-{clean_client_id}.json",
                    {
                        "session_id": safe_id,
                        "text": clean_text,
                        "base_version": int(base_version or 0),
                        "server_version": current_version,
                        "client_id": clean_client_id,
                        "created_at": now_iso(),
                    },
                )
                return {
                    "composer": self.composer_payload(safe_id),
                    "session": session,
                    "created": created,
                    "conflict": True,
                }

            if clean_text != current_text or clean_client_id != current_client_id:
                current_version += 1
                current = {
                    "session_id": safe_id,
                    "text": clean_text,
                    "version": current_version,
                    "updated_at": now_iso(),
                    "client_id": clean_client_id,
                }
                write_json(path, current)
                self.emit_event(
                    "composer_updated",
                    safe_id,
                    {
                        "session_id": safe_id,
                        "version": current_version,
                        "client_id": clean_client_id,
                    },
                )

        return {
            "composer": self.composer_payload(safe_id),
            "session": session,
            "created": created,
            "conflict": False,
        }

    def emit_event(self, event_type: str, session_id: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_session = safe_session_id(session_id) if session_id else ""
        with self.event_lock:
            self.event_seq += 1
            event = {
                "id": self.event_seq,
                "type": str(event_type or "changed")[:80],
                "session_id": safe_session,
                "payload": payload or {},
                "created_at": now_iso(),
            }
            self.events.append(event)
            self.event_lock.notify_all()
            return event

    def event_matches_session(self, event: dict[str, Any], session_id: str) -> bool:
        if not session_id:
            return True
        event_session = str(event.get("session_id") or "")
        if not event_session or event_session == session_id:
            return True
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        session_ids = payload.get("session_ids") if isinstance(payload, dict) else []
        return session_id in {safe_session_id(str(item)) for item in list_from_value(session_ids)}

    def events_since(self, last_id: int, session_id: str = "") -> list[dict[str, Any]]:
        safe_session = safe_session_id(session_id) if session_id else ""
        return [event for event in list(self.events) if int(event.get("id") or 0) > last_id and self.event_matches_session(event, safe_session)]

    def wait_for_events(self, last_id: int, session_id: str = "", timeout: float = 25.0) -> list[dict[str, Any]]:
        deadline = time.time() + max(1.0, timeout)
        with self.event_lock:
            while True:
                events = self.events_since(last_id, session_id)
                if events:
                    return events
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self.event_lock.wait(timeout=remaining)

    def artifact_dir(self, session_id: str) -> Path:
        return ARTIFACT_ROOT / safe_session_id(session_id)

    def artifact_index_path(self, session_id: str) -> Path:
        return self.artifact_dir(session_id) / "artifacts.json"

    def artifact_files_dir(self, session_id: str) -> Path:
        return self.artifact_dir(session_id) / "files"

    def read_artifact_index(self, session_id: str) -> dict[str, Any]:
        payload = read_json(self.artifact_index_path(session_id), default={"items": [], "selected_artifact_id": ""})
        if not isinstance(payload.get("items"), list):
            payload["items"] = []
        payload["selected_artifact_id"] = str(payload.get("selected_artifact_id") or "")
        return payload

    def write_artifact_index(self, session_id: str, payload: dict[str, Any]) -> None:
        payload["updated_at"] = now_iso()
        write_json(self.artifact_index_path(session_id), payload)
        self.emit_event("artifacts_changed", session_id, {"session_id": safe_session_id(session_id)})

    def safe_artifact_relative_path(self, raw_path: str) -> str:
        value = str(raw_path or "").strip()
        if not value:
            return ""
        raw = Path(value)
        resolved = raw.resolve() if raw.is_absolute() else (ROOT_DIR / raw).resolve()
        root = ROOT_DIR.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise WebAppError("artifact path must stay inside the LazyBlog workspace") from exc
        normalized = str(relative).replace(os.sep, "/")
        lowered = normalized.lower()
        parts = [part.lower() for part in relative.parts]
        if any(part in ARTIFACT_SECRET_MARKERS for part in parts):
            raise WebAppError("artifact path is blocked because it looks secret-like")
        secret_words = {"password", "secret", "token", "credential", "private-key"}
        if any(word in lowered for word in secret_words):
            raise WebAppError("artifact path is blocked because it looks secret-like")
        if not any(lowered == prefix or lowered.startswith(f"{prefix}/") for prefix in ARTIFACT_ALLOWED_PREFIXES):
            raise WebAppError("artifact path is outside the artifact-safe generated areas")
        return normalized

    def public_artifact(self, item: dict[str, Any], selected_id: str = "") -> dict[str, Any]:
        artifact_id = str(item.get("id") or "")
        return {
            "id": artifact_id,
            "session_id": str(item.get("session_id") or ""),
            "kind": artifact_kind_for(str(item.get("path") or ""), str(item.get("mime") or ""), str(item.get("kind") or "")),
            "title": str(item.get("title") or "Artifact")[:160],
            "path": str(item.get("path") or ""),
            "preview": compact_preview(item.get("preview") or item.get("path") or ""),
            "source": str(item.get("source") or "backend"),
            "tab": str(item.get("tab") or "canvas"),
            "created_at": str(item.get("created_at") or item.get("updated_at") or ""),
            "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
            "mime": str(item.get("mime") or mime_for_artifact_path(str(item.get("path") or ""))),
            "size": int(item.get("size") or 0),
            "selected": artifact_id == selected_id,
        }

    def register_artifact(
        self,
        session_id: str,
        *,
        title: str,
        kind: str = "text",
        path: str = "",
        text: str = "",
        mime: str = "",
        preview: str = "",
        source: str = "backend",
        tab: str = "canvas",
        selected: bool = True,
        artifact_id: str = "",
    ) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        relative_path = self.safe_artifact_relative_path(path) if path else ""
        resolved_kind = artifact_kind_for(relative_path, mime, kind)
        if not relative_path and text:
            candidate_id = artifact_id or stable_artifact_id(safe_session, source, resolved_kind, title, text[:512])
            safe_artifact = safe_artifact_id(candidate_id)
            extension = {
                "markdown": ".md",
                "json": ".json",
                "diff": ".diff",
                "text": ".txt",
            }.get(resolved_kind, ".txt")
            filename = f"{safe_artifact}-{safe_slug_token(title, 'artifact')}{extension}"
            file_path = self.artifact_files_dir(safe_session) / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(str(text), encoding="utf-8")
            relative_path = str(file_path.relative_to(ROOT_DIR)).replace(os.sep, "/")
        if not relative_path:
            raise WebAppError("artifact requires either a safe path or inline text")
        resolved_mime = mime or mime_for_artifact_path(relative_path)
        if not artifact_id:
            artifact_id = stable_artifact_id(safe_session, source, relative_path, resolved_kind)
        safe_artifact = safe_artifact_id(artifact_id)
        item = {
            "id": safe_artifact,
            "session_id": safe_session,
            "kind": resolved_kind,
            "title": str(title or Path(relative_path).name or "Artifact")[:160],
            "path": relative_path,
            "preview": compact_preview(preview or title or relative_path),
            "source": str(source or "backend")[:80],
            "tab": str(tab or "canvas")[:40],
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "mime": resolved_mime,
            "size": (ROOT_DIR / relative_path).stat().st_size if (ROOT_DIR / relative_path).exists() else 0,
            "selected": bool(selected),
        }
        with self.artifact_lock:
            index = self.read_artifact_index(safe_session)
            by_id = {str(existing.get("id") or ""): existing for existing in index.get("items", []) if isinstance(existing, dict)}
            if safe_artifact in by_id:
                item["created_at"] = str(by_id[safe_artifact].get("created_at") or item["created_at"])
            by_id[safe_artifact] = {**by_id.get(safe_artifact, {}), **item}
            index["items"] = sorted(by_id.values(), key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
            if selected:
                index["selected_artifact_id"] = safe_artifact
            self.write_artifact_index(safe_session, index)
        return self.public_artifact(item, selected_id=safe_artifact if selected else "")

    def register_artifact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = self.register_artifact(
            str(payload.get("session_id", "")),
            title=str(payload.get("title") or "Backend artifact"),
            kind=str(payload.get("kind") or "text"),
            path=str(payload.get("path") or payload.get("file_path") or ""),
            text=str(payload.get("text") or payload.get("content") or payload.get("markdown") or ""),
            mime=str(payload.get("mime") or ""),
            preview=str(payload.get("preview") or payload.get("note") or ""),
            source=str(payload.get("source") or "api"),
            tab=str(payload.get("tab") or "canvas"),
            selected=payload.get("selected", True) is not False,
            artifact_id=str(payload.get("artifact_id") or payload.get("id") or ""),
        )
        return {"artifact": item, **self.artifact_bundle(str(payload.get("session_id", "")))}

    def attachment_artifact_rows(self, session_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        safe_session = safe_session_id(session_id)
        for message_path in self.message_paths(safe_session):
            try:
                front_matter, _body = split_front_matter(message_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            created_at = str(front_matter.get("created_at") or "")
            attachments = self.parse_attachments_json(front_matter.get("attachments_json") or front_matter.get("attachments"))
            for attachment in attachments:
                name = str(attachment.get("name") or "Attachment")
                mime = str(attachment.get("mime") or "")
                stored_path = str(attachment.get("stored_path") or "")
                preview = str(attachment.get("analysis_note") or attachment.get("text_excerpt") or "")
                if stored_path:
                    try:
                        relative = self.safe_artifact_relative_path(stored_path)
                    except WebAppError:
                        relative = ""
                    if relative:
                        kind = artifact_kind_for(relative, mime, str(attachment.get("preview_kind") or attachment.get("kind") or "file"))
                        tab = "canvas" if kind in {"image", "video"} else ("pdf" if kind == "pdf" else "explorer")
                        rows.append(
                            {
                                "id": stable_artifact_id(safe_session, "upload", relative),
                                "session_id": safe_session,
                                "kind": kind,
                                "title": name,
                                "path": relative,
                                "preview": compact_preview(preview or f"Uploaded {kind} attachment."),
                                "source": "upload",
                                "tab": tab,
                                "created_at": created_at,
                                "updated_at": created_at,
                                "mime": mime or mime_for_artifact_path(relative),
                                "size": int(attachment.get("size") or 0),
                            }
                        )
                mirror_path = str(attachment.get("mirror_markdown_path") or "")
                if mirror_path:
                    try:
                        relative = self.safe_artifact_relative_path(mirror_path)
                    except WebAppError:
                        relative = ""
                    if relative:
                        rows.append(
                            {
                                "id": stable_artifact_id(safe_session, "upload-mirror", relative),
                                "session_id": safe_session,
                                "kind": "markdown",
                                "title": f"Analysis: {name}",
                                "path": relative,
                                "preview": compact_preview(preview or "Attachment analysis markdown mirror."),
                                "source": "upload-mirror",
                                "tab": "editor",
                                "created_at": created_at,
                                "updated_at": created_at,
                                "mime": "text/markdown; charset=utf-8",
                                "size": int((ROOT_DIR / relative).stat().st_size) if (ROOT_DIR / relative).exists() else 0,
                            }
                        )
        return rows

    def register_attachment_artifacts(self, session_id: str, queue_id: str, attachments: list[dict[str, Any]]) -> None:
        for attachment in attachments:
            name = str(attachment.get("name") or "Attachment")
            mime = str(attachment.get("mime") or "")
            stored_path = str(attachment.get("stored_path") or "")
            if stored_path:
                kind = artifact_kind_for(stored_path, mime, str(attachment.get("preview_kind") or attachment.get("kind") or "file"))
                tab = "canvas" if kind in {"image", "video"} else ("pdf" if kind == "pdf" else "explorer")
                try:
                    self.register_artifact(
                        session_id,
                        title=name,
                        kind=kind,
                        path=stored_path,
                        mime=mime,
                        preview=str(attachment.get("analysis_note") or attachment.get("text_excerpt") or ""),
                        source="upload",
                        tab=tab,
                        selected=kind in {"image", "video", "pdf"},
                        artifact_id=stable_artifact_id(session_id, "upload", stored_path),
                    )
                except WebAppError:
                    pass
            mirror_path = str(attachment.get("mirror_markdown_path") or "")
            if mirror_path:
                try:
                    self.register_artifact(
                        session_id,
                        title=f"Analysis: {name}",
                        kind="markdown",
                        path=mirror_path,
                        mime="text/markdown; charset=utf-8",
                        preview=str(attachment.get("analysis_note") or attachment.get("text_excerpt") or "Attachment analysis markdown mirror."),
                        source="upload-mirror",
                        tab="editor",
                        selected=False,
                        artifact_id=stable_artifact_id(session_id, "upload-mirror", mirror_path),
                    )
                except WebAppError:
                    pass

    def artifact_bundle(self, session_id: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        index = self.read_artifact_index(safe_session)
        selected_id = str(index.get("selected_artifact_id") or "")
        by_id: dict[str, dict[str, Any]] = {}
        for row in self.attachment_artifact_rows(safe_session):
            by_id[str(row.get("id") or "")] = row
        for row in index.get("items", []):
            if isinstance(row, dict) and row.get("id"):
                by_id[str(row["id"])] = {**by_id.get(str(row["id"]), {}), **row}
        items = sorted(by_id.values(), key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        if not selected_id or selected_id not in by_id:
            selected_id = next((str(item.get("id")) for item in items if item.get("tab") == "canvas"), "") or (str(items[0].get("id")) if items else "")
        return {
            "items": [self.public_artifact(item, selected_id=selected_id) for item in items],
            "selected_artifact_id": selected_id,
        }

    def select_artifact(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        safe_artifact = safe_artifact_id(artifact_id)
        bundle = self.artifact_bundle(safe_session)
        if not any(item.get("id") == safe_artifact for item in bundle["items"]):
            raise WebAppError("unknown artifact")
        with self.artifact_lock:
            index = self.read_artifact_index(safe_session)
            index["selected_artifact_id"] = safe_artifact
            self.write_artifact_index(safe_session, index)
        return self.artifact_bundle(safe_session)

    def artifact_content(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        safe_artifact = safe_artifact_id(artifact_id)
        bundle = self.artifact_bundle(safe_session)
        item = next((row for row in bundle["items"] if row.get("id") == safe_artifact), None)
        if not item:
            raise WebAppError("unknown artifact")
        relative = self.safe_artifact_relative_path(str(item.get("path") or ""))
        path = ROOT_DIR / relative
        if not path.exists():
            raise WebAppError("artifact file is missing")
        size = path.stat().st_size
        kind = artifact_kind_for(relative, str(item.get("mime") or ""), str(item.get("kind") or ""))
        mime = str(item.get("mime") or mime_for_artifact_path(relative))
        base = {
            "id": safe_artifact,
            "session_id": safe_session,
            "kind": kind,
            "title": str(item.get("title") or path.name),
            "path": relative,
            "mime": mime,
            "size": size,
        }
        if kind in {"image", "video", "pdf"}:
            if size > MAX_ARTIFACT_BINARY_BYTES:
                return {**base, "error": f"artifact is too large to preview inline ({size} bytes)"}
            return {**base, "data_url": bytes_to_data_url(path.read_bytes(), mime)}
        raw = path.read_bytes()[: MAX_ARTIFACT_TEXT_BYTES + 1]
        truncated = len(raw) > MAX_ARTIFACT_TEXT_BYTES
        text = raw[:MAX_ARTIFACT_TEXT_BYTES].decode("utf-8", errors="replace")
        return {**base, "text": text, "truncated": truncated}

    def register_job_output_artifacts(self, job: dict[str, Any], output: Any) -> None:
        session_id = str(job.get("session_id") or "")
        if not session_id or not isinstance(output, dict):
            return
        artifacts = output.get("artifacts")
        if not isinstance(artifacts, list):
            return
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            raw_kind = str(artifact.get("kind") or "text").strip().lower()
            raw_content = str(artifact.get("content") or "")
            artifact_path = str(artifact.get("path") or artifact.get("file_path") or "")
            artifact_text = str(artifact.get("text") or artifact.get("markdown") or "")
            if raw_kind == "path" and not artifact_path:
                artifact_path = raw_content
            elif raw_content and not artifact_text and not artifact_path:
                artifact_text = raw_content
            resolved_kind = "text" if raw_kind in {"path", "url", "code"} else raw_kind
            try:
                self.register_artifact(
                    session_id,
                    title=str(artifact.get("title") or artifact.get("name") or "Codex artifact"),
                    kind=resolved_kind,
                    path=artifact_path,
                    text=artifact_text,
                    mime=str(artifact.get("mime") or ""),
                    preview=str(artifact.get("preview") or artifact.get("note") or ""),
                    source=f"codex:{job.get('tool') or 'job'}",
                    tab=str(artifact.get("tab") or "canvas"),
                    selected=artifact.get("selected", True) is not False,
                    artifact_id=str(artifact.get("id") or artifact.get("artifact_id") or ""),
                )
            except Exception:
                continue

    def should_generate_function_artifacts(self, message: str) -> bool:
        lowered = str(message or "").casefold()
        wants_output = any(token in lowered for token in ["generate", "create", "plot", "figure", "graph", "pdf", "draw", "compile", "生成", "画", "繪"])
        mentions_function = "f(x)" in lowered or "x - e^x" in lowered or "x-e^x" in lowered or "x - exp(x)" in lowered or "x-exp(x)" in lowered
        wants_visual = any(token in lowered for token in ["figure", "plot", "graph", "image", "pdf", "canvas", "图", "圖", "曲线", "曲線"])
        return wants_output and mentions_function and wants_visual

    def generate_function_artifacts(self, session_id: str, message: str, queue_id: str = "") -> list[dict[str, Any]]:
        safe_session = safe_session_id(session_id)
        files_dir = self.artifact_files_dir(safe_session)
        files_dir.mkdir(parents=True, exist_ok=True)
        base = f"{stamp()}-{safe_slug_token(queue_id or 'function-plot', 'function-plot')}-x-minus-exp-x"
        png_path = files_dir / f"{base}.png"
        tex_path = files_dir / f"{base}.tex"
        pdf_path = files_dir / f"{base}.pdf"
        md_path = files_dir / f"{base}.md"

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        xs = [-4.0 + index * (6.2 / 620) for index in range(621)]
        ys = [x - (2.718281828459045**x) for x in xs]
        fig, ax = plt.subplots(figsize=(8, 4.8), dpi=160)
        ax.plot(xs, ys, color="#0f766e", linewidth=2.4, label=r"$f(x)=x-e^x$")
        ax.axhline(0, color="#1d2520", linewidth=0.8, alpha=0.55)
        ax.axvline(0, color="#1d2520", linewidth=0.8, alpha=0.55)
        ax.scatter([0], [-1], color="#d96b43", s=42, zorder=5, label=r"maximum at $(0,-1)$")
        ax.set_title(r"Plot of $f(x)=x-e^x$", fontsize=15)
        ax.set_xlabel("x")
        ax.set_ylabel("f(x)")
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(png_path)
        plt.close(fig)

        tex = rf"""\documentclass[11pt]{{article}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{amsmath,amssymb,graphicx}}
\usepackage{{hyperref}}
\title{{Backend Artifact Test: $f(x)=x-e^x$}}
\author{{LazyBlog Studio}}
\date{{{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}}}
\begin{{document}}
\maketitle

\section*{{Function}}
This generated artifact studies
\[
f(x)=x-e^x.
\]
Its derivative is
\[
f'(x)=1-e^x,
\]
so the only stationary point is at $x=0$. Since
\[
f''(x)=-e^x<0,
\]
that point is a maximum and $f(0)=-1$. The curve is always below zero, because $e^x>x$ for all real $x$.

\section*{{Figure}}
\begin{{center}}
\includegraphics[width=0.92\linewidth]{{{png_path.name}}}
\end{{center}}

\section*{{Backend Pipe Check}}
This PDF was compiled from a generated \texttt{{.tex}} file and registered with the LazyBlog Studio artifact pipe together with the plot image.

\end{{document}}
"""
        tex_path.write_text(tex, encoding="utf-8")

        compile_error = ""
        if shutil.which("pdflatex"):
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                cwd=files_dir,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if proc.returncode != 0 or not pdf_path.exists():
                compile_error = (proc.stderr or proc.stdout or "pdflatex failed").strip()[-1200:]
        if not pdf_path.exists():
            from matplotlib.backends.backend_pdf import PdfPages

            with PdfPages(pdf_path) as pdf:
                fig, ax = plt.subplots(figsize=(8.5, 11))
                ax.axis("off")
                ax.text(0.06, 0.94, "Backend Artifact Test: f(x)=x-e^x", fontsize=18, weight="bold", va="top")
                ax.text(
                    0.06,
                    0.84,
                    "f'(x)=1-e^x, so the stationary point is x=0.\n"
                    "f''(x)=-e^x<0, hence f(0)=-1 is a maximum.\n"
                    "The curve remains below zero because e^x>x for all real x.",
                    fontsize=12,
                    va="top",
                )
                ax.imshow(plt.imread(png_path), extent=(0.06, 0.94, 0.18, 0.68), aspect="auto")
                pdf.savefig(fig)
                plt.close(fig)

        md_body = "\n".join(
            [
                "# Backend Artifact Test: f(x)=x-e^x",
                "",
                "Generated from a LazyBlog Studio chat request.",
                "",
                f"- Plot image: `{png_path.relative_to(ROOT_DIR)}`",
                f"- LaTeX source: `{tex_path.relative_to(ROOT_DIR)}`",
                f"- Compiled PDF: `{pdf_path.relative_to(ROOT_DIR)}`",
                f"- Compile fallback used: {'yes' if compile_error else 'no'}",
                "",
                "## Notes",
                "",
                "- `f'(x)=1-e^x`",
                "- The maximum is at `(0, -1)`.",
                "- `f(x)` is always negative over the real line.",
                "",
                f"## Original request\n\n{message.strip()}",
            ]
        )
        if compile_error:
            md_body += f"\n\n## pdflatex output tail\n\n```text\n{compile_error}\n```\n"
        write_markdown(
            md_path,
            {
                "kind": "lazyblog-artifact-summary",
                "session_id": safe_session,
                "queue_id": queue_id,
                "created_at": now_iso(),
            },
            md_body,
        )

        artifacts = [
            self.register_artifact(
                safe_session,
                title="Plot: f(x)=x-e^x",
                kind="image",
                path=str(png_path.relative_to(ROOT_DIR)),
                mime="image/png",
                preview="Generated plot image for f(x)=x-e^x.",
                source="backend:function-plot",
                tab="canvas",
                selected=True,
            ),
            self.register_artifact(
                safe_session,
                title="PDF report: f(x)=x-e^x",
                kind="pdf",
                path=str(pdf_path.relative_to(ROOT_DIR)),
                mime="application/pdf",
                preview="Compiled PDF report containing the generated figure.",
                source="backend:function-plot",
                tab="pdf",
                selected=True,
            ),
            self.register_artifact(
                safe_session,
                title="LaTeX source: f(x)=x-e^x",
                kind="text",
                path=str(tex_path.relative_to(ROOT_DIR)),
                mime="text/x-tex; charset=utf-8",
                preview="LaTeX source used to compile the PDF artifact.",
                source="backend:function-plot",
                tab="editor",
                selected=False,
            ),
            self.register_artifact(
                safe_session,
                title="Artifact notes: f(x)=x-e^x",
                kind="markdown",
                path=str(md_path.relative_to(ROOT_DIR)),
                mime="text/markdown; charset=utf-8",
                preview="Markdown summary for the generated plot and PDF.",
                source="backend:function-plot",
                tab="editor",
                selected=False,
            ),
        ]
        return artifacts

    def maybe_generate_backend_artifacts(self, session_id: str, message: str, queue_id: str = "") -> dict[str, Any]:
        if not self.should_generate_function_artifacts(message):
            return {"generated": False, "artifacts": []}
        try:
            artifacts = self.generate_function_artifacts(session_id, message, queue_id=queue_id)
            return {"generated": True, "artifacts": artifacts, "error": ""}
        except Exception as exc:  # noqa: BLE001
            return {"generated": False, "artifacts": [], "error": str(exc)}

    def commit_session_state(self, session_id: str, message: str) -> None:
        try:
            codex_git_commit_push_mixed(
                tracked_paths=[self.session_meta_path(session_id)],
                message=message,
                branch=self.args.branch,
                enabled=self.args.commit_push,
                timeout=self.args.git_codex_timeout,
            )
        except subprocess.CalledProcessError:
            try:
                git_commit_push_mixed(
                    tracked_paths=[self.session_meta_path(session_id)],
                    message=message,
                    branch=self.args.branch,
                    enabled=self.args.commit_push,
                )
            except subprocess.CalledProcessError:
                pass

    def create_session(self, first_message: str = "") -> dict[str, Any]:
        session_id = self.new_session_id()
        title = first_message.strip().splitlines()[0][:80] if first_message.strip() else "Untitled chat"
        meta = {
            "id": session_id,
            "title": title,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "message_count": 0,
            "latest_draft": None,
            "published": [],
        }
        self.save_session(session_id, meta)
        return meta

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for path in CHAT_ROOT.glob("*/session.json"):
            try:
                sessions.append(read_json(path))
            except json.JSONDecodeError:
                continue
        return sorted(sessions, key=lambda item: item.get("updated_at", ""), reverse=True)

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        safe_id = safe_session_id(session_id)
        clean_title = " ".join(title.strip().split())[:120]
        if not clean_title:
            raise WebAppError("chat title cannot be empty")
        meta = self.load_session(safe_id)
        meta["title"] = clean_title
        self.save_session(safe_id, meta)
        return self.session_payload(safe_id)

    def auto_rename_session(self, session_id: str) -> dict[str, Any]:
        safe_id = safe_session_id(session_id)
        session = self.load_session(safe_id)
        recent_messages = [self.read_message(path) for path in self.message_paths(safe_id)[-20:]]
        if not recent_messages:
            raise WebAppError("cannot auto-rename an empty chat")
        prompt = """Generate a concise chat-history title.

Rules:
- Use the supplied session transcript only.
- Answer with only the title text in the `answer` field.
- 3 to 8 words is ideal.
- Keep the title in the dominant language of the chat.
- Do not include quotes, labels, prefixes, markdown, or punctuation unless it is part of a proper noun."""
        result = self.respond_with_codex(
            {
                "tool": "response",
                "schema": "response",
                "session_id": safe_id,
                "prompt": prompt,
                "input": {
                    "current_title": session.get("title", ""),
                    "recent_messages": recent_messages,
                },
                "wait": True,
            }
        )
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        if job.get("status") != "succeeded":
            raise WebAppError(f"auto rename failed: {job.get('error') or job.get('status') or 'unknown error'}")
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        title = str(output.get("answer") or output.get("summary") or "").strip()
        title = re.sub(r"^[`\"'“”‘’]+|[`\"'“”‘’]+$", "", title).strip()
        title = re.sub(r"^(title|chat title)\s*:\s*", "", title, flags=re.IGNORECASE).strip()
        title = " ".join(title.split())[:120]
        if not title:
            raise WebAppError("auto rename returned an empty title")
        payload = self.rename_session(safe_id, title)
        payload["auto_rename"] = {"title": title, "job": job}
        return payload

    def delete_session(self, session_id: str) -> dict[str, Any]:
        safe_id = safe_session_id(session_id)
        session_dir = self.session_dir(safe_id)
        if not session_dir.exists():
            raise WebAppError(f"unknown session: {safe_id}")
        trash_dir = CHAT_ROOT / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = trash_dir / f"{stamp()}-{safe_id}"
        session_dir.rename(target)
        self.emit_event("session_deleted", safe_id, {"session_id": safe_id})
        self.emit_event("sessions_changed", "", {"session_id": safe_id})
        return {"deleted": safe_id, "trash_path": str(target.relative_to(ROOT_DIR)), "sessions": self.list_sessions()}

    def append_message(self, session_id: str, role: str, content: str, extra: dict[str, Any] | None = None) -> Path:
        meta = self.load_session(session_id)
        msg_id = f"{stamp()}-{uuid.uuid4().hex[:6]}-{role}"
        path = self.session_dir(session_id) / "messages" / f"{msg_id}.md"
        front_matter = {
            "kind": "lazyblog-chat-message",
            "session_id": session_id,
            "role": role,
            "created_at": now_iso(),
        }
        if extra:
            front_matter.update(extra)
        write_markdown(path, front_matter, content)
        meta["message_count"] = int(meta.get("message_count", 0)) + 1
        if role == "user" and meta.get("title") in {"Untitled chat", ""}:
            title_candidate = content.strip().splitlines()[0][:80] if content.strip() else ""
            if not title_candidate and isinstance(extra, dict):
                try:
                    parsed = json.loads(str(extra.get("attachments_json") or "[]"))
                except json.JSONDecodeError:
                    parsed = []
                if isinstance(parsed, list) and parsed:
                    first_name = str((parsed[0] or {}).get("name") or "").strip()
                    title_candidate = first_name[:80]
            meta["title"] = title_candidate or "Untitled chat"
        self.save_session(session_id, meta)
        return path

    def attachment_storage_dir(self, session_id: str, queue_id: str) -> Path:
        return UPLOAD_ROOT / safe_session_id(session_id) / safe_job_id(queue_id or "manual")

    def store_chat_attachment_file(self, session_id: str, queue_id: str, attachment: dict[str, Any], blob: bytes) -> str:
        target_dir = self.attachment_storage_dir(session_id, queue_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = slugify(Path(str(attachment.get("name") or "attachment")).stem, "attachment")
        suffix = extension_for_mime(str(attachment.get("mime") or ""), str(attachment.get("name") or ""))
        path = target_dir / f"{len(list(target_dir.glob('*'))):03d}-{stem}{suffix}"
        path.write_bytes(blob)
        return str(path.relative_to(ROOT_DIR))

    def attachment_mirror_dir(self, session_id: str, queue_id: str) -> Path:
        return UPLOAD_MIRROR_ROOT / safe_session_id(session_id) / safe_job_id(queue_id or "manual")

    def write_attachment_mirror_markdown(self, session_id: str, queue_id: str, attachment: dict[str, Any]) -> str:
        markdown = str(attachment.get("analysis_markdown") or "").strip()
        if not markdown:
            markdown = self.attachment_analysis_markdown(attachment)
        stored_path = str(attachment.get("stored_path") or "")
        stored_name = Path(stored_path).name if stored_path else str(attachment.get("name") or "attachment")
        stem = safe_slug_token(Path(stored_name).stem, "attachment")
        mirror_dir = self.attachment_mirror_dir(session_id, queue_id)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        path = mirror_dir / f"{stem}.md"
        front_matter = {
            "kind": "lazyblog-upload-mirror",
            "session_id": session_id,
            "queue_id": queue_id,
            "original_name": str(attachment.get("name") or ""),
            "attachment_kind": str(attachment.get("kind") or "file"),
            "mime": str(attachment.get("mime") or "application/octet-stream"),
            "size": int(attachment.get("size") or 0),
            "stored_path": stored_path,
            "analysis_source": str(attachment.get("analysis_source") or ""),
            "analysis_status": str(attachment.get("analysis_status") or ""),
            "created_at": now_iso(),
        }
        body = "\n".join(
            [
                markdown,
                "",
                "### Storage",
                f"- Original upload path: `{stored_path}`" if stored_path else "- Original upload path: not stored",
                "- Raw uploads are intentionally stored under `content/uploads/` and ignored by git.",
            ]
        ).strip()
        write_markdown(path, front_matter, body)
        return str(path.relative_to(ROOT_DIR))

    def image_attachment_codex_result(self, session_id: str, image_path: Path, attachment: dict[str, Any]) -> dict[str, Any]:
        run_dir = self.session_dir(session_id) / "tool-runs" / f"{stamp()}-{uuid.uuid4().hex[:6]}-attachment-vision"
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "attachment": {
                "name": str(attachment.get("name") or ""),
                "mime": str(attachment.get("mime") or ""),
                "stored_path": str(attachment.get("stored_path") or ""),
                "size": int(attachment.get("size") or 0),
            }
        }
        prompt_text = load_prompt(ATTACHMENT_VISION_PROMPT)
        full_prompt = (
            prompt_text
            + "\n\nAttachment JSON follows. Return only JSON matching the requested schema.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n"
        )
        write_json(run_dir / "input.json", payload)
        (run_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")
        output_path = run_dir / "output.json"
        if self.args.mock_codex:
            result = {
                "summary": "Image uploaded. Mock vision mode did not inspect pixels.",
                "detected_text": "",
                "visible_language": "unknown",
                "notable_elements": [],
                "needs_review": True,
            }
            write_json(output_path, result)
            return result
        started = time.time()
        profile = self.codex_profile("response")
        routed = self.run_structured_prompt(
            full_prompt=full_prompt,
            schema_path=ATTACHMENT_VISION_SCHEMA,
            output_path=output_path,
            model=profile["model"],
            reasoning=profile["reasoning"],
            extra_codex_args=["--image", str(image_path)],
            allow_aginti=False,
        )
        (run_dir / "stdout.log").write_text(str(routed.get("stdout") or ""), encoding="utf-8")
        (run_dir / "stderr.log").write_text(str(routed.get("stderr") or ""), encoding="utf-8")
        write_json(
            run_dir / "run.json",
            {
                "tool": "attachment-vision",
                "model": str(routed.get("route", {}).get("model") or profile["model"]),
                "reasoning": str(routed.get("route", {}).get("reasoning") or profile["reasoning"]),
                "route": routed.get("route"),
                "attempts": routed.get("attempts", []),
                "returncode": 0,
                "elapsed_seconds": round(time.time() - started, 2),
                "image": str(image_path.relative_to(ROOT_DIR)) if ROOT_DIR in image_path.parents else str(image_path),
                "output": str(output_path.relative_to(ROOT_DIR)) if output_path.exists() else "",
            },
        )
        if not output_path.exists():
            raise WebAppError("attachment vision tool did not write output JSON")
        return read_json(output_path)

    def attachment_image_vision_markdown(self, attachment: dict[str, Any], vision: dict[str, Any]) -> str:
        name = str(attachment.get("name") or "attachment")
        mime = str(attachment.get("mime") or "image")
        size = int(attachment.get("size") or 0)
        lines = [
            f"### Attachment: {name}",
            "- Kind: image",
            f"- MIME: {mime}",
            f"- Size: {size} bytes",
        ]
        width = int(attachment.get("width") or 0)
        height = int(attachment.get("height") or 0)
        if width > 0 and height > 0:
            lines.append(f"- Dimensions: {width} x {height}")
        summary = str(vision.get("summary") or "").strip()
        language = str(vision.get("visible_language") or "").strip()
        if summary:
            lines.append(f"- Summary: {summary}")
        if language:
            lines.append(f"- Visible language: {language}")
        notable = [str(item).strip() for item in list_from_value(vision.get("notable_elements")) if str(item).strip()]
        if notable:
            lines.append(f"- Notable elements: {', '.join(notable[:8])}")
        detected_text = str(vision.get("detected_text") or "").strip()
        if detected_text:
            lines.extend(["", "```text", detected_text[:4000], "```"])
        if bool(vision.get("needs_review", False)):
            lines.append("- Note: OCR/caption output should be reviewed by a human.")
        return "\n".join(lines).strip()

    def attachment_image_size(self, raw: bytes) -> tuple[int, int]:
        if not raw:
            return (0, 0)
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as image:
                return (int(image.width), int(image.height))
        except Exception:
            return (0, 0)

    def attachment_pdf_preview(self, raw: bytes) -> tuple[str, int, str]:
        if not raw:
            return ("", 0, "")
        try:
            import fitz

            document = fitz.open(stream=raw, filetype="pdf")
            page_count = int(document.page_count or 0)
            text_excerpt = ""
            try:
                text_parts: list[str] = []
                for index in range(min(page_count, 4)):
                    chunk = document.load_page(index).get_text("text").strip()
                    if chunk:
                        text_parts.append(chunk)
                text_excerpt = "\n\n".join(text_parts).strip()[:6000]
            except Exception:
                text_excerpt = ""
            preview_url = ""
            if page_count > 0:
                page = document.load_page(0)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.1, 1.1), alpha=False)
                preview_url = bytes_to_data_url(pixmap.tobytes("png"), "image/png")
            return (preview_url, page_count, text_excerpt)
        except Exception:
            return ("", 0, "")

    def attachment_video_preview(self, raw: bytes, mime: str, name: str) -> tuple[str, dict[str, Any]]:
        if not raw:
            return ("", {})
        suffix = Path(name or "video").suffix or mimetypes.guess_extension(mime or "") or ".bin"
        source_path = None
        preview_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="lazyblog-video-", suffix=suffix, delete=False) as source:
                source.write(raw)
                source_path = Path(source.name)
            info: dict[str, Any] = {}
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                try:
                    info = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    info = {}
            with tempfile.NamedTemporaryFile(prefix="lazyblog-video-preview-", suffix=".png", delete=False) as preview:
                preview_path = Path(preview.name)
            frame = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(960,iw)':-2",
                    str(preview_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            preview_url = ""
            if frame.returncode == 0 and preview_path.exists():
                preview_url = bytes_to_data_url(preview_path.read_bytes(), "image/png")
            return (preview_url, info if isinstance(info, dict) else {})
        except Exception:
            return ("", {})
        finally:
            for path in (source_path, preview_path):
                if path and path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def attachment_analysis_markdown(self, attachment: dict[str, Any]) -> str:
        name = str(attachment.get("name") or "attachment")
        kind = str(attachment.get("kind") or "file")
        mime = str(attachment.get("mime") or "application/octet-stream")
        size = int(attachment.get("size") or 0)
        lines = [f"### Attachment: {name}", f"- Kind: {kind}", f"- MIME: {mime}", f"- Size: {size} bytes"]
        width = int(attachment.get("width") or 0)
        height = int(attachment.get("height") or 0)
        if width > 0 and height > 0:
            lines.append(f"- Dimensions: {width} x {height}")
        duration = float(attachment.get("duration_seconds") or 0)
        if duration > 0:
            lines.append(f"- Duration: {duration:.2f} seconds")
        page_count = int(attachment.get("page_count") or 0)
        if page_count > 0:
            lines.append(f"- PDF pages: {page_count}")
        note = str(attachment.get("analysis_note") or "").strip()
        if note:
            lines.append(f"- Note: {note}")
        excerpt = str(attachment.get("text_excerpt") or "").strip()
        if excerpt:
            lines.extend(["", "```text", excerpt[:4000], "```"])
        return "\n".join(lines).strip()

    def attachment_context_text(self, attachments: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for attachment in attachments:
            markdown = str(attachment.get("analysis_markdown") or "").strip()
            if markdown:
                chunks.append(markdown)
        return "\n\n".join(chunks).strip()

    def effective_message_from_row(self, row: dict[str, Any]) -> str:
        parts: list[str] = []
        content = str(row.get("content") or "").strip()
        if content:
            parts.append(content)
        attachment_context = self.attachment_context_text(list(row.get("attachments") or []))
        if attachment_context:
            parts.append(attachment_context)
        return "\n\n".join(parts).strip()

    def normalize_chat_attachments(self, raw_attachments: Any, session_id: str = "", queue_id: str = "", analyze: bool = True) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not isinstance(raw_attachments, list):
            return normalized
        for raw in raw_attachments:
            if not isinstance(raw, dict):
                continue
            data_url = str(raw.get("data_url", "")).strip()
            stored_path = str(raw.get("stored_path", "")).strip()
            stored_file = (ROOT_DIR / stored_path).resolve() if stored_path else None
            if (not data_url.startswith("data:") or "," not in data_url) and stored_file and stored_file.exists() and ROOT_DIR in stored_file.parents:
                data_url = bytes_to_data_url(stored_file.read_bytes(), str(raw.get("mime") or "application/octet-stream"))
            if not data_url.startswith("data:") or "," not in data_url:
                continue
            try:
                size = int(raw.get("size", 0))
            except Exception:
                size = 0
            mime = str(raw.get("mime", raw.get("mime_type", "application/octet-stream"))).strip() or "application/octet-stream"
            name = str(raw.get("name", "attachment")).strip() or "attachment"
            kind = str(raw.get("kind", "")).strip().lower()
            if not kind:
                if mime.startswith("image/"):
                    kind = "image"
                elif mime.startswith("video/"):
                    kind = "video"
                else:
                    kind = "file"
            if kind not in {"image", "video", "file"}:
                kind = "file"
            detected_mime, blob = data_url_to_bytes(data_url)
            if detected_mime:
                mime = detected_mime
            if stored_path:
                try:
                    resolved_stored = (ROOT_DIR / stored_path).resolve()
                    if not resolved_stored.exists() or ROOT_DIR not in resolved_stored.parents:
                        stored_path = ""
                except OSError:
                    stored_path = ""
            if not stored_path and session_id and queue_id and blob:
                stored_path = self.store_chat_attachment_file(session_id, queue_id, {"name": name, "mime": mime}, blob)
            preview_url = str(raw.get("preview_url", data_url)).strip() or data_url
            preview_kind = kind
            text_excerpt = ""
            analysis_note = ""
            width = 0
            height = 0
            page_count = 0
            duration_seconds = 0.0
            if kind == "image":
                width, height = self.attachment_image_size(blob)
                analysis_note = "Image queued for Codex image reading." if not analyze else "Image uploaded."
                if analyze and stored_path:
                    try:
                        vision = self.image_attachment_codex_result(session_id, ROOT_DIR / stored_path, {"name": name, "mime": mime, "size": size, "stored_path": stored_path, "width": width, "height": height})
                        summary = str(vision.get("summary") or "").strip()
                        detected_text = str(vision.get("detected_text") or "").strip()
                        visible_language = str(vision.get("visible_language") or "").strip()
                        text_excerpt = detected_text[:6000]
                        analysis_note = summary or "Image uploaded. Codex inspected the image."
                        preview_kind = "image"
                    except Exception as exc:
                        visible_language = ""
                        analysis_note = f"Image uploaded. Codex image reading failed: {exc}"
                        vision = {
                            "summary": analysis_note,
                            "detected_text": "",
                            "visible_language": "",
                            "notable_elements": [],
                            "needs_review": True,
                        }
                elif analyze:
                    visible_language = ""
                    vision = {
                        "summary": "Image uploaded but no stored file was available for Codex inspection.",
                        "detected_text": "",
                        "visible_language": "",
                        "notable_elements": [],
                        "needs_review": True,
                    }
                else:
                    visible_language = str(raw.get("visible_language") or "")
                    vision = {
                        "summary": analysis_note,
                        "detected_text": "",
                        "visible_language": visible_language,
                        "notable_elements": [],
                        "needs_review": False,
                    }
            elif kind == "video":
                if analyze:
                    preview_candidate, info = self.attachment_video_preview(blob, mime, name)
                    if preview_candidate:
                        preview_url = preview_candidate
                    streams = info.get("streams") if isinstance(info.get("streams"), list) else []
                    video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), {})
                    width = int(video_stream.get("width") or 0)
                    height = int(video_stream.get("height") or 0)
                    format_info = info.get("format") if isinstance(info.get("format"), dict) else {}
                    try:
                        duration_seconds = float(format_info.get("duration") or 0)
                    except Exception:
                        duration_seconds = 0.0
                    analysis_note = "Video uploaded. A preview frame and technical metadata were extracted locally."
                else:
                    width = int(raw.get("width") or 0)
                    height = int(raw.get("height") or 0)
                    try:
                        duration_seconds = float(raw.get("duration_seconds") or 0)
                    except Exception:
                        duration_seconds = 0.0
                    analysis_note = "Video queued for preview extraction and metadata analysis."
            elif mime_is_pdf(mime, name):
                if analyze:
                    preview_candidate, page_count, text_excerpt = self.attachment_pdf_preview(blob)
                    if preview_candidate:
                        preview_url = preview_candidate
                        preview_kind = "pdf"
                    else:
                        preview_kind = "pdf"
                    analysis_note = "PDF uploaded. Text was extracted locally when available."
                else:
                    preview_kind = "pdf"
                    page_count = int(raw.get("page_count") or 0)
                    text_excerpt = str(raw.get("text_excerpt") or "")
                    analysis_note = "PDF queued for text extraction."
            attachment = {
                "name": name,
                "kind": kind,
                "mime": mime,
                "size": size,
                "data_url": data_url,
                "stored_path": stored_path,
                "preview_url": preview_url,
                "preview_kind": preview_kind,
                "width": width,
                "height": height,
                "page_count": page_count,
                "duration_seconds": duration_seconds,
                "text_excerpt": text_excerpt[:6000],
                "analysis_note": analysis_note,
                "analysis_source": "",
                "analysis_status": "succeeded" if analyze else "queued",
            }
            if kind == "image":
                attachment["visible_language"] = visible_language
                attachment["analysis_source"] = "codex-exec:gpt-5.4:medium:image" if analyze else "pending"
                attachment["analysis_markdown"] = self.attachment_image_vision_markdown(attachment, vision) if analyze else ""
            else:
                attachment["analysis_source"] = "local-extraction" if analyze else "pending"
                attachment["analysis_markdown"] = self.attachment_analysis_markdown(attachment) if analyze else ""
            if analyze and session_id and queue_id:
                try:
                    attachment["mirror_markdown_path"] = self.write_attachment_mirror_markdown(session_id, queue_id, attachment)
                except Exception as exc:
                    attachment["mirror_markdown_path"] = ""
                    attachment["analysis_note"] = (str(attachment.get("analysis_note") or "").strip() + f" Mirror markdown failed: {exc}").strip()
            normalized.append(
                attachment
            )
        return normalized

    def chat_queue_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "queue"

    def chat_queue_path(self, session_id: str, queue_id: str) -> Path:
        return self.chat_queue_dir(session_id) / f"{safe_job_id(queue_id)}.json"

    def read_chat_queue_item(self, path: Path) -> dict[str, Any]:
        return read_json(path)

    def write_chat_queue_item(self, item: dict[str, Any]) -> None:
        session_id = safe_session_id(str(item["session_id"]))
        queue_id = safe_job_id(str(item["id"]))
        write_json(self.chat_queue_path(session_id, queue_id), item)
        self.emit_event(
            "session_updated",
            session_id,
            {"session_id": session_id, "queue_id": queue_id, "queue_status": str(item.get("status") or "")},
        )

    def update_chat_queue_item(self, item: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        with self.chat_queue_lock:
            current = self.read_chat_queue_item(self.chat_queue_path(str(item["session_id"]), str(item["id"])))
            current.update(updates)
            current["updated_at"] = now_iso()
            self.write_chat_queue_item(current)
            return current

    def chat_queue_items(self, session_id: str | None = None, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        roots = [self.chat_queue_dir(safe_session_id(session_id))] if session_id else [path / "queue" for path in CHAT_ROOT.glob("*") if path.is_dir()]
        items: list[dict[str, Any]] = []
        for root in roots:
            for path in root.glob("*.json"):
                try:
                    item = self.read_chat_queue_item(path)
                except json.JSONDecodeError:
                    continue
                if statuses and item.get("status") not in statuses:
                    continue
                items.append(item)
        return sorted(items, key=lambda item: (item.get("created_at", ""), item.get("id", "")))

    def chat_queue_summary(self, session_id: str) -> dict[str, Any]:
        items = self.chat_queue_items(session_id=session_id)
        active = [item for item in items if item.get("status") in {"queued", "running"}]
        return {
            "active_count": len(active),
            "queued_count": sum(1 for item in active if item.get("status") == "queued"),
            "running_count": sum(1 for item in active if item.get("status") == "running"),
            "items": active[-10:],
        }

    def reset_stale_chat_queue_items(self) -> None:
        for item in self.chat_queue_items(statuses={"running"}):
            item["status"] = "queued"
            item["updated_at"] = now_iso()
            item["requeued_after_restart"] = True
            self.write_chat_queue_item(item)

    def enqueue_chat_message(self, message: str, session_id: str | None = None, attachments: Any = None) -> dict[str, Any]:
        if not message.strip():
            if not attachments:
                raise WebAppError("message is empty")
        session = self.create_session(message) if not session_id else self.load_session(safe_session_id(session_id))
        resolved_session_id = session["id"]
        queue_id = f"{stamp()}-{uuid.uuid4().hex[:8]}-chat"
        clean_attachments = self.normalize_chat_attachments(attachments, session_id=resolved_session_id, queue_id=queue_id, analyze=False)
        user_path = self.append_message(
            resolved_session_id,
            "user",
            message,
            {"queue_id": queue_id, "queue_status": "queued", "attachments_json": json.dumps(clean_attachments, ensure_ascii=False)},
        )
        item = {
            "id": queue_id,
            "session_id": resolved_session_id,
            "status": "queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "message": message,
            "user_message_path": str(user_path.relative_to(ROOT_DIR)),
            "assistant_message_path": "",
            "attachment_analysis_status": "queued" if clean_attachments else "none",
            "action_result": None,
            "error": "",
        }
        self.write_chat_queue_item(item)
        self.chat_queue_event.set()
        payload = self.session_payload(resolved_session_id)
        payload["queued_chat"] = item
        return payload

    def chat_queue_loop(self) -> None:
        while True:
            try:
                item = self.next_chat_queue_item()
                if item is None:
                    self.chat_queue_event.wait(timeout=2.0)
                    self.chat_queue_event.clear()
                    continue
                self.process_chat_queue_item(item)
            except Exception:
                traceback.print_exc()
                time.sleep(1.0)

    def next_chat_queue_item(self) -> dict[str, Any] | None:
        with self.chat_queue_lock:
            items = self.chat_queue_items(statuses={"queued"})
            if not items:
                return None
            item = items[0]
            item["status"] = "running"
            item["started_at"] = item.get("started_at") or now_iso()
            item["updated_at"] = now_iso()
            self.write_chat_queue_item(item)
            return item

    def process_chat_queue_item(self, item: dict[str, Any]) -> None:
        try:
            user_path = ROOT_DIR / str(item.get("user_message_path") or "")
            if user_path.exists():
                self.update_message_queue_status(user_path, "running")
                self.update_chat_queue_item(item, {"attachment_analysis_status": "running"})
                self.analyze_message_attachments(user_path, str(item["session_id"]), str(item["id"]))
                item = self.read_chat_queue_item(self.chat_queue_path(str(item["session_id"]), str(item["id"])))
            result = self.reply_to_stored_message(
                str(item["message"]),
                str(item["session_id"]),
                user_path if user_path.exists() else None,
                queue_id=str(item["id"]),
            )
            self.update_chat_queue_item(
                item,
                {
                    "status": "succeeded",
                    "finished_at": now_iso(),
                    "assistant_message_path": result.get("assistant_path", ""),
                    "attachment_analysis_status": "succeeded",
                    "action_result": result.get("action_result"),
                    "error": "",
                },
            )
            if user_path.exists():
                self.update_message_queue_status(user_path, "succeeded")
        except Exception as exc:  # noqa: BLE001
            if user_path.exists():
                try:
                    self.update_message_queue_status(user_path, "failed")
                except Exception:
                    pass
            self.update_chat_queue_item(
                item,
                {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "attachment_analysis_status": "failed",
                    "error": str(exc),
                },
            )
            try:
                self.append_message(str(item["session_id"]), "assistant", f"Reply tool failed: {exc}", {"queue_id": item["id"], "queue_status": "failed"})
            except Exception:
                pass

    def message_paths(self, session_id: str) -> list[Path]:
        return sorted((self.session_dir(session_id) / "messages").glob("*.md"))

    def message_file(self, session_id: str, message_id: str) -> Path:
        safe_session = safe_session_id(session_id)
        safe_message = safe_job_id(message_id)
        path = self.session_dir(safe_session) / "messages" / f"{safe_message}.md"
        if not path.exists():
            raise WebAppError(f"unknown message: {safe_message}")
        return path

    def parse_attachments_json(self, raw_attachments: Any) -> list[dict[str, Any]]:
        if isinstance(raw_attachments, list):
            return [row for row in raw_attachments if isinstance(row, dict)]
        candidate: Any = raw_attachments
        for _attempt in range(4):
            if isinstance(candidate, list):
                return [row for row in candidate if isinstance(row, dict)]
            if not isinstance(candidate, str) or not candidate.strip():
                return []
            try:
                candidate = json.loads(candidate)
                continue
            except json.JSONDecodeError:
                # Front matter stores JSON as a quoted YAML string, so quotes can
                # be escaped one or more times. Decode those JSON-string escapes
                # without using unicode_escape, which corrupts real UTF-8 text.
                try:
                    decoded = json.loads(f'"{candidate}"')
                except Exception:
                    return []
                if decoded == candidate:
                    return []
                candidate = decoded
        return []

    def update_message_queue_status(self, message_path: Path, status: str) -> None:
        text = message_path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(text)
        front_matter["queue_status"] = status
        write_markdown(message_path, front_matter, body)

    def analyze_message_attachments(self, message_path: Path, session_id: str, queue_id: str) -> None:
        text = message_path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(text)
        attachments = self.parse_attachments_json(front_matter.get("attachments_json") or front_matter.get("attachments"))
        if not attachments:
            return
        normalized = self.normalize_chat_attachments(attachments, session_id=session_id, queue_id=queue_id, analyze=True)
        self.register_attachment_artifacts(session_id, queue_id, normalized)
        front_matter["attachments_json"] = json.dumps(normalized, ensure_ascii=False)
        front_matter["queue_status"] = "running"
        write_markdown(message_path, front_matter, body)

    def edit_message(self, session_id: str, message_id: str, content: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        path = self.message_file(safe_session, message_id)
        front_matter, _body = split_front_matter(path.read_text(encoding="utf-8"))
        front_matter["edited_at"] = now_iso()
        write_markdown(path, front_matter, str(content or "").strip())
        meta = self.load_session(safe_session)
        self.save_session(safe_session, meta)
        return self.session_payload(safe_session)

    def resend_message(self, session_id: str, message_id: str, content: str | None = None) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        path = self.message_file(safe_session, message_id)
        row = self.read_message(path)
        message = str(content if content is not None else row.get("content") or "").strip()
        attachments = list(row.get("attachments") or [])
        if not message and not attachments:
            raise WebAppError("message has no content or attachment to resend")
        return self.enqueue_chat_message(message, session_id=safe_session, attachments=attachments)

    def unsend_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        path = self.message_file(safe_session, message_id)
        front_matter, _body = split_front_matter(path.read_text(encoding="utf-8"))
        queue_id = str(front_matter.get("queue_id") or "")
        if queue_id:
            queue_path = self.chat_queue_path(safe_session, queue_id)
            if queue_path.exists():
                try:
                    item = read_json(queue_path)
                    if item.get("status") == "queued":
                        item.update({"status": "canceled", "finished_at": now_iso(), "updated_at": now_iso(), "error": "message unsent"})
                        self.write_chat_queue_item(item)
                except Exception:
                    pass
        trash_dir = self.session_dir(safe_session) / "messages" / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        path.rename(trash_dir / f"{stamp()}-{path.name}")
        meta = self.load_session(safe_session)
        meta["message_count"] = len(self.message_paths(safe_session))
        self.save_session(safe_session, meta)
        return self.session_payload(safe_session)

    def read_message(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(text)
        raw_attachments = front_matter.get("attachments_json") or front_matter.get("attachments")
        attachments: list[dict[str, str]] = []
        for row in self.parse_attachments_json(raw_attachments):
            attachments.append(
                {
                    "name": str(row.get("name", "")),
                    "kind": str(row.get("kind", "file")),
                    "mime": str(row.get("mime", row.get("mime_type", ""))),
                    "size": int(row.get("size", 0)) if str(row.get("size", 0)).isdigit() else 0,
                    "data_url": str(row.get("data_url", "")),
                    "stored_path": str(row.get("stored_path", "")),
                    "mirror_markdown_path": str(row.get("mirror_markdown_path", "")),
                    "preview_url": str(row.get("preview_url", row.get("data_url", ""))),
                    "preview_kind": str(row.get("preview_kind", row.get("kind", "file"))),
                    "analysis_markdown": str(row.get("analysis_markdown", "")),
                    "analysis_note": str(row.get("analysis_note", "")),
                    "analysis_source": str(row.get("analysis_source", "")),
                    "analysis_status": str(row.get("analysis_status", "")),
                    "visible_language": str(row.get("visible_language", "")),
                    "text_excerpt": str(row.get("text_excerpt", "")),
                    "page_count": int(row.get("page_count", 0)) if str(row.get("page_count", 0)).isdigit() else 0,
                    "duration_seconds": float(row.get("duration_seconds", 0) or 0),
                    "width": int(row.get("width", 0)) if str(row.get("width", 0)).isdigit() else 0,
                    "height": int(row.get("height", 0)) if str(row.get("height", 0)).isdigit() else 0,
                }
            )
        row = {
            "id": path.stem,
            "role": front_matter.get("role") or path.stem.rsplit("-", 1)[-1],
            "created_at": front_matter.get("created_at", ""),
            "content": body.strip(),
            "attachments": attachments,
            "path": str(path.relative_to(ROOT_DIR)),
            "queue_id": front_matter.get("queue_id", ""),
            "queue_status": front_matter.get("queue_status", ""),
        }
        row["effective_content"] = self.effective_message_from_row(row)
        return row

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        return [self.read_message(path) for path in self.message_paths(session_id)]

    def message_page(self, session_id: str, limit: int = DEFAULT_MESSAGE_BATCH_SIZE, before: str = "") -> dict[str, Any]:
        safe_id = safe_session_id(session_id)
        limit = max(1, min(int(limit or DEFAULT_MESSAGE_BATCH_SIZE), 50))
        paths = self.message_paths(safe_id)
        end = len(paths)
        if before:
            end = next((index for index, path in enumerate(paths) if path.stem == before), end)
        start = max(0, end - limit)
        rows = [self.read_message(path) for path in paths[start:end]]
        return {
            "messages": rows,
            "message_page": {
                "limit": limit,
                "total": len(paths),
                "loaded_count": len(rows),
                "has_more": start > 0,
                "next_before": rows[0]["id"] if rows and start > 0 else "",
            },
        }

    def transcript(self, session_id: str, limit: int = 24) -> str:
        rows = self.messages(session_id)[-limit:]
        lines: list[str] = []
        for row in rows:
            lines.append(f"{row['role'].upper()}:\n{row.get('effective_content') or row['content']}")
        return "\n\n---\n\n".join(lines)

    def search_local_content(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        terms = extract_terms(query)
        if not terms:
            return []
        matches: list[dict[str, Any]] = []
        for path in sorted((ROOT_DIR / "content" / "posts").glob("*/post.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower = text.lower()
            score = sum(lower.count(term.lower()) for term in terms)
            if score <= 0:
                continue
            manifest = read_json(path.parent / "lazyblog.json", {})
            matches.append(
                {
                    "post_id": manifest.get("post_id") or path.parent.name,
                    "title": extract_title(text, path.parent.name),
                    "path": str(path.relative_to(ROOT_DIR)),
                    "score": score,
                    "snippet": trim_snippet(text, terms),
                    "categories": manifest.get("categories", []),
                    "tags": manifest.get("tags", []),
                }
            )
        return sorted(matches, key=lambda item: item["score"], reverse=True)[:limit]

    def wp_client(self) -> WPClient:
        load_env_file(ROOT_DIR / ".env")
        require_auth()
        return make_client(SimpleNamespace(site_url=None))

    def paginated_wp_get(self, client: WPClient, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while True:
            query = dict(params)
            query["per_page"] = per_page
            query["page"] = page
            payload = client.request("GET", f"{path}?{urllib.parse.urlencode(query)}")
            if not isinstance(payload, list):
                raise WebAppError(f"unexpected WordPress response for {path}")
            rows.extend([row for row in payload if isinstance(row, dict)])
            if len(payload) < per_page:
                break
            page += 1
        return rows

    def normalize_category_record(self, category: dict[str, Any]) -> dict[str, Any]:
        term_id = int(category.get("term_id") or category.get("id") or 0)
        return {
            "term_id": term_id,
            "slug": str(category.get("slug") or ""),
            "name": html.unescape(str(category.get("name") or "")),
            "parent": int(category.get("parent") or 0),
            "description": str(category.get("description") or ""),
            "count": int(category.get("count") or 0),
            "link": str(category.get("link") or ""),
        }

    def sync_category_mirror(self) -> dict[str, Any]:
        client = self.wp_client()
        rows = self.paginated_wp_get(
            client,
            "/wp-json/wp/v2/categories",
            {
                "hide_empty": "false",
                "orderby": "id",
                "order": "asc",
                "context": "edit",
            },
        )
        categories = [self.normalize_category_record(row) for row in rows]
        categories = [row for row in categories if int(row.get("term_id") or 0) > 0]
        categories.sort(key=lambda item: int(item["term_id"]))
        payload = {
            "version": 1,
            "source": client.site_url,
            "taxonomy": "category",
            "categories": categories,
            "synced_at": int(time.time()),
        }
        write_json(CATEGORY_SNAPSHOT_PATH, payload)
        return payload

    def load_category_mirror(self, sync_if_missing: bool = True) -> dict[str, Any]:
        if not CATEGORY_SNAPSHOT_PATH.exists():
            if sync_if_missing:
                return self.sync_category_mirror()
            return {"version": 1, "taxonomy": "category", "categories": []}
        try:
            payload = read_json(CATEGORY_SNAPSHOT_PATH)
        except json.JSONDecodeError:
            if sync_if_missing:
                return self.sync_category_mirror()
            raise
        if not isinstance(payload.get("categories"), list):
            payload["categories"] = []
        payload["categories"] = [
            self.normalize_category_record(row)
            for row in payload.get("categories", [])
            if isinstance(row, dict) and int(row.get("term_id") or row.get("id") or 0) > 0
        ]
        return payload

    def category_records(self, sync_if_missing: bool = True) -> list[dict[str, Any]]:
        return list(self.load_category_mirror(sync_if_missing=sync_if_missing).get("categories", []))

    def category_snapshot(self, limit: int = 40) -> list[str]:
        try:
            categories = self.category_records(sync_if_missing=True)
            names = [str(category.get("name") or "") for category in categories if str(category.get("name") or "")]
            if names:
                return names[:limit]
        except Exception:
            pass
        counts: dict[str, int] = {}
        for manifest_path in sorted((ROOT_DIR / "content" / "posts").glob("*/lazyblog.json")):
            manifest = read_json(manifest_path, {})
            for category in list_from_value(manifest.get("categories")):
                counts[category] = counts.get(category, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[:limit]]

    def search_categories(self, query: str = "", limit: int = 50, sync: bool = False) -> dict[str, Any]:
        mirror = self.sync_category_mirror() if sync else self.load_category_mirror(sync_if_missing=True)
        categories = [self.normalize_category_record(row) for row in mirror.get("categories", []) if isinstance(row, dict)]
        needle = query.strip().casefold()
        if needle:
            categories = [
                category
                for category in categories
                if needle in str(category.get("name") or "").casefold()
                or needle in str(category.get("slug") or "").casefold()
                or needle == str(category.get("term_id") or "")
            ]
        return {
            "source": mirror.get("source", ""),
            "synced_at": mirror.get("synced_at"),
            "categories": categories[: max(1, min(limit, 200))],
            "total": len(categories),
        }

    def find_category_ref(self, ref: Any, sync: bool = True) -> dict[str, Any] | None:
        if ref is None or ref == "":
            return None
        categories = self.category_records(sync_if_missing=sync)
        if isinstance(ref, dict):
            ref = ref.get("term_id") or ref.get("id") or ref.get("slug") or ref.get("name")
        text = str(ref).strip()
        if not text:
            return None
        if text.isdigit():
            term_id = int(text)
            return next((category for category in categories if int(category.get("term_id") or 0) == term_id), None)
        folded = html.unescape(text).casefold()
        return next(
            (
                category
                for category in categories
                if str(category.get("slug") or "").casefold() == folded
                or str(category.get("name") or "").casefold() == folded
            ),
            None,
        )

    def create_category(self, name: str, parent: Any = None, slug: str = "", description: str = "") -> dict[str, Any]:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise WebAppError("category name cannot be empty")
        parent_record = self.find_category_ref(parent) if parent not in {None, ""} else None
        parent_id = int(parent_record["term_id"]) if parent_record else 0
        existing = self.find_category_ref(slug or clean_name)
        if existing and (parent in {None, ""} or int(existing.get("parent") or 0) == parent_id):
            return {"category": existing, "created": False, "mirror": self.load_category_mirror(sync_if_missing=True)}
        client = self.wp_client()
        payload: dict[str, Any] = {"name": clean_name}
        if slug.strip():
            payload["slug"] = slug.strip()
        if description.strip():
            payload["description"] = description.strip()
        if parent_id:
            payload["parent"] = parent_id
        try:
            created = client.request("POST", "/wp-json/wp/v2/categories", payload)
        except Exception:
            self.sync_category_mirror()
            existing = self.find_category_ref(slug or clean_name)
            if existing and (parent in {None, ""} or int(existing.get("parent") or 0) == parent_id):
                return {"category": existing, "created": False, "mirror": self.load_category_mirror(sync_if_missing=True)}
            raise
        mirror = self.sync_category_mirror()
        category = self.find_category_ref(created.get("id") or created.get("slug") or clean_name, sync=False) or self.normalize_category_record(created)
        return {"category": category, "created": True, "mirror": mirror}

    def update_category(self, ref: Any, updates: dict[str, Any]) -> dict[str, Any]:
        category = self.find_category_ref(ref)
        if not category:
            raise WebAppError(f"unknown category: {ref}")
        allowed: dict[str, Any] = {}
        for key in ["name", "slug", "description"]:
            value = str(updates.get(key) or "").strip()
            if value:
                allowed[key] = value
        if "parent" in updates:
            parent_value = updates.get("parent")
            if parent_value in {None, "", 0, "0"}:
                allowed["parent"] = 0
            else:
                parent = self.find_category_ref(parent_value)
                if not parent:
                    raise WebAppError(f"unknown parent category: {parent_value}")
                allowed["parent"] = int(parent["term_id"])
        if not allowed:
            raise WebAppError("no category updates supplied")
        client = self.wp_client()
        updated = client.request("POST", f"/wp-json/wp/v2/categories/{int(category['term_id'])}", allowed)
        mirror = self.sync_category_mirror()
        return {"category": self.normalize_category_record(updated), "mirror": mirror}

    def delete_category(self, ref: Any, force: bool = True) -> dict[str, Any]:
        category = self.find_category_ref(ref)
        if not category:
            raise WebAppError(f"unknown category: {ref}")
        client = self.wp_client()
        query = urllib.parse.urlencode({"force": "true" if force else "false"})
        deleted = client.request("DELETE", f"/wp-json/wp/v2/categories/{int(category['term_id'])}?{query}")
        mirror = self.sync_category_mirror()
        return {"deleted": deleted, "mirror": mirror}

    def guess_categories_from_text(self, text: str) -> list[str]:
        lowered = text.casefold()
        available = {str(category.get("name") or "").casefold(): str(category.get("name") or "") for category in self.category_records()}
        guessed: list[str] = []
        if any(word in lowered for word in ["journal", "journals", "diary", "日记", "日誌"]):
            for name in ["writing", "journals"]:
                if name in available:
                    guessed.append(available[name])
        if any(word in lowered for word in ["wordpress", "docker", "linux", "keyboard", "xrdp", "python", "api"]):
            for name in ["tech", "technology", "software", "hardware & system", "lazy hacks"]:
                if name in available and available[name] not in guessed:
                    guessed.append(available[name])
                    break
        return guessed

    def extract_category_override(self, instruction: str) -> list[str]:
        text = instruction.casefold()
        if not text:
            return []
        restrictive = any(token in text for token in ["only", "just", "只", "仅", "僅", "唯一", "不要其他", "no other"])
        if not restrictive:
            return []
        matches: list[str] = []
        for category in self.category_records(sync_if_missing=True):
            name = str(category.get("name") or "").strip()
            slug = str(category.get("slug") or "").strip()
            candidates = [name.casefold(), slug.casefold().replace("-", " "), slug.casefold().replace("_", " ")]
            if name and any(candidate and candidate in text for candidate in candidates):
                if name not in matches:
                    matches.append(name)
        return matches

    def extract_mentioned_categories(self, instruction: str) -> list[str]:
        text = instruction.casefold()
        if not text:
            return []
        matches: list[str] = []
        for category in self.category_records(sync_if_missing=True):
            name = str(category.get("name") or "").strip()
            slug = str(category.get("slug") or "").strip()
            candidates = [name.casefold(), slug.casefold().replace("-", " "), slug.casefold().replace("_", " ")]
            if name and any(candidate and candidate in text for candidate in candidates):
                if name not in matches:
                    matches.append(name)
        return matches

    def requested_categories_from_action(self, routed: dict[str, Any]) -> list[str]:
        values = list_from_value(routed.get("category"))
        instruction = str(routed.get("instruction") or routed.get("reason") or "")
        mentioned = self.extract_mentioned_categories(instruction)
        out: list[str] = []
        available = {str(category.get("name") or "").casefold(): str(category.get("name") or "") for category in self.category_records(sync_if_missing=True)}
        available.update({str(category.get("slug") or "").casefold(): str(category.get("name") or "") for category in self.category_records(sync_if_missing=True)})
        for value in [*values, *mentioned]:
            clean = " ".join(str(value).strip().split())
            if not clean:
                continue
            resolved = available.get(clean.casefold(), clean)
            if resolved not in out:
                out.append(resolved)
        return out

    def latest_session_publication_time(self, session_id: str) -> datetime | None:
        session = self.load_session(session_id)
        times: list[datetime] = []
        for item in session.get("published", []):
            if isinstance(item, dict):
                parsed = parse_iso_datetime(item.get("published_at"))
                if parsed:
                    times.append(parsed)
        return max(times) if times else None

    def transcript_after(self, session_id: str, after: datetime, limit: int = 36) -> str:
        rows = []
        for row in self.messages(session_id):
            created = parse_iso_datetime(row.get("created_at"))
            if created and created > after:
                rows.append(row)
        lines: list[str] = []
        for row in rows[-limit:]:
            lines.append(f"{row['role'].upper()}:\n{row['content']}")
        return "\n\n---\n\n".join(lines)

    def draft_scope_for_instruction(self, session_id: str, instruction: str, limit: int = 36) -> dict[str, Any]:
        lowered = instruction.casefold()
        wants_after_publication = any(
            token in lowered
            for token in [
                "after last publication",
                "after the last publication",
                "after last publish",
                "since last publish",
                "since the last publication",
                "上一次发表之后",
                "上一次發表之後",
                "上次发表之后",
                "上次發表之後",
                "上一次发布之后",
                "上次发布之后",
                "last publication",
            ]
        )
        latest_publish = self.latest_session_publication_time(session_id)
        focused = self.transcript_after(session_id, latest_publish, limit=limit) if wants_after_publication and latest_publish else ""
        return {
            "mode": "after_last_publication" if wants_after_publication else "recent_chat",
            "latest_publication_at": latest_publish.isoformat().replace("+00:00", "Z") if latest_publish else "",
            "focused_transcript": focused,
            "used_focused_transcript": bool(focused),
        }

    def rewrite_markdown_categories(self, draft_path: Path, categories: list[str]) -> None:
        if not categories:
            return
        markdown = draft_path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(markdown)
        front_matter["categories"] = categories
        write_markdown(draft_path, front_matter, body)

    def new_post_project_id(self, title: str) -> str:
        base = safe_slug_token(title, "post")[:48].strip("-") or "post"
        return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{base}-{uuid.uuid4().hex[:6]}"

    def post_project_dir(self, post_project_id: str) -> Path:
        return POST_PROJECT_ROOT / safe_post_project_id(post_project_id)

    def post_project_meta_path(self, post_project_id: str) -> Path:
        return self.post_project_dir(post_project_id) / "post.json"

    def normalize_post_project(self, project: dict[str, Any]) -> dict[str, Any]:
        project.setdefault("version", 1)
        project.setdefault("id", "")
        project.setdefault("title", "Untitled post")
        project.setdefault("slug", slugify(str(project.get("title") or "post")))
        project.setdefault("created_at", now_iso())
        project.setdefault("updated_at", now_iso())
        project.setdefault("source_language", "en")
        project["categories"] = list_from_value(project.get("categories"))
        project["tags"] = list_from_value(project.get("tags"))
        project["source_sessions"] = list_from_value(project.get("source_sessions"))
        project.setdefault("current_draft", "")
        wordpress = project.get("wordpress") if isinstance(project.get("wordpress"), dict) else {}
        project["wordpress"] = {
            "post_id": wordpress.get("post_id"),
            "status": wordpress.get("status") or "local_draft",
            "link": wordpress.get("link") or "",
        }
        return project

    def load_post_project(self, post_project_id: str) -> dict[str, Any]:
        safe_id = safe_post_project_id(post_project_id)
        path = self.post_project_meta_path(safe_id)
        if not path.exists():
            raise WebAppError(f"unknown post project: {safe_id}")
        project = self.normalize_post_project(read_json(path))
        project["id"] = safe_id
        return project

    def save_post_project(self, project: dict[str, Any]) -> dict[str, Any]:
        project = self.normalize_post_project(project)
        project_id = safe_post_project_id(str(project.get("id") or ""))
        project["id"] = project_id
        project["updated_at"] = now_iso()
        write_json(self.post_project_meta_path(project_id), project)
        session_ids = [safe_session_id(str(item)) for item in list_from_value(project.get("source_sessions")) if str(item).strip()]
        self.emit_event("posts_changed", "", {"post_project_id": project_id, "session_ids": session_ids})
        return project

    def list_post_projects(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        safe_session = safe_session_id(session_id) if session_id else None
        active_id = ""
        if safe_session:
            try:
                active_id = str(self.load_session(safe_session).get("active_post_project_id") or "")
            except WebAppError:
                active_id = ""
        projects: list[dict[str, Any]] = []
        for path in POST_PROJECT_ROOT.glob("*/post.json"):
            try:
                project = self.normalize_post_project(read_json(path))
                project["id"] = path.parent.name
            except (json.JSONDecodeError, WebAppError, OSError):
                continue
            project["active"] = bool(active_id and project["id"] == active_id)
            project["from_current_session"] = bool(safe_session and safe_session in project.get("source_sessions", []))
            projects.append(project)
        active_rows = [item for item in projects if item.get("active")]
        session_rows = [item for item in projects if not item.get("active") and item.get("from_current_session")]
        other_rows = [item for item in projects if not item.get("active") and not item.get("from_current_session")]
        other_rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        session_rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return [*active_rows, *session_rows, *other_rows][: max(1, min(limit, 500))]

    def current_draft_path_for_project(self, project: dict[str, Any]) -> Path | None:
        current = str(project.get("current_draft") or "").strip()
        project_dir = self.post_project_dir(str(project["id"])).resolve()
        candidates: list[Path] = []
        if current:
            candidates.extend([(project_dir / current).resolve(), (ROOT_DIR / current).resolve()])
        candidates.extend(sorted((project_dir / "drafts").glob("*.md"), reverse=True))
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.exists() and (project_dir == resolved.parent or project_dir in resolved.parents):
                return resolved
        return None

    def post_project_payload(self, post_project_id: str) -> dict[str, Any]:
        project = self.load_post_project(post_project_id)
        draft_path = self.current_draft_path_for_project(project)
        draft = None
        if draft_path:
            draft = {
                "path": str(draft_path.relative_to(ROOT_DIR)),
                "markdown": draft_path.read_text(encoding="utf-8"),
            }
        return {"post_project": project, "draft": draft}

    def set_active_post_project(self, session_id: str, post_project_id: str) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        if not str(post_project_id or "").strip():
            session = self.load_session(safe_session)
            session["active_post_project_id"] = ""
            self.save_session(safe_session, session)
            self.commit_session_state(safe_session, f"Clear LazyBlog Studio selected post for {safe_session}")
            return self.session_payload(safe_session)
        project = self.load_post_project(post_project_id)
        sessions = list_from_value(project.get("source_sessions"))
        if safe_session not in sessions:
            sessions.append(safe_session)
            project["source_sessions"] = sessions
            self.save_post_project(project)
        session = self.load_session(safe_session)
        session["active_post_project_id"] = project["id"]
        draft_path = self.current_draft_path_for_project(project)
        if draft_path:
            session["latest_draft"] = str(draft_path.relative_to(ROOT_DIR))
        self.save_session(safe_session, session)
        self.commit_post_state(
            post_project_id=str(project["id"]),
            session_id=safe_session,
            message=f"Select LazyBlog Studio post {project['id']}",
        )
        return self.session_payload(safe_session)

    def create_post_project(
        self,
        session_id: str | None = None,
        title: str = "",
        instruction: str = "",
        categories: list[str] | None = None,
        source_language: str = "en",
    ) -> dict[str, Any]:
        safe_session = safe_session_id(session_id) if session_id else None
        seed_text = instruction.strip()
        session_title = ""
        if safe_session:
            session = self.load_session(safe_session)
            session_title = str(session.get("title") or "")
            seed_text = (seed_text + "\n" + self.transcript(safe_session, limit=36)).strip()
        clean_title = " ".join((title or "").strip().split())
        if not clean_title:
            clean_title = session_title if session_title and session_title != "Untitled chat" else ""
        if not clean_title:
            clean_title = "Today's Journal" if "journal" in seed_text.casefold() else "Untitled post"
        post_project_id = self.new_post_project_id(clean_title)
        project_categories = categories or self.extract_category_override(instruction) or self.guess_categories_from_text(seed_text)
        project = {
            "version": 1,
            "id": post_project_id,
            "title": clean_title[:160],
            "slug": slugify(clean_title, "post"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "source_language": source_language if source_language in {"en", "ja", "zh"} else "en",
            "categories": project_categories,
            "tags": [],
            "source_sessions": [safe_session] if safe_session else [],
            "current_draft": "",
            "wordpress": {"post_id": None, "status": "local_draft", "link": ""},
        }
        self.save_post_project(project)
        if safe_session:
            session = self.load_session(safe_session)
            session["active_post_project_id"] = post_project_id
            self.save_session(safe_session, session)
        self.commit_post_state(
            post_project_id=post_project_id,
            session_id=safe_session,
            message=f"Create LazyBlog Studio post {post_project_id}",
        )
        return self.post_project_payload(post_project_id)

    def active_post_project_id(self, session_id: str) -> str:
        session = self.load_session(safe_session_id(session_id))
        active_id = str(session.get("active_post_project_id") or "")
        if active_id:
            try:
                self.load_post_project(active_id)
                return active_id
            except WebAppError:
                session["active_post_project_id"] = ""
                self.save_session(session_id, session)
        return ""

    def commit_post_state(
        self,
        *,
        post_project_id: str | None = None,
        session_id: str | None = None,
        local_post_dir: Path | None = None,
        extra_force_paths: list[Path] | None = None,
        message: str,
    ) -> None:
        force_paths: list[Path] = []
        if post_project_id:
            force_paths.append(self.post_project_dir(post_project_id))
        if local_post_dir:
            force_paths.append(local_post_dir)
        force_paths.extend(extra_force_paths or [])
        tracked_paths = [self.session_meta_path(session_id)] if session_id else []
        try:
            codex_git_commit_push_mixed(
                force_paths=force_paths,
                tracked_paths=tracked_paths,
                message=message,
                branch=self.args.branch,
                enabled=self.args.commit_push,
                timeout=self.args.git_codex_timeout,
            )
        except subprocess.CalledProcessError:
            try:
                git_commit_push_mixed(
                    force_paths=force_paths,
                    tracked_paths=tracked_paths,
                    message=message,
                    branch=self.args.branch,
                    enabled=self.args.commit_push,
                )
            except subprocess.CalledProcessError:
                pass

    def post_project_for_wp_post_id(self, post_id: int) -> dict[str, Any] | None:
        for path in POST_PROJECT_ROOT.glob("*/post.json"):
            try:
                project = self.normalize_post_project(read_json(path))
            except (json.JSONDecodeError, OSError):
                continue
            wordpress = project.get("wordpress") if isinstance(project.get("wordpress"), dict) else {}
            if int(wordpress.get("post_id") or 0) == int(post_id):
                project["id"] = path.parent.name
                return project
        return None

    def local_post_dir(self, post_id: int) -> Path:
        return ROOT_DIR / "content" / "posts" / str(int(post_id))

    def local_post_manifest(self, post_id: int) -> dict[str, Any] | None:
        path = self.local_post_dir(post_id) / "lazyblog.json"
        if not path.exists():
            return None
        try:
            return read_json(path)
        except json.JSONDecodeError:
            return None

    def category_names_for_ids(self, category_ids: list[int]) -> tuple[list[str], list[str]]:
        categories = {int(row.get("term_id") or 0): row for row in self.category_records(sync_if_missing=True)}
        names: list[str] = []
        slugs: list[str] = []
        for category_id in category_ids:
            row = categories.get(int(category_id))
            if not row:
                continue
            names.append(str(row.get("name") or ""))
            slugs.append(str(row.get("slug") or ""))
        return [name for name in names if name], [slug for slug in slugs if slug]

    def pull_wordpress_post_to_local(self, post: dict[str, Any], source_language: str = "en") -> tuple[Path, Path, dict[str, Any]]:
        post_id = int(post.get("id") or 0)
        if post_id <= 0:
            raise WebAppError("WordPress post response did not include a valid id")
        post_dir = self.local_post_dir(post_id)
        post_dir.mkdir(parents=True, exist_ok=True)
        for dirname in ["translations", "prompts", "logs"]:
            (post_dir / dirname).mkdir(parents=True, exist_ok=True)

        category_ids = [int(value) for value in post.get("categories", []) if str(value).isdigit()]
        category_names, category_slugs = self.category_names_for_ids(category_ids)
        front_matter = {
            "id": post_id,
            "source_language": source_language,
            "title": html.unescape(re.sub(r"<[^>]+>", "", str(post.get("title", {}).get("rendered", "")))).strip(),
            "slug": post.get("slug", ""),
            "date": post.get("date", ""),
            "modified": post.get("modified", ""),
            "status": post.get("status", ""),
            "link": post.get("link", ""),
            "categories": category_names,
        }
        raw_html = post.get("content", {}).get("raw") or post.get("content", {}).get("rendered", "")
        markdown_body = html_to_markdown(raw_html, self.wp_client().site_url)
        post_path = post_dir / "post.md"
        write_markdown(post_path, front_matter, markdown_body)

        existing_manifest = self.local_post_manifest(post_id) or {}
        manifest = {
            "version": 1,
            "post_id": post_id,
            "source_language": source_language,
            "source_file": "post.md",
            "translations_dir": "translations",
            "media": existing_manifest.get("media", {}),
            "categories": category_names,
            "category_slugs": category_slugs,
            "category_ids": category_ids,
            "last_pull": {
                "at": int(time.time()),
                "modified": post.get("modified"),
                "link": post.get("link"),
                "status": post.get("status"),
            },
            "last_push": existing_manifest.get("last_push"),
        }
        write_json(post_dir / "lazyblog.json", manifest)
        return post_dir, post_path, manifest

    def url_post_candidates(self, raw_url: str) -> tuple[list[int], str]:
        parsed = urllib.parse.urlparse(raw_url)
        numbers = [int(match) for match in re.findall(r"/(\d+)(?=/|$)", parsed.path)]
        preferred = [value for value in numbers if value >= 1000 and not 1900 <= value <= 2100]
        ordered = [*preferred, *[value for value in numbers if value not in preferred]]
        seen: set[int] = set()
        post_ids = []
        for value in ordered:
            if value not in seen:
                seen.add(value)
                post_ids.append(value)
        slug = Path(urllib.parse.unquote(parsed.path)).name
        if slug.endswith(".html"):
            slug = slug[:-5]
        return post_ids, slug

    def local_post_candidates(self, query: str, limit: int = 10) -> list[int]:
        terms = extract_terms(query, limit=8)
        if not terms:
            return []
        matches: list[tuple[int, int]] = []
        for manifest_path in sorted((ROOT_DIR / "content" / "posts").glob("*/lazyblog.json")):
            try:
                manifest = read_json(manifest_path)
            except json.JSONDecodeError:
                continue
            post_id = int(manifest.get("post_id") or manifest_path.parent.name)
            post_text = ""
            post_path = manifest_path.parent / "post.md"
            if post_path.exists():
                post_text = post_path.read_text(encoding="utf-8", errors="replace")
            haystack = json.dumps(manifest, ensure_ascii=False) + "\n" + post_text
            score = sum(haystack.casefold().count(term.casefold()) for term in terms)
            if score > 0:
                matches.append((post_id, score))
        return [post_id for post_id, _ in sorted(matches, key=lambda item: item[1], reverse=True)[:limit]]

    def resolve_wordpress_post(self, query: str) -> dict[str, Any]:
        clean = query.strip()
        if not clean:
            raise WebAppError("post query cannot be empty")
        client = self.wp_client()
        parsed = urllib.parse.urlparse(clean)
        post_ids: list[int] = []
        slug = ""
        if parsed.scheme in {"http", "https"}:
            post_ids, slug = self.url_post_candidates(clean)
        elif clean.isdigit():
            post_ids = [int(clean)]

        for post_id in post_ids:
            try:
                return client.get_post(post_id)
            except Exception:
                continue

        if slug:
            path = "/wp-json/wp/v2/posts?" + urllib.parse.urlencode({"slug": slug, "status": "any", "context": "edit", "per_page": 5})
            rows = client.request("GET", path)
            if isinstance(rows, list) and rows:
                return rows[0]

        for post_id in self.local_post_candidates(clean, limit=5):
            try:
                return client.get_post(post_id)
            except Exception:
                continue

        search_path = "/wp-json/wp/v2/posts?" + urllib.parse.urlencode({"search": clean, "status": "any", "context": "edit", "per_page": 10})
        rows = client.request("GET", search_path)
        if isinstance(rows, list) and rows:
            return rows[0]
        raise WebAppError(f"could not resolve WordPress post from: {clean}")

    def select_or_import_wordpress_post(self, session_id: str, query: str, sync_mode: str = "pull") -> dict[str, Any]:
        session_id = safe_session_id(session_id)
        post = self.resolve_wordpress_post(query)
        post_id = int(post.get("id") or 0)
        source_language = "en"
        try:
            translation_meta = self.wp_client().get_translations(post_id)
            source_language = str(translation_meta.get("source_language") or source_language)
        except Exception:
            pass

        local_post_dir = self.local_post_dir(post_id)
        local_post_path = local_post_dir / "post.md"
        manifest = self.local_post_manifest(post_id) or {}
        if sync_mode in {"pull", "auto"} or not local_post_path.exists():
            local_post_dir, local_post_path, manifest = self.pull_wordpress_post_to_local(post, source_language=source_language)

        markdown = local_post_path.read_text(encoding="utf-8") if local_post_path.exists() else ""
        metadata = self.markdown_post_metadata(markdown, fallback_title=f"WordPress post {post_id}")
        project = self.post_project_for_wp_post_id(post_id)
        if project is None:
            title = metadata["title"] or f"WordPress post {post_id}"
            project_id = f"wp-{post_id}-{safe_slug_token(title, 'post')[:48].strip('-') or 'post'}"
            counter = 2
            base_project_id = project_id
            while self.post_project_meta_path(project_id).exists():
                project_id = f"{base_project_id}-{counter}"
                counter += 1
            project = {
                "version": 1,
                "id": project_id,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "source_sessions": [],
                "current_draft": "",
                "wordpress": {},
            }
        project = self.normalize_post_project(project)
        sessions = list_from_value(project.get("source_sessions"))
        if session_id not in sessions:
            sessions.append(session_id)

        draft_path = self.post_project_dir(str(project["id"])) / "drafts" / f"{stamp()}-pulled-{safe_slug_token(metadata['title'], 'post')}.md"
        if sync_mode in {"pull", "auto"} or not self.current_draft_path_for_project(project):
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(markdown, encoding="utf-8")
            project["current_draft"] = str(draft_path.relative_to(self.post_project_dir(str(project["id"]))))

        project.update(
            {
                "title": metadata["title"],
                "slug": metadata["slug"],
                "source_language": source_language if source_language in {"en", "ja", "zh"} else metadata["source_language"],
                "categories": list_from_value(metadata.get("categories")) or list_from_value(manifest.get("categories")),
                "tags": list_from_value(metadata.get("tags")),
                "source_sessions": sessions,
                "wordpress": {
                    "post_id": post_id,
                    "status": post.get("status") or manifest.get("last_pull", {}).get("status") or "draft",
                    "link": post.get("link") or manifest.get("last_pull", {}).get("link") or "",
                },
                "local_mirror": {
                    "post_dir": str(local_post_dir.relative_to(ROOT_DIR)),
                    "post_path": str(local_post_path.relative_to(ROOT_DIR)),
                    "last_sync_mode": sync_mode,
                    "last_selected_at": now_iso(),
                },
            }
        )
        self.save_post_project(project)
        session = self.load_session(session_id)
        session["active_post_project_id"] = str(project["id"])
        if project.get("current_draft"):
            current = self.current_draft_path_for_project(project)
            if current:
                session["latest_draft"] = str(current.relative_to(ROOT_DIR))
        self.save_session(session_id, session)
        self.commit_post_state(
            post_project_id=str(project["id"]),
            session_id=session_id,
            local_post_dir=local_post_dir,
            message=f"Select LazyBlog post {post_id}",
        )
        return {
            **self.session_payload(session_id),
            **self.post_project_payload(str(project["id"])),
            "resolved_post": {
                "post_id": post_id,
                "title": metadata["title"],
                "status": post.get("status"),
                "link": post.get("link"),
                "local_post_dir": str(local_post_dir.relative_to(ROOT_DIR)),
                "sync_mode": sync_mode,
            },
        }

    def extract_post_reference_from_message(self, message: str) -> str:
        urls = re.findall(r"https?://[^\s<>)\"']+", message)
        if urls:
            return urls[0].rstrip(".,;!?")
        lowered = message.casefold()
        if any(word in lowered for word in ["select post", "edit post", "update post", "pull post", "sync post"]):
            match = re.search(r"\bpost\s+#?(\d{2,})\b", message, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def maybe_select_post_from_chat(self, session_id: str, message: str) -> dict[str, Any] | None:
        reference = self.extract_post_reference_from_message(message)
        if not reference:
            return None
        try:
            result = self.select_or_import_wordpress_post(session_id, reference, sync_mode="pull")
            return {
                "action": "select_post",
                "reference": reference,
                "resolved_post": result.get("resolved_post", {}),
                "post_project": result.get("post_project", {}),
            }
        except Exception as exc:  # noqa: BLE001
            return {"action": "select_post", "reference": reference, "error": str(exc)}

    def infer_post_target_mode(self, message: str) -> str:
        lowered = message.casefold()
        reference = self.extract_post_reference_from_message(message)
        new_markers = [
            "write new",
            "new post",
            "new article",
            "new one",
            "another post",
            "another article",
            "create a new post",
            "create new",
            "start a new post",
            "from scratch",
            "新建",
            "新增",
            "另一篇",
            "重新写一篇",
            "寫新",
            "写新",
            "不要覆盖",
            "不要覆蓋",
        ]
        update_markers = [
            "update old post",
            "update the post",
            "update this post",
            "update selected post",
            "update current post",
            "edit this post",
            "edit the post",
            "revise this post",
            "revise the post",
            "polish this post",
            "polish the post",
            "polish old post",
            "update old",
            "修改这篇",
            "修改這篇",
            "更新这篇",
            "更新這篇",
            "润色这篇",
            "潤色這篇",
            "编辑这篇",
            "編輯這篇",
            "改这篇",
            "改這篇",
            "更新旧文",
            "更新舊文",
        ]
        if any(marker in lowered for marker in new_markers):
            return "create_new"
        if reference or any(marker in lowered for marker in update_markers):
            return "update_selected"
        if any(term in lowered for term in ["draft", "publish", "create a post", "create post", "generate draft", "publish it", "整理成", "生成文章", "发布", "發佈", "發表", "更新"]):
            return "ask"
        return ""

    def clarification_message_for_post_action(self, session_id: str, action: str, routed: dict[str, Any] | None = None) -> str:
        active_id = self.active_post_project_id(session_id)
        selected = None
        if active_id:
            try:
                selected = self.load_post_project(active_id)
            except WebAppError:
                selected = None
        verb = "draft" if action == "draft_post" else "publish"
        if selected:
            title = str(selected.get("title") or active_id)
            return (
                f"I need one confirmation before I {verb}. "
                f"A post is currently selected: {title}.\n\n"
                "Reply with `write new` to create a new post, or `update selected` to revise the selected post."
            )
        return (
            f"I need one confirmation before I {verb}.\n\n"
            "Reply with `write new` to create a new post, or `update selected` after you select/paste the old post you want to revise."
        )

    def normalize_post_target_mode(self, session_id: str, target_mode: str = "", post_project_id: str | None = None) -> str:
        normalized = str(target_mode or "").strip().lower()
        if normalized in {"create_new", "new"}:
            return "create_new"
        if normalized in {"update_selected", "update", "selected"}:
            return "update_selected"
        if normalized == "ask":
            return "ask"
        if normalized == "auto":
            return "update_selected" if (post_project_id or self.active_post_project_id(session_id)) else "create_new"
        if post_project_id:
            return "update_selected"
        return ""

    def resolve_post_project_for_write_action(
        self,
        *,
        session_id: str,
        action: str,
        post_project_id: str | None = None,
        instruction: str = "",
        target_mode: str = "auto",
        post_reference: str = "",
    ) -> dict[str, Any]:
        safe_session = safe_session_id(session_id)
        reference = str(post_reference or "").strip()
        resolved_target_mode = self.normalize_post_target_mode(safe_session, target_mode, post_project_id)
        if reference:
            selected = self.select_or_import_wordpress_post(safe_session, reference, sync_mode="pull")
            project = selected.get("post_project", {}) if isinstance(selected.get("post_project"), dict) else {}
            resolved_post_project_id = str(project.get("id") or "")
            if not resolved_post_project_id:
                raise WebAppError("failed to resolve referenced post into a local post project")
            return {
                "status": "ready",
                "post_project_id": resolved_post_project_id,
                "target_mode": "update_selected",
                "selected": selected,
            }
        if resolved_target_mode == "ask":
            return {
                "status": "needs_clarification",
                "target_mode": "ask",
                "question": self.clarification_message_for_post_action(safe_session, action),
            }
        if resolved_target_mode == "update_selected":
            resolved_post_project_id = safe_post_project_id(post_project_id) if post_project_id else self.active_post_project_id(safe_session)
            if not resolved_post_project_id:
                return {
                    "status": "needs_clarification",
                    "target_mode": "ask",
                    "question": self.clarification_message_for_post_action(safe_session, action),
                }
            active_id = self.active_post_project_id(safe_session)
            if resolved_post_project_id != active_id:
                self.set_active_post_project(safe_session, resolved_post_project_id)
            return {
                "status": "ready",
                "post_project_id": resolved_post_project_id,
                "target_mode": "update_selected",
            }
        payload = self.create_post_project(session_id=safe_session, instruction=instruction)
        project = payload.get("post_project", {}) if isinstance(payload.get("post_project"), dict) else {}
        resolved_post_project_id = str(project.get("id") or "")
        if not resolved_post_project_id:
            raise WebAppError("failed to create a new post project")
        return {
            "status": "ready",
            "post_project_id": resolved_post_project_id,
            "target_mode": "create_new",
            "selected": payload,
        }

    def new_job_id(self, tool_name: str) -> str:
        return f"{stamp()}-{uuid.uuid4().hex[:8]}-{slugify(tool_name, 'codex')}"

    def job_dir(self, job_id: str) -> Path:
        return JOB_ROOT / safe_job_id(job_id)

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def read_job(self, job_id: str) -> dict[str, Any]:
        path = self.job_path(job_id)
        if not path.exists():
            raise WebAppError(f"unknown job: {job_id}")
        return read_json(path)

    def write_job(self, job_id: str, job: dict[str, Any]) -> None:
        with self.job_lock:
            write_json(self.job_path(job_id), job)
        self.emit_event(
            "jobs_changed",
            str(job.get("session_id") or ""),
            {"job_id": safe_job_id(job_id), "status": str(job.get("status") or ""), "session_id": str(job.get("session_id") or "")},
        )

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self.job_lock:
            job = read_json(self.job_path(job_id))
            job.update(updates)
            job["updated_at"] = now_iso()
            write_json(self.job_path(job_id), job)
        self.emit_event(
            "jobs_changed",
            str(job.get("session_id") or ""),
            {"job_id": safe_job_id(job_id), "status": str(job.get("status") or ""), "session_id": str(job.get("session_id") or "")},
        )
        return job

    def schema_path_for_name(self, schema_name: str) -> Path:
        schemas = {
            "response": CODEX_RESPONSE_SCHEMA,
            "reply": CHAT_REPLY_SCHEMA,
            "task": CHAT_TASK_SCHEMA,
            "action": CHAT_ACTION_SCHEMA,
            "translation": CODEX_TRANSLATION_SCHEMA,
        }
        if schema_name not in schemas:
            raise WebAppError("schema must be one of: response, reply, task, action, translation")
        return schemas[schema_name]

    def prompt_path_for_tool(self, tool_name: str) -> Path:
        prompts = {
            "response": CODEX_RESPONSE_PROMPT,
            "assistant": CODEX_RESPONSE_PROMPT,
            "reply": CHAT_REPLY_PROMPT,
            "task": CHAT_TASK_PROMPT,
            "action": CHAT_ACTION_PROMPT,
        }
        if tool_name not in prompts:
            raise WebAppError("tool must be one of: response, assistant, reply, task, action")
        return prompts[tool_name]

    def default_schema_for_tool(self, tool_name: str) -> str:
        if tool_name == "reply":
            return "reply"
        if tool_name == "task":
            return "task"
        if tool_name == "action":
            return "action"
        return "response"

    def default_model_settings(self) -> dict[str, dict[str, str]]:
        return {name: dict(values) for name, values in DEFAULT_PROFILE_SETTINGS.items()}

    def normalize_model_settings(self, raw: Any) -> dict[str, dict[str, str]]:
        defaults = self.default_model_settings()
        data = raw if isinstance(raw, dict) else {}
        out: dict[str, dict[str, str]] = {}
        for profile, fallback in defaults.items():
            row = data.get(profile) if isinstance(data.get(profile), dict) else {}
            model = str(row.get("model") or fallback["model"]).strip() or fallback["model"]
            reasoning = str(row.get("reasoning") or fallback["reasoning"]).strip().lower() or fallback["reasoning"]
            if reasoning not in REASONING_LEVELS:
                reasoning = fallback["reasoning"]
            out[profile] = {"model": model, "reasoning": reasoning}
        return out

    def load_model_settings(self) -> dict[str, dict[str, str]]:
        if not STUDIO_SETTINGS_PATH.exists():
            return self.default_model_settings()
        try:
            return self.normalize_model_settings(read_json(STUDIO_SETTINGS_PATH))
        except Exception:
            return self.default_model_settings()

    def save_model_settings(self, updates: dict[str, Any]) -> dict[str, dict[str, str]]:
        current = self.load_model_settings()
        merged: dict[str, Any] = {**current}
        for profile in DEFAULT_PROFILE_SETTINGS:
            if isinstance(updates.get(profile), dict):
                merged[profile] = {**current.get(profile, {}), **updates[profile]}
        normalized = self.normalize_model_settings(merged)
        write_json(STUDIO_SETTINGS_PATH, normalized)
        return normalized

    def codex_profile(self, profile: str) -> dict[str, str]:
        settings = self.load_model_settings()
        return dict(settings.get(profile, DEFAULT_PROFILE_SETTINGS.get(profile, DEFAULT_PROFILE_SETTINGS["response"])))

    def codex_accounts(self) -> list[str]:
        raw = os.environ.get("LAZYBLOG_CODEX_ACCOUNTS", "")
        accounts: list[str] = []
        for value in raw.split(","):
            account = value.strip()
            if account and re.fullmatch(r"[A-Za-z0-9_.-]+", account) and account not in accounts:
                accounts.append(account)
        return accounts

    def structured_prompt_routes(self, model: str, reasoning: str) -> list[dict[str, str]]:
        accounts = self.codex_accounts() if shutil.which("agent-run") else []
        account_rows = accounts or [""]
        model_rows = [(model, reasoning)]
        fallback_model = os.environ.get("LAZYBLOG_CODEX_FALLBACK_MODEL", "gpt-5.3-codex-spark").strip()
        fallback_reasoning = os.environ.get("LAZYBLOG_CODEX_FALLBACK_REASONING", "low").strip().lower()
        if fallback_reasoning not in REASONING_LEVELS:
            fallback_reasoning = "low"
        if fallback_model and fallback_model != model:
            model_rows.append((fallback_model, fallback_reasoning))
        return [
            {"provider": "codex", "account": account, "model": route_model, "reasoning": route_reasoning}
            for route_model, route_reasoning in model_rows
            for account in account_rows
        ]

    def run_structured_prompt(
        self,
        *,
        full_prompt: str,
        schema_path: Path,
        output_path: Path,
        model: str,
        reasoning: str,
        extra_codex_args: list[str] | None = None,
        allow_aginti: bool = True,
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempts: list[dict[str, Any]] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        for route in self.structured_prompt_routes(model, reasoning):
            remaining = self.args.codex_timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            output_path.unlink(missing_ok=True)
            command = []
            if route["account"]:
                command.extend(["agent-run", "--account", route["account"]])
            command.extend(
                [
                    "codex",
                    "exec",
                    "--ephemeral",
                    "--model",
                    route["model"],
                    "-c",
                    f'model_reasoning_effort="{route["reasoning"]}"',
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--cd",
                    str(ROOT_DIR),
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                ]
            )
            command.extend(extra_codex_args or [])
            command.append("-")
            try:
                proc = subprocess.run(
                    command,
                    input=full_prompt,
                    text=True,
                    cwd=ROOT_DIR,
                    capture_output=True,
                    timeout=max(1, int(remaining)),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                attempts.append({**route, "status": "timed_out", "returncode": None})
                raise PromptRouteError(
                    "Codex route timed out; the prompt was not replayed on another provider",
                    attempts=attempts,
                    stdout="\n".join(stdout_parts),
                    stderr="\n".join(stderr_parts),
                )
            stdout_parts.append(proc.stdout or "")
            stderr_parts.append(proc.stderr or "")
            valid_output = False
            if proc.returncode == 0 and output_path.is_file():
                try:
                    valid_output = isinstance(read_json(output_path), dict)
                except (json.JSONDecodeError, OSError):
                    valid_output = False
            attempts.append(
                {
                    **route,
                    "status": "succeeded" if valid_output else "failed",
                    "returncode": proc.returncode,
                }
            )
            if valid_output:
                return {
                    "output": read_json(output_path),
                    "route": route,
                    "attempts": attempts,
                    "stdout": "\n".join(stdout_parts),
                    "stderr": "\n".join(stderr_parts),
                }

        aginti_enabled = bool_env("LAZYBLOG_AGINTI_DEEPSEEK_FALLBACK", False)
        if allow_aginti and aginti_enabled and shutil.which("aginti"):
            remaining = self.args.codex_timeout - (time.monotonic() - started)
            if remaining > 0:
                output_path.unlink(missing_ok=True)
                deepseek_model = os.environ.get("LAZYBLOG_AGINTI_DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
                schema_prompt = (
                    full_prompt
                    + "\n\nThe preceding output contract is mandatory. Return only one JSON object matching this JSON Schema:\n"
                    + schema_path.read_text(encoding="utf-8")
                    + "\n"
                )
                command = [
                    "aginti",
                    "run",
                    "--stdin",
                    "--json",
                    "--task-profile",
                    "chatops",
                    "--no-scs",
                    "-s",
                    "safe",
                    "--provider",
                    "deepseek",
                    "--routing",
                    "manual",
                    "--model",
                    deepseek_model,
                ]
                try:
                    proc = subprocess.run(
                        command,
                        input=schema_prompt,
                        text=True,
                        cwd=ROOT_DIR,
                        capture_output=True,
                        timeout=max(1, int(remaining)),
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    route = {
                        "provider": "aginti-deepseek",
                        "account": "",
                        "model": deepseek_model,
                        "reasoning": "provider-default",
                    }
                    attempts.append({**route, "status": "timed_out", "returncode": None})
                    raise PromptRouteError(
                        "AgInTi DeepSeek fallback timed out",
                        attempts=attempts,
                        stdout="\n".join(stdout_parts),
                        stderr="\n".join(stderr_parts),
                    )
                stdout_parts.append(proc.stdout or "")
                stderr_parts.append(proc.stderr or "")
                valid_output = False
                parsed_output: dict[str, Any] = {}
                if proc.returncode == 0:
                    try:
                        envelope = json.loads(proc.stdout)
                        raw_result = envelope.get("result") if isinstance(envelope, dict) else ""
                        if isinstance(raw_result, dict):
                            parsed_output = raw_result
                        else:
                            clean_result = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw_result or "").strip(), flags=re.IGNORECASE)
                            parsed_output = json.loads(clean_result)
                        valid_output = isinstance(parsed_output, dict)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        valid_output = False
                route = {"provider": "aginti-deepseek", "account": "", "model": deepseek_model, "reasoning": "provider-default"}
                attempts.append(
                    {
                        **route,
                        "status": "succeeded" if valid_output else "failed",
                        "returncode": proc.returncode,
                    }
                )
                if valid_output:
                    write_json(output_path, parsed_output)
                    return {
                        "output": parsed_output,
                        "route": route,
                        "attempts": attempts,
                        "stdout": "\n".join(stdout_parts),
                        "stderr": "\n".join(stderr_parts),
                    }

        raise PromptRouteError(
            "all configured Codex and AgInTi DeepSeek routes failed",
            attempts=attempts,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
        )

    def profile_for_tool(self, tool_name: str, schema_name: str = "") -> dict[str, str]:
        if schema_name == "translation":
            return self.codex_profile("translation")
        if tool_name == "reply":
            return self.codex_profile("reply")
        if tool_name == "task":
            return self.codex_profile("task")
        if tool_name == "action":
            return self.codex_profile("action")
        return self.codex_profile("response")

    def session_context(self, session_id: str | None, prompt: str) -> dict[str, Any]:
        if not session_id:
            return {}
        safe_id = safe_session_id(session_id)
        session = self.load_session(safe_id)
        transcript = self.transcript(safe_id, limit=36)
        return {
            "session": session,
            "transcript": transcript,
            "local_matches": self.search_local_content(transcript + "\n" + prompt, limit=10),
            "category_snapshot": self.category_snapshot(),
        }

    def build_codex_api_prompt(self, job: dict[str, Any], request_payload: dict[str, Any]) -> tuple[str, Path]:
        tool_name = str(job["tool"])
        prompt = str(request_payload.get("prompt") or "").strip()
        input_payload = request_payload.get("input") if isinstance(request_payload.get("input"), dict) else {}
        schema_path = self.schema_path_for_name(str(job["schema"]))
        template = load_prompt(self.prompt_path_for_tool(tool_name))
        session_context = self.session_context(job.get("session_id"), prompt)

        if tool_name == "reply":
            message = prompt or str(input_payload.get("message") or "")
            if not message.strip():
                raise WebAppError("reply tool requires prompt or input.message")
            tool_input = {
                **session_context,
                "message": message,
                "input": input_payload,
                "api_contract": {
                    "job_id": job["id"],
                    "tool": tool_name,
                    "schema": job["schema"],
                    "output_path": job["paths"]["output"],
                },
            }
        elif tool_name == "task":
            tool_input = {
                **session_context,
                "instruction": prompt,
                "input": input_payload,
                "requested_status": str(input_payload.get("requested_status") or "draft"),
                "storage": {
                    "job_dir": str(self.job_dir(job["id"]).relative_to(ROOT_DIR)),
                    "output_path": job["paths"]["output"],
                },
                "api_contract": {
                    "job_id": job["id"],
                    "tool": tool_name,
                    "schema": job["schema"],
                },
            }
        elif tool_name == "action":
            message = prompt or str(input_payload.get("message") or "")
            if not message.strip():
                raise WebAppError("action tool requires prompt or input.message")
            tool_input = {
                **session_context,
                "message": message,
                "input": input_payload,
                "api_contract": {
                    "job_id": job["id"],
                    "tool": tool_name,
                    "schema": job["schema"],
                    "output_path": job["paths"]["output"],
                },
            }
        else:
            if not prompt:
                raise WebAppError("response/assistant tool requires prompt")
            tool_input = {
                **session_context,
                "prompt": prompt,
                "input": input_payload,
                "mode": "assistant_handoff" if tool_name == "assistant" else "definite_response",
                "api_contract": {
                    "job_id": job["id"],
                    "tool": tool_name,
                    "schema": job["schema"],
                    "output_path": job["paths"]["output"],
                },
            }

        full_prompt = (
            template
            + "\n\nInput JSON follows. Return only JSON matching the selected schema.\n\n"
            + json.dumps(tool_input, ensure_ascii=False, indent=2)
            + "\n"
        )
        return full_prompt, schema_path

    def mock_codex_api_result(self, job: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(job["tool"])
        if job.get("schema") == "translation":
            source = request_payload.get("input") if isinstance(request_payload.get("input"), dict) else {}
            language = str(source.get("target_language") or "en")
            return {
                "language": language,
                "title": f"[{language}] {source.get('title', 'Untitled')}",
                "content": f"<p>[{language}] Mock translation.</p>\n" + str(source.get("content", ""))[:2000],
                "excerpt": f"[{language}] {source.get('excerpt', '')}".strip(),
                "notes": "Mock translation generated without calling Codex.",
            }
        if tool_name == "reply":
            return self.mock_tool("reply", {"message": request_payload.get("prompt") or request_payload.get("input", {}).get("message", "")})
        if tool_name == "task":
            return self.mock_tool("task", {"transcript": request_payload.get("prompt", "")})
        if tool_name == "action":
            return self.mock_tool("action", {"message": request_payload.get("prompt") or request_payload.get("input", {}).get("message", "")})
        return {
            "status": "completed",
            "answer": f"Mock Codex API response for: {str(request_payload.get('prompt') or '')[:240]}",
            "summary": "Mock response generated without calling Codex.",
            "actions": [
                {
                    "label": "poll",
                    "detail": f"Poll /api/codex/job?id={job['id']} for the durable job record.",
                }
            ],
            "artifacts": [],
            "needs_followup": False,
            "confidence": 0.5,
        }

    def submit_codex_job(self, request_payload: dict[str, Any], start: bool = True) -> dict[str, Any]:
        tool_name = str(request_payload.get("tool") or "response").strip().lower()
        if tool_name == "respond":
            tool_name = "response"
        self.prompt_path_for_tool(tool_name)
        schema_name = str(request_payload.get("schema") or self.default_schema_for_tool(tool_name)).strip().lower()
        self.schema_path_for_name(schema_name)
        default_profile = self.profile_for_tool(tool_name, schema_name)
        session_id = request_payload.get("session_id")
        if session_id:
            session_id = safe_session_id(str(session_id))
            self.load_session(session_id)

        job_id = self.new_job_id(tool_name)
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "id": job_id,
            "tool": tool_name,
            "schema": schema_name,
            "status": "queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": None,
            "session_id": session_id,
            "model": str(request_payload.get("model") or default_profile["model"]),
            "reasoning": str(request_payload.get("reasoning") or default_profile["reasoning"]),
            "prompt_preview": str(request_payload.get("prompt") or "")[:240],
            "poll_url": f"/api/codex/job?id={job_id}",
            "result_url": f"/api/codex/result?id={job_id}",
            "paths": {
                "dir": str(job_dir.relative_to(ROOT_DIR)),
                "input": str((job_dir / "input.json").relative_to(ROOT_DIR)),
                "prompt": str((job_dir / "prompt.txt").relative_to(ROOT_DIR)),
                "output": str((job_dir / "output.json").relative_to(ROOT_DIR)),
                "stdout": str((job_dir / "stdout.log").relative_to(ROOT_DIR)),
                "stderr": str((job_dir / "stderr.log").relative_to(ROOT_DIR)),
            },
        }
        write_json(job_dir / "input.json", request_payload)
        self.write_job(job_id, job)
        if start:
            thread = threading.Thread(target=self.execute_codex_job, args=(job_id,), daemon=True)
            thread.start()
        return self.job_status(job_id, include_logs=False, include_output=False)

    def execute_codex_job(self, job_id: str) -> None:
        job_dir = self.job_dir(job_id)
        request_payload = read_json(job_dir / "input.json")
        started = time.time()
        try:
            job = self.update_job(job_id, {"status": "running", "started_at": now_iso()})
            full_prompt, schema_path = self.build_codex_api_prompt(job, request_payload)
            (job_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")

            if self.args.mock_codex or bool(request_payload.get("mock", False)):
                result = self.mock_codex_api_result(job, request_payload)
                write_json(job_dir / "output.json", result)
                self.register_job_output_artifacts(job, result)
                self.update_job(
                    job_id,
                    {
                        "status": "succeeded",
                        "finished_at": now_iso(),
                        "elapsed_seconds": round(time.time() - started, 2),
                        "returncode": 0,
                    },
                )
                return

            routed = self.run_structured_prompt(
                full_prompt=full_prompt,
                schema_path=schema_path,
                output_path=job_dir / "output.json",
                model=str(job["model"]),
                reasoning=str(job["reasoning"]),
            )
            (job_dir / "stdout.log").write_text(str(routed.get("stdout") or ""), encoding="utf-8")
            (job_dir / "stderr.log").write_text(str(routed.get("stderr") or ""), encoding="utf-8")
            status = "succeeded" if (job_dir / "output.json").exists() else "failed"
            updates: dict[str, Any] = {
                "status": status,
                "finished_at": now_iso(),
                "elapsed_seconds": round(time.time() - started, 2),
                "returncode": 0 if status == "succeeded" else 1,
                "route": routed.get("route"),
                "attempts": routed.get("attempts", []),
            }
            if status == "failed":
                updates["error"] = "all structured prompt routes failed"
            elif (job_dir / "output.json").exists():
                try:
                    self.register_job_output_artifacts(job, read_json(job_dir / "output.json"))
                except Exception:
                    pass
            self.update_job(job_id, updates)
        except PromptRouteError as exc:
            (job_dir / "stdout.log").write_text(exc.stdout, encoding="utf-8")
            (job_dir / "stderr.log").write_text(exc.stderr, encoding="utf-8")
            self.update_job(
                job_id,
                {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(time.time() - started, 2),
                    "returncode": 1,
                    "route": None,
                    "attempts": exc.attempts,
                    "error": str(exc),
                },
            )
        except Exception as exc:  # noqa: BLE001
            (job_dir / "stderr.log").write_text(traceback.format_exc(), encoding="utf-8")
            self.update_job(
                job_id,
                {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(time.time() - started, 2),
                    "error": str(exc),
                },
            )

    def job_status(self, job_id: str, include_logs: bool = True, include_output: bool = True) -> dict[str, Any]:
        job = self.read_job(safe_job_id(job_id))
        job_dir = self.job_dir(job["id"])
        payload = {"job": job}
        if include_output and (job_dir / "output.json").exists():
            try:
                payload["output"] = read_json(job_dir / "output.json")
            except json.JSONDecodeError:
                payload["output_text"] = (job_dir / "output.json").read_text(encoding="utf-8", errors="replace")
        if include_logs:
            payload["logs"] = {
                "stdout_tail": tail_text(job_dir / "stdout.log"),
                "stderr_tail": tail_text(job_dir / "stderr.log"),
            }
        return payload

    def list_jobs(self, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        safe_session = safe_session_id(session_id) if session_id else None
        for path in JOB_ROOT.glob("*/job.json"):
            try:
                job = read_json(path)
            except json.JSONDecodeError:
                continue
            if safe_session and job.get("session_id") != safe_session:
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)[:limit]

    def respond_with_codex(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(request_payload)
        payload["tool"] = payload.get("tool") or "response"
        wait = bool(payload.pop("wait", False))
        wait_seconds = max(0.0, min(float(payload.pop("wait_seconds", 0 if not wait else 30)), 300.0))
        job_payload = self.submit_codex_job(payload, start=not wait)
        job_id = job_payload["job"]["id"]
        if wait:
            self.execute_codex_job(job_id)
        elif wait_seconds > 0:
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                status = self.read_job(job_id).get("status")
                if status in {"succeeded", "failed"}:
                    break
                time.sleep(0.25)
        return self.job_status(job_id, include_logs=True, include_output=True)

    def translation_key(self, payload: dict[str, Any]) -> str:
        site_url = str(payload.get("site_url") or "")
        post_id = str(payload.get("post_id") or "")
        target_language = str(payload.get("target_language") or "")
        source = "|".join([site_url, post_id, target_language])
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]

    def translation_index_path(self, key: str) -> Path:
        return TRANSLATION_JOB_ROOT / f"{key}.json"

    def translation_prompt(self, payload: dict[str, Any]) -> str:
        target_language = str(payload.get("target_language") or "")
        source_language = str(payload.get("source_language") or "")
        target_label = str(payload.get("target_label") or target_language)
        source_label = str(payload.get("source_label") or source_language)
        return f"""Translate this WordPress post from {source_label} ({source_language}) to {target_label} ({target_language}).

Return JSON matching the translation schema.

Rules:
- Output `language` exactly as `{target_language}`.
- Translate title, excerpt, and content.
- Preserve WordPress HTML structure, links, image tags, code blocks, math shortcodes, LaTeX markers, and embeds.
- Do not add translator notes, AI disclaimers, extra headings, or unrelated commentary inside content.
- Keep prose natural. For technical posts, keep commands, identifiers, and code unchanged unless they are explanatory prose.
- If a field is empty in the source, return an empty string for that field.
"""

    def start_translation_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = ["post_id", "source_language", "target_language"]
        missing = [key for key in required if str(payload.get(key) or "") == ""]
        if missing:
            raise WebAppError("missing translation fields: " + ", ".join(missing))
        if str(payload.get("title") or "").strip() == "" and str(payload.get("content") or "").strip() == "":
            raise WebAppError("translation requires title or content")

        key = self.translation_key(payload)
        index_path = self.translation_index_path(key)
        if index_path.exists():
            index = read_json(index_path)
            job_id = str(index.get("job_id") or "")
            if job_id:
                try:
                    status = self.job_status(job_id, include_logs=False, include_output=True)
                    if status["job"]["status"] in {"queued", "running", "succeeded"}:
                        return {"translation_key": key, **status}
                except WebAppError:
                    pass

        profile = self.codex_profile("translation")
        request_payload = {
            "tool": "response",
            "schema": "translation",
            "prompt": self.translation_prompt(payload),
            "input": payload,
            "model": payload.get("model") or profile["model"],
            "reasoning": payload.get("reasoning") or profile["reasoning"],
            "mock": bool(payload.get("mock", False)),
        }
        job_payload = self.submit_codex_job(request_payload)
        job_id = job_payload["job"]["id"]
        write_json(
            index_path,
            {
                "translation_key": key,
                "job_id": job_id,
                "post_id": payload.get("post_id"),
                "site_url": payload.get("site_url", ""),
                "source_language": payload.get("source_language"),
                "target_language": payload.get("target_language"),
                "created_at": now_iso(),
            },
        )
        return {"translation_key": key, **self.job_status(job_id, include_logs=False, include_output=True)}

    def run_codex_tool(
        self,
        *,
        session_id: str,
        tool_name: str,
        prompt_template_path: Path,
        schema_path: Path,
        payload: dict[str, Any],
        model: str | None = None,
        reasoning: str | None = None,
    ) -> dict[str, Any]:
        run_dir = self.session_dir(session_id) / "tool-runs" / f"{stamp()}-{uuid.uuid4().hex[:6]}-{tool_name}"
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_text = load_prompt(prompt_template_path)
        full_prompt = (
            prompt_text
            + "\n\nInput JSON follows. Return only JSON matching the requested schema.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n"
        )
        write_json(run_dir / "input.json", payload)
        (run_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")
        output_path = run_dir / "output.json"

        if self.args.mock_codex:
            result = self.mock_tool(tool_name, payload)
            write_json(output_path, result)
            return result

        profile = self.profile_for_tool(tool_name)
        resolved_model = model or profile["model"]
        resolved_reasoning = reasoning or profile["reasoning"]
        started = time.time()
        routed = self.run_structured_prompt(
            full_prompt=full_prompt,
            schema_path=schema_path,
            output_path=output_path,
            model=resolved_model,
            reasoning=resolved_reasoning,
        )
        (run_dir / "stdout.log").write_text(str(routed.get("stdout") or ""), encoding="utf-8")
        (run_dir / "stderr.log").write_text(str(routed.get("stderr") or ""), encoding="utf-8")
        write_json(
            run_dir / "run.json",
            {
                "tool": tool_name,
                "model": str(routed.get("route", {}).get("model") or resolved_model),
                "reasoning": str(routed.get("route", {}).get("reasoning") or resolved_reasoning),
                "route": routed.get("route"),
                "attempts": routed.get("attempts", []),
                "returncode": 0,
                "elapsed_seconds": round(time.time() - started, 2),
                "output": str(output_path.relative_to(ROOT_DIR)) if output_path.exists() else "",
            },
        )
        if not output_path.exists():
            raise WebAppError(f"{tool_name} did not write output JSON")
        return read_json(output_path)

    def mock_tool(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "action":
            message = str(payload.get("message") or "")
            reference = self.extract_post_reference_from_message(message)
            inferred_target_mode = self.infer_post_target_mode(message)
            return {
                "action": "select_post" if reference else "no_op",
                "post_reference": reference,
                "post_target_mode": inferred_target_mode,
                "clarification_question": "",
                "category": "",
                "parent_category": "",
                "sync_mode": "pull" if reference else "",
                "status": "",
                "force_redraft": False,
                "instruction": message,
                "reason": "Mock action router used deterministic link/id extraction.",
                "confidence": 0.9 if reference else 0.4,
            }
        if tool_name == "reply":
            message = payload.get("message", "")
            return {
                "reply": f"I stored this note and can turn it into a draft: {message[:180]}",
                "intent": "capture",
                "summary": message[:160],
                "should_draft": False,
                "suggested_title": "Draft from chat",
                "next_actions": ["Click Draft to generate a post candidate."],
                "memory_tags": extract_terms(message, limit=5),
                "confidence": 0.6,
            }
        transcript = payload.get("transcript", "")
        title = "Draft from LazyBlog chat"
        return {
            "reply": "I drafted a short publishable post from the chat session.",
            "action": "draft_post",
            "draft": {
                "title": title,
                "slug": slugify(title),
                "source_language": "en",
                "excerpt": "A short draft generated from a LazyBlog Studio chat.",
                "markdown": f"# {title}\n\n{transcript[-1200:] or 'Start from a clear note, then revise before publishing.'}\n",
                "categories": ["Notes"],
                "tags": ["lazyblog"],
                "status": "draft",
            },
            "storage_plan": {
                "folder": "content/drafts/mock",
                "files": ["draft.md", "manifest.json"],
            },
            "research_queries": [],
            "research_sources": [],
            "local_matches_used": [],
            "needs_review": True,
            "notes": ["Mock mode was enabled; no Codex model was called."],
        }

    def route_chat_action(self, session_id: str, message: str) -> dict[str, Any]:
        deterministic_hint = self.deterministic_action_hint(message)
        explicit_request = self.is_control_action_request(message)
        inferred_target_mode = self.infer_post_target_mode(message)
        action_profile = self.codex_profile("action")
        payload = {
            "session": self.load_session(session_id),
            "message": message,
            "transcript": self.transcript(session_id, limit=12),
            "active_post_project_id": self.active_post_project_id(session_id),
            "post_projects": self.list_post_projects(session_id=session_id, limit=20),
            "category_mirror": self.search_categories(limit=120).get("categories", []),
            "detected_post_reference": self.extract_post_reference_from_message(message),
            "inferred_post_target_mode": inferred_target_mode,
            "deterministic_hint": deterministic_hint,
            "control_surface": {
                "allowed_actions": ["select_post", "update_post_categories", "draft_post", "publish_post", "create_category", "sync_categories", "no_op"],
                "execution_rule": "Return one action. Backend executes the corresponding controlled API.",
                "routing_model": f"{action_profile['model']}/{action_profile['reasoning']}",
                "hybrid_rule": "Use deterministic_hint as evidence, but classify dynamically from message and recent transcript. For draft/publish, also decide whether the user means create_new, update_selected, or ask for clarification.",
            },
        }
        try:
            routed = self.run_codex_tool(
                session_id=session_id,
                tool_name="action",
                prompt_template_path=CHAT_ACTION_PROMPT,
                schema_path=CHAT_ACTION_SCHEMA,
                payload=payload,
                model=action_profile["model"],
                reasoning=action_profile["reasoning"],
            )
        except Exception as exc:  # noqa: BLE001
            reference = self.extract_post_reference_from_message(message)
            routed = {
                "action": "select_post" if reference else "no_op",
                "post_reference": reference,
                "post_target_mode": inferred_target_mode,
                "clarification_question": self.clarification_message_for_post_action(session_id, "publish_post") if inferred_target_mode == "ask" else "",
                "category": "",
                "parent_category": "",
                "sync_mode": "pull" if reference else "",
                "status": "",
                "force_redraft": False,
                "instruction": message,
                "reason": f"action router failed, used deterministic fallback: {exc}",
                "confidence": 0.85 if reference else 0.0,
            }
        reference = self.extract_post_reference_from_message(message)
        if reference and routed.get("action") == "no_op":
            routed.update({"action": "select_post", "post_reference": reference, "sync_mode": "pull", "confidence": 0.9})
        if reference and routed.get("action") == "select_post" and not str(routed.get("post_reference") or "").strip():
            routed["post_reference"] = reference
        if not explicit_request and routed.get("action") in {"draft_post", "publish_post"}:
            routed["action"] = "no_op"
            routed["reason"] = "No explicit draft/publish instruction detected."
            routed["confidence"] = 0.0
        routed.setdefault("post_target_mode", inferred_target_mode)
        routed.setdefault("clarification_question", "")
        routed.setdefault("status", "")
        routed.setdefault("force_redraft", False)
        routed.setdefault("instruction", message)
        if routed.get("action") in {"draft_post", "publish_post"}:
            if not str(routed.get("post_target_mode") or "").strip():
                routed["post_target_mode"] = inferred_target_mode or "ask"
            if routed.get("post_target_mode") == "ask" and not str(routed.get("clarification_question") or "").strip():
                routed["clarification_question"] = self.clarification_message_for_post_action(session_id, str(routed.get("action") or "draft_post"), routed)
        if routed.get("action") == "no_op" and deterministic_hint.get("action") != "no_op":
            routed.update({**deterministic_hint, "confidence": max(float(deterministic_hint.get("confidence") or 0), 0.74)})
        if routed.get("action") == "select_post" and deterministic_hint.get("action") == "update_post_categories":
            routed.update({**deterministic_hint, "confidence": max(float(deterministic_hint.get("confidence") or 0), 0.82)})
        return routed

    def is_control_action_request(self, message: str) -> bool:
        lowered = message.casefold()
        if not lowered.strip():
            return False
        explicit_reflection_markers = [
            "just a note",
            "just an idea",
            "just a design",
            "it's a thought",
            "it's only",
            "這是設計",
            "這是分割",
            "這只是",
            "i'm just",
            "我只是",
            "我只是在說",
            "just thinking",
            "not asking",
            "no need to",
            "not need to publish",
            "not asking to",
            "只是在思考",
            "先不用發",
            "先不要發",
            "not a publish request",
            "不是要發",
            "不是要发布",
            "not for publishing",
            "不要發表",
            "不要發布",
            "先不要動作",
            "只是記錄",
            "只是記錄一下",
            "只是记录",
            "just record",
            "這只是想法",
            "这只是想法",
            "純粹記錄",
            "这是想法",
            "just writing",
            "just an design",
            "仅为记录",
            "僅為記錄",
        ]
        if any(marker in lowered for marker in explicit_reflection_markers):
            return False

        explicit_commands = [
            "draft this",
            "draft a",
            "create a post",
            "create an article",
            "rewrite this",
            "revise this",
            "finish this",
            "finish the post",
            "generate a draft",
            "generate draft",
            "turn this into",
            "organize this",
            "整理這篇",
            "整理成",
            "整理一篇",
            "生成一篇",
            "生成文章",
            "publish this",
            "publish it",
            "publish post",
            "push this",
            "update this",
            "update the post",
            "發佈這篇",
            "發表這篇",
            "請幫我發",
            "请帮我发",
            "update markdown",
            "polish this",
            "finish draft",
        ]
        if any(marker in lowered for marker in explicit_commands):
            return True

        negation_markers = [
            "not ",
            "n't",
            " no ",
            "不要",
            "不需要",
            "不必",
            "不用",
            "先不要",
            "先不",
            "暂时不用",
            "暫時不用",
            "暂时不要",
            "暫時不要",
            "別",
            "别",
            "don't",
            "don not",
        ]
        if any(marker in lowered for marker in negation_markers):
            if any(marker in lowered for marker in ("draft", "publish", "create", "generate", "finish", "polish", "update", "整理", "发布", "發表", "發佈")):
                return False

        control_markers = [
            "draft",
            "redraft",
            "整理",
            "整理成",
            "整理一篇",
            "生成",
            "generate",
            "create",
            "turn",
            "finish",
            "完成",
            "polish",
            "publish",
            "发布",
            "發表",
            "發佈",
            "推送",
            "update",
            "update post",
            "更新",
        ]
        return any(marker in lowered for marker in control_markers)

    def deterministic_action_hint(self, message: str) -> dict[str, Any]:
        lowered = message.casefold()
        reference = self.extract_post_reference_from_message(message)
        category_terms = ["category", "categories", "分类", "分類", "栏目", "欄目"]
        mentioned_categories = self.extract_mentioned_categories(message)
        if any(term in lowered for term in category_terms) and mentioned_categories:
            return {
                "action": "update_post_categories",
                "post_reference": reference,
                "post_target_mode": "",
                "clarification_question": "",
                "category": ", ".join(mentioned_categories),
                "parent_category": "",
                "sync_mode": "pull" if reference else "",
                "status": "",
                "force_redraft": False,
                "instruction": message,
                "reason": "Detected a category update request.",
                "confidence": 0.82,
            }
        if reference:
            explicit_publish_with_reference = any(
                term in lowered
                for term in [
                    "publish ",
                    "publish this",
                    "publish it",
                    "publish post",
                    "update the live",
                    "update live",
                    "push to wordpress",
                    "发布到",
                    "更新到网站",
                    "推送到",
                    "發佈",
                    "發表",
                    "发布到网站",
                    "推到网站",
                ]
            )
            explicit_draft_with_reference = any(
                term in lowered
                for term in [
                    "draft",
                    "redraft",
                    "整理成",
                    "整理为",
                    "整理一篇",
                    "整理成一篇文章",
                    "生成文章",
                    "生成一篇",
                    "update markdown",
                    "更新 markdown",
                    "finish this",
                    "finish it",
                    "polish this",
                    "update this post",
                    "update the post",
                    "turn this into",
                    "generate draft",
                    "create draft",
                    "trigger draft",
                ]
            )
            if explicit_publish_with_reference:
                status = "publish"
                if "private" in lowered or "私密" in lowered:
                    status = "private"
                if "draft" in lowered or "草稿" in lowered:
                    status = "draft"
                return {
                    "action": "publish_post",
                    "post_reference": reference,
                    "post_target_mode": "update_selected",
                    "clarification_question": "",
                    "category": "",
                    "parent_category": "",
                    "sync_mode": "pull",
                    "status": status,
                    "force_redraft": "redraft" in lowered or "重新" in lowered,
                    "instruction": message,
                    "reason": "Detected an explicit publish/update request for a referenced post.",
                    "confidence": 0.85,
                }
            if explicit_draft_with_reference:
                return {
                    "action": "draft_post",
                    "post_reference": reference,
                    "post_target_mode": "update_selected",
                    "clarification_question": "",
                    "category": "",
                    "parent_category": "",
                    "sync_mode": "pull",
                    "status": "draft",
                    "force_redraft": "redraft" in lowered or "重新" in lowered,
                    "instruction": message,
                    "reason": "Detected an explicit draft/update request for a referenced post.",
                    "confidence": 0.84,
                }
            return {
                "action": "select_post",
                "post_reference": reference,
                "post_target_mode": "",
                "clarification_question": "",
                "category": "",
                "parent_category": "",
                "sync_mode": "pull",
                "status": "",
                "force_redraft": False,
                "instruction": message,
                "reason": "Detected a WordPress post reference.",
                "confidence": 0.9,
            }
        draft_terms = [
            "draft",
            "redraft",
            "整理成",
            "整理为",
            "整理一篇",
            "整理成一篇文章",
            "生成文章",
            "生成一篇",
            "update markdown",
            "更新 markdown",
            "finish this",
            "finish it",
            "polish this",
            "turn this into",
            "generate draft",
            "create draft",
            "trigger draft",
        ]
        publish_terms = [
            "publish ",
            "publish this",
            " publish it",
            "发布到",
            "更新到网站",
            "推送到",
            "發佈",
            "發表",
            "publish post",
            "update the live",
            "update live",
            "push to wordpress",
            "发布到网站",
            "推到网站",
        ]
        if any(term in lowered for term in publish_terms):
            status = "publish"
            if "private" in lowered or "私密" in lowered:
                status = "private"
            if "draft" in lowered or "草稿" in lowered:
                status = "draft"
            target_mode = self.infer_post_target_mode(message) or "ask"
            return {
                "action": "publish_post",
                "post_reference": "",
                "post_target_mode": target_mode,
                "clarification_question": "",
                "category": "",
                "parent_category": "",
                "sync_mode": "",
                "status": status,
                "force_redraft": "redraft" in lowered or "重新" in lowered,
                "instruction": message,
                "reason": "Detected an explicit publish/update request.",
                "confidence": 0.78,
            }
        if any(term in lowered for term in draft_terms):
            target_mode = self.infer_post_target_mode(message) or "ask"
            return {
                "action": "draft_post",
                "post_reference": "",
                "post_target_mode": target_mode,
                "clarification_question": "",
                "category": "",
                "parent_category": "",
                "sync_mode": "",
                "status": "draft",
                "force_redraft": "redraft" in lowered or "重新" in lowered,
                "instruction": message,
                "reason": "Detected an explicit draft/update request.",
                "confidence": 0.76,
            }
        return {
            "action": "no_op",
            "post_reference": "",
            "post_target_mode": "",
            "clarification_question": "",
            "category": "",
            "parent_category": "",
            "sync_mode": "",
            "status": "",
            "force_redraft": False,
            "instruction": message,
            "reason": "No deterministic action signal.",
            "confidence": 0.0,
        }

    def update_post_project_categories(
        self,
        session_id: str,
        categories: list[str],
        post_reference: str = "",
    ) -> dict[str, Any]:
        session_id = safe_session_id(session_id)
        selected: dict[str, Any] | None = None
        if post_reference.strip():
            selected = self.select_or_import_wordpress_post(session_id, post_reference, sync_mode="pull")
        active_id = self.active_post_project_id(session_id)
        if not active_id:
            raise WebAppError("no selected post project to update categories")
        project = self.load_post_project(active_id)
        clean_categories: list[str] = []
        for category in categories:
            clean = " ".join(str(category).strip().split())
            if clean and clean not in clean_categories:
                clean_categories.append(clean)
        if not clean_categories:
            raise WebAppError("no categories provided")

        draft_path = self.current_draft_path_for_project(project)
        if draft_path:
            self.rewrite_markdown_categories(draft_path, clean_categories)
            project["current_draft"] = str(draft_path.relative_to(self.post_project_dir(str(project["id"]))))

        wordpress = project.get("wordpress") if isinstance(project.get("wordpress"), dict) else {}
        post_id = wordpress.get("post_id")
        post: dict[str, Any] | None = None
        warnings: list[str] = []
        if post_id:
            client = self.wp_client()
            category_ids, warnings = self.resolve_terms(client, "categories", clean_categories)
            if category_ids:
                post = client.update_post(int(post_id), {"categories": category_ids})
                wordpress.update(
                    {
                        "post_id": post.get("id") or post_id,
                        "status": post.get("status") or wordpress.get("status") or "draft",
                        "link": post.get("link") or wordpress.get("link") or "",
                    }
                )

        project["categories"] = clean_categories
        project["wordpress"] = wordpress
        self.save_post_project(project)
        self.commit_post_state(
            post_project_id=str(project["id"]),
            session_id=session_id,
            extra_force_paths=[CATEGORY_SNAPSHOT_PATH] if CATEGORY_SNAPSHOT_PATH.exists() else [],
            message=f"Update LazyBlog Studio post categories {project['id']}",
        )
        payload = {
            **self.session_payload(session_id),
            **self.post_project_payload(str(project["id"])),
            "updated_categories": clean_categories,
            "wordpress_post": post,
            "warnings": warnings,
        }
        if selected:
            payload["selected"] = selected.get("resolved_post") or selected.get("post_project")
        return payload

    def execute_chat_action(self, session_id: str, routed: dict[str, Any]) -> dict[str, Any]:
        action = str(routed.get("action") or "no_op")
        confidence = float(routed.get("confidence") or 0)
        if action == "select_post" and confidence >= 0.55:
            reference = str(routed.get("post_reference") or "").strip()
            if not reference:
                return {"action": action, "status": "skipped", "reason": "missing post reference", "routed": routed}
            sync_mode = str(routed.get("sync_mode") or "pull")
            if sync_mode not in {"pull", "push", "auto"}:
                sync_mode = "pull"
            result = self.select_or_import_wordpress_post(session_id, reference, sync_mode=sync_mode)
            return {
                "action": action,
                "status": "executed",
                "routed": routed,
                "resolved_post": result.get("resolved_post", {}),
                "post_project": result.get("post_project", {}),
            }
        if action == "update_post_categories" and confidence >= 0.55:
            reference = str(routed.get("post_reference") or "").strip()
            categories = self.requested_categories_from_action(routed)
            if not categories:
                return {"action": action, "status": "skipped", "reason": "missing categories", "routed": routed}
            result = self.update_post_project_categories(session_id, categories, post_reference=reference)
            return {
                "action": action,
                "status": "executed",
                "routed": routed,
                "categories": result.get("updated_categories", []),
                "post_project": result.get("post_project", {}),
                "wordpress_post": result.get("wordpress_post"),
                "warnings": result.get("warnings", []),
            }
        if action == "create_category" and confidence >= 0.7:
            category = str(routed.get("category") or "").strip()
            if not category:
                return {"action": action, "status": "skipped", "reason": "missing category", "routed": routed}
            result = self.create_category(category, parent=str(routed.get("parent_category") or ""))
            self.commit_post_state(extra_force_paths=[CATEGORY_SNAPSHOT_PATH], message=f"Sync LazyBlog category {category}")
            return {"action": action, "status": "executed", "routed": routed, "category": result.get("category")}
        if action == "sync_categories" and confidence >= 0.55:
            mirror = self.sync_category_mirror()
            self.commit_post_state(extra_force_paths=[CATEGORY_SNAPSHOT_PATH], message="Sync LazyBlog category mirror")
            return {"action": action, "status": "executed", "routed": routed, "category_count": len(mirror.get("categories", []))}
        if action == "draft_post" and confidence >= 0.55:
            target_mode = str(routed.get("post_target_mode") or "ask").strip() or "ask"
            if target_mode == "ask":
                return {
                    "action": action,
                    "status": "needs_clarification",
                    "question": str(routed.get("clarification_question") or self.clarification_message_for_post_action(session_id, action, routed)),
                    "routed": routed,
                }
            reference = str(routed.get("post_reference") or "").strip()
            instruction = str(routed.get("instruction") or "").strip() or str(routed.get("reason") or "").strip()
            result = self.draft_post_project(
                self.active_post_project_id(session_id) or None,
                session_id,
                instruction=instruction,
                status="draft",
                target_mode=target_mode,
                post_reference=reference,
            )
            return {"action": action, "status": "executed", "routed": routed, "draft_path": result.get("draft_path"), "post_project": result.get("post_project", {})}
        if action == "publish_post" and confidence >= 0.7:
            target_mode = str(routed.get("post_target_mode") or "ask").strip() or "ask"
            if target_mode == "ask":
                return {
                    "action": action,
                    "status": "needs_clarification",
                    "question": str(routed.get("clarification_question") or self.clarification_message_for_post_action(session_id, action, routed)),
                    "routed": routed,
                }
            reference = str(routed.get("post_reference") or "").strip()
            requested_status = str(routed.get("status") or "draft").strip()
            if requested_status not in {"draft", "publish", "private"}:
                requested_status = "draft"
            instruction = str(routed.get("instruction") or "").strip() or str(routed.get("reason") or "").strip()
            result = self.publish_post_project(
                self.active_post_project_id(session_id) or None,
                session_id,
                status=requested_status,
                force_redraft=bool(routed.get("force_redraft", False)),
                instruction=instruction,
                target_mode=target_mode,
                post_reference=reference,
            )
            return {"action": action, "status": "executed", "routed": routed, "published": result.get("published"), "post_project": result.get("post_project", {})}
        return {"action": action, "status": "no_op", "routed": routed}

    def reply_to_stored_message(
        self,
        message: str,
        session_id: str,
        user_path: Path | None = None,
        queue_id: str = "",
    ) -> dict[str, Any]:
        effective_message = str(message or "").strip()
        if user_path and user_path.exists():
            try:
                stored_row = self.read_message(user_path)
                effective_message = str(stored_row.get("effective_content") or effective_message).strip() or effective_message
            except Exception:
                pass
        action_result = self.execute_chat_action(session_id, self.route_chat_action(session_id, effective_message))
        if str(action_result.get("status") or "") == "needs_clarification":
            clarification = str(action_result.get("question") or "I need one clarification before I continue.").strip()
            assistant_path = self.append_message(
                session_id,
                "assistant",
                clarification,
                {
                    "intent": "clarification",
                    "should_draft": False,
                    "suggested_title": "",
                    "queue_id": queue_id,
                    "queue_status": "succeeded",
                },
            )
            return {
                **self.session_payload(session_id),
                "reply": {
                    "reply": clarification,
                    "intent": "clarification",
                    "should_draft": False,
                    "suggested_title": "",
                },
                "action_result": action_result,
                "assistant_path": str(assistant_path.relative_to(ROOT_DIR)),
            }
        backend_artifacts = self.maybe_generate_backend_artifacts(session_id, effective_message, queue_id=queue_id)
        local_matches = self.search_local_content(effective_message)
        payload = {
            "session": self.load_session(session_id),
            "message": effective_message,
            "transcript": self.transcript(session_id),
            "local_matches": local_matches,
            "category_snapshot": self.category_snapshot(),
            "controlled_action_result": action_result,
            "backend_artifact_result": backend_artifacts,
            "control_surface": {
                "chat_rule": "Send & Store only records chat memory and replies. It must not claim a WordPress/category action has been completed.",
                "managed_objects": ["ChatSession", "PostProject", "WordPressPost", "CategoryMirror"],
                "available_controlled_actions": [
                    "POST /api/posts creates a local post project",
                    "POST /api/post/draft drafts or revises the selected post project",
                    "POST /api/post/publish publishes or updates the selected WordPress post",
                    "POST /api/category creates a category",
                    "POST /api/category/update updates a category",
                    "POST /api/category/delete deletes a category",
                    "POST /api/categories/sync refreshes the local category mirror",
                ],
            },
            "storage": {
                "session_dir": str(self.session_dir(session_id).relative_to(ROOT_DIR)),
                "user_message_path": str(user_path.relative_to(ROOT_DIR)) if user_path else "",
                "queue_id": queue_id,
            },
        }
        result = self.run_codex_tool(
            session_id=session_id,
            tool_name="reply",
            prompt_template_path=CHAT_REPLY_PROMPT,
            schema_path=CHAT_REPLY_SCHEMA,
            payload=payload,
        )
        reply_text = str(result["reply"])
        if backend_artifacts.get("generated"):
            names = ", ".join(str(item.get("title") or item.get("path") or "artifact") for item in backend_artifacts.get("artifacts", [])[:4])
            reply_text = (
                reply_text.rstrip()
                + "\n\nBackend artifacts generated in the Pipe: "
                + names
                + ". Open `Pipe`; Canvas will show the plot image and the PDF tab will show the compiled report."
            )
        elif backend_artifacts.get("error"):
            reply_text = reply_text.rstrip() + f"\n\nBackend artifact generation failed: {backend_artifacts.get('error')}"
        assistant_path = self.append_message(
            session_id,
            "assistant",
            reply_text,
            {
                "intent": result.get("intent", ""),
                "should_draft": bool(result.get("should_draft", False)),
                "suggested_title": result.get("suggested_title", ""),
                "backend_artifacts_json": json.dumps(backend_artifacts, ensure_ascii=False),
                "queue_id": queue_id,
                "queue_status": "succeeded",
            },
        )
        return {
            **self.session_payload(session_id),
            "reply": result,
            "action_result": action_result,
            "assistant_path": str(assistant_path.relative_to(ROOT_DIR)),
        }

    def reply(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        if not message.strip():
            raise WebAppError("message is empty")
        session = self.create_session(message) if not session_id else self.load_session(safe_session_id(session_id))
        session_id = session["id"]
        user_path = self.append_message(session_id, "user", message)
        return self.reply_to_stored_message(message, session_id, user_path)

    def draft_folder(self, session_id: str) -> Path:
        return DRAFT_ROOT / safe_session_id(session_id)

    def latest_draft_path(self, session_id: str) -> Path | None:
        meta = self.load_session(session_id)
        raw = meta.get("latest_draft")
        if raw:
            path = (ROOT_DIR / raw).resolve()
            if path.exists() and ROOT_DIR in path.parents:
                return path
        candidates = sorted(self.draft_folder(session_id).glob("*.md"))
        return candidates[-1] if candidates else None

    def draft_front_matter(self, draft: dict[str, Any], status: str | None = None) -> dict[str, Any]:
        return {
            "title": draft.get("title", ""),
            "slug": slugify(draft.get("slug") or draft.get("title") or "lazyblog-draft"),
            "source_language": draft.get("source_language") or "en",
            "status": status or draft.get("status") or "draft",
            "excerpt": draft.get("excerpt", ""),
            "categories": list_from_value(draft.get("categories")),
            "tags": list_from_value(draft.get("tags")),
        }

    def create_draft(self, session_id: str, instruction: str = "", status: str = "draft") -> dict[str, Any]:
        session = self.load_session(safe_session_id(session_id))
        transcript = self.transcript(session_id, limit=36)
        local_matches = self.search_local_content(transcript + "\n" + instruction, limit=10)
        payload = {
            "session": session,
            "instruction": instruction,
            "requested_status": status,
            "transcript": transcript,
            "local_matches": local_matches,
            "category_snapshot": self.category_snapshot(),
            "storage": {
                "session_dir": str(self.session_dir(session_id).relative_to(ROOT_DIR)),
                "draft_dir": str(self.draft_folder(session_id).relative_to(ROOT_DIR)),
            },
        }
        result = self.run_codex_tool(
            session_id=session_id,
            tool_name="task",
            prompt_template_path=CHAT_TASK_PROMPT,
            schema_path=CHAT_TASK_SCHEMA,
            payload=payload,
        )
        draft = result["draft"]
        slug = slugify(draft.get("slug") or draft.get("title") or session_id, fallback=session_id)
        draft_dir = self.draft_folder(session_id)
        draft_path = draft_dir / f"{stamp()}-{slug}.md"
        markdown = draft.get("markdown", "").strip()
        write_markdown(draft_path, self.draft_front_matter(draft, status=status), markdown)
        task_profile = self.codex_profile("task")
        manifest = {
            "session_id": session_id,
            "created_at": now_iso(),
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "title": draft.get("title"),
            "slug": slug,
            "source_language": draft.get("source_language") or "en",
            "categories": list_from_value(draft.get("categories")),
            "tags": list_from_value(draft.get("tags")),
            "codex": {
                "model": task_profile["model"],
                "reasoning": task_profile["reasoning"],
                "reply": result.get("reply", ""),
                "action": result.get("action", ""),
                "needs_review": result.get("needs_review", False),
                "notes": result.get("notes", []),
                "research_queries": result.get("research_queries", []),
                "research_sources": result.get("research_sources", []),
                "local_matches_used": result.get("local_matches_used", []),
            },
        }
        write_json(draft_path.with_suffix(".json"), manifest)
        session["latest_draft"] = str(draft_path.relative_to(ROOT_DIR))
        self.save_session(session_id, session)
        return {
            **self.session_payload(session_id),
            "draft": {
                "path": str(draft_path.relative_to(ROOT_DIR)),
                "markdown": draft_path.read_text(encoding="utf-8"),
            },
            "task": result,
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "manifest_path": str(draft_path.with_suffix(".json").relative_to(ROOT_DIR)),
            "markdown": draft_path.read_text(encoding="utf-8"),
        }

    def markdown_post_metadata(self, markdown: str, fallback_title: str = "Untitled post") -> dict[str, Any]:
        front_matter, body = split_front_matter(markdown)
        title = front_matter.get("title") or first_heading(body) or fallback_title
        return {
            "title": title,
            "slug": front_matter.get("slug") or slugify(title, "post"),
            "source_language": front_matter.get("source_language") or front_matter.get("language") or "en",
            "excerpt": front_matter.get("excerpt") or "",
            "categories": front_matter_list(markdown, "categories") or list_from_value(front_matter.get("categories")),
            "tags": front_matter_list(markdown, "tags") or list_from_value(front_matter.get("tags")),
        }

    def ensure_active_post_project(self, session_id: str, instruction: str = "") -> str:
        active_id = self.active_post_project_id(session_id)
        if active_id:
            return active_id
        payload = self.create_post_project(session_id=session_id, instruction=instruction)
        return str(payload["post_project"]["id"])

    def draft_post_project(
        self,
        post_project_id: str | None,
        session_id: str,
        instruction: str = "",
        status: str = "draft",
        target_mode: str = "auto",
        post_reference: str = "",
    ) -> dict[str, Any]:
        status = status if status in {"draft", "publish", "private"} else "draft"
        session_id = safe_session_id(session_id)
        resolution = self.resolve_post_project_for_write_action(
            session_id=session_id,
            action="draft_post",
            post_project_id=post_project_id,
            instruction=instruction,
            target_mode=target_mode,
            post_reference=post_reference,
        )
        if resolution.get("status") != "ready":
            raise WebAppError(str(resolution.get("question") or "clarification required before drafting"))
        resolved_post_project_id = str(resolution.get("post_project_id") or "")
        project = self.load_post_project(resolved_post_project_id)
        sessions = list_from_value(project.get("source_sessions"))
        if session_id not in sessions:
            sessions.append(session_id)
            project["source_sessions"] = sessions
            self.save_post_project(project)
        self.set_active_post_project(session_id, resolved_post_project_id)

        current_draft_path = self.current_draft_path_for_project(project)
        current_draft = current_draft_path.read_text(encoding="utf-8") if current_draft_path else ""
        session = self.load_session(session_id)
        full_transcript = self.transcript(session_id, limit=36)
        scope = self.draft_scope_for_instruction(session_id, instruction, limit=36)
        transcript = str(scope.get("focused_transcript") or full_transcript)
        category_override = self.extract_category_override(instruction)
        local_matches = self.search_local_content(transcript + "\n" + current_draft + "\n" + instruction, limit=10)
        payload = {
            "session": session,
            "post_project": project,
            "instruction": instruction,
            "requested_status": status,
            "transcript": transcript,
            "full_transcript": full_transcript,
            "scope": scope,
            "category_override": category_override,
            "current_draft": current_draft,
            "local_matches": local_matches,
            "category_snapshot": self.category_snapshot(),
            "category_mirror": self.search_categories(limit=120).get("categories", []),
            "control_surface": {
                "allowed_actions": [
                    "draft selected post project",
                    "revise selected post project",
                    "publish selected post project as draft/publish/private",
                    "create/update/delete category through taxonomy API only",
                ],
                "rule": "Chat is context only. PostProject is the managed article object.",
            },
            "storage": {
                "session_dir": str(self.session_dir(session_id).relative_to(ROOT_DIR)),
                "post_project_dir": str(self.post_project_dir(resolved_post_project_id).relative_to(ROOT_DIR)),
                "draft_dir": str((self.post_project_dir(resolved_post_project_id) / "drafts").relative_to(ROOT_DIR)),
            },
        }
        result = self.run_codex_tool(
            session_id=session_id,
            tool_name="task",
            prompt_template_path=CHAT_TASK_PROMPT,
            schema_path=CHAT_TASK_SCHEMA,
            payload=payload,
        )
        draft = result["draft"]
        slug = slugify(draft.get("slug") or draft.get("title") or project.get("slug") or resolved_post_project_id, fallback=resolved_post_project_id)
        draft_dir = self.post_project_dir(resolved_post_project_id) / "drafts"
        draft_path = draft_dir / f"{stamp()}-{slug}.md"
        markdown = str(draft.get("markdown") or "").strip()
        front_matter = self.draft_front_matter(draft, status=status)
        if category_override:
            front_matter["categories"] = category_override
        if not front_matter["categories"] and project.get("categories"):
            front_matter["categories"] = list_from_value(project.get("categories"))
        if not front_matter["tags"] and project.get("tags"):
            front_matter["tags"] = list_from_value(project.get("tags"))
        write_markdown(draft_path, front_matter, markdown)
        metadata = self.markdown_post_metadata(draft_path.read_text(encoding="utf-8"), fallback_title=str(project.get("title") or "Untitled post"))
        project.update(
            {
                "title": metadata["title"],
                "slug": metadata["slug"],
                "source_language": metadata["source_language"] if metadata["source_language"] in {"en", "ja", "zh"} else "en",
                "categories": category_override or metadata["categories"] or list_from_value(project.get("categories")),
                "tags": metadata["tags"] or list_from_value(project.get("tags")),
                "current_draft": str(draft_path.relative_to(self.post_project_dir(resolved_post_project_id))),
            }
        )
        self.save_post_project(project)
        task_profile = self.codex_profile("task")
        draft_manifest = {
            "post_project_id": resolved_post_project_id,
            "session_id": session_id,
            "created_at": now_iso(),
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "title": metadata["title"],
            "slug": metadata["slug"],
            "source_language": metadata["source_language"],
            "categories": category_override or metadata["categories"],
            "tags": metadata["tags"],
            "codex": {
                "model": task_profile["model"],
                "reasoning": task_profile["reasoning"],
                "reply": result.get("reply", ""),
                "action": result.get("action", ""),
                "needs_review": result.get("needs_review", False),
                "notes": result.get("notes", []),
                "research_queries": result.get("research_queries", []),
                "research_sources": result.get("research_sources", []),
                "local_matches_used": result.get("local_matches_used", []),
            },
        }
        write_json(draft_path.with_suffix(".json"), draft_manifest)
        session = self.load_session(session_id)
        session["active_post_project_id"] = resolved_post_project_id
        session["latest_draft"] = str(draft_path.relative_to(ROOT_DIR))
        self.save_session(session_id, session)
        warnings: list[str] = []
        self.commit_post_state(
            post_project_id=resolved_post_project_id,
            session_id=session_id,
            message=f"Draft LazyBlog Studio post {resolved_post_project_id}",
        )
        payload = {
            **self.session_payload(session_id),
            **self.post_project_payload(resolved_post_project_id),
            "task": result,
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "manifest_path": str(draft_path.with_suffix(".json").relative_to(ROOT_DIR)),
            "markdown": draft_path.read_text(encoding="utf-8"),
            "warnings": warnings,
        }
        return payload

    def resolve_terms(self, client: WPClient, endpoint: str, names: list[str]) -> tuple[list[int], list[str]]:
        ids: list[int] = []
        warnings: list[str] = []
        for name in names:
            query = urllib.parse.urlencode({"search": name, "per_page": 100, "context": "edit"})
            try:
                rows = client.request("GET", f"/wp-json/wp/v2/{endpoint}?{query}")
                exact = next(
                    (
                        row
                        for row in rows
                        if isinstance(row, dict) and html.unescape(str(row.get("name", ""))).casefold() == name.casefold()
                    ),
                    None,
                )
                if exact:
                    ids.append(int(exact["id"]))
                    continue
                created = client.request("POST", f"/wp-json/wp/v2/{endpoint}", {"name": name})
                ids.append(int(created["id"]))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not resolve {endpoint} term {name!r}: {exc}")
        return ids, warnings

    def publish(self, session_id: str, status: str = "draft", force_redraft: bool = False, instruction: str = "") -> dict[str, Any]:
        status = status if status in {"draft", "publish", "private"} else "draft"
        session_id = safe_session_id(session_id)
        draft_result: dict[str, Any] | None = None
        draft_path = None if force_redraft else self.latest_draft_path(session_id)
        if draft_path is None:
            draft_result = self.create_draft(session_id, instruction=instruction, status=status)
            draft_path = ROOT_DIR / draft_result["draft_path"]
        if draft_path is None:
            raise WebAppError("failed to create draft")

        load_env_file(ROOT_DIR / ".env")
        require_auth()
        client = make_client(SimpleNamespace(site_url=None))
        markdown = draft_path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(markdown)
        title = front_matter.get("title") or first_heading(body) or draft_path.stem
        source_language = front_matter.get("source_language") or "en"
        categories = front_matter_list(markdown, "categories")
        tags = front_matter_list(markdown, "tags")
        category_ids, category_warnings = self.resolve_terms(client, "categories", categories)
        tag_ids, tag_warnings = self.resolve_terms(client, "tags", tags)
        payload: dict[str, Any] = {
            "title": title,
            "content": markdown_to_html(markdown),
            "status": status,
            "slug": front_matter.get("slug") or slugify(title),
        }
        if front_matter.get("excerpt"):
            payload["excerpt"] = front_matter["excerpt"]
        if category_ids:
            payload["categories"] = category_ids
        if tag_ids:
            payload["tags"] = tag_ids

        post = client.request("POST", "/wp-json/wp/v2/posts", payload)
        source_warning = ""
        try:
            client.set_source_language(int(post["id"]), source_language)
        except Exception as exc:  # noqa: BLE001
            source_warning = f"published, but source language meta was not set: {exc}"

        published = {
            "session_id": session_id,
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "published_at": now_iso(),
            "status": status,
            "post_id": post.get("id"),
            "link": post.get("link"),
            "source_language": source_language,
            "warnings": [*category_warnings, *tag_warnings, *([source_warning] if source_warning else [])],
        }
        publish_path = draft_path.with_suffix(".published.json")
        write_json(publish_path, published)
        session = self.load_session(session_id)
        session.setdefault("published", []).append(published)
        self.save_session(session_id, session)
        self.commit_post_state(
            session_id=session_id,
            extra_force_paths=[self.draft_folder(session_id)],
            message=f"Publish LazyBlog Studio draft {post.get('id')}",
        )
        return {
            **self.session_payload(session_id),
            "draft": {
                "path": str(draft_path.relative_to(ROOT_DIR)),
                "markdown": draft_path.read_text(encoding="utf-8"),
            },
            "redraft": draft_result,
            "published": published,
        }

    def publish_post_project(
        self,
        post_project_id: str | None,
        session_id: str,
        status: str = "draft",
        force_redraft: bool = False,
        instruction: str = "",
        update_existing: bool = True,
        target_mode: str = "auto",
        post_reference: str = "",
    ) -> dict[str, Any]:
        status = status if status in {"draft", "publish", "private"} else "draft"
        session_id = safe_session_id(session_id)
        resolution = self.resolve_post_project_for_write_action(
            session_id=session_id,
            action="publish_post",
            post_project_id=post_project_id,
            instruction=instruction,
            target_mode=target_mode,
            post_reference=post_reference,
        )
        if resolution.get("status") != "ready":
            raise WebAppError(str(resolution.get("question") or "clarification required before publishing"))
        resolved_post_project_id = str(resolution.get("post_project_id") or "")
        redraft: dict[str, Any] | None = None
        project = self.load_post_project(resolved_post_project_id)
        draft_path = None if force_redraft else self.current_draft_path_for_project(project)
        if draft_path is None:
            redraft = self.draft_post_project(
                resolved_post_project_id,
                session_id,
                instruction=instruction,
                status=status,
                target_mode="update_selected",
            )
            project = self.load_post_project(resolved_post_project_id)
            draft_path = self.current_draft_path_for_project(project)
        if draft_path is None:
            raise WebAppError("failed to create a draft for the selected post")

        client = self.wp_client()
        category_override = self.extract_category_override(instruction)
        if category_override:
            self.rewrite_markdown_categories(draft_path, category_override)
        markdown = draft_path.read_text(encoding="utf-8")
        metadata = self.markdown_post_metadata(markdown, fallback_title=str(project.get("title") or draft_path.stem))
        categories = category_override or metadata["categories"] or list_from_value(project.get("categories"))
        tags = metadata["tags"] or list_from_value(project.get("tags"))
        category_ids, category_warnings = self.resolve_terms(client, "categories", categories)
        tag_ids, tag_warnings = self.resolve_terms(client, "tags", tags)
        payload: dict[str, Any] = {
            "title": metadata["title"],
            "content": markdown_to_html(markdown),
            "status": status,
            "slug": metadata["slug"],
        }
        if metadata.get("excerpt"):
            payload["excerpt"] = metadata["excerpt"]
        if category_ids:
            payload["categories"] = category_ids
        if tag_ids:
            payload["tags"] = tag_ids

        wordpress = project.get("wordpress") if isinstance(project.get("wordpress"), dict) else {}
        existing_id = wordpress.get("post_id")
        if update_existing and existing_id:
            post = client.update_post(int(existing_id), payload)
            action = "updated"
        else:
            post = client.request("POST", "/wp-json/wp/v2/posts", payload)
            action = "created"

        source_warning = ""
        try:
            client.set_source_language(int(post["id"]), str(metadata["source_language"] or project.get("source_language") or "en"))
        except Exception as exc:  # noqa: BLE001
            source_warning = f"post saved, but source language meta was not set: {exc}"

        project.update(
            {
                "title": metadata["title"],
                "slug": metadata["slug"],
                "source_language": metadata["source_language"] if metadata["source_language"] in {"en", "ja", "zh"} else "en",
                "categories": categories,
                "tags": tags,
                "wordpress": {
                    "post_id": post.get("id"),
                    "status": status,
                    "link": post.get("link") or "",
                },
            }
        )
        self.save_post_project(project)
        try:
            self.sync_category_mirror()
        except Exception:
            pass

        published = {
            "post_project_id": resolved_post_project_id,
            "session_id": session_id,
            "draft_path": str(draft_path.relative_to(ROOT_DIR)),
            "published_at": now_iso(),
            "action": action,
            "status": status,
            "post_id": post.get("id"),
            "link": post.get("link"),
            "source_language": metadata["source_language"],
            "warnings": [*category_warnings, *tag_warnings, *([source_warning] if source_warning else [])],
        }
        event_dir = self.post_project_dir(resolved_post_project_id) / "publish-events"
        event_path = event_dir / f"{stamp()}-{post.get('id')}.json"
        write_json(event_path, published)
        session = self.load_session(session_id)
        session["active_post_project_id"] = resolved_post_project_id
        session.setdefault("published", []).append(published)
        self.save_session(session_id, session)
        self.commit_post_state(
            post_project_id=resolved_post_project_id,
            session_id=session_id,
            extra_force_paths=[CATEGORY_SNAPSHOT_PATH],
            message=f"Publish LazyBlog Studio post {post.get('id')}",
        )
        return {
            **self.session_payload(session_id),
            **self.post_project_payload(resolved_post_project_id),
            "redraft": redraft,
            "published": published,
        }

    def link_post_project(self, post_project_id: str, post_id: Any, status: str = "", link: str = "") -> dict[str, Any]:
        project = self.load_post_project(post_project_id)
        if not str(post_id).strip().isdigit():
            raise WebAppError("post_id must be numeric")
        resolved_post_id = int(str(post_id).strip())
        resolved_status = status.strip() if status.strip() in {"draft", "publish", "private", "pending", "future"} else ""
        resolved_link = link.strip()
        if not resolved_link or not resolved_status:
            try:
                post = self.wp_client().get_post(resolved_post_id)
                resolved_link = resolved_link or str(post.get("link") or "")
                resolved_status = resolved_status or str(post.get("status") or "draft")
            except Exception:
                resolved_status = resolved_status or "draft"
        project["wordpress"] = {
            "post_id": resolved_post_id,
            "status": resolved_status,
            "link": resolved_link,
        }
        self.save_post_project(project)
        self.commit_post_state(
            post_project_id=str(project["id"]),
            message=f"Link LazyBlog Studio post {project['id']} to WordPress {resolved_post_id}",
        )
        return self.post_project_payload(str(project["id"]))

    def session_payload(self, session_id: str, limit: int = DEFAULT_MESSAGE_BATCH_SIZE, before: str = "") -> dict[str, Any]:
        session = self.load_session(safe_session_id(session_id))
        page = self.message_page(session_id, limit=limit, before=before)
        draft_path = self.latest_draft_path(session_id)
        draft = None
        if draft_path:
            draft = {
                "path": str(draft_path.relative_to(ROOT_DIR)),
                "markdown": draft_path.read_text(encoding="utf-8"),
            }
        active_post = None
        active_id = str(session.get("active_post_project_id") or "")
        if active_id:
            try:
                active_post = self.post_project_payload(active_id)
                if active_post.get("draft"):
                    draft = active_post["draft"]
            except WebAppError:
                active_post = None
        return {
            "session": session,
            "messages": page["messages"],
            "message_page": page["message_page"],
            "draft": draft,
            "active_post_project": active_post,
            "chat_queue": self.chat_queue_summary(session_id),
        }


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#16a394">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="LazyBlog Studio">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/icons/lazyblog.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/icons/lazyblog.svg">
  <link rel="stylesheet" href="/assets/vendor/katex.css">
  <script src="/assets/vendor/marked.js"></script>
  <script src="/assets/vendor/dompurify.js"></script>
  <script src="/assets/vendor/katex.js"></script>
  <script src="/assets/vendor/katex-auto-render.js"></script>
  <title>LazyBlog Studio</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,650&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap");
    :root {
      --ink: #15231e;
      --muted: #607069;
      --paper: #ffffff;
      --surface: #f4fbf8;
      --line: rgba(31, 71, 58, 0.16);
      --teal: #16a394;
      --teal-dark: #08776c;
      --coral: #f06449;
      --sun: #ffc857;
      --shadow: 0 18px 50px rgba(22, 79, 62, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Newsreader", Georgia, serif;
      background: #edf8f4;
      min-height: 100vh;
      overflow-x: hidden;
    }
    button, input, textarea, select { font: inherit; }
    .shell { display: grid; grid-template-columns: 260px minmax(0, 1fr) 360px; gap: 18px; width: 100%; max-width: 100vw; height: 100vh; padding: 20px; overflow: hidden; }
    .panel { min-width: 0; background: rgba(255, 255, 255, 0.96); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); backdrop-filter: blur(18px); overflow: hidden; }
    .side, .publish { min-width: 0; max-height: calc(100vh - 40px); padding: 18px; overflow-y: auto; }
    .brand { padding: 22px; border-bottom: 1px solid var(--line); background: #e8f8f3; }
    h1, h2 { font-family: "Fraunces", Georgia, serif; line-height: 1; margin: 0; }
    h1 { font-size: 34px; letter-spacing: 0; }
    h2 { font-size: 20px; letter-spacing: 0; }
    .sub { color: var(--muted); margin: 10px 0 0; font-size: 15px; }
    .session-list { display: grid; gap: 10px; margin-top: 16px; }
    .session { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; border: 1px solid var(--line); border-radius: 18px; padding: 12px; cursor: pointer; background: rgba(255, 255, 255, 0.42); transition: transform 160ms ease, border-color 160ms ease, background 160ms ease; }
    .session:hover, .session.active { transform: translateY(-1px); border-color: rgba(15, 118, 110, 0.42); background: rgba(255, 255, 255, 0.68); }
    .session-main { min-width: 0; }
    .session strong { display: block; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session span { display: block; color: var(--muted); font-size: 12px; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-more { width: 30px; height: 30px; padding: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.06); color: var(--ink); font-size: 18px; line-height: 1; }
    .modal-backdrop { position: fixed; inset: 0; z-index: 1000; display: none; background: rgba(22, 30, 25, 0.34); backdrop-filter: blur(8px); overflow: hidden; }
    .modal-backdrop.open { display: block; }
    .session-modal { position: fixed; top: 50%; left: 50%; width: min(360px, calc(100vw - 32px)); max-width: calc(100vw - 32px); max-height: calc(100vh - 32px); overflow-y: auto; transform: translate(-50%, -50%); border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 26px; padding: 18px; background: rgba(255, 250, 240, 0.96); box-shadow: 0 30px 80px rgba(22, 30, 25, 0.28); }
    .session-modal h2 { font-size: 23px; }
    .session-modal-title { margin: 8px 0 16px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .modal-actions { display: grid; gap: 8px; }
    .modal-actions button { width: 100%; border-radius: 16px; padding: 11px 13px; text-align: left; background: rgba(29, 37, 32, 0.07); color: var(--ink); }
    .modal-actions button:hover { background: rgba(29, 37, 32, 0.12); transform: none; }
    .modal-actions .danger { color: #9b2f16; background: rgba(217, 107, 67, 0.13); }
    .modal-actions .cancel { text-align: center; background: transparent; color: var(--muted); }
    .settings-trigger { width: 40px; height: 40px; min-width: 40px; padding: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.08); color: var(--ink); font-size: 20px; line-height: 1; }
    .settings-trigger:hover { background: rgba(29, 37, 32, 0.14); transform: none; }
    .settings-grid { display: grid; gap: 12px; margin-top: 14px; }
    .settings-card { border: 1px solid var(--line); border-radius: 18px; padding: 12px; background: rgba(255, 255, 255, 0.54); }
    .settings-card strong { display: block; font-size: 14px; margin-bottom: 4px; }
    .settings-card .sub { margin: 0 0 10px; font-size: 12px; }
    .settings-row { display: grid; grid-template-columns: minmax(0, 1fr) 128px; gap: 8px; }
    .settings-actions { display: flex; gap: 8px; margin-top: 16px; }
    .settings-actions button { flex: 1 1 auto; }
    .chat { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-width: 0; max-width: 100%; height: calc(100vh - 40px); overflow: hidden; }
    .chat-head { min-width: 0; padding: 22px 24px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .chat-head > div:first-child { min-width: 0; }
    #chatTitle, #chatMeta, #modelLabel { overflow: hidden; text-overflow: ellipsis; }
    #chatTitle, #chatMeta { white-space: nowrap; }
    .chat-head-actions { display: inline-flex; align-items: center; gap: 10px; min-width: 0; }
    .status { min-width: 0; max-width: 220px; display: inline-flex; gap: 8px; align-items: center; padding: 8px 12px; border-radius: 999px; background: rgba(15, 118, 110, 0.1); color: var(--teal-dark); font-size: 13px; white-space: nowrap; }
    #modelLabel { display: block; min-width: 0; }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--teal); box-shadow: 0 0 0 6px rgba(15, 118, 110, 0.12); }
    .messages { min-width: 0; min-height: 0; max-width: 100%; padding: 18px 24px 24px; overflow-y: auto; overflow-x: hidden; display: flex; flex-direction: column; gap: 14px; }
    .message-list { min-width: 0; display: flex; flex-direction: column; gap: 14px; }
    .more-messages { display: none; align-self: center; margin: 0 auto 2px; padding: 8px 12px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .more-messages.visible { display: inline-flex; }
    .more-messages.loading { cursor: wait; opacity: 0.7; }
    .msg { min-width: 0; max-width: min(760px, 88%); padding: 14px 16px; border-radius: 22px; border: 1px solid var(--line); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; line-height: 1.48; animation: rise 220ms ease both; }
    .msg.user { align-self: flex-end; background: #087f74; color: white; border-color: rgba(8, 119, 108, 0.34); }
    .msg.assistant { align-self: flex-start; background: rgba(255, 255, 255, 0.62); }
    .msg.failed { border-color: rgba(167, 43, 43, 0.4); background: rgba(255, 232, 226, 0.72); color: #7a241d; }
    .msg-body { position: relative; min-width: 0; }
    .msg-content { white-space: normal; margin: 0 20px 0 0; min-height: 18px; }
    .msg-content > :first-child { margin-top: 0; }
    .msg-content > :last-child { margin-bottom: 0; }
    .msg-content p, .msg-content ul, .msg-content ol, .msg-content blockquote, .msg-content pre, .msg-content table { margin: 0.58em 0; }
    .msg-content h1, .msg-content h2, .msg-content h3, .msg-content h4 { margin: 0.8em 0 0.35em; line-height: 1.2; letter-spacing: 0; }
    .msg-content h1 { font-size: 1.45em; }
    .msg-content h2 { font-size: 1.28em; }
    .msg-content h3 { font-size: 1.14em; }
    .msg-content ul, .msg-content ol { padding-left: 1.4em; }
    .msg-content blockquote { padding: 0.18em 0 0.18em 0.8em; border-left: 3px solid rgba(22, 163, 148, 0.5); color: var(--muted); }
    .msg-content code { padding: 0.12em 0.32em; border-radius: 5px; background: rgba(21, 35, 30, 0.09); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em; }
    .msg-content pre { max-width: 100%; padding: 10px 12px; overflow-x: auto; border-radius: 8px; background: #17251f; color: #f1f8f5; }
    .msg-content pre code { padding: 0; background: transparent; color: inherit; }
    .msg-content table { display: block; width: 100%; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
    .msg-content th, .msg-content td { padding: 6px 8px; border: 1px solid rgba(31, 71, 58, 0.2); text-align: left; }
    .msg-content th { background: rgba(22, 163, 148, 0.1); }
    .msg-content a { color: var(--teal-dark); text-decoration: underline; text-underline-offset: 2px; }
    .msg-content img { display: block; max-width: 100%; max-height: min(42vh, 360px); border-radius: 8px; object-fit: contain; }
    .msg-content .katex-display { max-width: 100%; overflow-x: auto; overflow-y: hidden; padding: 0.2em 0; }
    .msg.user .msg-content a { color: #fff7bb; }
    .msg.user .msg-content blockquote { color: rgba(255, 255, 255, 0.82); border-left-color: rgba(255, 255, 255, 0.58); }
    .msg.user .msg-content code { background: rgba(255, 255, 255, 0.17); }
    .msg-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
    .msg-quote-action { width: 34px; height: 34px; min-width: 34px; padding: 0; border: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.08); color: currentColor; cursor: pointer; opacity: 0.9; line-height: 1; font-size: 20px; font-weight: 600; }
    .msg-quote-action:hover { opacity: 1; background: rgba(29, 37, 32, 0.14); }
    .msg-more-action { width: 34px; height: 34px; min-width: 34px; padding: 0; border: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.08); color: currentColor; cursor: pointer; opacity: 0.9; line-height: 1; font-size: 18px; font-weight: 700; }
    .msg-more-action:hover { opacity: 1; background: rgba(29, 37, 32, 0.14); }
    .msg.user .msg-quote-action { color: rgba(255, 255, 255, 0.96); background: rgba(255, 255, 255, 0.16); }
    .msg.user .msg-quote-action:hover { background: rgba(255, 255, 255, 0.22); }
    .msg.user .msg-more-action { color: rgba(255, 255, 255, 0.96); background: rgba(255, 255, 255, 0.16); }
    .msg.user .msg-more-action:hover { background: rgba(255, 255, 255, 0.22); }
    .msg-attachments { margin-top: 10px; display: grid; gap: 8px; }
    .msg-attachment { border-radius: 14px; padding: 8px; background: rgba(255, 255, 255, 0.2); border: 1px solid rgba(39, 55, 46, 0.14); overflow: hidden; }
    .msg-attachment img,
    .msg-attachment video { display: block; max-width: 100%; width: auto; max-height: min(42vh, 320px); margin: 0 auto; border-radius: 10px; object-fit: contain; }
    .msg-attachment iframe { display: block; width: 100%; min-height: 180px; max-height: min(42vh, 320px); border: 0; border-radius: 10px; background: rgba(255, 255, 255, 0.72); }
    .msg-attachment-meta { margin-top: 6px; color: var(--muted); font-size: 11px; }
    .msg-attachment-preview { color: inherit; border-radius: 8px; padding: 8px; background: rgba(255, 255, 255, 0.18); border: 1px dashed rgba(39, 55, 46, 0.24); }
    .attachment-open { width: 100%; padding: 0; background: transparent; border: 0; border-radius: 10px; overflow: hidden; display: block; }
    .attachment-open:hover { transform: none; }
    .attachment-open .tap-meta { display: block; margin-top: 6px; color: var(--muted); font-size: 11px; text-align: left; }
    .attachment-pdf-frame { width: 100%; min-height: 180px; border: 0; border-radius: 10px; background: rgba(255, 255, 255, 0.8); }
    .attachment-video-thumb { position: relative; display: block; }
    .attachment-video-thumb::after { content: "▶"; position: absolute; right: 12px; bottom: 12px; width: 34px; height: 34px; display: inline-flex; align-items: center; justify-content: center; border-radius: 999px; background: rgba(22, 30, 25, 0.72); color: white; font-size: 16px; }
    .composer { min-width: 0; max-width: 100%; padding: 18px; border-top: 1px solid var(--line); background: #f2faf7; overflow: hidden; }
    textarea { width: 100%; min-height: 108px; resize: vertical; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 20px; background: rgba(255, 255, 255, 0.72); color: var(--ink); padding: 14px 15px; outline: none; line-height: 1.45; }
    textarea:focus, select:focus, input:focus { border-color: rgba(15, 118, 110, 0.55); box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
    .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
    .row > * { min-width: 0; }
    .attach-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .attach-btn { width: 40px; height: 40px; min-width: 40px; padding: 0; border-radius: 999px; font-size: 19px; line-height: 1; display: inline-flex; align-items: center; justify-content: center; }
    .attach-btn[title] { text-decoration: none; }
    .attach-btn:hover { transform: none; }
    .attach-btn svg { width: 20px; height: 20px; pointer-events: none; }
    .mic-btn.listening { background: var(--coral); color: white; animation: mic-pulse 1.35s ease-in-out infinite; }
    .composer-sync-status { margin-left: auto; min-width: 0; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .composer-sync-status.saving { color: #8a5c00; }
    .composer-sync-status.saved { color: var(--teal-dark); }
    .composer-sync-status.error { color: #a52d1f; }
    .attach-hint { margin-left: 2px; }
    .attachment-pills { display: flex; flex-wrap: wrap; gap: 6px; }
    .attachment-pill { display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 6px 10px; border: 1px solid var(--line); background: rgba(255, 255, 255, 0.72); max-width: 100%; }
    .attachment-pill button { width: 20px; height: 20px; padding: 0; border: 0; background: transparent; border-radius: 50%; }
    .attachment-pill button:hover { background: rgba(29, 37, 32, 0.12); }
    .attachment-pill span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }
    .attachment-preview-area { display: grid; gap: 8px; margin-top: 8px; max-height: 180px; overflow-y: auto; }
    .attachment-preview-card { border-radius: 14px; border: 1px solid rgba(39, 55, 46, 0.16); padding: 8px; background: rgba(255, 255, 255, 0.42); }
    .attachment-preview-card img,
    .attachment-preview-card video { display: block; max-width: 100%; max-height: 140px; margin: 0 auto; border-radius: 10px; object-fit: contain; }
    .attachment-preview-card strong { font-size: 12px; }
    .attachment-preview-card .meta { margin-top: 4px; color: var(--muted); font-size: 11px; display: block; }
    .attachment-file-chip { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; display: inline-flex; align-items: center; gap: 6px; }
    .composer-reply { margin-top: 10px; border-radius: 18px; border: 1px solid rgba(15, 118, 110, 0.16); background: rgba(255, 255, 255, 0.64); padding: 10px 12px; }
    .composer-reply[hidden] { display: none; }
    .composer-reply-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .composer-reply-label { font-size: 12px; font-weight: 700; color: var(--teal-dark); text-transform: uppercase; letter-spacing: 0; }
    .composer-reply-clear { width: 28px; height: 28px; min-width: 28px; padding: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .composer-reply-preview { margin-top: 6px; color: var(--ink); font-size: 14px; line-height: 1.38; white-space: pre-wrap; overflow-wrap: anywhere; }
    .file-input { display: none; }
    button { border: 0; border-radius: 999px; padding: 11px 16px; background: var(--ink); color: white; cursor: pointer; transition: transform 160ms ease, opacity 160ms ease; }
    button:hover { transform: translateY(-1px); }
    button:disabled { opacity: 0.55; cursor: wait; transform: none; }
    .secondary { background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .accent { background: var(--sun); color: #352500; font-weight: 700; }
    .publish-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .publish-close { display: none; }
    .field { margin-top: 16px; }
    label { display: block; font-size: 13px; color: var(--muted); margin: 0 0 6px 4px; }
    select, input { width: 100%; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 16px; background: rgba(255, 255, 255, 0.68); padding: 10px 12px; outline: none; }
    .preview { height: 360px; min-height: 220px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; background: #18251f; color: #eef6ec; border-color: rgba(255, 255, 255, 0.08); }
    .log { margin-top: 12px; padding: 12px; border-radius: 16px; background: rgba(255, 255, 255, 0.56); color: var(--muted); font-size: 13px; line-height: 1.4; white-space: pre-wrap; min-height: 44px; }
    .path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--teal-dark); overflow-wrap: anywhere; }
    .post-meta strong { color: var(--ink); }
    .post-meta a { color: var(--teal-dark); overflow-wrap: anywhere; }
    .post-meta-title { color: var(--ink); font-weight: 700; overflow-wrap: anywhere; }
    .post-meta-id { display: block; margin-top: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--teal-dark); overflow-wrap: anywhere; }
    .post-meta-grid { display: grid; gap: 6px; margin-top: 8px; }
    .post-meta-row { display: grid; grid-template-columns: 84px minmax(0, 1fr); gap: 8px; }
    .post-meta-key { color: var(--muted); }
    .chip-row { display: flex; flex-wrap: wrap; gap: 5px; }
    .chip { display: inline-flex; max-width: 100%; border-radius: 999px; padding: 3px 8px; background: rgba(15, 118, 110, 0.11); color: var(--teal-dark); overflow-wrap: anywhere; }
    .category-hits { display: grid; gap: 6px; }
    .category-hit { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid rgba(39, 55, 46, 0.1); padding-bottom: 5px; }
    .category-hit:last-child { border-bottom: 0; padding-bottom: 0; }
    .monitor-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 22px; }
    .job-list { display: grid; gap: 9px; margin-top: 12px; }
    .job-card { border: 1px solid var(--line); border-radius: 16px; padding: 10px; background: rgba(255, 255, 255, 0.52); cursor: pointer; }
    .job-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .job-card strong { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .job-card small { display: block; color: var(--muted); margin-top: 5px; overflow-wrap: anywhere; }
    .job-status { border-radius: 999px; padding: 4px 8px; font-size: 11px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .job-status.running, .job-status.queued { background: rgba(227, 169, 47, 0.22); color: #68470e; }
    .job-status.succeeded { background: rgba(15, 118, 110, 0.14); color: var(--teal-dark); }
    .job-status.failed { background: rgba(217, 107, 67, 0.18); color: #7a2f18; }
    .artifact-trigger { position: relative; min-width: 64px; padding: 10px 15px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .artifact-trigger:hover { background: rgba(29, 37, 32, 0.14); transform: none; }
    .artifact-badge { position: absolute; top: -7px; right: -6px; min-width: 21px; height: 21px; padding: 0 6px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; background: #c7331f; color: white; border: 2px solid var(--paper); font-size: 11px; font-weight: 700; box-shadow: 0 8px 18px rgba(156, 37, 24, 0.28); }
    .artifact-badge[hidden] { display: none; }
    .artifact-modal { width: min(1120px, calc(100vw - 24px)); max-width: calc(100vw - 24px); }
    .artifact-help { margin: 8px 0 0; }
    .artifact-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
    .artifact-tab { padding: 8px 12px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .artifact-tab.active { background: var(--teal-dark); color: white; }
    .artifact-layout { display: grid; grid-template-columns: minmax(220px, 0.34fr) minmax(0, 1fr); gap: 12px; margin-top: 12px; min-height: min(68vh, 720px); }
    .artifact-list-panel, .artifact-viewer { min-width: 0; border: 1px solid var(--line); border-radius: 20px; background: rgba(255, 255, 255, 0.5); overflow: hidden; }
    .artifact-list-panel { display: grid; grid-template-rows: auto minmax(0, 1fr); }
    .artifact-list-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line); }
    .compact-button { padding: 7px 10px; font-size: 12px; }
    .artifact-list { min-height: 0; max-height: min(62vh, 660px); overflow-y: auto; padding: 10px; display: grid; gap: 8px; align-content: start; }
    .artifact-row { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px; align-items: stretch; padding: 7px; border-radius: 15px; border: 1px solid rgba(39, 55, 46, 0.12); background: rgba(255, 250, 240, 0.76); color: var(--ink); }
    .artifact-row:hover { transform: none; border-color: rgba(15, 118, 110, 0.34); }
    .artifact-row.active { border-color: rgba(15, 118, 110, 0.62); box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.1); }
    .artifact-row.unread strong::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 999px; background: #c7331f; }
    .artifact-row-main { min-width: 0; display: grid; gap: 5px; padding: 3px; border: 0; border-radius: 10px; background: transparent; color: inherit; text-align: left; }
    .artifact-row-main:hover { transform: none; background: rgba(15, 118, 110, 0.06); }
    .artifact-download { width: 34px; height: 34px; min-width: 34px; align-self: start; padding: 0; border-radius: 999px; background: rgba(29, 37, 32, 0.08); color: var(--ink); font-size: 16px; }
    .artifact-download:hover { transform: none; background: rgba(29, 37, 32, 0.14); }
    .artifact-row-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .artifact-row-top strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
    .artifact-kind { flex: 0 0 auto; border-radius: 999px; padding: 3px 7px; background: rgba(15, 118, 110, 0.1); color: var(--teal-dark); font-size: 11px; }
    .artifact-row-meta, .artifact-row-preview { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .artifact-viewer { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
    .artifact-viewer-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--line); }
    .artifact-viewer-actions { display: inline-flex; align-items: center; gap: 8px; }
    .artifact-viewer-header h3 { margin: 0; font-family: "Fraunces", Georgia, serif; font-size: 22px; letter-spacing: 0; overflow-wrap: anywhere; }
    .artifact-viewer-body { min-width: 0; min-height: 0; padding: 12px; overflow: auto; }
    .artifact-image-frame { margin: 0; min-height: 340px; display: grid; place-items: center; border-radius: 18px; background: #edf7f3; }
    .artifact-preview-image { display: block; max-width: 100%; max-height: min(66vh, 760px); object-fit: contain; border-radius: 14px; box-shadow: 0 18px 46px rgba(22, 30, 25, 0.16); }
    .artifact-video { display: block; width: 100%; max-height: min(66vh, 760px); border-radius: 14px; background: #111; }
    .artifact-pdf-frame { display: block; width: 100%; min-height: min(66vh, 760px); border: 0; border-radius: 14px; background: white; }
    .artifact-editor { width: 100%; min-height: min(66vh, 760px); resize: vertical; border-radius: 16px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; background: #18251f; color: #eef6ec; }
    .artifact-status { padding: 0 14px 12px; color: var(--muted); font-size: 13px; }
    .media-modal { width: min(920px, calc(100vw - 24px)); max-width: calc(100vw - 24px); }
    .media-modal-body { margin-top: 12px; display: grid; gap: 12px; }
    .media-modal-body img,
    .media-modal-body video,
    .media-modal-body iframe { width: 100%; max-height: min(78vh, 920px); border-radius: 18px; border: 0; background: rgba(255, 255, 255, 0.72); }
    .media-modal-meta { color: var(--muted); font-size: 13px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
    .mobile-menu-toggle, .mobile-publish-toggle, .mobile-top-title { display: none; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes mic-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(240, 100, 73, 0.28); } 50% { box-shadow: 0 0 0 7px rgba(240, 100, 73, 0); } }
    @media (max-width: 1080px) { .shell { grid-template-columns: 1fr; } .chat { min-height: 70vh; } .publish { order: 3; } }
    @media (max-width: 720px) {
      .shell { display: block; height: 100svh; padding: calc(52px + env(safe-area-inset-top)) 8px max(8px, env(safe-area-inset-bottom)); overflow: hidden; }
      .mobile-menu-toggle {
        position: fixed;
        top: calc(10px + env(safe-area-inset-top));
        left: 10px;
        z-index: 40;
        display: inline-flex;
        width: 42px;
        height: 38px;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        gap: 4px;
        padding: 0;
        background: rgba(255, 250, 240, 0.92);
        color: var(--ink);
        border: 1px solid var(--line);
        box-shadow: 0 12px 30px rgba(28, 45, 38, 0.12);
      }
      .mobile-menu-toggle span { width: 18px; height: 2px; border-radius: 999px; background: currentColor; }
      .mobile-top-title {
        position: fixed;
        top: calc(10px + env(safe-area-inset-top));
        left: 60px;
        right: 10px;
        z-index: 39;
        display: flex;
        align-items: center;
        height: 38px;
        padding: 0 14px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 250, 240, 0.86);
        box-shadow: 0 12px 30px rgba(28, 45, 38, 0.1);
        font-family: "Fraunces", Georgia, serif;
        font-size: 18px;
        letter-spacing: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .side {
        display: none;
        margin-bottom: 8px;
        padding: 12px;
        border-radius: 22px;
        max-height: 46vh;
        overflow-y: auto;
      }
      .shell.nav-open .side { display: block; }
      .brand { padding: 14px; border-radius: 18px; }
      h1 { font-size: 28px; }
      h2 { font-size: 18px; }
      .sub { font-size: 13px; }
      .session-list { gap: 8px; margin-top: 10px; }
      .session { padding: 9px 10px; border-radius: 14px; }
      .session strong { font-size: 14px; }
      .chat {
        height: calc(100svh - 60px - env(safe-area-inset-top) - env(safe-area-inset-bottom));
        min-height: 0;
        grid-template-rows: auto minmax(0, 1fr) auto;
        border-radius: 22px;
      }
      .chat-head { padding: 10px 12px; gap: 8px; }
      .chat-head .sub { margin-top: 4px; font-size: 12px; max-width: 100%; }
      .chat-head-actions { gap: 6px; }
      .status { flex: 0 1 118px; max-width: 118px; gap: 6px; padding: 6px 8px; font-size: 12px; }
      .artifact-trigger { min-width: 52px; padding: 8px 10px; font-size: 13px; }
      .settings-trigger { width: 36px; height: 36px; min-width: 36px; font-size: 18px; }
      .settings-row { grid-template-columns: 1fr; }
      .dot { width: 7px; height: 7px; box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
      .messages { padding: 8px 10px 10px; gap: 8px; }
      .message-list { gap: 8px; }
      .more-messages { padding: 7px 11px; font-size: 13px; }
      .msg { max-width: 96%; padding: 10px 11px; border-radius: 16px; line-height: 1.38; }
      .msg.user { max-width: 94%; }
      .composer {
        padding: 8px 8px max(8px, env(safe-area-inset-bottom));
        background: rgba(242, 250, 247, 0.96);
      }
      .composer-reply { padding: 8px 10px; border-radius: 14px; }
      .composer-reply-preview { font-size: 13px; }
      textarea { min-height: 72px; max-height: 32vh; border-radius: 16px; padding: 10px 11px; }
      .attach-row { flex-wrap: nowrap; min-width: 0; }
      .attach-btn { width: 42px; height: 42px; min-width: 42px; }
      .attach-hint { display: none; }
      .composer-sync-status { margin-left: 0; flex: 1 1 auto; text-align: right; }
      .composer .row { position: relative; flex-wrap: nowrap; gap: 6px; margin-top: 8px; padding-bottom: 16px; align-items: center; }
      .composer .row button { min-width: 0; padding: 9px 10px; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      #sendButton { flex: 1 1 44%; }
      #draftButton { flex: 1 1 34%; }
      #busyLabel { position: absolute; left: 2px; right: 2px; bottom: -2px; flex: none; font-size: 12px; margin-top: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .mobile-publish-toggle {
        display: inline-flex;
        flex: 0 0 40px;
        width: 40px;
        height: 36px;
        align-items: center;
        justify-content: center;
      }
      .mobile-publish-toggle .triangle {
        width: 0;
        height: 0;
        border-left: 6px solid transparent;
        border-right: 6px solid transparent;
        border-top: 8px solid currentColor;
        transition: transform 160ms ease;
      }
      .shell.publish-open .mobile-publish-toggle .triangle { transform: rotate(180deg); }
      .publish {
        display: none;
        position: fixed;
        left: 8px;
        right: 8px;
        bottom: 8px;
        z-index: 46;
        margin-top: 0;
        padding: 12px;
        border-radius: 22px;
        max-height: min(70svh, calc(100svh - 74px));
        overflow-y: auto;
        box-shadow: 0 22px 60px rgba(22, 30, 25, 0.28);
      }
      .shell.publish-open .publish { display: block; }
      .publish-close { display: inline-flex; padding: 8px 11px; font-size: 13px; }
      .field { margin-top: 10px; }
      .publish .row { flex-wrap: wrap; gap: 8px; }
      .publish button { padding: 9px 12px; }
      .preview { height: 180px; min-height: 140px; }
      .log { padding: 10px; font-size: 12px; }
      .monitor-head { margin-top: 14px; }
      .artifact-modal { width: calc(100vw - 16px); }
      .artifact-layout { grid-template-columns: 1fr; min-height: 0; }
      .artifact-list { max-height: 30vh; }
      .artifact-viewer-body { max-height: 52vh; }
      .artifact-image-frame { min-height: 220px; }
      .artifact-preview-image, .artifact-video { max-height: 48vh; }
      .artifact-pdf-frame, .artifact-editor { min-height: 48vh; }
    }
    @supports not (height: 100svh) {
      @media (max-width: 720px) {
        .shell { height: 100vh; }
        .chat { height: calc(100vh - 60px); }
      }
    }
  </style>
</head>
<body>
  <main class="shell" id="shell">
    <button id="mobileMenuToggle" class="mobile-menu-toggle" type="button" aria-label="Toggle chat history" aria-expanded="false">
      <span></span><span></span><span></span>
    </button>
    <div class="mobile-top-title">LazyBlog Studio</div>
    <aside class="panel side">
      <div class="brand">
        <h1>LazyBlog Studio</h1>
        <p class="sub">Chat becomes Markdown memory, then a WordPress-ready post.</p>
      </div>
      <div class="row">
        <button id="newSession" class="secondary" type="button">New chat</button>
        <button id="refreshSessions" class="secondary" type="button">Refresh</button>
      </div>
      <div id="sessions" class="session-list"></div>
    </aside>
    <section class="panel chat">
      <header class="chat-head">
        <div>
          <h2 id="chatTitle">New chat</h2>
          <p class="sub" id="chatMeta">Messages will be saved as Markdown.</p>
        </div>
        <div class="chat-head-actions">
          <div class="status"><span class="dot"></span><span id="modelLabel">Codex ready</span></div>
          <button id="artifactButton" class="artifact-trigger" type="button" aria-label="Open backend pipe">Pipe <span id="artifactBadge" class="artifact-badge" hidden>0</span></button>
          <button id="settingsButton" class="settings-trigger" type="button" aria-label="Open settings" title="Model settings">⚙</button>
        </div>
      </header>
      <div id="messages" class="messages">
        <button id="moreMessages" class="more-messages" type="button">More messages</button>
        <div id="messageList" class="message-list"></div>
      </div>
      <form id="composer" class="composer">
        <input id="attachmentInput" class="file-input" type="file" multiple>
        <div class="attach-row">
          <button id="attachAnyButton" class="secondary attach-btn" type="button" aria-label="Attach file" title="Attach file">＋</button>
          <button id="micButton" class="secondary attach-btn mic-btn" type="button" aria-label="Start voice input" title="Start voice input">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
              <path d="M12 19v3"></path>
            </svg>
          </button>
          <span id="attachmentHint" class="sub attach-hint">Attach files, images, or video</span>
          <span id="composerStatus" class="composer-sync-status" role="status" aria-live="polite">Saved locally</span>
        </div>
        <div id="attachmentPills" class="attachment-pills" hidden></div>
        <div id="attachmentPreviewArea" class="attachment-preview-area" hidden></div>
        <div id="composerReply" class="composer-reply" hidden>
          <div class="composer-reply-head">
            <span id="composerReplyLabel" class="composer-reply-label">Replying</span>
            <button id="composerReplyClear" class="composer-reply-clear" type="button" aria-label="Clear quoted message">×</button>
          </div>
          <div id="composerReplyPreview" class="composer-reply-preview"></div>
        </div>
        <textarea id="messageInput" placeholder="Write a note, idea, outline, memory, or instruction. The reply tool will store it and respond; the task tool can turn the session into a post."></textarea>
        <div class="row">
          <button id="quotePreviousButton" class="secondary attach-btn" type="button" aria-label="Reply to latest message" title="Reply to latest message">❝</button>
          <button id="sendButton" type="submit">Send & Store</button>
          <button id="draftButton" class="secondary" type="button">Draft Post</button>
          <button id="publishToggle" class="secondary mobile-publish-toggle" type="button" aria-label="Toggle publish tools" aria-controls="publishPanel" aria-expanded="false"><span class="triangle"></span><span class="sr-only">Publish tools</span></button>
          <span class="sub" id="busyLabel"></span>
        </div>
      </form>
    </section>
    <aside class="panel publish" id="publishPanel">
      <div class="publish-head">
        <h2>Publish</h2>
        <button id="publishClose" class="secondary publish-close" type="button" aria-label="Hide publish tools">Hide</button>
      </div>
      <p class="sub">Chat is memory. Posts are independent local projects that can be drafted, selected, published, or updated through controlled APIs.</p>
      <div class="field">
        <label for="postProjectSelect">Selected post project</label>
        <select id="postProjectSelect">
          <option value="">No post project selected</option>
        </select>
      </div>
      <div class="row">
        <button id="newPostProjectButton" class="secondary" type="button">New Post</button>
        <button id="refreshPostProjects" class="secondary" type="button">Refresh Posts</button>
      </div>
      <div id="postProjectMeta" class="log post-meta">No post selected. Draft Post will create one from the current chat.</div>
      <div class="field">
        <label for="postTargetMode">Write target</label>
        <select id="postTargetMode">
          <option value="auto">Auto: selected post or new</option>
          <option value="create_new">Create New Post</option>
          <option value="update_selected">Update Selected Post</option>
        </select>
      </div>
      <div class="field">
        <label for="publishStatus">WordPress status</label>
        <select id="publishStatus">
          <option value="draft">Draft</option>
          <option value="publish">Publish</option>
          <option value="private">Private</option>
        </select>
      </div>
      <div class="field">
        <label for="extraInstruction">Extra instruction for the task tool</label>
        <input id="extraInstruction" placeholder="e.g. make this a journal, category Journals, keep it reflective">
      </div>
      <div class="row">
        <button id="publishButton" class="accent" type="button">Publish Selected</button>
        <button id="redraftButton" class="secondary" type="button">Force Redraft</button>
      </div>
      <div class="field">
        <label for="draftPreview">Latest Markdown draft</label>
        <textarea id="draftPreview" class="preview" readonly></textarea>
      </div>
      <div id="publishLog" class="log">No draft yet.</div>
      <div class="monitor-head">
        <h2>Categories</h2>
        <button id="syncCategories" class="secondary" type="button">Sync</button>
      </div>
      <p class="sub">Category actions use the synced WordPress taxonomy mirror instead of guessing from old local manifests.</p>
      <div class="field">
        <label for="categorySearch">Search category mirror</label>
        <input id="categorySearch" placeholder="Writing, Journals, Hardware...">
      </div>
      <div class="row">
        <button id="searchCategories" class="secondary" type="button">Search</button>
      </div>
      <div id="categoryLog" class="log">Category mirror is loaded on demand.</div>
      <div class="monitor-head">
        <h2>Codex Monitor</h2>
        <button id="refreshJobs" class="secondary" type="button">Poll</button>
      </div>
      <p class="sub">Background prompt-tool jobs are durable and pollable.</p>
      <div id="jobs" class="job-list"></div>
    </aside>
  </main>
  <div id="sessionActionModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="sessionActionTitle">
    <div class="session-modal">
      <h2 id="sessionActionTitle">Chat actions</h2>
      <p id="sessionActionName" class="session-modal-title"></p>
      <div class="modal-actions">
        <button id="modalRename" type="button">Rename</button>
        <button id="modalAutoRename" type="button">Auto rename</button>
        <button id="modalDelete" class="danger" type="button">Delete</button>
        <button id="modalCancel" class="cancel" type="button">Cancel</button>
      </div>
    </div>
  </div>
  <div id="messageActionModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="messageActionTitle">
    <div class="session-modal">
      <h2 id="messageActionTitle">Message actions</h2>
      <p id="messageActionPreview" class="session-modal-title"></p>
      <div class="modal-actions">
        <button id="messageEdit" type="button">Edit</button>
        <button id="messageResend" type="button">Resend</button>
        <button id="messageUnsend" class="danger" type="button">Unsend</button>
        <button id="messageCancel" class="cancel" type="button">Cancel</button>
      </div>
    </div>
  </div>
  <div id="settingsModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="settingsModalTitle">
    <div class="session-modal">
      <h2 id="settingsModalTitle">Model Settings</h2>
      <p class="session-modal-title">Configure the model and reasoning used by the Studio.</p>
      <div class="settings-grid">
        <div class="settings-card">
          <strong>Chat Reply</strong>
          <p class="sub">Normal chat replies in the Studio.</p>
          <div class="settings-row">
            <input id="settingsReplyModel" placeholder="gpt-5.6-sol">
            <select id="settingsReplyReasoning"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select>
          </div>
        </div>
        <div class="settings-card">
          <strong>Write Assistant</strong>
          <p class="sub">Drafting and revising posts from chat context.</p>
          <div class="settings-row">
            <input id="settingsTaskModel" placeholder="gpt-5.6-sol">
            <select id="settingsTaskReasoning"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select>
          </div>
        </div>
        <div class="settings-card">
          <strong>Router / Clarification</strong>
          <p class="sub">Action routing, sanity checks, and clarification decisions.</p>
          <div class="settings-row">
            <input id="settingsActionModel" placeholder="gpt-5.6-sol">
            <select id="settingsActionReasoning"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select>
          </div>
        </div>
        <div class="settings-card">
          <strong>General Response API</strong>
          <p class="sub">Direct response-style Codex API calls.</p>
          <div class="settings-row">
            <input id="settingsResponseModel" placeholder="gpt-5.6-sol">
            <select id="settingsResponseReasoning"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select>
          </div>
        </div>
        <div class="settings-card">
          <strong>Translation</strong>
          <p class="sub">On-demand translation jobs.</p>
          <div class="settings-row">
            <input id="settingsTranslationModel" placeholder="gpt-5.6-sol">
            <select id="settingsTranslationReasoning"><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select>
          </div>
        </div>
      </div>
      <div class="settings-actions">
        <button id="settingsSave" class="accent" type="button">Save</button>
        <button id="settingsCancel" class="secondary" type="button">Close</button>
      </div>
      <div id="settingsLog" class="log">Loading current settings.</div>
    </div>
  </div>
  <div id="mediaModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="mediaModalTitle">
    <div class="session-modal media-modal">
      <div class="publish-head">
        <h2 id="mediaModalTitle">Attachment Preview</h2>
        <button id="mediaModalClose" class="secondary" type="button" aria-label="Close preview">Close</button>
      </div>
      <div id="mediaModalBody" class="media-modal-body"></div>
    </div>
  </div>
  <div id="artifactModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="artifactModalTitle">
    <div class="session-modal artifact-modal">
      <div class="publish-head">
        <div>
          <h2 id="artifactModalTitle">Backend Pipe</h2>
          <p class="sub artifact-help">Canvas, editor, PDF reader, and file explorer for backend artifacts.</p>
        </div>
        <button id="artifactModalClose" class="secondary" type="button" aria-label="Close backend pipe">Close</button>
      </div>
      <div id="artifactTabs" class="artifact-tabs" role="tablist" aria-label="Backend pipe tabs">
        <button class="artifact-tab active" type="button" data-artifact-tab="canvas">Canvas</button>
        <button class="artifact-tab" type="button" data-artifact-tab="editor">Editor</button>
        <button class="artifact-tab" type="button" data-artifact-tab="pdf">PDF</button>
        <button class="artifact-tab" type="button" data-artifact-tab="explorer">Explorer</button>
      </div>
      <div class="artifact-layout">
        <aside class="artifact-list-panel">
          <div class="artifact-list-header">
            <strong>Artifacts</strong>
            <button id="artifactMarkSeen" class="secondary compact-button" type="button">Mark seen</button>
          </div>
          <div id="artifactList" class="artifact-list">
            <p class="sub">No artifacts yet.</p>
          </div>
        </aside>
        <section class="artifact-viewer">
          <div class="artifact-viewer-header">
            <div>
              <h3 id="artifactViewerTitle">Nothing selected</h3>
              <p id="artifactViewerMeta" class="sub"></p>
            </div>
            <div class="artifact-viewer-actions">
              <span id="artifactViewerKind" class="artifact-kind"></span>
              <button id="artifactDownloadCurrent" class="secondary compact-button" type="button" hidden>Download</button>
            </div>
          </div>
          <div id="artifactViewerBody" class="artifact-viewer-body">
            <p class="sub">Select an artifact to preview it here.</p>
          </div>
          <div id="artifactStatus" class="artifact-status"></div>
        </section>
      </div>
    </div>
  </div>
  <script>
    const state = {
      sessionId: null,
      busy: false,
      messagePage: null,
      loadingMore: false,
      visibleMessages: [],
      modalSession: null,
      modalMessage: null,
      messageSignature: "",
      sessionSignature: "",
      jobSignature: "",
      postProjects: [],
      activePostId: "",
      queuePollTimer: null,
      sessionPollInFlight: false,
      eventSource: null,
      eventSessionId: "",
      eventReconnectTimer: null,
      eventReconnectDelay: 1000,
      eventConnected: false,
      fallbackPollTimer: null,
      safetySyncTimer: null,
      refreshTimer: null,
      pendingRefreshes: new Set(),
      settings: null,
      replyTarget: null,
      composerAttachments: [],
      attachmentSequence: 0,
      modalAttachmentKey: "",
      artifactItems: [],
      selectedArtifactId: "",
      artifactTab: "canvas",
      artifactSignature: "",
      artifactUnreadCount: 0,
      composerClientId: "",
      composerVersion: 0,
      composerDirty: false,
      composerLoadedSessionId: "",
      composerSyncTimer: null,
      composerSaveInFlight: null,
      composerSaveAgain: false,
      speechRecognition: null,
      speechSupported: false,
      speechActive: false,
      speechKeepAlive: false,
      speechBaseText: "",
      speechFinalText: ""
    };
    const $ = (id) => document.getElementById(id);
    const shell = $("shell");
    state.composerClientId = browserClientId();
    $("modelLabel").textContent = "__MODEL_LABEL__";

    function browserClientId() {
      const key = "lazyblog.studio.browserClientId";
      try {
        let value = localStorage.getItem(key) || "";
        if (!value) {
          value = globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
            ? globalThis.crypto.randomUUID()
            : `browser-${Date.now()}-${Math.random().toString(16).slice(2)}`;
          localStorage.setItem(key, value);
        }
        return value;
      } catch {
        return `browser-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      }
    }

    function composerStorageKey(sessionId = state.sessionId || "new") {
      return `lazyblog.studio.composer.${sessionId || "new"}`;
    }

    function readLocalComposer(sessionId = state.sessionId || "new") {
      try {
        const parsed = JSON.parse(localStorage.getItem(composerStorageKey(sessionId)) || "null");
        return parsed && typeof parsed.text === "string" ? parsed : null;
      } catch {
        return null;
      }
    }

    function persistLocalComposer(text, options = {}) {
      try {
        localStorage.setItem(composerStorageKey(options.sessionId), JSON.stringify({
          text: String(text || ""),
          updated_at_ms: Date.now(),
          server_version: Number(options.serverVersion ?? state.composerVersion) || 0,
          synced: Boolean(options.synced)
        }));
      } catch {
        // The server copy remains available when browser storage is unavailable.
      }
    }

    function clearLocalComposer(sessionId = state.sessionId || "new") {
      try {
        localStorage.removeItem(composerStorageKey(sessionId));
      } catch {}
    }

    function setComposerStatus(message, kind = "") {
      const root = $("composerStatus");
      root.textContent = message || "";
      root.className = `composer-sync-status${kind ? ` ${kind}` : ""}`;
    }

    function scheduleComposerSave(delay = 450) {
      if (state.composerSyncTimer) clearTimeout(state.composerSyncTimer);
      state.composerSyncTimer = setTimeout(() => {
        state.composerSyncTimer = null;
        saveComposerDraft().catch(() => {});
      }, delay);
    }

    function composerInputChanged() {
      state.composerDirty = true;
      persistLocalComposer($("messageInput").value, { synced: false });
      setComposerStatus(navigator.onLine ? "Saving draft..." : "Saved on this device", navigator.onLine ? "saving" : "error");
      scheduleComposerSave();
    }

    async function saveComposerDraft(options = {}) {
      const input = $("messageInput");
      const text = input.value;
      if (!state.sessionId && !text.trim()) {
        persistLocalComposer(text, { sessionId: "new", synced: false });
        setComposerStatus("Saved on this device", "saved");
        return null;
      }
      if (state.composerSaveInFlight) {
        state.composerSaveAgain = true;
        return state.composerSaveInFlight;
      }
      if (state.composerSyncTimer) {
        clearTimeout(state.composerSyncTimer);
        state.composerSyncTimer = null;
      }
      const sourceSessionId = state.sessionId || "";
      const sourceStorageId = sourceSessionId || "new";
      const sentText = text;
      const sentVersion = state.composerVersion;
      setComposerStatus("Saving draft...", "saving");
      state.composerSaveInFlight = api("/api/composer", {
        session_id: sourceSessionId,
        text: sentText,
        client_id: state.composerClientId,
        base_version: sentVersion
      });
      try {
        const data = await state.composerSaveInFlight;
        const composer = data.composer || {};
        const newSessionId = composer.session_id || (data.session && data.session.id) || sourceSessionId;
        if (!sourceSessionId && newSessionId && !state.sessionId) {
          state.sessionId = newSessionId;
          state.composerLoadedSessionId = newSessionId;
          const previousLocal = readLocalComposer("new");
          if (previousLocal) {
            localStorage.setItem(composerStorageKey(newSessionId), JSON.stringify(previousLocal));
          }
          clearLocalComposer("new");
          $("chatTitle").textContent = data.session && data.session.title ? data.session.title : "Untitled chat";
          $("chatMeta").textContent = `0 messages stored in content/chat/${newSessionId} · composer saved`;
          startEventStream(true);
          loadSessions().catch(() => {});
        }
        if (newSessionId === state.sessionId) {
          state.composerVersion = Number(composer.version) || 0;
        }
        if (data.conflict) {
          state.composerDirty = true;
          persistLocalComposer(input.value, { synced: false });
          setComposerStatus("Saved locally; another device changed this draft", "error");
          return data;
        }
        if (newSessionId === state.sessionId && input.value === sentText) {
          state.composerDirty = false;
          persistLocalComposer(sentText, { serverVersion: state.composerVersion, synced: true });
          setComposerStatus("Draft saved", "saved");
        } else if (newSessionId === state.sessionId) {
          state.composerDirty = true;
          persistLocalComposer(input.value, { synced: false });
          state.composerSaveAgain = true;
        }
        return data;
      } catch (error) {
        state.composerDirty = true;
        persistLocalComposer(input.value, { sessionId: sourceStorageId, synced: false });
        setComposerStatus("Saved on this device; server unavailable", "error");
        if (options.throwOnError) throw error;
        return null;
      } finally {
        state.composerSaveInFlight = null;
        if (state.composerSaveAgain) {
          state.composerSaveAgain = false;
          scheduleComposerSave(120);
        }
      }
    }

    async function loadComposerDraft(options = {}) {
      const sessionId = state.sessionId;
      if (!sessionId) {
        const local = readLocalComposer("new");
        $("messageInput").value = local ? local.text : "";
        state.composerVersion = 0;
        state.composerDirty = Boolean(local && local.text);
        setComposerStatus(local && local.text ? "Recovered on this device" : "Saved locally", "saved");
        return;
      }
      const local = readLocalComposer(sessionId);
      const data = await api(`/api/composer?session_id=${encodeURIComponent(sessionId)}`);
      if (state.sessionId !== sessionId) return;
      const composer = data.composer || {};
      state.composerVersion = Number(composer.version) || 0;
      state.composerLoadedSessionId = sessionId;
      const localUnsynced = Boolean(local && local.synced === false && local.text !== composer.text);
      if (localUnsynced) {
        $("messageInput").value = local.text;
        state.composerDirty = true;
        setComposerStatus("Recovered unsent text; saving...", "saving");
        scheduleComposerSave(120);
        return;
      }
      if (!state.composerDirty || options.force) {
        $("messageInput").value = String(composer.text || "");
        state.composerDirty = false;
        persistLocalComposer($("messageInput").value, { serverVersion: state.composerVersion, synced: true });
        setComposerStatus(composer.text ? "Draft synced" : "Draft saved", "saved");
      }
    }

    function profileLabel(profile) {
      const model = String(profile && profile.model || "").trim();
      const reasoning = String(profile && profile.reasoning || "").trim();
      return model && reasoning ? `${model} / ${reasoning}` : "Codex ready";
    }

    function updateModelLabel() {
      const replyProfile = state.settings && state.settings.reply ? state.settings.reply : null;
      $("modelLabel").textContent = profileLabel(replyProfile) || "__MODEL_LABEL__";
    }

    function setBusy(label) {
      state.busy = Boolean(label);
      $("busyLabel").textContent = label || "";
      for (const id of ["sendButton", "draftButton", "publishButton", "redraftButton", "attachAnyButton", "quotePreviousButton"]) $(id).disabled = state.busy;
      $("micButton").disabled = state.busy || !state.speechSupported;
    }

    function setQueueStatus(queue) {
      const active = queue && queue.active_count ? queue.active_count : 0;
      if (!state.busy) {
        const latest = queue && Array.isArray(queue.items) && queue.items.length ? queue.items[queue.items.length - 1] : null;
        const attachmentStatus = latest && latest.attachment_analysis_status && latest.attachment_analysis_status !== "none"
          ? ` · attachments ${latest.attachment_analysis_status}`
          : "";
        $("busyLabel").textContent = active ? `${active} chat message${active === 1 ? "" : "s"} queued/running${attachmentStatus}` : "";
      }
      if (state.queuePollTimer) {
        clearTimeout(state.queuePollTimer);
        state.queuePollTimer = null;
      }
      if (active && state.sessionId && !state.eventConnected) {
        state.queuePollTimer = setTimeout(() => {
          pollActiveSession().catch((err) => { $("publishLog").textContent = err.message; });
        }, 2500);
      }
    }

    async function api(path, payload) {
      const options = payload === undefined ? {} : {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      };
      const res = await fetch(path, options);
      const data = await res.json();
      if (res.status === 401 && data.login_url) {
        window.location.href = data.login_url;
        throw new Error("Login required.");
      }
      if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
      return data;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    function renderMarkdown(root, source) {
      const markdown = String(source || "").trim();
      if (!markdown) {
        root.textContent = "";
        return;
      }
      if (!window.marked || !window.DOMPurify) {
        root.textContent = markdown;
        return;
      }
      const rendered = window.marked.parse(markdown, { gfm: true, breaks: true });
      root.innerHTML = window.DOMPurify.sanitize(rendered, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["style", "script"],
        FORBID_ATTR: ["style", "onerror", "onload"]
      });
      for (const link of root.querySelectorAll("a[href]")) {
        const href = String(link.getAttribute("href") || "").trim();
        if (/^(https?:|mailto:)/i.test(href)) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
      }
      if (typeof window.renderMathInElement === "function") {
        window.renderMathInElement(root, {
          delimiters: [
            { left: "$$", right: "$$", display: true },
            { left: "\\[", right: "\\]", display: true },
            { left: "\\(", right: "\\)", display: false },
            { left: "$", right: "$", display: false }
          ],
          ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          throwOnError: false,
          strict: "ignore",
          trust: false
        });
      }
    }

    function stableJson(value) {
      try {
        return JSON.stringify(value || null);
      } catch {
        return "";
      }
    }

    function messagesSignature(messages) {
      return stableJson((messages || []).map((msg) => ({
        id: msg.id,
        role: msg.role,
        content: msg.content,
        queue_status: msg.queue_status,
        attachments: (msg.attachments || []).map((attachment) => ({
          name: attachment.name,
          stored_path: attachment.stored_path,
          mirror_markdown_path: attachment.mirror_markdown_path,
          analysis_status: attachment.analysis_status,
          analysis_note: attachment.analysis_note,
          text_excerpt: attachment.text_excerpt,
          preview_kind: attachment.preview_kind,
          width: attachment.width,
          height: attachment.height
        }))
      })));
    }

    function sessionListSignature(sessions) {
      return stableJson({ active: state.sessionId || "", rows: (sessions || []).map((item) => [item.id, item.title, item.updated_at, item.message_count]) });
    }

    function jobsSignature(jobs) {
      return stableJson((jobs || []).map((job) => [job.id, job.status, job.updated_at]));
    }

    function artifactsSignature(items) {
      return stableJson((items || []).map((item) => [item.id, item.kind, item.path, item.updated_at, item.selected]));
    }

    function artifactReadKey() {
      return `lazyblog.studio.artifacts.readIds.${state.sessionId || "none"}`;
    }

    function artifactReadIds() {
      try {
        const raw = localStorage.getItem(artifactReadKey()) || "[]";
        const parsed = JSON.parse(raw);
        return new Set(Array.isArray(parsed) ? parsed : []);
      } catch {
        return new Set();
      }
    }

    function saveArtifactReadIds(readIds) {
      try {
        localStorage.setItem(artifactReadKey(), JSON.stringify(Array.from(readIds).slice(-1000)));
      } catch {
        // Local read state is best-effort.
      }
    }

    function computeArtifactUnread() {
      const readIds = artifactReadIds();
      return (state.artifactItems || []).filter((item) => item.id && !readIds.has(item.id)).length;
    }

    function renderArtifactBadge() {
      const badge = $("artifactBadge");
      state.artifactUnreadCount = computeArtifactUnread();
      badge.textContent = state.artifactUnreadCount > 99 ? "99+" : String(state.artifactUnreadCount);
      badge.hidden = state.artifactUnreadCount <= 0;
    }

    function markAllArtifactsRead() {
      const readIds = artifactReadIds();
      for (const item of state.artifactItems || []) {
        if (item.id) readIds.add(item.id);
      }
      saveArtifactReadIds(readIds);
      renderArtifactBadge();
      renderArtifactList();
    }

    function artifactTabVisible(item) {
      const tab = state.artifactTab || "canvas";
      const kind = String(item && item.kind || "");
      if (tab === "explorer") return true;
      if (tab === "canvas") return item.tab === "canvas" || kind === "image" || kind === "video";
      if (tab === "editor") return ["markdown", "text", "json", "diff"].includes(kind) || item.tab === "editor";
      if (tab === "pdf") return kind === "pdf" || item.tab === "pdf";
      return true;
    }

    function artifactLabel(item) {
      return [item.source || "artifact", item.path || "", item.created_at || ""].filter(Boolean).join(" · ");
    }

    function clearArtifactViewer() {
      $("artifactViewerTitle").textContent = "Nothing selected";
      $("artifactViewerMeta").textContent = "";
      $("artifactViewerKind").textContent = "";
      $("artifactDownloadCurrent").hidden = true;
      $("artifactViewerBody").innerHTML = `<p class="sub">Select an artifact to preview it here.</p>`;
      $("artifactStatus").textContent = "";
    }

    function preferredArtifactForCurrentTab() {
      const visible = (state.artifactItems || []).filter(artifactTabVisible);
      return visible.find((item) => item.id === state.selectedArtifactId)
        || visible.find((item) => item.selected)
        || visible[0]
        || null;
    }

    function renderArtifactList() {
      for (const tab of document.querySelectorAll(".artifact-tab")) {
        tab.classList.toggle("active", tab.dataset.artifactTab === state.artifactTab);
      }
      const list = $("artifactList");
      if (!state.sessionId) {
        list.innerHTML = `<p class="sub">Select or create a chat first.</p>`;
        clearArtifactViewer();
        return;
      }
      const readIds = artifactReadIds();
      const visible = (state.artifactItems || []).filter(artifactTabVisible);
      if (!visible.length) {
        list.innerHTML = `<p class="sub">No ${escapeHtml(state.artifactTab)} artifacts yet.</p>`;
        if (!state.selectedArtifactId) clearArtifactViewer();
        return;
      }
      list.innerHTML = "";
      for (const item of visible) {
        const row = document.createElement("div");
        row.className = "artifact-row";
        row.classList.toggle("active", item.id === state.selectedArtifactId);
        row.classList.toggle("unread", item.id && !readIds.has(item.id));
        row.dataset.artifactId = item.id || "";
        row.innerHTML = `
          <button class="artifact-row-main" type="button" data-artifact-open-id="${escapeHtml(item.id || "")}">
            <span class="artifact-row-top">
              <strong>${escapeHtml(item.title || item.path || "Artifact")}</strong>
              <span class="artifact-kind">${escapeHtml(item.kind || "file")}</span>
            </span>
            <span class="artifact-row-meta">${escapeHtml(artifactLabel(item))}</span>
            <span class="artifact-row-preview">${escapeHtml(item.preview || "")}</span>
          </button>
          <button class="artifact-download" type="button" data-artifact-download-id="${escapeHtml(item.id || "")}" aria-label="Download artifact" title="Download artifact">↓</button>
        `;
        row.querySelector("[data-artifact-open-id]").addEventListener("click", () => {
          selectArtifact(item.id).catch((err) => { $("artifactStatus").textContent = err.message; });
        });
        row.querySelector("[data-artifact-download-id]").addEventListener("click", (event) => {
          event.stopPropagation();
          downloadArtifact(item.id).catch((err) => { $("artifactStatus").textContent = err.message; });
        });
        list.appendChild(row);
      }
    }

    function renderArtifactContent(content) {
      const item = (state.artifactItems || []).find((candidate) => candidate.id === content.id) || {};
      $("artifactViewerTitle").textContent = content.title || item.title || "Artifact";
      $("artifactViewerMeta").textContent = [content.path || item.path || "", artifactLabel(item)].filter(Boolean).join(" · ");
      $("artifactViewerKind").textContent = content.kind || item.kind || "";
      $("artifactDownloadCurrent").hidden = !content.id;
      if (content.error) {
        $("artifactViewerBody").innerHTML = `<p class="sub">${escapeHtml(content.error)}</p>`;
        return;
      }
      if (content.kind === "image" && content.data_url) {
        $("artifactViewerBody").innerHTML = `<figure class="artifact-image-frame"><img class="artifact-preview-image" src="${content.data_url}" alt="${escapeHtml(content.title || "Artifact image")}"></figure>`;
        return;
      }
      if (content.kind === "video" && content.data_url) {
        $("artifactViewerBody").innerHTML = `<video class="artifact-video" src="${content.data_url}" controls playsinline preload="metadata"></video>`;
        return;
      }
      if (content.kind === "pdf" && content.data_url) {
        $("artifactViewerBody").innerHTML = `<iframe class="artifact-pdf-frame" src="${content.data_url}" title="${escapeHtml(content.title || "Artifact PDF")}"></iframe>`;
        return;
      }
      const text = content.text || "";
      $("artifactViewerBody").innerHTML = `<textarea class="artifact-editor" readonly spellcheck="false">${escapeHtml(text)}</textarea>`;
    }

    function artifactDownloadName(item, content) {
      const sourceName = content.path || item?.path || content.title || item?.title || `artifact-${content.id || "download"}`;
      const fallbackExt =
        content.mime === "application/pdf" || content.kind === "pdf" ? ".pdf"
        : String(content.mime || "").startsWith("image/png") ? ".png"
        : String(content.mime || "").startsWith("image/svg") ? ".svg"
        : content.kind === "json" ? ".json"
        : content.kind === "diff" ? ".diff"
        : content.kind === "markdown" ? ".md"
        : ".txt";
      const base = String(sourceName)
        .split("/")
        .filter(Boolean)
        .pop()
        || "";
      const cleanBase = base
        .replace(/[^A-Za-z0-9._-]+/g, "-")
        .replace(/^-+|-+$/g, "");
      const name = cleanBase || `artifact-${content.id || Date.now()}`;
      return /\.[A-Za-z0-9]{1,8}$/.test(name) ? name : `${name}${fallbackExt}`;
    }

    function blobFromDataUrl(dataUrl) {
      const [meta = "", payload = ""] = String(dataUrl || "").split(",", 2);
      const mime = (meta.match(/^data:([^;,]+)/) || [])[1] || "application/octet-stream";
      if (meta.includes(";base64")) {
        const binary = atob(payload);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        return new Blob([bytes], { type: mime });
      }
      return new Blob([decodeURIComponent(payload)], { type: mime });
    }

    async function downloadArtifact(artifactId) {
      if (!state.sessionId || !artifactId) return;
      const item = (state.artifactItems || []).find((candidate) => candidate.id === artifactId) || {};
      $("artifactStatus").textContent = "Preparing download...";
      const data = await api(`/api/artifact?session_id=${encodeURIComponent(state.sessionId)}&artifact_id=${encodeURIComponent(artifactId)}`);
      const blob = data.data_url
        ? blobFromDataUrl(data.data_url)
        : new Blob([typeof data.text === "string" ? data.text : JSON.stringify(data, null, 2)], { type: data.mime || "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = artifactDownloadName(item, data);
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      const readIds = artifactReadIds();
      readIds.add(artifactId);
      saveArtifactReadIds(readIds);
      renderArtifactBadge();
      renderArtifactList();
      $("artifactStatus").textContent = "Download ready.";
    }

    async function loadArtifacts(options = {}) {
      if (!state.sessionId) {
        state.artifactItems = [];
        state.selectedArtifactId = "";
        state.artifactSignature = "";
        renderArtifactBadge();
        renderArtifactList();
        return;
      }
      const data = await api(`/api/artifacts?session_id=${encodeURIComponent(state.sessionId)}`);
      const items = data.items || [];
      const signature = artifactsSignature(items);
      state.artifactItems = items;
      if (!state.selectedArtifactId || !items.some((item) => item.id === state.selectedArtifactId)) {
        state.selectedArtifactId = data.selected_artifact_id || items[0]?.id || "";
      }
      if (signature !== state.artifactSignature) {
        state.artifactSignature = signature;
        renderArtifactBadge();
        renderArtifactList();
      } else {
        renderArtifactBadge();
      }
      if (options.loadSelected && state.selectedArtifactId) {
        await selectArtifact(state.selectedArtifactId, { persist: false });
      }
    }

    async function selectArtifact(artifactId, options = {}) {
      if (!state.sessionId || !artifactId) return;
      state.selectedArtifactId = artifactId;
      renderArtifactList();
      $("artifactStatus").textContent = "Loading artifact...";
      if (options.persist !== false) {
        await api("/api/artifact/select", { session_id: state.sessionId, artifact_id: artifactId });
      }
      const data = await api(`/api/artifact?session_id=${encodeURIComponent(state.sessionId)}&artifact_id=${encodeURIComponent(artifactId)}`);
      renderArtifactContent(data);
      $("artifactStatus").textContent = "";
      const readIds = artifactReadIds();
      readIds.add(artifactId);
      saveArtifactReadIds(readIds);
      renderArtifactBadge();
      renderArtifactList();
    }

    async function openArtifactModal() {
      $("artifactModal").classList.add("open");
      await loadArtifacts({ loadSelected: true });
      const preferred = preferredArtifactForCurrentTab();
      if (preferred) await selectArtifact(preferred.id, { persist: false });
      markAllArtifactsRead();
    }

    function closeArtifactModal() {
      $("artifactModal").classList.remove("open");
      $("artifactStatus").textContent = "";
    }

    function openSessionModal(session) {
      state.modalSession = session;
      $("sessionActionName").textContent = session.title || session.id;
      $("sessionActionModal").classList.add("open");
    }

    function closeSessionModal() {
      $("sessionActionModal").classList.remove("open");
    }

    function openMessageModal(message) {
      state.modalMessage = message;
      const preview = String(message && message.content || "(attachment)").trim().replace(/\s+/g, " ").slice(0, 180);
      $("messageActionPreview").textContent = `${message.role || "message"} · ${preview || "(empty message)"}`;
      $("messageActionModal").classList.add("open");
    }

    function closeMessageModal() {
      $("messageActionModal").classList.remove("open");
    }

    function settingsFormValue(profile, field) {
      return $(`settings${profile}${field}`);
    }

    function openSettingsModal() {
      $("settingsModal").classList.add("open");
    }

    function closeSettingsModal() {
      $("settingsModal").classList.remove("open");
    }

    function attachmentKey(attachment) {
      return [attachment && attachment.name || "", attachment && attachment.mime || "", attachment && attachment.size || 0, attachment && attachment.data_url || ""].join("|");
    }

    function closeMediaModal() {
      state.modalAttachmentKey = "";
      $("mediaModal").classList.remove("open");
      $("mediaModalBody").innerHTML = "";
      $("mediaModalTitle").textContent = "Attachment Preview";
    }

    function openAttachmentModal(attachment) {
      const key = attachmentKey(attachment);
      if (state.modalAttachmentKey && state.modalAttachmentKey === key && $("mediaModal").classList.contains("open")) {
        closeMediaModal();
        return;
      }
      state.modalAttachmentKey = key;
      $("mediaModalTitle").textContent = attachment.name || "Attachment Preview";
      const body = $("mediaModalBody");
      body.innerHTML = "";
      const mime = String(attachment.mime || "").toLowerCase();
      if (mime === "application/pdf" || String(attachment.preview_kind || "") === "pdf") {
        const frame = document.createElement("iframe");
        frame.src = attachment.data_url;
        frame.title = attachment.name || "PDF preview";
        body.appendChild(frame);
      } else if (attachment.kind === "video") {
        const video = document.createElement("video");
        video.src = attachment.data_url;
        video.controls = true;
        video.playsInline = true;
        video.preload = "metadata";
        body.appendChild(video);
      } else if (attachment.kind === "image") {
        const image = document.createElement("img");
        image.src = attachment.data_url || attachment.preview_url;
        image.alt = attachment.name || "Image preview";
        body.appendChild(image);
      } else if (attachment.preview_url && attachment.preview_url !== attachment.data_url) {
        const image = document.createElement("img");
        image.src = attachment.preview_url;
        image.alt = attachment.name || "Attachment preview";
        body.appendChild(image);
      }
      const meta = document.createElement("div");
      meta.className = "media-modal-meta";
      meta.textContent = [
        attachment.name || "attachment",
        attachment.mime || "application/octet-stream",
        formatBytes(attachment.size || 0),
        attachment.analysis_status ? `analysis: ${attachment.analysis_status}` : "",
        attachment.analysis_note || "",
        attachment.mirror_markdown_path ? `mirror: ${attachment.mirror_markdown_path}` : "",
        attachment.text_excerpt ? `\n\n${attachment.text_excerpt}` : ""
      ].filter(Boolean).join(" · ");
      body.appendChild(meta);
      $("mediaModal").classList.add("open");
    }

    function applySettingsToForm(settings) {
      const map = {
        Reply: settings.reply || {},
        Task: settings.task || {},
        Action: settings.action || {},
        Response: settings.response || {},
        Translation: settings.translation || {},
      };
      for (const [name, profile] of Object.entries(map)) {
        settingsFormValue(name, "Model").value = profile.model || "";
        settingsFormValue(name, "Reasoning").value = profile.reasoning || "medium";
      }
    }

    async function loadSettings() {
      const data = await api("/api/settings");
      state.settings = data.settings || null;
      if (state.settings) {
        applySettingsToForm(state.settings);
      }
      updateModelLabel();
      $("settingsLog").textContent = "Loaded current model settings.";
    }

    function collectSettingsPayload() {
      return {
        reply: { model: settingsFormValue("Reply", "Model").value.trim(), reasoning: settingsFormValue("Reply", "Reasoning").value },
        task: { model: settingsFormValue("Task", "Model").value.trim(), reasoning: settingsFormValue("Task", "Reasoning").value },
        action: { model: settingsFormValue("Action", "Model").value.trim(), reasoning: settingsFormValue("Action", "Reasoning").value },
        response: { model: settingsFormValue("Response", "Model").value.trim(), reasoning: settingsFormValue("Response", "Reasoning").value },
        translation: { model: settingsFormValue("Translation", "Model").value.trim(), reasoning: settingsFormValue("Translation", "Reasoning").value },
      };
    }

    async function saveSettings() {
      $("settingsLog").textContent = "Saving settings...";
      const data = await api("/api/settings", collectSettingsPayload());
      state.settings = data.settings || null;
      if (state.settings) {
        applySettingsToForm(state.settings);
      }
      updateModelLabel();
      $("settingsLog").textContent = "Settings saved. New jobs will use these profiles.";
    }

    function clearChat() {
      stopSpeechRecognition();
      state.sessionId = null;
      state.messagePage = null;
      state.visibleMessages = [];
      state.messageSignature = "";
      state.activePostId = "";
      state.artifactItems = [];
      state.selectedArtifactId = "";
      state.artifactSignature = "";
      state.composerVersion = 0;
      state.composerDirty = false;
      state.composerLoadedSessionId = "";
      renderArtifactBadge();
      renderArtifactList();
      clearComposerAttachments();
      $("chatTitle").textContent = "New chat";
      $("chatMeta").textContent = "Messages will be saved as Markdown.";
      $("messageList").innerHTML = "";
      $("messages").scrollTop = 0;
      updateMoreButton();
      $("draftPreview").value = "";
      $("publishLog").textContent = "No draft yet.";
      setQueueStatus(null);
      renderPostProjects([], "");
      loadComposerDraft().catch(() => {});
      startEventStream();
    }

    function updateMoreButton() {
      const page = state.messagePage || {};
      const button = $("moreMessages");
      button.classList.toggle("visible", Boolean(page.has_more));
      button.classList.toggle("loading", state.loadingMore);
      button.textContent = state.loadingMore ? "Loading..." : "More messages";
      button.disabled = state.loadingMore || !page.has_more;
    }

    function renderSessions(sessions) {
      const signature = sessionListSignature(sessions);
      if (state.sessionSignature === signature) return;
      state.sessionSignature = signature;
      const root = $("sessions");
      root.innerHTML = "";
      for (const item of sessions) {
        const el = document.createElement("div");
        el.className = "session" + (item.id === state.sessionId ? " active" : "");
        const title = item.title || item.id;
        el.innerHTML = `
          <div class="session-main">
            <strong>${escapeHtml(title)}</strong>
            <span>${escapeHtml(item.updated_at || "")}</span>
          </div>
          <button class="session-more" type="button" aria-label="Chat actions" aria-expanded="false">&#8943;</button>
        `;
        el.addEventListener("click", () => {
          loadSession(item.id);
          shell.classList.remove("nav-open");
          $("mobileMenuToggle").setAttribute("aria-expanded", "false");
        });
        el.querySelector(".session-more").addEventListener("click", (event) => {
          event.stopPropagation();
          openSessionModal({ id: item.id, title });
        });
        root.appendChild(el);
      }
    }

    function renderJobs(jobs) {
      const signature = jobsSignature(jobs);
      if (state.jobSignature === signature) return;
      state.jobSignature = signature;
      const root = $("jobs");
      root.innerHTML = "";
      if (!jobs || jobs.length === 0) {
        root.innerHTML = `<div class="log">No Codex API jobs yet.</div>`;
        return;
      }
      for (const job of jobs) {
        const el = document.createElement("div");
        el.className = "job-card";
        const status = escapeHtml(job.status || "unknown");
        el.innerHTML = `
          <div class="job-top">
            <strong>${escapeHtml(job.tool || "codex")} / ${escapeHtml(job.schema || "response")}</strong>
            <span class="job-status ${status}">${status}</span>
          </div>
          <small>${escapeHtml(job.id || "")}</small>
          <small>${escapeHtml(job.prompt_preview || job.updated_at || "")}</small>
        `;
        el.onclick = async () => {
          try {
            const data = await api(`/api/codex/job?id=${encodeURIComponent(job.id)}`);
            $("publishLog").textContent = JSON.stringify(data.output || data.job, null, 2);
          } catch (err) {
            $("publishLog").textContent = err.message;
          }
        };
        root.appendChild(el);
      }
    }

    function projectLabel(project) {
      const wp = project.wordpress || {};
      const status = wp.post_id ? `${wp.status || "wp"} #${wp.post_id}` : "local";
      return `${project.id} · ${project.title || "Untitled post"} · ${status}`;
    }

    function renderPostProjects(projects, activeId) {
      state.postProjects = projects || [];
      state.activePostId = activeId || "";
      const select = $("postProjectSelect");
      select.innerHTML = `<option value="">No post project selected</option>`;
      for (const project of state.postProjects) {
        const option = document.createElement("option");
        option.value = project.id;
        option.textContent = projectLabel(project);
        if (project.id === state.activePostId) option.selected = true;
        select.appendChild(option);
      }
      const selected = state.postProjects.find((project) => project.id === state.activePostId);
      renderPostProjectMeta(selected);
    }

    function renderPostProjectMeta(project) {
      if (!project) {
        $("postProjectMeta").textContent = "No post selected. Draft Post will create one from the current chat.";
        return;
      }
      const wp = project.wordpress || {};
      const categories = project.categories || [];
      const source = (project.source_sessions || []).length ? `${project.source_sessions.length} chat source(s)` : "No chat source";
      const local = project.local_mirror || {};
      const link = wp.link ? `<a href="${escapeHtml(wp.link)}" target="_blank" rel="noreferrer">${escapeHtml(wp.link)}</a>` : "Not linked";
      const categoryHtml = categories.length
        ? `<div class="chip-row">${categories.map((name) => `<span class="chip">${escapeHtml(name)}</span>`).join("")}</div>`
        : "No category yet";
      $("postProjectMeta").innerHTML = `
        <div class="post-meta-title">${escapeHtml(project.title || "Untitled post")}</div>
        <span class="post-meta-id">${escapeHtml(project.id || "")}</span>
        <div class="post-meta-grid">
          <div class="post-meta-row"><span class="post-meta-key">WordPress</span><span>${escapeHtml(wp.post_id ? `#${wp.post_id} · ${wp.status || "saved"}` : "Local draft only")}</span></div>
          <div class="post-meta-row"><span class="post-meta-key">Categories</span><span>${categoryHtml}</span></div>
          <div class="post-meta-row"><span class="post-meta-key">Language</span><span>${escapeHtml(project.source_language || "en")}</span></div>
          <div class="post-meta-row"><span class="post-meta-key">Source</span><span>${escapeHtml(source)}</span></div>
          <div class="post-meta-row"><span class="post-meta-key">Mirror</span><span>${escapeHtml(local.post_path || "No local mirror linked")}</span></div>
          <div class="post-meta-row"><span class="post-meta-key">Link</span><span>${link}</span></div>
        </div>
      `;
    }

    async function loadPostProjects() {
      const suffix = state.sessionId ? `?session_id=${encodeURIComponent(state.sessionId)}&limit=100` : "?limit=100";
      const data = await api(`/api/posts${suffix}`);
      const active = data.active_post_project && data.active_post_project.post_project ? data.active_post_project.post_project.id : (data.active_post_project_id || "");
      renderPostProjects(data.post_projects || [], active || "");
      if (data.active_post_project && data.active_post_project.draft) {
        $("draftPreview").value = data.active_post_project.draft.markdown || "";
        $("publishLog").innerHTML = `Selected draft: <span class="path">${escapeHtml(data.active_post_project.draft.path)}</span>`;
      }
    }

    async function selectPostProject(id) {
      if (!state.sessionId) {
        state.activePostId = id || "";
        renderPostProjectMeta(state.postProjects.find((project) => project.id === id));
        return;
      }
      if (!id) {
        const data = await api("/api/post/select", { session_id: state.sessionId, post_project_id: "" });
        state.activePostId = "";
        renderSession(data);
        $("draftPreview").value = "";
        $("publishLog").textContent = "No post selected. Draft Post will create one from this chat.";
        return;
      }
      const data = await api("/api/post/select", { session_id: state.sessionId, post_project_id: id });
      renderSession(data);
    }

    async function newPostProject() {
      if (!state.sessionId) {
        $("publishLog").textContent = "Send at least one message first.";
        return;
      }
      setBusy("creating post project...");
      try {
        const data = await api("/api/posts", {
          session_id: state.sessionId,
          instruction: $("extraInstruction").value
        });
        state.activePostId = data.post_project.id;
        $("publishLog").innerHTML = `Post project created: <span class="path">${escapeHtml(data.post_project.id)}</span>`;
        await loadPostProjects();
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        setBusy("");
      }
    }

    function renderCategories(data) {
      const categories = data.categories || [];
      if (!categories.length) {
        $("categoryLog").textContent = "No categories matched.";
        return;
      }
      $("categoryLog").innerHTML = `<div class="category-hits">${categories.slice(0, 12).map((item) => `
        <div class="category-hit"><span>${escapeHtml(item.name || item.slug)}</span><span>#${escapeHtml(item.term_id || "")} · ${escapeHtml(item.slug || "")}</span></div>
      `).join("")}</div>`;
    }

    async function loadCategories(sync = false) {
      const query = $("categorySearch").value.trim();
      const params = new URLSearchParams({ limit: "80" });
      if (query) params.set("search", query);
      if (sync) params.set("sync", "1");
      const data = await api(`/api/categories?${params.toString()}`);
      renderCategories(data);
    }

    function formatBytes(bytes) {
      const value = Number(bytes) || 0;
      if (value === 0) return "0 B";
      if (value < 1024) return `${value} B`;
      const units = ["KB", "MB", "GB", "TB"];
      let n = value;
      if (n < 1024 ** 2) {
        return `${(n / 1024).toFixed(1)} KB`;
      }
      n /= 1024;
      for (const label of units) {
        if (n < 1024 || label === "TB") {
          return `${n.toFixed(1)} ${label}`;
        }
        n /= 1024;
      }
      return `${n.toFixed(1)} TB`;
    }

    function attachmentKindFromMime(mime, name) {
      const safeMime = (mime || "").toLowerCase();
      const safeName = (name || "").toLowerCase();
      if (safeMime.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic)$/i.test(safeName)) return "image";
      if (safeMime.startsWith("video/") || /\.(mp4|mov|m4v|webm|avi|mkv|flv)$/i.test(safeName)) return "video";
      return "file";
    }

    function attachmentIsPdf(attachment) {
      const mime = String(attachment && attachment.mime || "").toLowerCase();
      const name = String(attachment && attachment.name || "").toLowerCase();
      return mime === "application/pdf" || name.endsWith(".pdf") || String(attachment && attachment.preview_kind || "") === "pdf";
    }

    function renderAttachmentPreview() {
      const pills = $("attachmentPills");
      const preview = $("attachmentPreviewArea");
      if (!state.composerAttachments.length) {
        pills.hidden = true;
        preview.hidden = true;
        pills.innerHTML = "";
        preview.innerHTML = "";
        $("attachmentHint").textContent = "Attach files, images, or video";
        return;
      }
      pills.hidden = false;
      preview.hidden = false;
      pills.innerHTML = "";
      preview.innerHTML = "";
      $("attachmentHint").textContent = `${state.composerAttachments.length} attachment(s) ready`;
      for (const attachment of state.composerAttachments) {
        const pill = document.createElement("span");
        pill.className = "attachment-pill";
        pill.innerHTML = `<span class="attachment-file-chip"><span>${escapeHtml(attachment.name)}</span></span><button type="button" aria-label="Remove ${escapeHtml(attachment.name)}" data-id="${escapeHtml(attachment.id)}">×</button>`;
        const remove = pill.querySelector("button");
        remove.addEventListener("click", () => {
          state.composerAttachments = state.composerAttachments.filter((item) => item.id !== attachment.id);
          renderAttachmentPreview();
        });
        pills.appendChild(pill);

        const card = document.createElement("div");
        card.className = "attachment-preview-card";
        if (attachment.kind === "image") {
          const image = document.createElement("img");
          image.src = attachment.preview_url;
          image.alt = attachment.name;
          card.appendChild(image);
        } else if (attachment.kind === "video") {
          const video = document.createElement("video");
          video.src = attachment.preview_url;
          video.controls = true;
          video.playsInline = true;
          video.preload = "metadata";
          card.appendChild(video);
        } else if (attachmentIsPdf(attachment) && attachment.preview_url) {
          const image = document.createElement("img");
          image.src = attachment.preview_url;
          image.alt = `${attachment.name} preview`;
          card.appendChild(image);
        } else {
          card.innerHTML = `<strong>📎 ${escapeHtml(attachment.name)}</strong>`;
        }
        const status = attachment.analysis_status ? ` · ${attachment.analysis_status}` : "";
        card.insertAdjacentHTML("beforeend", `<span class="meta">${escapeHtml(attachment.kind)} · ${escapeHtml(attachment.mime || "application/octet-stream")} · ${escapeHtml(formatBytes(attachment.size || 0))}${escapeHtml(status)}</span>`);
        preview.appendChild(card);
      }
    }

    function clearComposerAttachments() {
      state.composerAttachments = [];
      renderAttachmentPreview();
      $("attachmentInput").value = "";
    }

    function readAttachmentAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(reader.error || new Error("Failed to read file"));
        reader.readAsDataURL(file);
      });
    }

    async function onAttachmentsSelected(event) {
      const files = Array.from(event.target.files || []);
      if (files.length === 0) return;
      const next = [...state.composerAttachments];
      for (const file of files) {
        const name = file.name || `attachment-${state.attachmentSequence++}`;
        const mime = file.type || "application/octet-stream";
        const kind = attachmentKindFromMime(mime, name);
        let dataUrl = "";
        try {
          dataUrl = await readAttachmentAsDataUrl(file);
        } catch {
          continue;
        }
        if (!dataUrl) continue;
        next.push({
          id: `attachment-${Date.now()}-${state.attachmentSequence++}`,
          name,
          kind,
          mime,
          size: file.size || 0,
          data_url: dataUrl,
          preview_url: kind === "file" ? dataUrl : dataUrl
        });
      }
      state.composerAttachments = next;
      renderAttachmentPreview();
      $("attachmentInput").value = "";
    }

    function setSpeechState(active, message = "") {
      state.speechActive = active;
      const button = $("micButton");
      button.classList.toggle("listening", active);
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-label", active ? "Stop voice input" : "Start voice input");
      button.title = active ? "Stop voice input" : "Start voice input";
      if (message) setComposerStatus(message, active ? "saving" : "error");
    }

    function speechTextWithInterim(interim = "") {
      const spoken = `${state.speechFinalText}${interim}`.trim();
      if (!spoken) return state.speechBaseText;
      const separator = state.speechBaseText && !/\s$/.test(state.speechBaseText) ? " " : "";
      return `${state.speechBaseText}${separator}${spoken}`;
    }

    function stopSpeechRecognition() {
      state.speechKeepAlive = false;
      if (state.speechRecognition && state.speechActive) {
        try {
          state.speechRecognition.stop();
        } catch {}
      }
      setSpeechState(false);
      if (state.composerDirty) scheduleComposerSave(80);
    }

    function startSpeechRecognition() {
      if (!state.speechSupported || !state.speechRecognition) {
        setComposerStatus("Voice input is not supported by this browser", "error");
        return;
      }
      state.speechBaseText = $("messageInput").value;
      state.speechFinalText = "";
      state.speechKeepAlive = true;
      try {
        state.speechRecognition.start();
      } catch (error) {
        state.speechKeepAlive = false;
        setSpeechState(false, error && error.message ? error.message : "Could not start voice input");
      }
    }

    function initSpeechRecognition() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      state.speechSupported = Boolean(SpeechRecognition);
      const button = $("micButton");
      if (!SpeechRecognition) {
        button.disabled = true;
        button.title = "Voice input is not supported by this browser";
        button.setAttribute("aria-label", button.title);
        return;
      }
      const recognition = new SpeechRecognition();
      recognition.lang = navigator.language || "en-US";
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      recognition.onstart = () => setSpeechState(true, "Listening...");
      recognition.onresult = (event) => {
        let interim = "";
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const transcript = String(event.results[index][0] && event.results[index][0].transcript || "");
          if (event.results[index].isFinal) {
            state.speechFinalText += `${transcript.trim()} `;
          } else {
            interim += transcript;
          }
        }
        $("messageInput").value = speechTextWithInterim(interim);
        composerInputChanged();
        setComposerStatus("Listening and saving...", "saving");
      };
      recognition.onerror = (event) => {
        const code = String(event.error || "voice input error");
        if (!["no-speech", "aborted"].includes(code)) {
          state.speechKeepAlive = false;
          setSpeechState(false, code === "not-allowed" ? "Microphone permission was not granted" : `Voice input: ${code}`);
        }
      };
      recognition.onend = () => {
        state.speechActive = false;
        if (state.speechKeepAlive && !state.busy && !document.hidden) {
          setTimeout(() => {
            try {
              recognition.start();
            } catch {
              state.speechKeepAlive = false;
              setSpeechState(false, "Voice input stopped");
            }
          }, 180);
          return;
        }
        setSpeechState(false);
      };
      state.speechRecognition = recognition;
      button.disabled = false;
    }

    function toggleSpeechRecognition() {
      if (state.speechActive || state.speechKeepAlive) {
        stopSpeechRecognition();
      } else {
        startSpeechRecognition();
      }
    }

    function buildMessageAttachmentNode(attachment) {
      const item = document.createElement("div");
      item.className = "msg-attachment";
      if (attachment.kind === "image") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "attachment-open";
        button.innerHTML = `<img src="${escapeHtml(attachment.preview_url)}" alt="${escapeHtml(attachment.name || "Image preview")}"><span class="tap-meta">Tap to open</span>`;
        button.addEventListener("click", () => openAttachmentModal(attachment));
        item.appendChild(button);
      } else if (attachment.kind === "video") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "attachment-open attachment-video-thumb";
        if (attachment.preview_url && attachment.preview_url !== attachment.data_url) {
          button.innerHTML = `<img src="${escapeHtml(attachment.preview_url)}" alt="${escapeHtml(attachment.name || "Video preview")}"><span class="tap-meta">Tap to play</span>`;
        } else {
          button.innerHTML = `<span class="msg-attachment-preview">🎬 ${escapeHtml(attachment.name)}<br><small>Tap to play</small></span>`;
        }
        button.addEventListener("click", () => openAttachmentModal(attachment));
        item.appendChild(button);
      } else if (attachmentIsPdf(attachment)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "attachment-open";
        if (attachment.preview_url) {
          button.innerHTML = `<img src="${escapeHtml(attachment.preview_url)}" alt="${escapeHtml(attachment.name || "PDF preview")}"><span class="tap-meta">Tap to open PDF</span>`;
        } else {
          button.innerHTML = `<span class="msg-attachment-preview">📄 ${escapeHtml(attachment.name)}<br><small>Tap to open PDF</small></span>`;
        }
        button.addEventListener("click", () => openAttachmentModal(attachment));
        item.appendChild(button);
      } else {
        const fileLink = document.createElement("a");
        fileLink.href = attachment.data_url;
        fileLink.className = "msg-attachment-preview";
        fileLink.download = attachment.name;
        fileLink.textContent = `📎 ${attachment.name}`;
        item.appendChild(fileLink);
      }
      const meta = document.createElement("div");
      meta.className = "msg-attachment-meta";
      const status = attachment.analysis_status ? ` · ${attachment.analysis_status}` : "";
      const note = attachment.analysis_note ? ` · ${attachment.analysis_note}` : "";
      meta.textContent = `${attachment.kind} · ${attachment.mime} · ${formatBytes(attachment.size || 0)}${status}${note}`;
      item.appendChild(meta);
      return item;
    }

    function quotePreviewText(raw) {
      const text = String(raw || "").trim();
      if (!text) return "> (empty message)";
      const compact = text.replace(/\n+/g, "\n").trim();
      const lines = compact.split("\n").slice(0, 6);
      return lines.map((line) => `> ${line}`).join("\n");
    }

    function replyRoleLabel(role) {
      if (role === "user") return "Your message";
      if (role === "assistant") return "Studio reply";
      return "Quoted message";
    }

    function replyPreviewText(raw) {
      const text = String(raw || "").trim();
      if (!text) return "(empty message)";
      const compact = text.replace(/\n+/g, "\n").trim();
      return compact.split("\n").slice(0, 3).join("\n");
    }

    function renderReplyTarget() {
      const root = $("composerReply");
      if (!state.replyTarget) {
        root.hidden = true;
        $("composerReplyLabel").textContent = "Replying";
        $("composerReplyPreview").textContent = "";
        return;
      }
      root.hidden = false;
      $("composerReplyLabel").textContent = replyRoleLabel(state.replyTarget.role);
      $("composerReplyPreview").textContent = replyPreviewText(state.replyTarget.content);
    }

    function clearReplyTarget() {
      state.replyTarget = null;
      renderReplyTarget();
    }

    function setReplyTarget(msg) {
      if (!msg || (!msg.content && (!Array.isArray(msg.attachments) || !msg.attachments.length))) return false;
      state.replyTarget = {
        role: msg.role || "assistant",
        content: String(msg.content || "").trim() || "(attachment)"
      };
      renderReplyTarget();
      const input = $("messageInput");
      input.focus();
      const end = input.value.length;
      input.setSelectionRange(end, end);
      return true;
    }

    function composeReplyPrefix() {
      if (!state.replyTarget) return "";
      const label = replyRoleLabel(state.replyTarget.role);
      const quoted = quotePreviewText(state.replyTarget.content);
      return `> ${label}:\n${quoted}`;
    }

    function composeChatMessage(raw) {
      const text = String(raw || "").trim();
      const prefix = composeReplyPrefix();
      if (!prefix) return text;
      return text ? `${prefix}\n\n${text}` : prefix;
    }

    function quotePreviousMessage() {
      const list = state.visibleMessages || [];
      if (!list.length) {
        $("publishLog").textContent = "No message to quote.";
        return;
      }
      const previous = list[list.length - 1];
      if (!previous.content && (!Array.isArray(previous.attachments) || !previous.attachments.length)) {
        $("publishLog").textContent = "Previous message has no text to quote.";
        return;
      }
      const quoted = setReplyTarget(previous);
      if (quoted) {
        $("publishLog").textContent = "Reply target set from latest message.";
      }
    }

    function isNearBottom() {
      const scroller = $("messages");
      return (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) < 72;
    }

    function scrollMessagesToBottom() {
      const scroller = $("messages");
      scroller.scrollTop = scroller.scrollHeight;
    }

    function mergeMessages(existing, incoming) {
      const order = [];
      const seen = new Set();
      const byId = new Map();
      for (const row of existing || []) {
        if (!row || !row.id) continue;
        byId.set(row.id, row);
        if (!seen.has(row.id)) {
          seen.add(row.id);
          order.push(row.id);
        }
      }
      for (const row of incoming || []) {
        if (!row || !row.id) continue;
        byId.set(row.id, row);
        if (!seen.has(row.id)) {
          seen.add(row.id);
          order.push(row.id);
        }
      }
      return order.map((id) => byId.get(id)).filter(Boolean);
    }

    function syncMessagePageFromVisible(payloadPage) {
      const total = payloadPage && payloadPage.total ? payloadPage.total : (state.visibleMessages || []).length;
      const loaded = (state.visibleMessages || []).length;
      const nextBefore = loaded < total && state.visibleMessages.length ? state.visibleMessages[0].id : "";
      state.messagePage = {
        limit: payloadPage && payloadPage.limit ? payloadPage.limit : 10,
        total,
        loaded_count: loaded,
        has_more: loaded < total,
        next_before: nextBefore
      };
    }

    function buildMessageNode(msg) {
      const el = document.createElement("div");
      el.className = `msg ${msg.role}`;
      if (msg.queue_status === "failed") el.classList.add("failed");

      const body = document.createElement("div");
      body.className = "msg-body";
      const content = document.createElement("div");
      content.className = "msg-content";
      renderMarkdown(content, msg.content || "");
      const actions = document.createElement("div");
      actions.className = "msg-actions";
      const quoteAction = document.createElement("button");
      quoteAction.type = "button";
      quoteAction.className = "msg-quote-action";
      quoteAction.setAttribute("aria-label", "Reply to this message");
      quoteAction.title = "Reply to this message";
      quoteAction.textContent = "❝";
      quoteAction.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const inserted = setReplyTarget(msg);
        if (inserted) {
          $("publishLog").textContent = "Reply target set from selected message.";
        }
      });
      const moreAction = document.createElement("button");
      moreAction.type = "button";
      moreAction.className = "msg-more-action";
      moreAction.setAttribute("aria-label", "Message actions");
      moreAction.title = "Message actions";
      moreAction.textContent = "⋯";
      moreAction.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openMessageModal(msg);
      });
      actions.appendChild(quoteAction);
      actions.appendChild(moreAction);
      body.append(content);

      if (Array.isArray(msg.attachments) && msg.attachments.length > 0) {
        const attachRoot = document.createElement("div");
        attachRoot.className = "msg-attachments";
        for (const attachment of msg.attachments) {
          attachRoot.appendChild(buildMessageAttachmentNode(attachment));
        }
        body.appendChild(attachRoot);
      }
      body.appendChild(actions);
      el.appendChild(body);
      return el;
    }

    function renderMessages(messages) {
      const root = $("messageList");
      state.messageSignature = messagesSignature(messages || []);
      root.innerHTML = "";
      state.visibleMessages = messages || [];
      for (const msg of messages || []) {
        const el = buildMessageNode(msg);
        root.appendChild(el);
      }
      scrollMessagesToBottom();
    }

    function prependMessages(messages) {
      if (!messages || messages.length === 0) return;
      const scroller = $("messages");
      const list = $("messageList");
      const previousHeight = scroller.scrollHeight;
      state.visibleMessages = [...messages, ...(state.visibleMessages || [])];
      state.messageSignature = messagesSignature(state.visibleMessages);
      for (const msg of [...messages].reverse()) {
        const el = buildMessageNode(msg);
        list.prepend(el);
      }
      scroller.scrollTop += scroller.scrollHeight - previousHeight;
    }

    function applySessionChrome(payload) {
      state.sessionId = payload.session.id;
      $("chatTitle").textContent = payload.session.title || payload.session.id;
      const queue = payload.chat_queue || {};
      const active = queue.active_count || 0;
      const latest = Array.isArray(queue.items) && queue.items.length ? queue.items[queue.items.length - 1] : null;
      const queueText = active
        ? ` · ${active} queued/running${latest && latest.attachment_analysis_status ? ` · attachments ${latest.attachment_analysis_status}` : ""}`
        : " · idle";
      $("chatMeta").textContent = `${payload.session.message_count || 0} messages stored in content/chat/${payload.session.id}${queueText}`;
      setQueueStatus(payload.chat_queue || null);
      if (payload.draft) {
        $("draftPreview").value = payload.draft.markdown || "";
        $("publishLog").innerHTML = `Latest draft: <span class="path">${escapeHtml(payload.draft.path)}</span>`;
      }
      if (payload.active_post_project && payload.active_post_project.post_project) {
        state.activePostId = payload.active_post_project.post_project.id;
        renderPostProjectMeta(payload.active_post_project.post_project);
      }
    }

    function renderSession(payload) {
      applySessionChrome(payload);
      startEventStream();
      state.messagePage = payload.message_page || null;
      renderMessages(payload.messages || []);
      updateMoreButton();
      loadSessions();
      loadPostProjects().catch((err) => { $("publishLog").textContent = err.message; });
      loadJobs();
      loadArtifacts({ loadSelected: $("artifactModal").classList.contains("open") }).catch((err) => { $("artifactStatus").textContent = err.message; });
    }

    function mergeSessionPayload(payload) {
      const shouldStickBottom = isNearBottom();
      applySessionChrome(payload);
      const merged = mergeMessages(state.visibleMessages || [], payload.messages || []);
      state.visibleMessages = merged;
      syncMessagePageFromVisible(payload.message_page || {});
      const nextSignature = messagesSignature(merged);
      if (nextSignature === state.messageSignature) {
        updateMoreButton();
        return;
      }
      state.messageSignature = nextSignature;
      const root = $("messageList");
      root.innerHTML = "";
      for (const msg of state.visibleMessages) {
        root.appendChild(buildMessageNode(msg));
      }
      if (shouldStickBottom) {
        scrollMessagesToBottom();
      }
      updateMoreButton();
      loadArtifacts({ loadSelected: $("artifactModal").classList.contains("open") }).catch(() => {});
    }

    async function loadSessions(options = {}) {
      const data = await api("/api/sessions");
      const sessions = data.sessions || [];
      renderSessions(sessions);
      if (options.autoload && !state.sessionId && sessions.length > 0) {
        await loadSession(sessions[0].id);
      }
    }

    async function loadJobs() {
      const suffix = state.sessionId ? `?limit=8&session_id=${encodeURIComponent(state.sessionId)}` : "?limit=8";
      const data = await api(`/api/codex/jobs${suffix}`);
      renderJobs(data.jobs || []);
    }

    async function loadSession(id) {
      if (state.sessionId && state.sessionId !== id && state.composerDirty) {
        await saveComposerDraft();
      }
      stopSpeechRecognition();
      const data = await api(`/api/session?id=${encodeURIComponent(id)}&limit=10`);
      renderSession(data);
      state.composerDirty = false;
      await loadComposerDraft({ force: true });
    }

    async function pollActiveSession() {
      if (!state.sessionId || state.sessionPollInFlight || state.loadingMore) return;
      state.sessionPollInFlight = true;
      try {
        const data = await api(`/api/session?id=${encodeURIComponent(state.sessionId)}&limit=10`);
        mergeSessionPayload(data);
      } finally {
        state.sessionPollInFlight = false;
      }
    }

    function queueRealtimeRefresh(kind) {
      state.pendingRefreshes.add(kind);
      if (state.refreshTimer) return;
      state.refreshTimer = setTimeout(() => flushRealtimeRefreshes().catch(() => {}), 120);
    }

    async function flushRealtimeRefreshes() {
      const pending = new Set(state.pendingRefreshes);
      state.pendingRefreshes.clear();
      state.refreshTimer = null;
      const tasks = [];
      if (pending.has("sessions")) tasks.push(loadSessions());
      if (pending.has("session")) tasks.push(pollActiveSession());
      if (pending.has("posts")) tasks.push(loadPostProjects());
      if (pending.has("jobs")) tasks.push(loadJobs());
      if (pending.has("artifacts")) tasks.push(loadArtifacts({ loadSelected: $("artifactModal").classList.contains("open") }));
      if (pending.has("composer")) tasks.push(loadComposerDraft());
      await Promise.allSettled(tasks);
    }

    async function runSafetySync() {
      const tasks = [loadSessions(), loadJobs()];
      if (state.sessionId) {
        tasks.push(pollActiveSession());
        tasks.push(loadPostProjects());
        tasks.push(loadArtifacts({ loadSelected: $("artifactModal").classList.contains("open") }));
        if (!state.composerDirty) tasks.push(loadComposerDraft());
      }
      await Promise.allSettled(tasks);
    }

    function clearTimer(name) {
      if (state[name]) {
        clearTimeout(state[name]);
        state[name] = null;
      }
    }

    function scheduleSafetySync(delay) {
      clearTimer("safetySyncTimer");
      state.safetySyncTimer = setTimeout(() => {
        runSafetySync().finally(() => {
          scheduleSafetySync(document.hidden ? 300000 : 90000);
        });
      }, delay);
    }

    function stopFallbackPolling() {
      clearTimer("fallbackPollTimer");
    }

    function startFallbackPolling() {
      stopFallbackPolling();
      const tick = () => {
        if (state.eventConnected) return;
        runSafetySync().finally(() => {
          state.fallbackPollTimer = setTimeout(tick, document.hidden ? 90000 : 15000);
        });
      };
      state.fallbackPollTimer = setTimeout(tick, document.hidden ? 30000 : 5000);
    }

    function closeEventStream() {
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      state.eventConnected = false;
      state.eventSessionId = "";
    }

    function reconnectEventStream() {
      if (state.eventReconnectTimer) return;
      const delay = state.eventReconnectDelay;
      state.eventReconnectDelay = Math.min(state.eventReconnectDelay * 2, 60000);
      state.eventReconnectTimer = setTimeout(() => {
        state.eventReconnectTimer = null;
        startEventStream(true);
      }, delay);
    }

    function handleRealtimeEvent(type, event) {
      const payload = event && event.payload ? event.payload : {};
      const eventSession = event && event.session_id ? event.session_id : (payload.session_id || "");
      const relevant = !eventSession || !state.sessionId || eventSession === state.sessionId;
      if (type === "session_updated" && relevant) queueRealtimeRefresh("session");
      if (type === "sessions_changed" || type === "session_deleted") queueRealtimeRefresh("sessions");
      if (type === "posts_changed") queueRealtimeRefresh("posts");
      if (type === "jobs_changed") queueRealtimeRefresh("jobs");
      if (type === "artifacts_changed" && relevant) queueRealtimeRefresh("artifacts");
      if (
        type === "composer_updated"
        && relevant
        && String(payload.client_id || "") !== state.composerClientId
      ) queueRealtimeRefresh("composer");
    }

    function startEventStream(force = false) {
      if (!("EventSource" in window)) {
        startFallbackPolling();
        scheduleSafetySync(document.hidden ? 300000 : 90000);
        return;
      }
      clearTimer("eventReconnectTimer");
      const eventSessionId = state.sessionId || "";
      if (!force && state.eventSource && state.eventSessionId === eventSessionId) return;
      if (state.eventSource) state.eventSource.close();
      state.eventSessionId = eventSessionId;
      const suffix = eventSessionId ? `?session_id=${encodeURIComponent(eventSessionId)}` : "";
      const source = new EventSource(`/api/events${suffix}`);
      state.eventSource = source;
      source.onopen = () => {
        state.eventConnected = true;
        state.eventReconnectDelay = 1000;
        stopFallbackPolling();
        scheduleSafetySync(document.hidden ? 300000 : 90000);
      };
      source.onerror = () => {
        if (state.eventSource !== source) return;
        closeEventStream();
        startFallbackPolling();
        reconnectEventStream();
      };
      source.addEventListener("heartbeat", () => {
        state.eventConnected = true;
      });
      for (const type of ["session_updated", "sessions_changed", "session_deleted", "posts_changed", "jobs_changed", "artifacts_changed", "composer_updated"]) {
        source.addEventListener(type, (evt) => {
          try {
            handleRealtimeEvent(type, JSON.parse(evt.data || "{}"));
          } catch (_err) {}
        });
      }
    }

    async function loadMoreMessages() {
      const page = state.messagePage || {};
      if (!state.sessionId || !page.has_more || state.loadingMore) return;
      state.loadingMore = true;
      updateMoreButton();
      try {
        const data = await api(`/api/messages?session_id=${encodeURIComponent(state.sessionId)}&limit=10&before=${encodeURIComponent(page.next_before || "")}`);
        prependMessages(data.messages || []);
        state.messagePage = data.message_page || null;
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        state.loadingMore = false;
        updateMoreButton();
      }
    }

    async function renameSession(id, currentTitle) {
      const title = window.prompt("Rename chat", currentTitle || "");
      if (title === null) return;
      const cleanTitle = title.trim();
      if (!cleanTitle || cleanTitle === currentTitle) return;
      try {
        const data = await api("/api/session/rename", { session_id: id, title: cleanTitle });
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
      }
    }

    async function autoRenameSession(id) {
      setBusy("auto-renaming chat...");
      try {
        const data = await api("/api/session/auto-rename", { session_id: id });
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        setBusy("");
      }
    }

    async function deleteSession(id, title) {
      if (!window.confirm(`Delete chat history "${title}"? It will be moved to local trash.`)) return;
      try {
        const data = await api("/api/session/delete", { session_id: id });
        renderSessions(data.sessions || []);
        if (state.sessionId === id) {
          clearChat();
          if ((data.sessions || []).length > 0) {
            await loadSession(data.sessions[0].id);
          }
        }
      } catch (err) {
        $("publishLog").textContent = err.message;
      }
    }

    async function editSelectedMessage() {
      const msg = state.modalMessage;
      closeMessageModal();
      if (!msg || !state.sessionId) return;
      const nextContent = window.prompt("Edit message", msg.content || "");
      if (nextContent === null) return;
      try {
        const data = await api("/api/message/edit", {
          session_id: state.sessionId,
          message_id: msg.id,
          content: nextContent
        });
        renderSession(data);
        $("publishLog").textContent = "Message edited in local Markdown memory.";
      } catch (err) {
        $("publishLog").textContent = err.message;
      }
    }

    async function resendSelectedMessage() {
      const msg = state.modalMessage;
      closeMessageModal();
      if (!msg || !state.sessionId) return;
      try {
        const data = await api("/api/message/resend", {
          session_id: state.sessionId,
          message_id: msg.id
        });
        renderSession(data);
        const count = Array.isArray(msg.attachments) ? msg.attachments.length : 0;
        $("publishLog").textContent = `Message resent${count ? ` with ${count} attachment(s)` : ""}.`;
      } catch (err) {
        $("publishLog").textContent = err.message;
      }
    }

    async function unsendSelectedMessage() {
      const msg = state.modalMessage;
      closeMessageModal();
      if (!msg || !state.sessionId) return;
      if (!window.confirm("Unsend this message from local chat memory?")) return;
      try {
        const data = await api("/api/message/unsend", {
          session_id: state.sessionId,
          message_id: msg.id
        });
        renderSession(data);
        $("publishLog").textContent = "Message moved to local trash.";
      } catch (err) {
        $("publishLog").textContent = err.message;
      }
    }

    async function sendMessage(event) {
      event.preventDefault();
      const rawMessage = $("messageInput").value.trim();
      const message = composeChatMessage(rawMessage);
      const attachments = state.composerAttachments.map((item) => ({
        name: item.name,
        kind: item.kind,
        mime: item.mime,
        size: item.size,
        data_url: item.data_url,
        preview_url: item.preview_url
      }));
      if (!message && attachments.length === 0) return;
      stopSpeechRecognition();
      setBusy("queued chat message...");
      try {
        if (rawMessage) await saveComposerDraft({ throwOnError: false });
        const data = await api("/api/chat", {
          session_id: state.sessionId,
          message,
          attachments
        });
        renderSession(data);
        $("messageInput").value = "";
        state.composerDirty = true;
        persistLocalComposer("", { synced: false });
        clearComposerAttachments();
        clearReplyTarget();
        await saveComposerDraft({ throwOnError: false });
        if (data.action_result && data.action_result.status === "executed") {
          if (data.action_result.action === "select_post") {
            const resolved = data.action_result.resolved_post || {};
            $("publishLog").innerHTML = `Selected WordPress post ${escapeHtml(resolved.post_id || "")}: ${escapeHtml(resolved.title || "")}<br><span class="path">${escapeHtml(resolved.local_post_dir || "")}</span>`;
          } else {
            $("publishLog").textContent = `Controlled action executed: ${data.action_result.action}`;
          }
        }
      } catch (err) {
        $("publishLog").textContent = err.message;
        state.composerDirty = true;
        persistLocalComposer($("messageInput").value, { synced: false });
        setComposerStatus("Message kept; retry when connected", "error");
      } finally {
        setBusy("");
      }
    }

    async function draftPost() {
      if (!state.sessionId) {
        $("publishLog").textContent = "Send at least one message first.";
        return;
      }
      setBusy("running task tool...");
      try {
        const data = await api("/api/post/draft", {
          session_id: state.sessionId,
          post_project_id: state.activePostId || "",
          target_mode: $("postTargetMode").value,
          status: $("publishStatus").value,
          instruction: $("extraInstruction").value
        });
        $("draftPreview").value = data.markdown || "";
        const project = data.post_project || {};
        state.activePostId = project.id || state.activePostId;
        $("publishLog").innerHTML = `Draft saved for ${escapeHtml(project.title || "selected post")}: <span class="path">${escapeHtml(data.draft_path)}</span>`;
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        setBusy("");
      }
    }

    async function publishPost(force) {
      if (!state.sessionId) {
        $("publishLog").textContent = "Send at least one message first.";
        return;
      }
      setBusy(force ? "redrafting and publishing..." : "publishing...");
      try {
        const data = await api("/api/post/publish", {
          session_id: state.sessionId,
          post_project_id: state.activePostId || "",
          target_mode: $("postTargetMode").value,
          status: $("publishStatus").value,
          force_redraft: Boolean(force),
          instruction: $("extraInstruction").value,
          update_existing: true
        });
        const link = data.published.link || "";
        const action = data.published.action === "updated" ? "updated" : "created";
        $("publishLog").innerHTML = `WordPress post ${data.published.post_id} ${action} as ${data.published.status}.<br><span class="path">${escapeHtml(link)}</span>`;
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        setBusy("");
      }
    }

    $("composer").addEventListener("submit", sendMessage);
    $("attachAnyButton").addEventListener("click", () => {
      if (state.busy) return;
      $("attachmentInput").click();
    });
    $("micButton").addEventListener("click", toggleSpeechRecognition);
    $("quotePreviousButton").addEventListener("click", quotePreviousMessage);
    $("composerReplyClear").addEventListener("click", clearReplyTarget);
    $("attachmentInput").addEventListener("change", onAttachmentsSelected);
    $("messageInput").addEventListener("input", composerInputChanged);
    $("messageInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!state.busy) {
          $("sendButton").click();
        }
      }
    });
    $("draftButton").addEventListener("click", draftPost);
    $("postProjectSelect").addEventListener("change", (event) => {
      selectPostProject(event.target.value).catch((err) => { $("publishLog").textContent = err.message; });
    });
    $("newPostProjectButton").addEventListener("click", newPostProject);
    $("refreshPostProjects").addEventListener("click", () => loadPostProjects().catch((err) => { $("publishLog").textContent = err.message; }));
    $("syncCategories").addEventListener("click", () => loadCategories(true).catch((err) => { $("categoryLog").textContent = err.message; }));
    $("searchCategories").addEventListener("click", () => loadCategories(false).catch((err) => { $("categoryLog").textContent = err.message; }));
    $("categorySearch").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadCategories(false).catch((err) => { $("categoryLog").textContent = err.message; });
      }
    });
    $("moreMessages").addEventListener("click", loadMoreMessages);
    $("messages").addEventListener("scroll", () => {
      const button = $("moreMessages");
      const scroller = $("messages");
      const buttonRect = button.getBoundingClientRect();
      const scrollerRect = scroller.getBoundingClientRect();
      if (
        button.classList.contains("visible") &&
        buttonRect.top <= scrollerRect.top + 24 &&
        buttonRect.bottom >= scrollerRect.top
      ) {
        loadMoreMessages();
      }
    }, { passive: true });
    $("publishButton").addEventListener("click", () => publishPost(false));
    $("redraftButton").addEventListener("click", () => publishPost(true));
    $("refreshSessions").addEventListener("click", loadSessions);
    $("refreshJobs").addEventListener("click", loadJobs);
    $("mobileMenuToggle").addEventListener("click", () => {
      const opened = shell.classList.toggle("nav-open");
      $("mobileMenuToggle").setAttribute("aria-expanded", String(opened));
    });
    $("publishToggle").addEventListener("click", () => {
      const opened = shell.classList.toggle("publish-open");
      $("publishToggle").setAttribute("aria-expanded", String(opened));
    });
    $("publishClose").addEventListener("click", () => {
      shell.classList.remove("publish-open");
      $("publishToggle").setAttribute("aria-expanded", "false");
    });
    $("modalRename").addEventListener("click", () => {
      const session = state.modalSession;
      closeSessionModal();
      if (session) renameSession(session.id, session.title);
    });
    $("modalAutoRename").addEventListener("click", () => {
      const session = state.modalSession;
      closeSessionModal();
      if (session) autoRenameSession(session.id);
    });
    $("modalDelete").addEventListener("click", () => {
      const session = state.modalSession;
      closeSessionModal();
      if (session) deleteSession(session.id, session.title);
    });
    $("modalCancel").addEventListener("click", closeSessionModal);
    $("messageEdit").addEventListener("click", editSelectedMessage);
    $("messageResend").addEventListener("click", resendSelectedMessage);
    $("messageUnsend").addEventListener("click", unsendSelectedMessage);
    $("messageCancel").addEventListener("click", closeMessageModal);
    $("settingsButton").addEventListener("click", () => {
      loadSettings().catch((err) => { $("settingsLog").textContent = err.message; });
      openSettingsModal();
    });
    $("settingsSave").addEventListener("click", () => {
      saveSettings().catch((err) => { $("settingsLog").textContent = err.message; });
    });
    $("settingsCancel").addEventListener("click", closeSettingsModal);
    $("artifactButton").addEventListener("click", () => {
      openArtifactModal().catch((err) => { $("artifactStatus").textContent = err.message; });
    });
    $("artifactModalClose").addEventListener("click", closeArtifactModal);
    $("artifactMarkSeen").addEventListener("click", markAllArtifactsRead);
    $("artifactTabs").addEventListener("click", (event) => {
      const tab = event.target.closest("[data-artifact-tab]");
      if (!tab) return;
      state.artifactTab = tab.dataset.artifactTab || "canvas";
      renderArtifactList();
      const preferred = preferredArtifactForCurrentTab();
      if (preferred) {
        selectArtifact(preferred.id, { persist: false }).catch((err) => { $("artifactStatus").textContent = err.message; });
      } else {
        clearArtifactViewer();
      }
    });
    $("artifactDownloadCurrent").addEventListener("click", () => {
      if (!state.selectedArtifactId) return;
      downloadArtifact(state.selectedArtifactId).catch((err) => { $("artifactStatus").textContent = err.message; });
    });
    $("mediaModalClose").addEventListener("click", closeMediaModal);
    $("sessionActionModal").addEventListener("click", (event) => {
      if (event.target === $("sessionActionModal")) closeSessionModal();
    });
    $("messageActionModal").addEventListener("click", (event) => {
      if (event.target === $("messageActionModal")) closeMessageModal();
    });
    $("settingsModal").addEventListener("click", (event) => {
      if (event.target === $("settingsModal")) closeSettingsModal();
    });
    $("mediaModal").addEventListener("click", (event) => {
      if (event.target === $("mediaModal")) closeMediaModal();
    });
    $("artifactModal").addEventListener("click", (event) => {
      if (event.target === $("artifactModal")) closeArtifactModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeSessionModal();
        closeMessageModal();
        closeSettingsModal();
        closeMediaModal();
        closeArtifactModal();
      }
    });
    $("newSession").addEventListener("click", () => {
      shell.classList.remove("nav-open");
      $("mobileMenuToggle").setAttribute("aria-expanded", "false");
      clearChat();
      loadSessions();
      loadPostProjects().catch(() => {});
    });
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/service-worker.js").catch(() => {});
      });
    }
    initSpeechRecognition();
    loadComposerDraft().catch(() => {});
    loadSettings().catch((err) => { $("settingsLog").textContent = err.message; });
    loadSessions({ autoload: true }).catch((err) => { $("publishLog").textContent = err.message; });
    loadPostProjects().catch(() => {});
    loadCategories(false).catch(() => {});
    loadJobs().catch(() => {});
    startEventStream();
    scheduleSafetySync(90000);
    window.addEventListener("online", () => {
      startEventStream(true);
      if (state.composerDirty) scheduleComposerSave(80);
      runSafetySync().catch(() => {});
    });
    window.addEventListener("pagehide", () => {
      const text = $("messageInput").value;
      persistLocalComposer(text, { synced: !state.composerDirty });
      if (!state.sessionId || !state.composerDirty || !navigator.sendBeacon) return;
      const body = new Blob([JSON.stringify({
        session_id: state.sessionId,
        text,
        client_id: state.composerClientId,
        base_version: state.composerVersion
      })], { type: "application/json" });
      navigator.sendBeacon("/api/composer", body);
    });
    document.addEventListener("visibilitychange", () => {
      scheduleSafetySync(document.hidden ? 300000 : 1000);
      if (document.hidden) {
        stopSpeechRecognition();
        if (state.composerDirty) saveComposerDraft().catch(() => {});
      }
      if (!document.hidden) {
        startEventStream(true);
        runSafetySync().catch(() => {});
      }
    });
  </script>
</body>
</html>
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f766e">
  <title>LazyBlog Studio Login</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,650&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap");
    :root { --ink: #1d2520; --muted: #667069; --teal: #0f766e; --clay: #d96b43; --gold: #e3a92f; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      color: var(--ink);
      font-family: "Newsreader", Georgia, serif;
      background: #edf8f4;
      padding: 24px;
    }
    .card {
      width: min(460px, 100%);
      border: 1px solid rgba(39, 55, 46, 0.16);
      border-radius: 32px;
      padding: 30px;
      background: rgba(255, 250, 240, 0.86);
      box-shadow: 0 24px 70px rgba(28, 45, 38, 0.16);
      backdrop-filter: blur(18px);
    }
    h1 { font-family: "Fraunces", Georgia, serif; font-size: 42px; line-height: 1; letter-spacing: 0; margin: 0; }
    p { color: var(--muted); line-height: 1.5; }
    label { display: block; font-size: 13px; color: var(--muted); margin: 16px 0 6px 4px; }
    input { width: 100%; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 18px; background: rgba(255, 255, 255, 0.7); padding: 12px 14px; font: inherit; outline: none; }
    input:focus { border-color: rgba(15, 118, 110, 0.55); box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
    button { width: 100%; margin-top: 20px; border: 0; border-radius: 999px; padding: 13px 18px; background: var(--gold); color: #231b12; font: inherit; font-weight: 700; cursor: pointer; }
    .error { margin-top: 14px; color: #8a2b12; min-height: 1.4em; }
    .hint { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--teal); overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <form class="card" id="loginForm">
    <h1>LazyBlog Studio</h1>
    <p>Public tunnel access is locked. Log in as <span class="hint">__USERNAME__</span> with the Studio token.</p>
    <label for="username">Account</label>
    <input id="username" name="username" value="__USERNAME__" autocomplete="username" required>
    <label for="token">Login token</label>
    <input id="token" name="token" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">Enter Studio</button>
    <div class="error" id="error"></div>
  </form>
  <script>
    document.getElementById("loginForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = document.getElementById("error");
      error.textContent = "";
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value,
          token: document.getElementById("token").value
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        error.textContent = data.error || "Login failed.";
        return;
      }
      window.location.href = "/";
    });
  </script>
</body>
</html>
"""


PWA_MANIFEST = {
    "name": "LazyBlog Studio",
    "short_name": "LazyBlog",
    "description": "Local chat-to-Markdown drafting and WordPress publishing for LazyBlog.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "display_override": ["window-controls-overlay", "standalone", "browser"],
    "background_color": "#edf8f4",
    "theme_color": "#16a394",
    "orientation": "any",
    "categories": ["productivity", "writing", "utilities"],
    "icons": [
        {
            "src": "/icons/lazyblog-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/icons/lazyblog-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/icons/lazyblog.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ],
    "shortcuts": [
        {
            "name": "New Chat",
            "short_name": "Chat",
            "description": "Open LazyBlog Studio to capture a new note.",
            "url": "/",
            "icons": [{"src": "/icons/lazyblog-192.png", "sizes": "192x192", "type": "image/png"}],
        }
    ],
}


SERVICE_WORKER = r"""const CACHE_NAME = "lazyblog-studio-v6";
const APP_SHELL = [
  "/manifest.webmanifest",
  "/icons/lazyblog.svg",
  "/icons/lazyblog-192.png",
  "/icons/lazyblog-512.png",
  "/assets/vendor/marked.js",
  "/assets/vendor/dompurify.js",
  "/assets/vendor/katex.js",
  "/assets/vendor/katex-auto-render.js",
  "/assets/vendor/katex.css"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (url.pathname === "/" || url.pathname === "/login") return;
  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      });
    })
  );
});
"""


APP_ICON_SVG = r"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="LazyBlog Studio">
  <defs>
    <linearGradient id="bg" x1="64" y1="48" x2="448" y2="464" gradientUnits="userSpaceOnUse">
      <stop stop-color="#fff4d9"/>
      <stop offset="0.52" stop-color="#d9ede8"/>
      <stop offset="1" stop-color="#0f766e"/>
    </linearGradient>
    <linearGradient id="mark" x1="130" y1="150" x2="390" y2="390" gradientUnits="userSpaceOnUse">
      <stop stop-color="#d96b43"/>
      <stop offset="1" stop-color="#e3a92f"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="118" fill="url(#bg)"/>
  <path d="M145 140h161c44 0 76 28 76 66 0 26-13 46-36 57 29 10 47 34 47 66 0 42-34 73-82 73H145V140Z" fill="#1d2520"/>
  <path d="M204 197v55h82c21 0 35-11 35-28s-14-27-35-27h-82Zm0 105v43h99c18 0 30-9 30-22s-12-21-30-21h-99Z" fill="#fffaf0"/>
  <path d="M121 382c69-12 111-41 134-88 8 50 41 82 102 98-72 29-150 26-236-10Z" fill="url(#mark)" opacity="0.96"/>
</svg>
"""


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_icon_png(size: int) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            nx = x / max(size - 1, 1)
            ny = y / max(size - 1, 1)
            r = int(255 * (1 - nx) + 15 * nx)
            g = int(250 * (1 - ny) + 118 * ny)
            b = int(240 * (1 - nx) + 110 * nx)
            radius = size * 0.18
            border = x < radius and y < radius and (x - radius) ** 2 + (y - radius) ** 2 > radius**2
            border = border or x > size - radius and y < radius and (x - size + radius) ** 2 + (y - radius) ** 2 > radius**2
            border = border or x < radius and y > size - radius and (x - radius) ** 2 + (y - size + radius) ** 2 > radius**2
            border = border or x > size - radius and y > size - radius and (x - size + radius) ** 2 + (y - size + radius) ** 2 > radius**2
            if border:
                row.extend((0, 0, 0, 0))
                continue
            if size * 0.27 < x < size * 0.73 and size * 0.28 < y < size * 0.73:
                r, g, b = 29, 37, 32
            if size * 0.38 < x < size * 0.63 and size * 0.38 < y < size * 0.47:
                r, g, b = 255, 250, 240
            if size * 0.38 < x < size * 0.67 and size * 0.55 < y < size * 0.64:
                r, g, b = 255, 250, 240
            if y > size * 0.72 and abs((x / size) - 0.5) < 0.34 - ((y / size) - 0.72) * 0.8:
                r, g, b = 217, 107, 67
            row.extend((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 9))
        + png_chunk(b"IEND", b"")
    )


def make_handler(app: LazyBlogStudio) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LazyBlogStudio/0.1"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

        def send_json(
            self,
            payload: dict[str, Any],
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps({"ok": status.value < 400, **payload}, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self, body_text: str | None = None, status: HTTPStatus = HTTPStatus.OK) -> None:
            reply_profile = app.codex_profile("reply")
            html_text = body_text or INDEX_HTML.replace("__MODEL_LABEL__", f"{reply_profile['model']} / {reply_profile['reasoning']}")
            body = html_text.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_login(self) -> None:
            self.send_html(LOGIN_HTML.replace("__USERNAME__", html.escape(studio_username(), quote=True)), HTTPStatus.UNAUTHORIZED)

        def send_text(self, body_text: str, content_type: str) -> None:
            body = body_text.encode("utf-8")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache" if content_type.startswith("application/javascript") else "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)

        def handle_error(self, exc: Exception) -> None:
            detail = traceback.format_exc() if app.args.debug else str(exc)
            self.send_json({"error": detail}, HTTPStatus.BAD_REQUEST)

        def write_sse(self, event: dict[str, Any]) -> None:
            body = (
                f"id: {int(event.get('id') or 0)}\n"
                f"event: {str(event.get('type') or 'changed')}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()

        def send_event_stream(self, parsed: urllib.parse.ParseResult) -> None:
            params = urllib.parse.parse_qs(parsed.query)
            session_id = safe_session_id(params.get("session_id", [""])[0]) if params.get("session_id") else ""
            last_id_header = self.headers.get("Last-Event-ID", "").strip()
            raw_last_id = params.get("last_id", [last_id_header or "0"])[0]
            try:
                last_id = max(0, int(raw_last_id or "0"))
            except ValueError:
                last_id = 0

            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
                while True:
                    events = app.wait_for_events(last_id, session_id=session_id, timeout=25.0)
                    if not events:
                        heartbeat = {"time": now_iso(), "session_id": session_id}
                        self.wfile.write(f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        continue
                    for event in events:
                        event_id = int(event.get("id") or 0)
                        if event_id <= last_id:
                            continue
                        self.write_sse(event)
                        last_id = event_id
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return

        def bearer_token(self) -> str:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return self.headers.get("X-LazyBlog-Token", "").strip()

        def has_studio_cookie(self) -> bool:
            return verify_studio_cookie(self.headers.get("Cookie", ""))

        def require_studio_auth(self, path: str) -> bool:
            if not studio_auth_enabled():
                return True
            if self.has_studio_cookie():
                return True
            if path.startswith("/api/"):
                self.send_json({"error": "LazyBlog Studio login required", "login_url": "/login"}, HTTPStatus.UNAUTHORIZED)
                return False
            self.send_login()
            return False

        def require_api_auth(self, path: str) -> bool:
            if not (path.startswith("/api/codex/") or path.startswith("/api/translate/")):
                return True
            if path.startswith("/api/codex/") and self.has_studio_cookie():
                return True
            configured = os.environ.get("LAZYBLOG_API_TOKEN", "").strip()
            if configured:
                provided = self.bearer_token()
                if hmac.compare_digest(provided, configured):
                    return True
                self.send_json({"error": "invalid or missing LazyBlog API token"}, HTTPStatus.UNAUTHORIZED)
                return False
            self.send_json({"error": "set LAZYBLOG_API_TOKEN before exposing Codex APIs beyond loopback"}, HTTPStatus.FORBIDDEN)
            return False

        def authorize_request(self, path: str) -> bool:
            mode = request_auth_mode(path)
            if mode == "public":
                return True
            if mode == "api":
                return self.require_api_auth(path)
            return self.require_studio_auth(path)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            try:
                if not self.authorize_request(parsed.path):
                    return
                if parsed.path == "/login":
                    self.send_login()
                    return
                if parsed.path == "/":
                    self.send_html()
                    return
                if parsed.path == "/manifest.webmanifest":
                    self.send_text(json.dumps(PWA_MANIFEST, ensure_ascii=False, indent=2), "application/manifest+json; charset=utf-8")
                    return
                if parsed.path == "/service-worker.js":
                    self.send_text(SERVICE_WORKER, "application/javascript; charset=utf-8")
                    return
                if parsed.path == "/icons/lazyblog.svg":
                    self.send_text(APP_ICON_SVG, "image/svg+xml; charset=utf-8")
                    return
                if parsed.path == "/icons/lazyblog-192.png":
                    self.send_bytes(make_icon_png(192), "image/png")
                    return
                if parsed.path == "/icons/lazyblog-512.png":
                    self.send_bytes(make_icon_png(512), "image/png")
                    return
                if parsed.path in VENDOR_ASSETS:
                    filename, content_type = VENDOR_ASSETS[parsed.path]
                    self.send_bytes((VENDOR_ROOT / filename).read_bytes(), content_type)
                    return
                if parsed.path == "/assets/vendor/katex-font":
                    params = urllib.parse.parse_qs(parsed.query)
                    font_name = params.get("name", [""])[0]
                    if not re.fullmatch(r"KaTeX_[A-Za-z0-9-]+\.woff2", font_name):
                        raise WebAppError("invalid KaTeX font name")
                    font_path = VENDOR_ROOT / "katex-fonts" / font_name
                    if not font_path.is_file():
                        raise WebAppError("unknown KaTeX font")
                    self.send_bytes(font_path.read_bytes(), "font/woff2")
                    return
                if parsed.path == "/api/health":
                    self.send_json({"status": "ok"})
                    return
                if parsed.path == "/api/events":
                    self.send_event_stream(parsed)
                    return
                if parsed.path == "/api/settings":
                    self.send_json({"settings": app.load_model_settings()})
                    return
                if parsed.path == "/api/sessions":
                    self.send_json({"sessions": app.list_sessions()})
                    return
                if parsed.path == "/api/session":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("id", [""])[0]
                    raw_limit = params.get("limit", [str(DEFAULT_MESSAGE_BATCH_SIZE)])[0]
                    before = params.get("before", [""])[0]
                    self.send_json(app.session_payload(session_id, limit=int(raw_limit), before=before))
                    return
                if parsed.path == "/api/composer":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [""])[0]
                    self.send_json({"composer": app.composer_payload(session_id)})
                    return
                if parsed.path == "/api/messages":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [""])[0]
                    raw_limit = params.get("limit", [str(DEFAULT_MESSAGE_BATCH_SIZE)])[0]
                    before = params.get("before", [""])[0]
                    self.send_json(app.message_page(session_id, limit=int(raw_limit), before=before))
                    return
                if parsed.path == "/api/categories":
                    params = urllib.parse.parse_qs(parsed.query)
                    query = params.get("search", [""])[0]
                    raw_limit = params.get("limit", ["50"])[0]
                    sync = params.get("sync", ["0"])[0].lower() in {"1", "true", "yes", "on"}
                    self.send_json(app.search_categories(query=query, limit=int(raw_limit), sync=sync))
                    return
                if parsed.path == "/api/posts":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [None])[0]
                    raw_limit = params.get("limit", ["100"])[0]
                    active_id = app.active_post_project_id(session_id) if session_id else ""
                    active_post = app.post_project_payload(active_id) if active_id else None
                    self.send_json(
                        {
                            "post_projects": app.list_post_projects(session_id=session_id, limit=int(raw_limit)),
                            "active_post_project_id": active_id,
                            "active_post_project": active_post,
                        }
                    )
                    return
                if parsed.path == "/api/post":
                    params = urllib.parse.parse_qs(parsed.query)
                    post_project_id = params.get("id", [""])[0]
                    self.send_json(app.post_project_payload(post_project_id))
                    return
                if parsed.path == "/api/artifacts":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [""])[0]
                    self.send_json(app.artifact_bundle(session_id))
                    return
                if parsed.path == "/api/artifact":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [""])[0]
                    artifact_id = params.get("artifact_id", [""])[0]
                    self.send_json(app.artifact_content(session_id, artifact_id))
                    return
                if parsed.path == "/api/codex/jobs":
                    params = urllib.parse.parse_qs(parsed.query)
                    raw_limit = params.get("limit", ["20"])[0]
                    session_id = params.get("session_id", [None])[0]
                    limit = max(1, min(int(raw_limit), 100))
                    self.send_json({"jobs": app.list_jobs(limit=limit, session_id=session_id)})
                    return
                if parsed.path == "/api/codex/job":
                    params = urllib.parse.parse_qs(parsed.query)
                    job_id = params.get("id", [""])[0]
                    self.send_json(app.job_status(job_id, include_logs=True, include_output=True))
                    return
                if parsed.path == "/api/codex/result":
                    params = urllib.parse.parse_qs(parsed.query)
                    job_id = params.get("id", [""])[0]
                    self.send_json(app.job_status(job_id, include_logs=False, include_output=True))
                    return
                if parsed.path == "/api/translate/job":
                    params = urllib.parse.parse_qs(parsed.query)
                    job_id = params.get("id", [""])[0]
                    self.send_json(app.job_status(job_id, include_logs=False, include_output=True))
                    return
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001
                self.handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            try:
                payload = self.read_body()
                if parsed.path == "/api/login":
                    username = str(payload.get("username", "")).strip()
                    token = str(payload.get("token", "")).strip()
                    if studio_auth_enabled() and username == studio_username() and hmac.compare_digest(token, studio_login_token()):
                        cookie = f"{STUDIO_AUTH_COOKIE}={make_studio_cookie(username)}; {studio_cookie_attributes()}"
                        self.send_json({"user": username}, headers={"Set-Cookie": cookie})
                        return
                    self.send_json({"error": "invalid LazyBlog Studio login"}, HTTPStatus.UNAUTHORIZED)
                    return
                if parsed.path == "/api/logout":
                    self.send_json(
                        {"status": "logged out"},
                        headers={
                            "Set-Cookie": (
                                f"{STUDIO_AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                                + ("; Secure" if studio_secure_cookie_enabled() else "")
                            )
                        },
                    )
                    return
                if not self.authorize_request(parsed.path):
                    return
                if parsed.path == "/api/settings":
                    self.send_json({"settings": app.save_model_settings(payload)})
                    return
                if parsed.path == "/api/composer":
                    self.send_json(
                        app.save_composer(
                            str(payload.get("text", "")),
                            payload.get("session_id") or None,
                            client_id=str(payload.get("client_id", "")),
                            base_version=int(payload.get("base_version") or 0),
                        )
                    )
                    return
                if parsed.path == "/api/session/rename":
                    self.send_json(app.rename_session(str(payload.get("session_id", "")), str(payload.get("title", ""))))
                    return
                if parsed.path == "/api/session/auto-rename":
                    self.send_json(app.auto_rename_session(str(payload.get("session_id", ""))))
                    return
                if parsed.path == "/api/session/delete":
                    self.send_json(app.delete_session(str(payload.get("session_id", ""))))
                    return
                if parsed.path == "/api/message/edit":
                    self.send_json(
                        app.edit_message(
                            str(payload.get("session_id", "")),
                            str(payload.get("message_id", "")),
                            str(payload.get("content", "")),
                        )
                    )
                    return
                if parsed.path == "/api/message/resend":
                    self.send_json(
                        app.resend_message(
                            str(payload.get("session_id", "")),
                            str(payload.get("message_id", "")),
                            content=str(payload.get("content")) if "content" in payload else None,
                        )
                    )
                    return
                if parsed.path == "/api/message/unsend":
                    self.send_json(app.unsend_message(str(payload.get("session_id", "")), str(payload.get("message_id", ""))))
                    return
                if parsed.path == "/api/chat":
                    self.send_json(
                        app.enqueue_chat_message(
                            str(payload.get("message", "")),
                            payload.get("session_id") or None,
                            attachments=payload.get("attachments"),
                        )
                    )
                    return
                if parsed.path == "/api/categories/sync":
                    self.send_json(app.sync_category_mirror())
                    return
                if parsed.path == "/api/category":
                    self.send_json(
                        app.create_category(
                            str(payload.get("name", "")),
                            parent=payload.get("parent"),
                            slug=str(payload.get("slug", "")),
                            description=str(payload.get("description", "")),
                        )
                    )
                    return
                if parsed.path == "/api/category/update":
                    self.send_json(app.update_category(payload.get("category") or payload.get("id") or payload.get("slug") or payload.get("name"), payload))
                    return
                if parsed.path == "/api/category/delete":
                    self.send_json(app.delete_category(payload.get("category") or payload.get("id") or payload.get("slug") or payload.get("name"), force=bool(payload.get("force", True))))
                    return
                if parsed.path == "/api/posts":
                    self.send_json(
                        app.create_post_project(
                            session_id=payload.get("session_id") or None,
                            title=str(payload.get("title", "")),
                            instruction=str(payload.get("instruction", "")),
                            categories=list_from_value(payload.get("categories")),
                            source_language=str(payload.get("source_language", "en")),
                        )
                    )
                    return
                if parsed.path == "/api/post/select":
                    self.send_json(app.set_active_post_project(str(payload.get("session_id", "")), str(payload.get("post_project_id", ""))))
                    return
                if parsed.path == "/api/post/select-source":
                    self.send_json(
                        app.select_or_import_wordpress_post(
                            str(payload.get("session_id", "")),
                            str(payload.get("query") or payload.get("url") or payload.get("post_id") or ""),
                            sync_mode=str(payload.get("sync_mode") or "pull"),
                        )
                    )
                    return
                if parsed.path == "/api/post/draft":
                    self.send_json(
                        app.draft_post_project(
                            str(payload.get("post_project_id") or "") or None,
                            str(payload.get("session_id", "")),
                            instruction=str(payload.get("instruction", "")),
                            status=str(payload.get("status", "draft")),
                            target_mode=str(payload.get("target_mode", "auto")),
                            post_reference=str(payload.get("post_reference", "")),
                        )
                    )
                    return
                if parsed.path == "/api/post/publish":
                    self.send_json(
                        app.publish_post_project(
                            str(payload.get("post_project_id") or "") or None,
                            str(payload.get("session_id", "")),
                            status=str(payload.get("status", "draft")),
                            force_redraft=bool(payload.get("force_redraft", False)),
                            instruction=str(payload.get("instruction", "")),
                            update_existing=bool(payload.get("update_existing", True)),
                            target_mode=str(payload.get("target_mode", "auto")),
                            post_reference=str(payload.get("post_reference", "")),
                        )
                    )
                    return
                if parsed.path == "/api/post/link":
                    self.send_json(
                        app.link_post_project(
                            str(payload.get("post_project_id", "")),
                            payload.get("post_id"),
                            status=str(payload.get("status", "")),
                            link=str(payload.get("link", "")),
                        )
                    )
                    return
                if parsed.path == "/api/artifacts":
                    self.send_json(app.register_artifact_payload(payload))
                    return
                if parsed.path == "/api/artifact/select":
                    self.send_json(app.select_artifact(str(payload.get("session_id", "")), str(payload.get("artifact_id", ""))))
                    return
                if parsed.path == "/api/draft":
                    self.send_json(
                        app.create_draft(
                            str(payload.get("session_id", "")),
                            instruction=str(payload.get("instruction", "")),
                            status=str(payload.get("status", "draft")),
                        )
                    )
                    return
                if parsed.path == "/api/publish":
                    self.send_json(
                        app.publish(
                            str(payload.get("session_id", "")),
                            status=str(payload.get("status", "draft")),
                            force_redraft=bool(payload.get("force_redraft", False)),
                            instruction=str(payload.get("instruction", "")),
                        )
                    )
                    return
                if parsed.path == "/api/codex/jobs":
                    self.send_json(app.submit_codex_job(payload))
                    return
                if parsed.path == "/api/codex/respond":
                    self.send_json(app.respond_with_codex(payload))
                    return
                if parsed.path == "/api/translate/jobs":
                    self.send_json(app.start_translation_job(payload))
                    return
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001
                self.handle_error(exc)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local LazyBlog chat-to-post webapp.")
    parser.add_argument("--host", default=os.environ.get("LAZYBLOG_WEBAPP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LAZYBLOG_WEBAPP_PORT", "8765")))
    parser.add_argument("--model", default=os.environ.get("LAZYBLOG_WEBAPP_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--reasoning",
        default=os.environ.get("LAZYBLOG_WEBAPP_REASONING", DEFAULT_REASONING),
        choices=["low", "medium", "high", "xhigh"],
    )
    parser.add_argument("--codex-timeout", type=int, default=int(os.environ.get("LAZYBLOG_WEBAPP_CODEX_TIMEOUT", "1800")))
    parser.add_argument("--git-codex-timeout", type=int, default=int(os.environ.get("LAZYBLOG_GIT_CODEX_TIMEOUT", "600")))
    parser.add_argument("--branch", default=os.environ.get("LAZYBLOG_PUSH_BRANCH", "main"))
    parser.add_argument("--commit-push", dest="commit_push", action="store_true", default=bool_env("LAZYBLOG_WEBAPP_COMMIT_PUSH", True))
    parser.add_argument("--no-commit-push", dest="commit_push", action="store_false")
    parser.add_argument("--mock-codex", action="store_true", help="Use deterministic mock outputs for UI testing.")
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> int:
    load_env_file(ROOT_DIR / ".env")
    args = build_parser().parse_args()
    app = LazyBlogStudio(args)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"LazyBlog Studio listening on http://{args.host}:{args.port}", flush=True)
    print(f"model={args.model} reasoning={args.reasoning} commit_push={args.commit_push}", flush=True)
    print(
        f"studio_auth={'on' if studio_auth_enabled() else 'off'} "
        f"mode={'cookie-only' if studio_cookie_only_auth_enabled() else 'api-compatible'} "
        f"user={studio_username()}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping LazyBlog Studio", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LazyBlogError, WebAppError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
