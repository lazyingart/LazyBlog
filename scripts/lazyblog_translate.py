#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TRANSLATIONS_DIR = ROOT_DIR / "translations"

LANGUAGE_LABELS = {
    "en": "English",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
}

LANGUAGE_ALIASES = {
    "en": "en",
    "en-us": "en",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "ja": "ja",
    "ja-jp": "ja",
    "jp": "ja",
}


class LazyBlogError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        os.environ[key] = os.path.expandvars(value)


def normalize_language(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[normalized]
    raise LazyBlogError(f"unsupported language: {value}")


def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text

    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text

    front_matter: dict[str, str] = {}
    for line in parts[0].splitlines()[1:]:
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        front_matter[key.strip()] = value.replace("''", "'")

    return front_matter, parts[1].lstrip("\n")


def first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def convert_inline_markdown(text: str) -> str:
    tokens: dict[str, str] = {}

    def token(value: str) -> str:
        key = f"%%LAZYBLOG_INLINE_{len(tokens)}%%"
        tokens[key] = value
        return key

    tokenized = re.sub(
        r"\\\((.*?)\\\)",
        lambda match: token(f"[math]{html.escape(match.group(1), quote=False)}[/math]"),
        text,
    )
    tokenized = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)",
        lambda match: token(
            f'<img alt="{html.escape(match.group(1), quote=True)}" '
            f'src="{html.escape(match.group(2), quote=True)}" />'
        ),
        tokenized,
    )
    tokenized = re.sub(
        r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)",
        lambda match: token(
            f'<a href="{html.escape(match.group(2), quote=True)}">'
            f"{html.escape(match.group(1), quote=False)}</a>"
        ),
        tokenized,
    )

    escaped = html.escape(tokenized, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    for key, value in tokens.items():
        escaped = escaped.replace(key, value)
    return escaped


def markdown_to_html(markdown: str) -> str:
    _, body = split_front_matter(markdown)
    lines = body.splitlines()
    output: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    ordered_items: list[str] = []
    code_lines: list[str] = []
    math_lines: list[str] = []
    in_code = False
    math_end: str | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append("<p>" + "<br />\n".join(convert_inline_markdown(line) for line in paragraph) + "</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            output.append("<ul>\n" + "\n".join(f"<li>{item}</li>" for item in list_items) + "\n</ul>")
            list_items = []

    def flush_ordered() -> None:
        nonlocal ordered_items
        if ordered_items:
            output.append("<ol>\n" + "\n".join(f"<li>{item}</li>" for item in ordered_items) + "\n</ol>")
            ordered_items = []

    for line in lines:
        if math_end is not None:
            if line.strip() == math_end:
                formula = html.escape("\n".join(math_lines), quote=False)
                output.append(f"[latex]\n{formula}\n[/latex]")
                math_lines = []
                math_end = None
            else:
                math_lines.append(line)
            continue

        if line.startswith("```"):
            if in_code:
                output.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                flush_ordered()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if line.strip() in {"\\[", "$$"}:
            flush_paragraph()
            flush_list()
            flush_ordered()
            math_end = "\\]" if line.strip() == "\\[" else "$$"
            math_lines = []
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            flush_ordered()
            continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush_paragraph()
            flush_list()
            flush_ordered()
            level = len(heading.group(1))
            output.append(f"<h{level}>{convert_inline_markdown(heading.group(2))}</h{level}>")
            continue

        unordered = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if unordered:
            flush_paragraph()
            flush_ordered()
            list_items.append(convert_inline_markdown(unordered.group(1)))
            continue

        ordered = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
        if ordered:
            flush_paragraph()
            flush_list()
            ordered_items.append(convert_inline_markdown(ordered.group(1)))
            continue

        if line.startswith("> "):
            flush_paragraph()
            flush_list()
            flush_ordered()
            output.append("<blockquote><p>" + convert_inline_markdown(line[2:].strip()) + "</p></blockquote>")
            continue

        paragraph.append(line)

    if in_code:
        output.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")

    flush_paragraph()
    flush_list()
    flush_ordered()
    return "\n\n".join(output).strip() + "\n"


class WPClient:
    def __init__(self, site_url: str, username: str | None, app_password: str | None) -> None:
        self.site_url = site_url.rstrip("/")
        self.username = username
        self.app_password = app_password

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.site_url + "/", path.lstrip("/"))
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "LazyBlog/0.1")
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if self.username and self.app_password:
            token = base64.b64encode(f"{self.username}:{self.app_password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LazyBlogError(f"WordPress HTTP {exc.code}: {detail}") from exc

        return json.loads(body) if body else {}


def make_client(args: argparse.Namespace) -> WPClient:
    load_env_file(ROOT_DIR / ".env")
    site_url = args.site_url or os.environ.get("WP_SITE_URL")
    if not site_url:
        raise LazyBlogError("set WP_SITE_URL in .env or pass --site-url")

    return WPClient(
        site_url=site_url,
        username=os.environ.get("WP_USERNAME"),
        app_password=os.environ.get("WP_APP_PASSWORD"),
    )


def require_auth() -> None:
    if not os.environ.get("WP_USERNAME") or not os.environ.get("WP_APP_PASSWORD"):
        raise LazyBlogError("set WP_USERNAME and WP_APP_PASSWORD in .env before writing translations")


def translation_dir(post_id: int) -> Path:
    return DEFAULT_TRANSLATIONS_DIR / str(post_id)


def write_translation_prompt(
    post_id: int,
    source_language: str,
    target_language: str,
    source_text: str,
    output_path: Path,
) -> str:
    source_label = LANGUAGE_LABELS.get(source_language, source_language)
    target_label = LANGUAGE_LABELS.get(target_language, target_language)

    return f"""Translate this WordPress blog post from {source_label} to {target_label}.

Return only the translated Markdown file. Do not explain the translation.
Keep the writing literary and natural. Preserve Markdown structure, links, images, and code blocks.
Use this front matter at the top, filling in a translated title:

---
post_id: {post_id}
source_language: {source_language}
language: {target_language}
title: ''
---

The output file should be saved as:
{output_path}

Source Markdown:

{source_text}
"""


def cmd_scaffold(args: argparse.Namespace) -> None:
    post_id = int(args.post_id)
    source_language = normalize_language(args.source_language)
    targets = [normalize_language(value) for value in args.target_languages]
    directory = translation_dir(post_id)
    directory.mkdir(parents=True, exist_ok=True)

    source_path = directory / f"source.{source_language}.md"
    if args.source:
        shutil.copyfile(args.source, source_path)
    elif not source_path.exists():
        source_path.write_text(
            f"---\npost_id: {post_id}\nsource_language: {source_language}\ntitle: ''\n---\n\n",
            encoding="utf-8",
        )

    source_text = source_path.read_text(encoding="utf-8")
    for target in targets:
        output_path = directory / f"{target}.md"
        if not output_path.exists():
            output_path.write_text(
                f"---\npost_id: {post_id}\nsource_language: {source_language}\nlanguage: {target}\ntitle: ''\n---\n\n",
                encoding="utf-8",
            )
        prompt_path = directory / f"prompt.{target}.txt"
        prompt_path.write_text(
            write_translation_prompt(post_id, source_language, target, source_text, output_path),
            encoding="utf-8",
        )

    print(f"created translation workspace: {directory}")


def cmd_draft(args: argparse.Namespace) -> None:
    post_id = int(args.post_id)
    source_language = normalize_language(args.source_language)
    target_language = normalize_language(args.target_language)
    source_path = Path(args.source)
    output_path = Path(args.output) if args.output else translation_dir(post_id) / f"{target_language}.md"
    prompt_path = Path(args.prompt_output) if args.prompt_output else translation_dir(post_id) / f"prompt.{target_language}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)

    source_text = source_path.read_text(encoding="utf-8")
    prompt = write_translation_prompt(post_id, source_language, target_language, source_text, output_path)
    prompt_path.write_text(prompt, encoding="utf-8")

    if args.run_codex:
        subprocess.run(
            [
                "codex",
                "exec",
                "--cd",
                str(ROOT_DIR),
                "--output-last-message",
                str(output_path),
                prompt,
            ],
            check=True,
        )
        print(f"drafted translation with codex: {output_path}")
    else:
        print(f"wrote prompt: {prompt_path}")
        print(f"run with: codex exec --cd {ROOT_DIR} --output-last-message {output_path} - < {prompt_path}")


def cmd_push(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    require_auth()
    client = make_client(args)
    post_id = int(args.post_id)
    language = normalize_language(args.language)
    markdown_path = Path(args.markdown)
    text = markdown_path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    source_language = args.source_language or front_matter.get("source_language")
    title = args.title or front_matter.get("title") or first_heading(body) or markdown_path.stem
    content = body if args.content_format == "html" else markdown_to_html(text)

    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "excerpt": args.excerpt or front_matter.get("excerpt", ""),
    }
    if source_language:
        payload["source_language"] = normalize_language(source_language)

    if args.dry_run:
        preview = dict(payload)
        preview["content"] = f"{len(content)} bytes"
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    response = client.request("PUT", f"/wp-json/lazyblog/v1/posts/{post_id}/translations/{language}", payload)
    updated_at = response.get("translation", {}).get("updated_at", "")
    print(f"pushed {language} translation for post {post_id}" + (f" at {updated_at}" if updated_at else ""))


def cmd_status(args: argparse.Namespace) -> None:
    client = make_client(args)
    post_id = int(args.post_id)
    response = client.request("GET", f"/wp-json/lazyblog/v1/posts/{post_id}/translations")
    print(json.dumps(response, ensure_ascii=False, indent=2))


def cmd_set_source(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    require_auth()
    client = make_client(args)
    post_id = int(args.post_id)
    source_language = normalize_language(args.source_language)
    client.request("PUT", f"/wp-json/lazyblog/v1/posts/{post_id}/translations", {"source_language": source_language})
    print(f"set source language for post {post_id}: {source_language}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LazyBlog translation workflow")
    parser.add_argument("--site-url", default=None, help="WordPress site URL; defaults to WP_SITE_URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scaffold = subparsers.add_parser("scaffold", help="Create local translation files and prompts")
    scaffold.add_argument("post_id")
    scaffold.add_argument("source_language")
    scaffold.add_argument("target_languages", nargs="+")
    scaffold.add_argument("--source", help="Existing source Markdown to copy into translations/<post_id>/")
    scaffold.set_defaults(func=cmd_scaffold)

    draft = subparsers.add_parser("draft", help="Create a translation prompt, optionally running codex exec")
    draft.add_argument("post_id")
    draft.add_argument("source_language")
    draft.add_argument("target_language")
    draft.add_argument("--source", required=True, help="Source Markdown file")
    draft.add_argument("--output", help="Target translation Markdown file")
    draft.add_argument("--prompt-output", help="Prompt file path")
    draft.add_argument("--run-codex", action="store_true", help="Run codex exec and write the result")
    draft.set_defaults(func=cmd_draft)

    push = subparsers.add_parser("push", help="Push one translated Markdown file into WordPress")
    push.add_argument("post_id")
    push.add_argument("language")
    push.add_argument("markdown")
    push.add_argument("--source-language", help="Original language for this post")
    push.add_argument("--title", help="Override translated title")
    push.add_argument("--excerpt", help="Translated excerpt")
    push.add_argument("--content-format", choices=["markdown", "html"], default="markdown")
    push.add_argument("--dry-run", action="store_true")
    push.set_defaults(func=cmd_push)

    status = subparsers.add_parser("status", help="Show translation metadata from WordPress")
    status.add_argument("post_id")
    status.set_defaults(func=cmd_status)

    set_source = subparsers.add_parser("set-source", help="Set original language for a WordPress post")
    set_source.add_argument("post_id")
    set_source.add_argument("source_language")
    set_source.set_defaults(func=cmd_set_source)

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
