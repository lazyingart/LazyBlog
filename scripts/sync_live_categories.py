#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from export_wordpress_posts import yaml_quote
from lazyblog_sync import DEFAULT_CONTENT_DIR, ROOT_DIR, WPClient
from lazyblog_translate import LazyBlogError, load_env_file


PER_PAGE = 100


def content_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("LAZYBLOG_CONTENT_DIR")
    return (ROOT_DIR / configured).resolve() if configured else DEFAULT_CONTENT_DIR


def taxonomy_snapshot_path(posts_root: Path, value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return posts_root.parent / "taxonomy" / "categories.json"


def make_client(site_url: str | None) -> WPClient:
    load_env_file(ROOT_DIR / ".env")
    resolved_site_url = site_url or os.environ.get("WP_SITE_URL")
    if not resolved_site_url:
        raise LazyBlogError("set WP_SITE_URL in .env or pass --site-url")
    return WPClient(
        site_url=resolved_site_url,
        username=os.environ.get("WP_USERNAME"),
        app_password=os.environ.get("WP_APP_PASSWORD"),
    )


def paginated_get(client: WPClient, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        query = dict(params)
        query["per_page"] = PER_PAGE
        query["page"] = page
        payload = client.request("GET", f"{path}?{urllib.parse.urlencode(query)}")
        if not isinstance(payload, list):
            raise LazyBlogError(f"unexpected WordPress response for {path}")
        rows.extend(payload)
        if len(payload) < PER_PAGE:
            break
        page += 1
    return rows


def fetch_categories(client: WPClient) -> list[dict[str, Any]]:
    categories = paginated_get(
        client,
        "/wp-json/wp/v2/categories",
        {
            "hide_empty": "false",
            "orderby": "id",
            "order": "asc",
            "context": "edit",
        },
    )
    normalized = []
    for category in categories:
        normalized.append(
            {
                "term_id": int(category.get("id", 0)),
                "slug": str(category.get("slug") or ""),
                "name": str(category.get("name") or ""),
                "parent": int(category.get("parent") or 0),
                "description": str(category.get("description") or ""),
                "count": int(category.get("count") or 0),
                "link": str(category.get("link") or ""),
            }
        )
    normalized.sort(key=lambda item: int(item["term_id"]))
    return normalized


def fetch_posts(client: WPClient, status: str) -> list[dict[str, Any]]:
    return paginated_get(
        client,
        "/wp-json/wp/v2/posts",
        {
            "status": status,
            "orderby": "id",
            "order": "asc",
            "context": "edit",
        },
    )


def split_front_matter_text(text: str) -> tuple[list[str], str] | None:
    if not text.startswith("---\n") or "\n---\n" not in text:
        return None
    header, body = text.split("\n---\n", 1)
    return header.splitlines()[1:], body


def key_for_line(line: str) -> str | None:
    if line.startswith((" ", "\t")) or ":" not in line:
        return None
    return line.split(":", 1)[0].strip()


def list_block(key: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"{key}:"] + [f"  - {yaml_quote(value)}" for value in values]


def rewrite_front_matter(
    path: Path,
    *,
    categories: list[str],
    scalar_updates: dict[str, str],
    write: bool = True,
) -> bool:
    text = path.read_text(encoding="utf-8")
    parsed = split_front_matter_text(text)
    if parsed is None:
        return False

    lines, body = parsed
    rewritten: list[str] = []
    applied_categories = False
    applied_scalars: set[str] = set()
    i = 0

    while i < len(lines):
        line = lines[i]
        key = key_for_line(line)
        if key == "categories":
            rewritten.extend(list_block("categories", categories))
            applied_categories = True
            i += 1
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                i += 1
            continue
        if key in scalar_updates:
            value = scalar_updates[key]
            if value:
                rewritten.append(f"{key}: {yaml_quote(value)}")
            applied_scalars.add(key)
            i += 1
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                i += 1
            continue
        rewritten.append(line)
        i += 1

    if not applied_categories and categories:
        insert_at = next(
            (index for index, line in enumerate(rewritten) if key_for_line(line) in {"source_language", "language"}),
            len(rewritten),
        )
        rewritten[insert_at:insert_at] = list_block("categories", categories)

    for key, value in scalar_updates.items():
        if key not in applied_scalars and value:
            rewritten.append(f"{key}: {yaml_quote(value)}")

    new_text = "---\n" + "\n".join(rewritten) + "\n---\n" + body
    if new_text == text:
        return False
    if write:
        path.write_text(new_text, encoding="utf-8")
    return True


def update_manifest(
    manifest_path: Path,
    *,
    category_ids: list[int],
    category_slugs: list[str],
    categories: list[str],
) -> bool:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    manifest["categories"] = categories
    manifest["category_slugs"] = category_slugs
    manifest["category_ids"] = category_ids
    after = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    if before == after:
        return False
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def sync_post_categories(posts_root: Path, posts: list[dict[str, Any]], category_by_id: dict[int, dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    changed_posts: list[dict[str, Any]] = []
    missing_local: list[int] = []
    touched_files = 0

    for post in posts:
        post_id = int(post.get("id") or 0)
        if post_id <= 0:
            continue
        post_dir = posts_root / str(post_id)
        manifest_path = post_dir / "lazyblog.json"
        if not manifest_path.exists():
            missing_local.append(post_id)
            continue

        category_ids = [int(value) for value in post.get("categories", []) if int(value) in category_by_id]
        category_records = [category_by_id[category_id] for category_id in category_ids]
        category_names = [record["name"] for record in category_records]
        category_slugs = [record["slug"] for record in category_records]
        scalar_updates = {
            "link": str(post.get("link") or ""),
            "modified": str(post.get("modified") or ""),
            "status": str(post.get("status") or ""),
        }

        file_changes: list[str] = []
        if dry_run:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("categories") != category_names
                or manifest.get("category_slugs") != category_slugs
                or manifest.get("category_ids") != category_ids
            ):
                file_changes.append(str(manifest_path.relative_to(ROOT_DIR)))
            candidate_files = [post_dir / "post.md", *sorted((post_dir / "translations").glob("*.md"))]
            for path in candidate_files:
                if path.exists() and rewrite_front_matter(
                    path,
                    categories=category_names,
                    scalar_updates=scalar_updates,
                    write=False,
                ):
                    file_changes.append(str(path.relative_to(ROOT_DIR)))
        else:
            if update_manifest(
                manifest_path,
                category_ids=category_ids,
                category_slugs=category_slugs,
                categories=category_names,
            ):
                touched_files += 1
                file_changes.append(str(manifest_path.relative_to(ROOT_DIR)))
            candidate_files = [post_dir / "post.md", *sorted((post_dir / "translations").glob("*.md"))]
            for path in candidate_files:
                if path.exists() and rewrite_front_matter(path, categories=category_names, scalar_updates=scalar_updates):
                    touched_files += 1
                    file_changes.append(str(path.relative_to(ROOT_DIR)))

        if file_changes:
            changed_posts.append(
                {
                    "post_id": post_id,
                    "categories": category_names,
                    "category_slugs": category_slugs,
                    "files": file_changes,
                }
            )

    return {
        "posts_seen": len(posts),
        "changed_posts": changed_posts,
        "changed_post_count": len(changed_posts),
        "missing_local_posts": missing_local,
        "touched_files": touched_files,
    }


def write_category_snapshot(path: Path, client: WPClient, categories: list[dict[str, Any]], dry_run: bool) -> bool:
    stable_payload = {
        "version": 1,
        "source": client.site_url,
        "taxonomy": "category",
        "categories": categories,
    }
    existing_payload = None
    if path.exists():
        try:
            existing_payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_payload = None

    if isinstance(existing_payload, dict):
        existing_stable = {key: existing_payload.get(key) for key in stable_payload}
        if existing_stable == stable_payload:
            return False

    payload = dict(stable_payload)
    payload["synced_at"] = int(time.time())
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync live WordPress category terms and post assignments into LazyBlog content.")
    parser.add_argument("--site-url", help="WordPress site URL; defaults to WP_SITE_URL")
    parser.add_argument("--status", default=os.environ.get("WP_POST_STATUS", "publish"), help="Post status to scan, default: publish")
    parser.add_argument("--content-dir", help="Local posts directory, default: LAZYBLOG_CONTENT_DIR or content/posts")
    parser.add_argument("--taxonomy-output", help="Category snapshot path, default: content/taxonomy/categories.json")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing files")
    return parser


def main() -> int:
    load_env_file(ROOT_DIR / ".env")
    parser = build_parser()
    args = parser.parse_args()
    try:
        posts_root = content_root(args.content_dir)
        snapshot_path = taxonomy_snapshot_path(posts_root, args.taxonomy_output)
        client = make_client(args.site_url)
        categories = fetch_categories(client)
        category_by_id = {int(category["term_id"]): category for category in categories}
        posts = fetch_posts(client, args.status)
        snapshot_changed = write_category_snapshot(snapshot_path, client, categories, args.dry_run)
        post_summary = sync_post_categories(posts_root, posts, category_by_id, args.dry_run)
        summary = {
            "dry_run": args.dry_run,
            "site_url": client.site_url,
            "status": args.status,
            "posts_root": str(posts_root),
            "category_snapshot": str(snapshot_path),
            "category_count": len(categories),
            "snapshot_changed": snapshot_changed,
            **post_summary,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except LazyBlogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
