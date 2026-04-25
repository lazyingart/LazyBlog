#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from export_wordpress_posts import HTMLToMarkdownParser, sanitize_slug, strip_html
from lazyblog_translate import (
    LazyBlogError,
    first_heading,
    load_env_file,
    markdown_to_html,
    normalize_language,
    split_front_matter,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_DIR = ROOT_DIR / "content" / "posts"
TRILINGUAL_LANGUAGES = {"ja", "zh", "en"}

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


class WPClient:
    def __init__(self, site_url: str, username: str | None, app_password: str | None) -> None:
        self.site_url = site_url.rstrip("/")
        self.username = username
        self.app_password = app_password

    def url(self, path: str) -> str:
        return urllib.parse.urljoin(self.site_url + "/", path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> Any:
        url = self.url(path)
        body = data
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "LazyBlogSync/0.1",
        }
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        if self.username and self.app_password:
            token = base64.b64encode(f"{self.username}:{self.app_password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LazyBlogError(f"WordPress HTTP {exc.code}: {detail}") from exc

        return json.loads(raw) if raw else {}

    def find_media_by_filename(self, filename: str) -> str | None:
        basename = Path(filename).name
        if not basename:
            return None

        stem = Path(basename).stem
        query = urllib.parse.urlencode({"search": stem, "per_page": 100, "context": "edit"})
        try:
            rows = self.request("GET", f"/wp-json/wp/v2/media?{query}")
        except LazyBlogError:
            return None

        if not isinstance(rows, list):
            return None

        for row in rows:
            if not isinstance(row, dict):
                continue
            source_url = str(row.get("source_url") or "")
            source_name = Path(urllib.parse.unquote(urllib.parse.urlparse(source_url).path)).name
            if source_name == basename:
                return source_url
        return None

    def get_post(self, post_id: int) -> dict[str, Any]:
        return self.request("GET", f"/wp-json/wp/v2/posts/{post_id}?context=edit")

    def update_post(self, post_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/wp-json/wp/v2/posts/{post_id}", payload)

    def get_translations(self, post_id: int) -> dict[str, Any]:
        return self.request("GET", f"/wp-json/lazyblog/v1/posts/{post_id}/translations")

    def update_translation(self, post_id: int, language: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/wp-json/lazyblog/v1/posts/{post_id}/translations/{language}", payload)

    def set_source_language(self, post_id: int, source_language: str) -> dict[str, Any]:
        return self.request(
            "PUT",
            f"/wp-json/lazyblog/v1/posts/{post_id}/translations",
            {"source_language": source_language},
        )

    def upload_media(self, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        quoted_filename = filename.replace('"', "")
        return self.request(
            "POST",
            "/wp-json/wp/v2/media",
            data=content,
            headers={
                "Content-Type": content_type,
                "Content-Disposition": f'attachment; filename="{quoted_filename}"',
            },
        )


def make_client(args: argparse.Namespace) -> WPClient:
    load_env_file(ROOT_DIR / ".env")
    site_url = getattr(args, "site_url", None) or os.environ.get("WP_SITE_URL")
    if not site_url:
        raise LazyBlogError("set WP_SITE_URL in .env or pass --site-url")

    return WPClient(
        site_url=site_url,
        username=os.environ.get("WP_USERNAME"),
        app_password=os.environ.get("WP_APP_PASSWORD"),
    )


def require_auth() -> None:
    if not os.environ.get("WP_USERNAME") or not os.environ.get("WP_APP_PASSWORD"):
        raise LazyBlogError("set WP_USERNAME and WP_APP_PASSWORD in .env before writing to WordPress")


def content_root() -> Path:
    load_env_file(ROOT_DIR / ".env")
    configured = os.environ.get("LAZYBLOG_CONTENT_DIR")
    return (ROOT_DIR / configured).resolve() if configured else DEFAULT_CONTENT_DIR


def post_dir_for_id(post_id: int) -> Path:
    return content_root() / str(post_id)


def resolve_post_dir(value: str) -> Path:
    if value.isdigit():
        return post_dir_for_id(int(value))
    return Path(value).expanduser().resolve()


def manifest_path(post_dir: Path) -> Path:
    return post_dir / "lazyblog.json"


def read_manifest(post_dir: Path) -> dict[str, Any]:
    path = manifest_path(post_dir)
    if not path.exists():
        raise LazyBlogError(f"missing manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(post_dir: Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(post_dir)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, front_matter: dict[str, Any], body: str) -> None:
    lines = ["---"]
    for key, value in front_matter.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(str(item), ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + body.lstrip(), encoding="utf-8")


def update_front_matter(path: Path, updates: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text:
        write_markdown(path, {k: v for k, v in updates.items() if v is not None}, text)
        return

    header, body = text.split("\n---\n", 1)
    lines = header.splitlines()[1:]
    remaining = {k: v for k, v in updates.items() if v is not None}
    rewritten: list[str] = []

    for line in lines:
        if line.startswith((" ", "\t")) or ":" not in line:
            rewritten.append(line)
            continue
        key = line.split(":", 1)[0].strip()
        if key in remaining:
            rewritten.append(f"{key}: {json.dumps(str(remaining.pop(key)), ensure_ascii=False)}")
        else:
            rewritten.append(line)

    for key, value in remaining.items():
        rewritten.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")

    path.write_text("---\n" + "\n".join(rewritten) + "\n---\n" + body, encoding="utf-8")


def source_markdown_path(post_dir: Path) -> Path:
    return post_dir / "post.md"


def translations_dir(post_dir: Path) -> Path:
    return post_dir / "translations"


def prompts_dir(post_dir: Path) -> Path:
    return post_dir / "prompts"


def logs_dir(post_dir: Path) -> Path:
    return post_dir / "logs"


def markdown_files(post_dir: Path) -> list[Path]:
    files = [source_markdown_path(post_dir)]
    translation_root = translations_dir(post_dir)
    if translation_root.exists():
        files.extend(sorted(translation_root.glob("*.md")))
    return [path for path in files if path.exists()]


def extract_title(markdown_path: Path) -> str:
    front_matter, body = split_front_matter(markdown_path.read_text(encoding="utf-8"))
    return front_matter.get("title") or first_heading(body) or markdown_path.stem


def html_to_markdown(content_html: str, site_url: str) -> str:
    parser = HTMLToMarkdownParser(image_map={}, base_url=site_url)
    parser.feed(content_html)
    return parser.markdown()


def post_front_matter(post: dict[str, Any], source_language: str) -> dict[str, Any]:
    return {
        "id": post.get("id"),
        "source_language": source_language,
        "title": strip_html(post.get("title", {}).get("rendered", "")),
        "slug": post.get("slug", ""),
        "date": post.get("date", ""),
        "modified": post.get("modified", ""),
        "status": post.get("status", ""),
        "link": post.get("link", ""),
    }


def cmd_init(args: argparse.Namespace) -> None:
    post_id = int(args.post_id)
    source_language = normalize_language(args.source_language)
    post_dir = Path(args.output).resolve() if args.output else post_dir_for_id(post_id)
    post_dir.mkdir(parents=True, exist_ok=True)
    translations_dir(post_dir).mkdir(parents=True, exist_ok=True)
    prompts_dir(post_dir).mkdir(parents=True, exist_ok=True)
    logs_dir(post_dir).mkdir(parents=True, exist_ok=True)

    source_path = source_markdown_path(post_dir)
    if args.source:
        source = Path(args.source)
        shutil.copyfile(source, source_path)
        source_images = source.parent / "images"
        target_images = post_dir / "images"
        if source_images.is_dir():
            if target_images.exists():
                shutil.rmtree(target_images)
            shutil.copytree(source_images, target_images)
        update_front_matter(source_path, {"id": post_id, "source_language": source_language})
    elif not source_path.exists():
        write_markdown(
            source_path,
            {"id": post_id, "source_language": source_language, "title": ""},
            "",
        )

    manifest = {
        "version": 1,
        "post_id": post_id,
        "source_language": source_language,
        "source_file": "post.md",
        "translations_dir": "translations",
        "media": {},
        "last_pull": None,
        "last_push": None,
    }
    write_manifest(post_dir, manifest)
    print(f"initialized sync folder: {post_dir}")


def cmd_pull(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    post_id = int(args.post_id)
    client = make_client(args)
    post = client.get_post(post_id)

    source_language = normalize_language(args.source_language) if args.source_language else "en"
    try:
        translation_meta = client.get_translations(post_id)
        source_language = normalize_language(translation_meta.get("source_language") or source_language)
    except LazyBlogError:
        pass

    post_dir = Path(args.output).resolve() if args.output else post_dir_for_id(post_id)
    post_dir.mkdir(parents=True, exist_ok=True)
    translations_dir(post_dir).mkdir(parents=True, exist_ok=True)
    prompts_dir(post_dir).mkdir(parents=True, exist_ok=True)
    logs_dir(post_dir).mkdir(parents=True, exist_ok=True)

    raw_html = post.get("content", {}).get("raw") or post.get("content", {}).get("rendered", "")
    markdown_body = html_to_markdown(raw_html, client.site_url)
    write_markdown(source_markdown_path(post_dir), post_front_matter(post, source_language), markdown_body)

    manifest = {
        "version": 1,
        "post_id": post_id,
        "source_language": source_language,
        "source_file": "post.md",
        "translations_dir": "translations",
        "media": {},
        "last_pull": {
            "at": int(time.time()),
            "modified": post.get("modified"),
            "link": post.get("link"),
        },
        "last_push": None,
    }
    write_manifest(post_dir, manifest)
    print(f"pulled post {post_id} -> {post_dir}")


def build_post_payload(markdown_path: Path, status: str | None) -> dict[str, Any]:
    text = markdown_path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    payload: dict[str, Any] = {
        "title": front_matter.get("title") or first_heading(body) or markdown_path.stem,
        "content": markdown_to_html(text),
    }
    if front_matter.get("slug"):
        payload["slug"] = front_matter["slug"]
    if front_matter.get("excerpt"):
        payload["excerpt"] = front_matter["excerpt"]
    if status:
        payload["status"] = status
    elif front_matter.get("status"):
        payload["status"] = front_matter["status"]
    return payload


def translation_payload(markdown_path: Path, source_language: str) -> tuple[str, dict[str, Any]]:
    text = markdown_path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    language = normalize_language(front_matter.get("language") or markdown_path.stem)
    payload = {
        "source_language": source_language,
        "title": front_matter.get("title") or first_heading(body) or markdown_path.stem,
        "content": markdown_to_html(text),
        "excerpt": front_matter.get("excerpt", ""),
    }
    return language, payload


def cmd_push(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    if not args.dry_run:
        require_auth()
    client = make_client(args)
    post_dir = resolve_post_dir(args.post_dir)
    manifest = read_manifest(post_dir)
    post_id = int(manifest["post_id"])
    source_language = normalize_language(args.source_language or manifest.get("source_language", "en"))
    source_path = source_markdown_path(post_dir)

    source_payload = build_post_payload(source_path, args.status)
    translation_files = sorted(translations_dir(post_dir).glob("*.md")) if translations_dir(post_dir).exists() else []
    translation_payloads = [translation_payload(path, source_language) for path in translation_files]

    if args.dry_run:
        preview = {
            "post_id": post_id,
            "source_language": source_language,
            "source": {
                "title": source_payload.get("title"),
                "status": source_payload.get("status"),
                "content_bytes": len(source_payload.get("content", "")),
            },
            "translations": [
                {
                    "language": language,
                    "title": payload.get("title"),
                    "content_bytes": len(payload.get("content", "")),
                }
                for language, payload in translation_payloads
            ],
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    if not args.translations_only:
        client.update_post(post_id, source_payload)
        client.set_source_language(post_id, source_language)
        print(f"pushed source post {post_id}")

    if not args.source_only:
        for language, payload in translation_payloads:
            client.update_translation(post_id, language, payload)
            print(f"pushed {language} translation for post {post_id}")

    manifest["source_language"] = source_language
    manifest["last_push"] = {"at": int(time.time()), "site_url": client.site_url}
    write_manifest(post_dir, manifest)


def same_site_url(url: str, site_url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    site = urllib.parse.urlparse(site_url)
    return bool(parsed.hostname and site.hostname and parsed.hostname.lower() == site.hostname.lower())


def resolve_asset_url(raw_url: str, markdown_path: Path, site_url: str) -> tuple[str, Path | None]:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme in {"http", "https"}:
        return raw_url, None
    if parsed.scheme:
        return raw_url, None
    if raw_url.startswith("/"):
        return urllib.parse.urljoin(site_url.rstrip("/") + "/", raw_url), None

    local_path = (markdown_path.parent / urllib.parse.unquote(raw_url)).resolve()
    return urllib.parse.urljoin(site_url.rstrip("/") + "/", raw_url), local_path


def download_url(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "LazyBlogSync/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers.get_content_type() or "application/octet-stream"


def guess_content_type(path_or_url: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(path_or_url)
    return guessed or fallback


def candidate_filename(raw_url: str, local_path: Path | None) -> str:
    if local_path is not None:
        return local_path.name
    parsed = urllib.parse.urlparse(raw_url)
    return Path(urllib.parse.unquote(parsed.path)).name or sanitize_slug(raw_url) + ".bin"


def image_references(text: str) -> list[tuple[str, str]]:
    visible_lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            visible_lines.append("")
            continue
        visible_lines.append("" if in_code else line)

    visible_text = "\n".join(visible_lines)
    refs = [(match.group(0), match.group(2)) for match in MD_IMAGE_RE.finditer(visible_text)]
    refs.extend((match.group(0), match.group(1)) for match in HTML_IMG_RE.finditer(visible_text))
    return refs


def replace_token_outside_code(text: str, token: str, replacement: str) -> str:
    rewritten: list[str] = []
    in_code = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            in_code = not in_code
            rewritten.append(line)
            continue
        rewritten.append(line if in_code else line.replace(token, replacement))
    return "".join(rewritten)


def append_media_log(post_dir: Path, event: dict[str, Any]) -> None:
    logs_dir(post_dir).mkdir(parents=True, exist_ok=True)
    log_path = logs_dir(post_dir) / "media-sync.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def migrate_markdown_media(
    *,
    client: WPClient,
    post_dir: Path,
    markdown_path: Path,
    apply: bool,
    remove_dead: bool,
    manifest: dict[str, Any],
) -> dict[str, int]:
    text = markdown_path.read_text(encoding="utf-8")
    new_text = text
    stats = {"seen": 0, "uploaded": 0, "skipped": 0, "dead": 0, "removed": 0}
    media_map = manifest.setdefault("media", {})

    for token, raw_url in image_references(text):
        stats["seen"] += 1
        resolved_url, local_path = resolve_asset_url(raw_url, markdown_path, client.site_url)

        if local_path is None and same_site_url(resolved_url, client.site_url):
            stats["skipped"] += 1
            append_media_log(post_dir, {"file": str(markdown_path), "url": raw_url, "status": "already-on-site"})
            continue

        if raw_url in media_map:
            if apply:
                new_text = replace_token_outside_code(new_text, token, token.replace(raw_url, media_map[raw_url]))
            stats["skipped"] += 1
            append_media_log(post_dir, {"file": str(markdown_path), "url": raw_url, "status": "already-mapped"})
            continue

        filename = candidate_filename(resolved_url, local_path)
        if apply:
            existing_url = client.find_media_by_filename(filename)
            if existing_url:
                media_map[raw_url] = existing_url
                new_text = replace_token_outside_code(new_text, token, token.replace(raw_url, existing_url))
                stats["skipped"] += 1
                append_media_log(
                    post_dir,
                    {"file": str(markdown_path), "url": raw_url, "status": "already-on-host", "media_url": existing_url},
                )
                continue

        try:
            if local_path is not None:
                binary = local_path.read_bytes()
                content_type = guess_content_type(filename)
            else:
                binary, content_type = download_url(resolved_url)
                content_type = content_type or guess_content_type(filename)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            stats["dead"] += 1
            append_media_log(post_dir, {"file": str(markdown_path), "url": raw_url, "status": "dead", "detail": str(exc)})
            if apply and remove_dead:
                new_text = replace_token_outside_code(new_text, token, "")
                stats["removed"] += 1
            continue

        if not apply:
            stats["skipped"] += 1
            append_media_log(
                post_dir,
                {
                    "file": str(markdown_path),
                    "url": raw_url,
                    "status": "would-upload",
                    "filename": filename,
                    "content_type": content_type,
                    "bytes": len(binary),
                },
            )
            continue

        media = client.upload_media(filename, binary, content_type)
        source_url = media.get("source_url")
        if not source_url:
            raise LazyBlogError(f"media upload did not return source_url for {raw_url}")
        media_map[raw_url] = source_url
        new_text = replace_token_outside_code(new_text, token, token.replace(raw_url, source_url))
        stats["uploaded"] += 1
        append_media_log(
            post_dir,
            {"file": str(markdown_path), "url": raw_url, "status": "uploaded", "media_url": source_url},
        )

    if apply and new_text != text:
        markdown_path.write_text(new_text, encoding="utf-8")

    return stats


def cmd_media(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    if args.apply:
        require_auth()
    client = make_client(args)
    post_dir = resolve_post_dir(args.post_dir)
    manifest = read_manifest(post_dir)

    totals = {"seen": 0, "uploaded": 0, "skipped": 0, "dead": 0, "removed": 0}
    for markdown_path in markdown_files(post_dir):
        stats = migrate_markdown_media(
            client=client,
            post_dir=post_dir,
            markdown_path=markdown_path,
            apply=args.apply,
            remove_dead=args.remove_dead,
            manifest=manifest,
        )
        for key, value in stats.items():
            totals[key] += value
    if args.apply:
        write_manifest(post_dir, manifest)
    print(json.dumps(totals, ensure_ascii=False, indent=2))


def recent_dead_media_log(post_dir: Path) -> str:
    log_path = logs_dir(post_dir) / "media-sync.jsonl"
    if not log_path.exists():
        return "No media-sync log exists yet."
    rows = []
    for line in log_path.read_text(encoding="utf-8").splitlines()[-200:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("status") == "dead":
            rows.append(f"- {event.get('url')}: {event.get('detail', '')}")
    return "\n".join(rows[-30:]) if rows else "No dead images recorded in the latest media-sync log."


def polish_prompt(markdown: str, post_dir: Path, language: str) -> str:
    return f"""Polish this WordPress blog post in {language}.

Return only the complete revised Markdown file, including front matter. Do not add explanations.

Editing goals:
- Clean the post so it is tidy, readable, and publishable.
- Remove broken image links and obviously bad image embeds.
- Keep good image links, local image paths, code blocks, equations, citations, and factual claims intact.
- If the post is unfinished, complete it naturally using only context already present in the draft.
- Do not make the writing sound like AI. Avoid generic summaries, marketing tone, and over-explaining.
- Preserve the author's voice. Prefer precise, quiet edits over rewriting everything.
- Keep front matter keys such as id, title, slug, status, source_language, language, and excerpt when present.

Known dead image links from LazyBlog media logs:
{recent_dead_media_log(post_dir)}

Markdown:

{markdown}
"""


def translation_prompt(markdown: str, source_language: str, target_language: str) -> str:
    labels = {"ja": "Japanese", "zh": "Simplified Chinese", "en": "English"}
    return f"""Translate this WordPress blog post from {labels[source_language]} to {labels[target_language]}.

Return only the complete translated Markdown file, including front matter. Do not add explanations.

Rules:
- Keep Markdown structure, links, images, code blocks, citations, equations, and front matter.
- Translate title and excerpt if present.
- Set front matter `language` to `{target_language}` and `source_language` to `{source_language}`.
- Keep the prose natural and literary when the source is literary.
- Do not sound like AI. Avoid generic translator notes.
- Do not invent facts or add new sections that are not implied by the source.

Markdown:

{markdown}
"""


def run_codex(prompt: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    extra_args = shlex.split(os.environ.get("LAZYBLOG_CODEX_ARGS", ""))
    subprocess.run(
        [
            "codex",
            "exec",
            *extra_args,
            "--cd",
            str(ROOT_DIR),
            "--output-last-message",
            str(output_path),
            "-",
        ],
        input=prompt,
        text=True,
        check=True,
    )


def cmd_polish(args: argparse.Namespace) -> None:
    post_dir = resolve_post_dir(args.post_dir)
    language = normalize_language(args.language)
    markdown_path = source_markdown_path(post_dir) if args.file is None else Path(args.file).resolve()
    markdown = markdown_path.read_text(encoding="utf-8")
    prompt = polish_prompt(markdown, post_dir, language)
    prompt_path = prompts_dir(post_dir) / f"polish.{language}.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    output_path = markdown_path if args.in_place else (Path(args.output).resolve() if args.output else markdown_path.with_name(markdown_path.stem + ".polished.md"))
    if args.run_codex:
        run_codex(prompt, output_path)
        print(f"wrote polished Markdown: {output_path}")
    else:
        print(f"wrote polish prompt: {prompt_path}")
        print(f"run with: codex exec --cd {ROOT_DIR} --output-last-message {output_path} - < {prompt_path}")


def cmd_translate(args: argparse.Namespace) -> None:
    post_dir = resolve_post_dir(args.post_dir)
    source_language = normalize_language(args.source_language)
    targets = [normalize_language(target) for target in args.targets]
    unsupported = [target for target in [source_language, *targets] if target not in TRILINGUAL_LANGUAGES]
    if unsupported:
        raise LazyBlogError("sync translate currently supports only ja, zh, and en")

    source_path = Path(args.source_file).resolve() if args.source_file else source_markdown_path(post_dir)
    markdown = source_path.read_text(encoding="utf-8")
    for target_language in targets:
        prompt = translation_prompt(markdown, source_language, target_language)
        prompt_path = prompts_dir(post_dir) / f"translate.{source_language}-to-{target_language}.txt"
        output_path = translations_dir(post_dir) / f"{target_language}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        if args.run_codex:
            run_codex(prompt, output_path)
            print(f"wrote {target_language} translation: {output_path}")
        else:
            print(f"wrote translation prompt: {prompt_path}")
            print(f"run with: codex exec --cd {ROOT_DIR} --output-last-message {output_path} - < {prompt_path}")


def cmd_status(args: argparse.Namespace) -> None:
    post_dir = resolve_post_dir(args.post_dir)
    manifest = read_manifest(post_dir)
    source_path = source_markdown_path(post_dir)
    translation_files = sorted(translations_dir(post_dir).glob("*.md")) if translations_dir(post_dir).exists() else []
    status = {
        "post_dir": str(post_dir),
        "post_id": manifest.get("post_id"),
        "source_language": manifest.get("source_language"),
        "source_file": str(source_path),
        "source_exists": source_path.exists(),
        "translations": [path.stem for path in translation_files],
        "media_mappings": len(manifest.get("media", {})),
        "last_pull": manifest.get("last_pull"),
        "last_push": manifest.get("last_push"),
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LazyBlog Markdown/Post sync workflow")
    parser.add_argument("--site-url", default=None, help="WordPress site URL; defaults to WP_SITE_URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a durable sync folder for a post")
    init.add_argument("post_id")
    init.add_argument("--source-language", required=True)
    init.add_argument("--source", help="Existing Markdown export to copy into post.md")
    init.add_argument("--output", help="Output folder; defaults to content/posts/<post-id>")
    init.set_defaults(func=cmd_init)

    pull = subparsers.add_parser("pull", help="Pull a WordPress post into content/posts/<post-id>")
    pull.add_argument("post_id")
    pull.add_argument("--source-language", help="Fallback source language if plugin metadata is unavailable")
    pull.add_argument("--output", help="Output folder; defaults to content/posts/<post-id>")
    pull.set_defaults(func=cmd_pull)

    push = subparsers.add_parser("push", help="Push source Markdown and translations to WordPress")
    push.add_argument("post_dir", help="Post sync folder or post id")
    push.add_argument("--source-language", help="Override original language")
    push.add_argument("--status", help="Override WordPress post status, e.g. draft or publish")
    push.add_argument("--source-only", action="store_true")
    push.add_argument("--translations-only", action="store_true")
    push.add_argument("--dry-run", action="store_true")
    push.set_defaults(func=cmd_push)

    media = subparsers.add_parser("media", help="Download/upload image references into WordPress media")
    media.add_argument("post_dir", help="Post sync folder or post id")
    media.add_argument("--apply", action="store_true", help="Upload and rewrite Markdown files")
    media.add_argument("--remove-dead", action="store_true", help="When applying, remove image tokens that cannot be downloaded")
    media.set_defaults(func=cmd_media)

    polish = subparsers.add_parser("polish", help="Generate or run a Codex polish prompt")
    polish.add_argument("post_dir", help="Post sync folder or post id")
    polish.add_argument("--language", default="en")
    polish.add_argument("--file", help="Markdown file to polish; defaults to post.md")
    polish.add_argument("--output", help="Output Markdown path")
    polish.add_argument("--in-place", action="store_true", help="Overwrite the input file when running Codex")
    polish.add_argument("--run-codex", action="store_true")
    polish.set_defaults(func=cmd_polish)

    translate = subparsers.add_parser("translate", help="Generate or run Codex translation prompts for ja/zh/en")
    translate.add_argument("post_dir", help="Post sync folder or post id")
    translate.add_argument("--source-language", required=True)
    translate.add_argument("--to", dest="targets", nargs="+", required=True)
    translate.add_argument("--source-file", help="Markdown source; defaults to post.md")
    translate.add_argument("--run-codex", action="store_true")
    translate.set_defaults(func=cmd_translate)

    status = subparsers.add_parser("status", help="Show local sync manifest state")
    status.add_argument("post_dir", help="Post sync folder or post id")
    status.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (LazyBlogError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
