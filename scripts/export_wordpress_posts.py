#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


def sanitize_slug(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-") or "post"


def sanitize_filename(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-") or "file"


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(value or ""))).strip()


def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        attr_map = dict(attrs)
        src = attr_map.get("src")
        alt = attr_map.get("alt") or ""
        if src:
            self.images.append((src, alt))


class HTMLToMarkdownParser(HTMLParser):
    def __init__(self, image_map: dict[str, str], base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.image_map = image_map
        self.base_url = base_url
        self.parts: list[str] = []
        self.list_stack: list[dict[str, int | str]] = []
        self.link_stack: list[str | None] = []
        self.blockquote_depth = 0
        self.pending_prefix = ""
        self.line_start = True
        self.in_pre = False
        self.inline_code_depth = 0

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text + "\n"

    def _tail(self) -> str:
        return "".join(self.parts[-6:])

    def _ensure_newlines(self, count: int) -> None:
        tail = self._tail()
        existing = len(tail) - len(tail.rstrip("\n"))
        if existing < count:
            self.parts.append("\n" * (count - existing))
        self.line_start = True

    def _write_prefix_if_needed(self) -> None:
        if not self.line_start:
            return
        prefix = ""
        if self.blockquote_depth:
            prefix += "> " * self.blockquote_depth
        if self.pending_prefix:
            prefix += self.pending_prefix
            self.pending_prefix = ""
        if prefix:
            self.parts.append(prefix)
        self.line_start = False

    def _write(self, text: str) -> None:
        if not text:
            return
        if self.in_pre:
            self.parts.append(text)
            self.line_start = text.endswith("\n")
            return
        if self.inline_code_depth == 0:
            text = re.sub(r"\s+", " ", text)
        if not text.strip():
            if self.parts and not self._tail().endswith((" ", "\n")):
                self.parts.append(" ")
            return
        self._write_prefix_if_needed()
        self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag in {"p", "div", "section", "article"}:
            self._ensure_newlines(2)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._ensure_newlines(2)
            self.pending_prefix = "#" * int(tag[1]) + " "
        elif tag in {"strong", "b"}:
            self._write("**")
        elif tag in {"em", "i"}:
            self._write("*")
        elif tag == "br":
            self.parts.append("\n")
            self.line_start = True
        elif tag == "hr":
            self._ensure_newlines(2)
            self.parts.append("---\n\n")
            self.line_start = True
        elif tag == "pre":
            self._ensure_newlines(2)
            self.parts.append("```\n")
            self.in_pre = True
            self.line_start = True
        elif tag == "code":
            if not self.in_pre:
                self.inline_code_depth += 1
                self._write("`")
        elif tag == "blockquote":
            self._ensure_newlines(2)
            self.blockquote_depth += 1
        elif tag in {"ul", "ol"}:
            self._ensure_newlines(2)
            self.list_stack.append({"type": tag, "index": 1})
        elif tag == "li":
            self._ensure_newlines(1)
            depth = max(len(self.list_stack), 1)
            indent = "  " * (depth - 1)
            if self.list_stack and self.list_stack[-1]["type"] == "ol":
                number = int(self.list_stack[-1]["index"])
                self.list_stack[-1]["index"] = number + 1
                bullet = f"{number}. "
            else:
                bullet = "- "
            self.pending_prefix = indent + bullet
        elif tag == "a":
            self._write("[")
            href = attr_map.get("href")
            self.link_stack.append(urllib.parse.urljoin(self.base_url, href) if href else None)
        elif tag == "img":
            src = attr_map.get("src")
            alt = (attr_map.get("alt") or "").replace("\n", " ").strip()
            if not src:
                return
            resolved = urllib.parse.urljoin(self.base_url, src)
            target = (
                self.image_map.get(src)
                or self.image_map.get(resolved)
                or src
            )
            self._write(f"![{alt}]({target})")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "article"}:
            self._ensure_newlines(2)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._ensure_newlines(2)
        elif tag in {"strong", "b"}:
            self._write("**")
        elif tag in {"em", "i"}:
            self._write("*")
        elif tag == "pre":
            if not self._tail().endswith("\n"):
                self.parts.append("\n")
            self.parts.append("```\n\n")
            self.in_pre = False
            self.line_start = True
        elif tag == "code":
            if not self.in_pre and self.inline_code_depth > 0:
                self._write("`")
                self.inline_code_depth -= 1
        elif tag == "blockquote":
            self.blockquote_depth = max(self.blockquote_depth - 1, 0)
            self._ensure_newlines(2)
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._ensure_newlines(2)
        elif tag == "li":
            self._ensure_newlines(1)
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else None
            if href:
                self._write(f"]({href})")
            else:
                self._write("]")

    def handle_data(self, data: str) -> None:
        self._write(data)

    def handle_entityref(self, name: str) -> None:
        self._write(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._write(html.unescape(f"&#{name};"))


@dataclass
class WPClient:
    site_url: str
    username: str | None
    app_password: str | None
    timeout: int = 8

    def _build_url(self, path: str, query: dict[str, str] | None = None) -> str:
        base = self.site_url.rstrip("/")
        url = urllib.parse.urljoin(base + "/", path.lstrip("/"))
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        return url

    def _request(self, url: str) -> urllib.request.Request:
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/json")
        request.add_header(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 LazyBlogExporter/1.0",
        )
        if self.username and self.app_password:
            token = f"{self.username}:{self.app_password}".encode("utf-8")
            request.add_header("Authorization", "Basic " + base64.b64encode(token).decode("ascii"))
        return request

    def get_json(self, path: str, query: dict[str, str] | None = None) -> tuple[object, dict[str, str]]:
        url = self._build_url(path, query)
        request = self._request(url)
        attempts = 5
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=max(self.timeout, 60)) as response:
                    payload = response.read().decode("utf-8")
                    headers = dict(response.headers.items())
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
            except urllib.error.URLError as exc:
                if attempt == attempts:
                    raise RuntimeError(f"request failed for {url}: {exc}") from exc
                time.sleep(attempt)
            except TimeoutError as exc:
                if attempt == attempts:
                    raise RuntimeError(f"request timed out for {url}: {exc}") from exc
                time.sleep(attempt)
        return json.loads(payload), headers

    def download(self, url: str) -> tuple[bytes, str | None]:
        request = self._request(url)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"download failed for {url}: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"download timed out for {url}: {exc}") from exc


