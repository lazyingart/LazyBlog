#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lazyblog_sync import LazyBlogError, WPClient, make_client, markdown_to_html, require_auth
from lazyblog_translate import first_heading, load_env_file, split_front_matter


ROOT_DIR = Path(__file__).resolve().parents[1]
CHAT_ROOT = ROOT_DIR / "content" / "chat"
DRAFT_ROOT = ROOT_DIR / "content" / "drafts"
JOB_ROOT = ROOT_DIR / "content" / "codex-jobs"
TRANSLATION_JOB_ROOT = ROOT_DIR / "content" / "translation-jobs"
CHAT_REPLY_PROMPT = ROOT_DIR / "prompts" / "web-chat-reply.txt"
CHAT_TASK_PROMPT = ROOT_DIR / "prompts" / "web-draft-task.txt"
CODEX_RESPONSE_PROMPT = ROOT_DIR / "prompts" / "web-codex-response.txt"
CHAT_REPLY_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_chat_reply.schema.json"
CHAT_TASK_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_chat_task.schema.json"
CODEX_RESPONSE_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_codex_response.schema.json"
CODEX_TRANSLATION_SCHEMA = ROOT_DIR / "schemas" / "lazyblog_web_translation.schema.json"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_REASONING = "low"
DEFAULT_MESSAGE_BATCH_SIZE = 10
STUDIO_AUTH_COOKIE = "lazyblog_studio_auth"
STUDIO_AUTH_TTL_SECONDS = 60 * 60 * 24 * 30


class WebAppError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def safe_session_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid session id")
    return value


def safe_job_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise WebAppError("invalid job id")
    return value


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