def fetch_posts(client: WPClient, status: str) -> list[dict]:
    page = 1
    posts: list[dict] = []
    while True:
        payload, headers = client.get_json(
            "/wp-json/wp/v2/posts",
            {
                "per_page": "10",
                "page": str(page),
                "_embed": "1",
                "orderby": "date",
                "order": "asc",
                "status": status,
            },
        )
        batch = list(payload)
        posts.extend(batch)
        total_pages = int(headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return posts


def collect_terms(post: dict) -> tuple[list[str], list[str]]:
    categories: list[str] = []
    tags: list[str] = []
    for group in post.get("_embedded", {}).get("wp:term", []):
        for term in group:
            taxonomy = term.get("taxonomy")
            name = term.get("name")
            if not name:
                continue
            if taxonomy == "category":
                categories.append(name)
            elif taxonomy == "post_tag":
                tags.append(name)
    return categories, tags


def guess_extension(filename: str, content_type: str | None) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return guessed
    return ".bin"


def post_id_value(post: dict) -> int | str:
    return post.get("id", "unknown")


def post_slug_value(post: dict) -> str:
    return post.get("slug", "") or sanitize_slug(strip_html(post.get("title", {}).get("rendered", "")))


@dataclass
class DownloadLogger:
    log_dir: Path

    def __post_init__(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.all_images_log = self.log_dir / "image-downloads.jsonl"
        self.dead_images_log = self.log_dir / "dead-images.csv"
        self.all_images_log.write_text("", encoding="utf-8")
        self.dead_images_log.write_text(
            "post_id,post_slug,image_url,status,detail,local_path\n",
            encoding="utf-8",
        )

    def _cell(self, value: str) -> str:
        return json.dumps(value.replace("\r", " ").replace("\n", " ").strip(), ensure_ascii=False)

    def log(
        self,
        *,
        post: dict,
        image_url: str,
        status: str,
        detail: str = "",
        local_path: str = "",
    ) -> None:
        payload = {
            "post_id": post_id_value(post),
            "post_slug": post_slug_value(post),
            "image_url": image_url,
            "status": status,
            "detail": detail,
            "local_path": local_path,
        }
        with self.all_images_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        if status != "saved":
            row = [
                self._cell(str(payload["post_id"])),
                self._cell(payload["post_slug"]),
                self._cell(payload["image_url"]),
                self._cell(payload["status"]),
                self._cell(payload["detail"]),
                self._cell(payload["local_path"]),
            ]
            with self.dead_images_log.open("a", encoding="utf-8") as handle:
                handle.write(",".join(row) + "\n")


def should_mark_host_dead(message: str) -> bool:
    markers = (
        "Name or service not known",
        "Temporary failure in name resolution",
        "nodename nor servname provided",
        "No address associated with hostname",
        "Connection refused",
        "timed out",
    )
    return any(marker in message for marker in markers)


def download_images(
    client: WPClient,
    site_url: str,
    post: dict,
    post_dir: Path,
    logger: DownloadLogger,
    dead_hosts: dict[str, str],
) -> dict[str, str]:
    content_html = post.get("content", {}).get("rendered", "") or ""
    collector = ImageCollector()
    collector.feed(content_html)

    featured = post.get("_embedded", {}).get("wp:featuredmedia", [])
    if featured:
        source_url = featured[0].get("source_url")
        alt_text = featured[0].get("alt_text") or "featured-image"
        if source_url:
            collector.images.append((source_url, alt_text))

    image_dir = post_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_map: dict[str, str] = {}
    used_names: set[str] = set()

    for index, (raw_src, alt) in enumerate(collector.images, start=1):
        resolved = urllib.parse.urljoin(site_url, raw_src)
        if raw_src in image_map or resolved in image_map:
            logger.log(post=post, image_url=resolved, status="already-mapped")
            continue
        parsed = urllib.parse.urlparse(resolved)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            message = f"unsupported URL scheme: {parsed.scheme}"
            logger.log(post=post, image_url=resolved, status="unsupported-scheme", detail=message)
            print(f"warning: post {post_id_value(post)} {message} for {resolved}", file=sys.stderr)
            continue
        host = parsed.hostname or ""
        if host and host in dead_hosts:
            message = f"skipped because host previously failed: {dead_hosts[host]}"
            logger.log(post=post, image_url=resolved, status="skipped-dead-host", detail=message)
            print(f"warning: post {post_id_value(post)} {message} for {resolved}", file=sys.stderr)
            continue
        base_name = sanitize_filename(Path(parsed.path).name or f"image-{index}")
        try:
            binary, content_type = client.download(resolved)
        except RuntimeError as exc:
            detail = str(exc)
            if host and should_mark_host_dead(detail):
                dead_hosts[host] = detail
            logger.log(post=post, image_url=resolved, status="download-failed", detail=detail)
            print(
                f"warning: post {post_id_value(post)} image download failed for {resolved}: {detail}",
                file=sys.stderr,
            )
            continue
        extension = guess_extension(base_name, content_type)
        stem = sanitize_filename(Path(base_name).stem or f"image-{index}")
        candidate = stem + extension
        counter = 2
        while candidate in used_names:
            candidate = f"{stem}-{counter}{extension}"
            counter += 1
        used_names.add(candidate)
        output_path = image_dir / candidate
        output_path.write_bytes(binary)
        relative = f"images/{candidate}"
        image_map[raw_src] = relative
        image_map[resolved] = relative
        logger.log(post=post, image_url=resolved, status="saved", local_path=relative)
        if alt:
            image_map[urllib.parse.unquote(raw_src)] = relative
    return image_map


def build_front_matter(post: dict, categories: list[str], tags: list[str], featured_image: str | None) -> str:
    title = strip_html(post.get("title", {}).get("rendered", ""))
    author = ""
    authors = post.get("_embedded", {}).get("author", [])
    if authors:
        author = authors[0].get("name", "")

    lines = [
        "---",
        f"id: {post.get('id')}",
        f"title: {yaml_quote(title)}",
        f"slug: {yaml_quote(post.get('slug', ''))}",
        f"date: {yaml_quote(post.get('date', ''))}",
        f"modified: {yaml_quote(post.get('modified', ''))}",
        f"status: {yaml_quote(post.get('status', ''))}",
        f"link: {yaml_quote(post.get('link', ''))}",
    ]
    if author:
        lines.append(f"author: {yaml_quote(author)}")
    if categories:
        lines.append("categories:")
        lines.extend(f"  - {yaml_quote(item)}" for item in categories)
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {yaml_quote(item)}" for item in tags)
    if featured_image:
        lines.append(f"featured_image: {yaml_quote(featured_image)}")
    lines.append("---")
    return "\n".join(lines)


def export_post(
    client: WPClient,
    site_url: str,
    post: dict,
    output_dir: Path,
    logger: DownloadLogger,
    dead_hosts: dict[str, str],
) -> None:
    post_id = int(post["id"])
    slug = sanitize_slug(post.get("slug") or strip_html(post.get("title", {}).get("rendered", "")))
    date_prefix = (post.get("date") or "undated")[:10]
    folder_name = sanitize_slug(f"{date_prefix}-{slug}-{post_id}")
    post_dir = output_dir / folder_name
    post_dir.mkdir(parents=True, exist_ok=True)

    image_map = download_images(client, site_url, post, post_dir, logger, dead_hosts)
    categories, tags = collect_terms(post)
    featured_image = None
    featured = post.get("_embedded", {}).get("wp:featuredmedia", [])
    if featured:
        source_url = featured[0].get("source_url")
        if source_url:
            featured_image = image_map.get(source_url)

    content_html = post.get("content", {}).get("rendered", "") or ""
    parser = HTMLToMarkdownParser(image_map=image_map, base_url=site_url)
    parser.feed(content_html)
    markdown_body = parser.markdown()
    front_matter = build_front_matter(post, categories, tags, featured_image)

    (post_dir / "index.md").write_text(front_matter + "\n\n" + markdown_body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export WordPress posts to Markdown folders with images.")
    parser.add_argument("--site-url", required=True, help="Base WordPress site URL, e.g. https://example.com")
    parser.add_argument("--username", help="WordPress username for Application Password auth")
    parser.add_argument("--app-password", help="WordPress Application Password")
    parser.add_argument("--status", default="publish", help="Post status to request, default: publish")
    parser.add_argument("--output-dir", required=True, help="Directory to write exported posts into")
    parser.add_argument("--timeout", type=int, default=8, help="HTTP timeout in seconds, default: 8")
    args = parser.parse_args()

    username = args.username or os.environ.get("WP_EXPORT_USERNAME")
    app_password = args.app_password or os.environ.get("WP_EXPORT_APP_PASSWORD")

    client = WPClient(
        site_url=args.site_url,
        username=username,
        app_password=app_password,
        timeout=args.timeout,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = DownloadLogger(output_dir.parent / "export-logs")
    dead_hosts: dict[str, str] = {}

    try:
        posts = fetch_posts(client, args.status)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for post in posts:
        try:
            export_post(client, args.site_url, post, output_dir, logger, dead_hosts)
            print(f"exported post {post['id']} -> {post.get('slug', post['id'])}", flush=True)
        except RuntimeError as exc:
            print(f"error: failed to export post {post.get('id')}: {exc}", file=sys.stderr)
            return 1

    print(f"done: exported {len(posts)} posts to {output_dir}", flush=True)
    print(f"logs: {logger.all_images_log} and {logger.dead_images_log}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