class LazyBlogStudio:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        CHAT_ROOT.mkdir(parents=True, exist_ok=True)
        DRAFT_ROOT.mkdir(parents=True, exist_ok=True)
        JOB_ROOT.mkdir(parents=True, exist_ok=True)
        TRANSLATION_JOB_ROOT.mkdir(parents=True, exist_ok=True)
        self.job_lock = threading.Lock()

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
            meta["title"] = content.strip().splitlines()[0][:80] or "Untitled chat"
        self.save_session(session_id, meta)
        return path

    def message_paths(self, session_id: str) -> list[Path]:
        return sorted((self.session_dir(session_id) / "messages").glob("*.md"))

    def read_message(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(text)
        return {
            "id": path.stem,
            "role": front_matter.get("role") or path.stem.rsplit("-", 1)[-1],
            "created_at": front_matter.get("created_at", ""),
            "content": body.strip(),
            "path": str(path.relative_to(ROOT_DIR)),
        }

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
            lines.append(f"{row['role'].upper()}:\n{row['content']}")
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

    def category_snapshot(self, limit: int = 40) -> list[str]:
        counts: dict[str, int] = {}
        for manifest_path in sorted((ROOT_DIR / "content" / "posts").glob("*/lazyblog.json")):
            manifest = read_json(manifest_path, {})
            for category in list_from_value(manifest.get("categories")):
                counts[category] = counts.get(category, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[:limit]]

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

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self.job_lock:
            job = read_json(self.job_path(job_id))
            job.update(updates)
            job["updated_at"] = now_iso()
            write_json(self.job_path(job_id), job)
            return job

    def schema_path_for_name(self, schema_name: str) -> Path:
        schemas = {
            "response": CODEX_RESPONSE_SCHEMA,
            "reply": CHAT_REPLY_SCHEMA,
            "task": CHAT_TASK_SCHEMA,
            "translation": CODEX_TRANSLATION_SCHEMA,
        }
        if schema_name not in schemas:
            raise WebAppError("schema must be one of: response, reply, task, translation")
        return schemas[schema_name]

    def prompt_path_for_tool(self, tool_name: str) -> Path:
        prompts = {
            "response": CODEX_RESPONSE_PROMPT,
            "assistant": CODEX_RESPONSE_PROMPT,
            "reply": CHAT_REPLY_PROMPT,
            "task": CHAT_TASK_PROMPT,
        }
        if tool_name not in prompts:
            raise WebAppError("tool must be one of: response, assistant, reply, task")
        return prompts[tool_name]

    def default_schema_for_tool(self, tool_name: str) -> str:
        if tool_name == "reply":
            return "reply"
        if tool_name == "task":
            return "task"
        return "response"

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
            "model": str(request_payload.get("model") or self.args.model),
            "reasoning": str(request_payload.get("reasoning") or self.args.reasoning),
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

            cmd = [
                "codex",
                "exec",
                "--ephemeral",
                "--model",
                str(job["model"]),
                "-c",
                f'model_reasoning_effort="{job["reasoning"]}"',
                "--dangerously-bypass-approvals-and-sandbox",
                "--cd",
                str(ROOT_DIR),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(job_dir / "output.json"),
                "-",
            ]
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                text=True,
                cwd=ROOT_DIR,
                capture_output=True,
                timeout=self.args.codex_timeout,
                check=False,
            )
            (job_dir / "stdout.log").write_text(proc.stdout or "", encoding="utf-8")
            (job_dir / "stderr.log").write_text(proc.stderr or "", encoding="utf-8")
            status = "succeeded" if proc.returncode == 0 and (job_dir / "output.json").exists() else "failed"
            updates: dict[str, Any] = {
                "status": status,
                "finished_at": now_iso(),
                "elapsed_seconds": round(time.time() - started, 2),
                "returncode": proc.returncode,
            }
            if status == "failed":
                updates["error"] = f"codex exec failed with returncode {proc.returncode}"
            self.update_job(job_id, updates)
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

        request_payload = {
            "tool": "response",
            "schema": "translation",
            "prompt": self.translation_prompt(payload),
            "input": payload,
            "model": payload.get("model") or self.args.model,
            "reasoning": payload.get("reasoning") or self.args.reasoning,
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

        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--model",
            self.args.model,
            "-c",
            f'model_reasoning_effort="{self.args.reasoning}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(ROOT_DIR),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        started = time.time()
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            text=True,
            cwd=ROOT_DIR,
            capture_output=True,
            timeout=self.args.codex_timeout,
            check=False,
        )
        (run_dir / "stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        (run_dir / "stderr.log").write_text(proc.stderr or "", encoding="utf-8")
        write_json(
            run_dir / "run.json",
            {
                "tool": tool_name,
                "model": self.args.model,
                "reasoning": self.args.reasoning,
                "returncode": proc.returncode,
                "elapsed_seconds": round(time.time() - started, 2),
                "output": str(output_path.relative_to(ROOT_DIR)) if output_path.exists() else "",
            },
        )
        if proc.returncode != 0:
            raise WebAppError(f"{tool_name} codex exec failed; see {run_dir.relative_to(ROOT_DIR)}")
        if not output_path.exists():
            raise WebAppError(f"{tool_name} did not write output JSON")
        return json.loads(output_path.read_text(encoding="utf-8"))

    def mock_tool(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
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

    def reply(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        if not message.strip():
            raise WebAppError("message is empty")
        session = self.create_session(message) if not session_id else self.load_session(safe_session_id(session_id))
        session_id = session["id"]
        user_path = self.append_message(session_id, "user", message)
        local_matches = self.search_local_content(message)
        payload = {
            "session": self.load_session(session_id),
            "message": message,
            "transcript": self.transcript(session_id),
            "local_matches": local_matches,
            "category_snapshot": self.category_snapshot(),
            "storage": {
                "session_dir": str(self.session_dir(session_id).relative_to(ROOT_DIR)),
                "user_message_path": str(user_path.relative_to(ROOT_DIR)),
            },
        }
        result = self.run_codex_tool(
            session_id=session_id,
            tool_name="reply",
            prompt_template_path=CHAT_REPLY_PROMPT,
            schema_path=CHAT_REPLY_SCHEMA,
            payload=payload,
        )
        assistant_path = self.append_message(
            session_id,
            "assistant",
            result["reply"],
            {
                "intent": result.get("intent", ""),
                "should_draft": bool(result.get("should_draft", False)),
                "suggested_title": result.get("suggested_title", ""),
            },
        )
        return {
            **self.session_payload(session_id),
            "reply": result,
            "assistant_path": str(assistant_path.relative_to(ROOT_DIR)),
        }

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
                "model": self.args.model,
                "reasoning": self.args.reasoning,
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
        try:
            git_commit_push(
                [self.session_dir(session_id), self.draft_folder(session_id)],
                f"Publish LazyBlog Studio draft {post.get('id')}",
                self.args.branch,
                self.args.commit_push,
            )
        except subprocess.CalledProcessError as exc:
            published.setdefault("warnings", []).append(f"git commit/push failed: {exc}")
            write_json(publish_path, published)
        return {
            **self.session_payload(session_id),
            "draft": {
                "path": str(draft_path.relative_to(ROOT_DIR)),
                "markdown": draft_path.read_text(encoding="utf-8"),
            },
            "redraft": draft_result,
            "published": published,
        }

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
        return {"session": session, "messages": page["messages"], "message_page": page["message_page"], "draft": draft}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f766e">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="LazyBlog Studio">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/icons/lazyblog.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/icons/lazyblog.svg">
  <title>LazyBlog Studio</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,650&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap");
    :root {
      --ink: #1d2520;
      --muted: #667069;
      --paper: #fffaf0;
      --line: rgba(39, 55, 46, 0.16);
      --teal: #0f766e;
      --teal-dark: #0b4f4a;
      --clay: #d96b43;
      --gold: #e3a92f;
      --shadow: 0 24px 70px rgba(28, 45, 38, 0.16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Newsreader", Georgia, serif;
      background:
        radial-gradient(circle at 12% 16%, rgba(227, 169, 47, 0.28), transparent 26rem),
        radial-gradient(circle at 92% 12%, rgba(15, 118, 110, 0.18), transparent 24rem),
        linear-gradient(135deg, #fffaf0 0%, #f3ead7 52%, #d9ede8 100%);
      min-height: 100vh;
      overflow-x: hidden;
    }
    button, input, textarea, select { font: inherit; }
    .shell { display: grid; grid-template-columns: 260px minmax(0, 1fr) 360px; gap: 18px; width: 100%; max-width: 100vw; height: 100vh; padding: 20px; overflow: hidden; }
    .panel { min-width: 0; background: rgba(255, 250, 240, 0.82); border: 1px solid var(--line); border-radius: 28px; box-shadow: var(--shadow); backdrop-filter: blur(18px); overflow: hidden; }
    .side, .publish { min-width: 0; max-height: calc(100vh - 40px); padding: 18px; overflow-y: auto; }
    .brand { padding: 22px; border-bottom: 1px solid var(--line); background: linear-gradient(135deg, rgba(15, 118, 110, 0.12), rgba(217, 107, 67, 0.12)); }
    h1, h2 { font-family: "Fraunces", Georgia, serif; line-height: 1; margin: 0; }
    h1 { font-size: 34px; letter-spacing: -0.05em; }
    h2 { font-size: 20px; letter-spacing: -0.03em; }
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
    .chat { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-width: 0; max-width: 100%; height: calc(100vh - 40px); overflow: hidden; }
    .chat-head { min-width: 0; padding: 22px 24px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .chat-head > div:first-child { min-width: 0; }
    #chatTitle, #chatMeta, #modelLabel { overflow: hidden; text-overflow: ellipsis; }
    #chatTitle, #chatMeta { white-space: nowrap; }
    .status { min-width: 0; max-width: 220px; display: inline-flex; gap: 8px; align-items: center; padding: 8px 12px; border-radius: 999px; background: rgba(15, 118, 110, 0.1); color: var(--teal-dark); font-size: 13px; white-space: nowrap; }
    #modelLabel { display: block; min-width: 0; }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--teal); box-shadow: 0 0 0 6px rgba(15, 118, 110, 0.12); }
    .messages { min-width: 0; min-height: 0; max-width: 100%; padding: 18px 24px 24px; overflow-y: auto; overflow-x: hidden; display: flex; flex-direction: column; gap: 14px; }
    .message-list { min-width: 0; display: flex; flex-direction: column; gap: 14px; }
    .more-messages { display: none; align-self: center; margin: 0 auto 2px; padding: 8px 12px; background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .more-messages.visible { display: inline-flex; }
    .more-messages.loading { cursor: wait; opacity: 0.7; }
    .msg { min-width: 0; max-width: min(760px, 88%); padding: 14px 16px; border-radius: 22px; border: 1px solid var(--line); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; line-height: 1.48; animation: rise 220ms ease both; }
    .msg.user { align-self: flex-end; background: linear-gradient(135deg, rgba(15, 118, 110, 0.93), rgba(11, 79, 74, 0.93)); color: white; border-color: rgba(15, 118, 110, 0.3); }
    .msg.assistant { align-self: flex-start; background: rgba(255, 255, 255, 0.62); }
    .composer { min-width: 0; max-width: 100%; padding: 18px; border-top: 1px solid var(--line); background: rgba(255, 244, 217, 0.58); overflow: hidden; }
    textarea { width: 100%; min-height: 108px; resize: vertical; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 20px; background: rgba(255, 255, 255, 0.72); color: var(--ink); padding: 14px 15px; outline: none; line-height: 1.45; }
    textarea:focus, select:focus, input:focus { border-color: rgba(15, 118, 110, 0.55); box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
    .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
    .row > * { min-width: 0; }
    button { border: 0; border-radius: 999px; padding: 11px 16px; background: var(--ink); color: white; cursor: pointer; transition: transform 160ms ease, opacity 160ms ease; }
    button:hover { transform: translateY(-1px); }
    button:disabled { opacity: 0.55; cursor: wait; transform: none; }
    .secondary { background: rgba(29, 37, 32, 0.08); color: var(--ink); }
    .accent { background: linear-gradient(135deg, var(--clay), var(--gold)); color: #231b12; font-weight: 600; }
    .publish-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .publish-close { display: none; }
    .field { margin-top: 16px; }
    label { display: block; font-size: 13px; color: var(--muted); margin: 0 0 6px 4px; }
    select, input { width: 100%; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 16px; background: rgba(255, 255, 255, 0.68); padding: 10px 12px; outline: none; }
    .preview { height: 360px; min-height: 220px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; background: #18251f; color: #eef6ec; border-color: rgba(255, 255, 255, 0.08); }
    .log { margin-top: 12px; padding: 12px; border-radius: 16px; background: rgba(255, 255, 255, 0.56); color: var(--muted); font-size: 13px; line-height: 1.4; white-space: pre-wrap; min-height: 44px; }
    .path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--teal-dark); overflow-wrap: anywhere; }
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
    .mobile-menu-toggle, .mobile-publish-toggle, .mobile-top-title { display: none; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    @media (max-width: 1080px) { .shell { grid-template-columns: 1fr; } .chat { min-height: 70vh; } .publish { order: 3; } }
    @media (max-width: 720px) {
      .shell { display: block; height: 100svh; padding: 52px 8px 8px; overflow: hidden; }
      .mobile-menu-toggle {
        position: fixed;
        top: 10px;
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
        top: 10px;
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
        letter-spacing: -0.04em;
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
        height: calc(100svh - 60px);
        min-height: 0;
        grid-template-rows: auto minmax(0, 1fr) auto;
        border-radius: 22px;
      }
      .chat-head { padding: 10px 12px; gap: 8px; }
      .chat-head .sub { margin-top: 4px; font-size: 12px; max-width: 100%; }
      .status { flex: 0 1 118px; max-width: 118px; gap: 6px; padding: 6px 8px; font-size: 12px; }
      .dot { width: 7px; height: 7px; box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
      .messages { padding: 8px 10px 10px; gap: 8px; }
      .message-list { gap: 8px; }
      .more-messages { padding: 7px 11px; font-size: 13px; }
      .msg { max-width: 96%; padding: 10px 11px; border-radius: 16px; line-height: 1.38; }
      .msg.user { max-width: 94%; }
      .composer {
        padding: 8px;
        background: rgba(255, 244, 217, 0.88);
      }
      textarea { min-height: 72px; max-height: 32vh; border-radius: 16px; padding: 10px 11px; }
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
        <div class="status"><span class="dot"></span><span id="modelLabel">Codex ready</span></div>
      </header>
      <div id="messages" class="messages">
        <button id="moreMessages" class="more-messages" type="button">More messages</button>
        <div id="messageList" class="message-list"></div>
      </div>
      <form id="composer" class="composer">
        <textarea id="messageInput" placeholder="Write a note, idea, outline, memory, or instruction. The reply tool will store it and respond; the task tool can turn the session into a post."></textarea>
        <div class="row">
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
      <p class="sub">The publish button creates a draft if needed, converts Markdown to WordPress HTML, creates terms, and posts through REST auth.</p>
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
        <input id="extraInstruction" placeholder="e.g. make it reflective, English, category Notes">
      </div>
      <div class="row">
        <button id="publishButton" class="accent" type="button">Draft and Publish</button>
        <button id="redraftButton" class="secondary" type="button">Force Redraft</button>
      </div>
      <div class="field">
        <label for="draftPreview">Latest Markdown draft</label>
        <textarea id="draftPreview" class="preview" readonly></textarea>
      </div>
      <div id="publishLog" class="log">No draft yet.</div>
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
  <script>
    const state = { sessionId: null, busy: false, messagePage: null, loadingMore: false, modalSession: null };
    const $ = (id) => document.getElementById(id);
    const shell = $("shell");
    $("modelLabel").textContent = "__MODEL_LABEL__";

    function setBusy(label) {
      state.busy = Boolean(label);
      $("busyLabel").textContent = label || "";
      for (const id of ["sendButton", "draftButton", "publishButton", "redraftButton"]) $(id).disabled = state.busy;
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

    function openSessionModal(session) {
      state.modalSession = session;
      $("sessionActionName").textContent = session.title || session.id;
      $("sessionActionModal").classList.add("open");
    }

    function closeSessionModal() {
      $("sessionActionModal").classList.remove("open");
    }

    function clearChat() {
      state.sessionId = null;
      state.messagePage = null;
      $("chatTitle").textContent = "New chat";
      $("chatMeta").textContent = "Messages will be saved as Markdown.";
      $("messageList").innerHTML = "";
      $("messages").scrollTop = 0;
      updateMoreButton();
      $("draftPreview").value = "";
      $("publishLog").textContent = "No draft yet.";
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

    function renderMessages(messages) {
      const root = $("messageList");
      root.innerHTML = "";
      for (const msg of messages || []) {
        const el = document.createElement("div");
        el.className = `msg ${msg.role}`;
        el.textContent = msg.content;
        root.appendChild(el);
      }
      $("messages").scrollTop = $("messages").scrollHeight;
    }

    function prependMessages(messages) {
      if (!messages || messages.length === 0) return;
      const scroller = $("messages");
      const list = $("messageList");
      const previousHeight = scroller.scrollHeight;
      for (const msg of [...messages].reverse()) {
        const el = document.createElement("div");
        el.className = `msg ${msg.role}`;
        el.textContent = msg.content;
        list.prepend(el);
      }
      scroller.scrollTop += scroller.scrollHeight - previousHeight;
    }

    function renderSession(payload) {
      state.sessionId = payload.session.id;
      state.messagePage = payload.message_page || null;
      $("chatTitle").textContent = payload.session.title || payload.session.id;
      $("chatMeta").textContent = `${payload.session.message_count || 0} messages stored in content/chat/${payload.session.id}`;
      renderMessages(payload.messages || []);
      updateMoreButton();
      if (payload.draft) {
        $("draftPreview").value = payload.draft.markdown || "";
        $("publishLog").innerHTML = `Latest draft: <span class="path">${escapeHtml(payload.draft.path)}</span>`;
      }
      loadSessions();
      loadJobs();
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
      const data = await api(`/api/session?id=${encodeURIComponent(id)}&limit=10`);
      renderSession(data);
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

    async function sendMessage(event) {
      event.preventDefault();
      const message = $("messageInput").value.trim();
      if (!message) return;
      setBusy("running reply tool...");
      try {
        const data = await api("/api/chat", { session_id: state.sessionId, message });
        $("messageInput").value = "";
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
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
        const data = await api("/api/draft", {
          session_id: state.sessionId,
          status: $("publishStatus").value,
          instruction: $("extraInstruction").value
        });
        $("draftPreview").value = data.markdown || "";
        $("publishLog").innerHTML = `Draft saved: <span class="path">${escapeHtml(data.draft_path)}</span>`;
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
        const data = await api("/api/publish", {
          session_id: state.sessionId,
          status: $("publishStatus").value,
          force_redraft: Boolean(force),
          instruction: $("extraInstruction").value
        });
        const link = data.published.link || "";
        $("publishLog").innerHTML = `WordPress post ${data.published.post_id} saved as ${data.published.status}.<br><span class="path">${escapeHtml(link)}</span>`;
        renderSession(data);
      } catch (err) {
        $("publishLog").textContent = err.message;
      } finally {
        setBusy("");
      }
    }

    $("composer").addEventListener("submit", sendMessage);
    $("draftButton").addEventListener("click", draftPost);
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
    $("sessionActionModal").addEventListener("click", (event) => {
      if (event.target === $("sessionActionModal")) closeSessionModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSessionModal();
    });
    $("newSession").addEventListener("click", () => {
      shell.classList.remove("nav-open");
      $("mobileMenuToggle").setAttribute("aria-expanded", "false");
      clearChat();
      loadSessions();
    });
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/service-worker.js").catch(() => {});
      });
    }
    loadSessions({ autoload: true }).catch((err) => { $("publishLog").textContent = err.message; });
    loadJobs().catch(() => {});
    setInterval(() => loadJobs().catch(() => {}), 4000);
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
      background:
        radial-gradient(circle at 18% 18%, rgba(227, 169, 47, 0.34), transparent 26rem),
        radial-gradient(circle at 82% 14%, rgba(15, 118, 110, 0.2), transparent 24rem),
        linear-gradient(135deg, #fffaf0 0%, #f3ead7 52%, #d9ede8 100%);
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
    h1 { font-family: "Fraunces", Georgia, serif; font-size: 42px; line-height: 0.95; letter-spacing: -0.05em; margin: 0; }
    p { color: var(--muted); line-height: 1.5; }
    label { display: block; font-size: 13px; color: var(--muted); margin: 16px 0 6px 4px; }
    input { width: 100%; border: 1px solid rgba(39, 55, 46, 0.18); border-radius: 18px; background: rgba(255, 255, 255, 0.7); padding: 12px 14px; font: inherit; outline: none; }
    input:focus { border-color: rgba(15, 118, 110, 0.55); box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12); }
    button { width: 100%; margin-top: 20px; border: 0; border-radius: 999px; padding: 13px 18px; background: linear-gradient(135deg, var(--clay), var(--gold)); color: #231b12; font: inherit; font-weight: 600; cursor: pointer; }
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
    "background_color": "#fffaf0",
    "theme_color": "#0f766e",
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


SERVICE_WORKER = r"""const CACHE_NAME = "lazyblog-studio-v2";
const APP_SHELL = ["/manifest.webmanifest", "/icons/lazyblog.svg", "/icons/lazyblog-192.png", "/icons/lazyblog-512.png"];

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
        server_version = "LazyBlogStudio/0.1"

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
            html_text = body_text or INDEX_HTML.replace("__MODEL_LABEL__", f"{app.args.model} / {app.args.reasoning}")
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
                return True
            if path.startswith("/api/translate/"):
                return self.require_api_auth(path)
            if path.startswith("/api/codex/"):
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
                if parsed.path == "/api/health":
                    self.send_json({"status": "ok", "root": str(ROOT_DIR)})
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
                if parsed.path == "/api/messages":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = params.get("session_id", [""])[0]
                    raw_limit = params.get("limit", [str(DEFAULT_MESSAGE_BATCH_SIZE)])[0]
                    before = params.get("before", [""])[0]
                    self.send_json(app.message_page(session_id, limit=int(raw_limit), before=before))
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
                        cookie = (
                            f"{STUDIO_AUTH_COOKIE}={make_studio_cookie(username)}; "
                            f"Path=/; HttpOnly; SameSite=Lax; Max-Age={STUDIO_AUTH_TTL_SECONDS}"
                        )
                        self.send_json({"user": username}, headers={"Set-Cookie": cookie})
                        return
                    self.send_json({"error": "invalid LazyBlog Studio login"}, HTTPStatus.UNAUTHORIZED)
                    return
                if parsed.path == "/api/logout":
                    self.send_json(
                        {"status": "logged out"},
                        headers={"Set-Cookie": f"{STUDIO_AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
                    )
                    return
                if not self.authorize_request(parsed.path):
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
                if parsed.path == "/api/chat":
                    self.send_json(app.reply(str(payload.get("message", "")), payload.get("session_id") or None))
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
    print(f"studio_auth={'on' if studio_auth_enabled() else 'off'} user={studio_username()}", flush=True)
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
