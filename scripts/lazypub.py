#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lazyblog_sync import WPClient, migrate_markdown_media
from lazyblog_translate import LazyBlogError, first_heading, load_env_file, markdown_to_html


ROOT_DIR = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = ROOT_DIR / "content" / "lazypub"

LANGUAGE_ALIASES = {
    "original": "original",
    "en": "en",
    "en-us": "en",
    "english": "en",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "jianti": "zh",
    "simplified": "zh",
    "simplified-chinese": "zh",
    "zh-hant": "zh-hant",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "fanti": "zh-hant",
    "traditional": "zh-hant",
    "traditional-chinese": "zh-hant",
    "ja": "ja",
    "ja-jp": "ja",
    "jp": "ja",
    "japanese": "ja",
    "ko": "ko",
    "ko-kr": "ko",
    "korean": "ko",
    "vi": "vi",
    "vi-vn": "vi",
    "vietnamese": "vi",
    "ar": "ar",
    "arabic": "ar",
    "fr": "fr",
    "fr-fr": "fr",
    "french": "fr",
    "es": "es",
    "es-es": "es",
    "spanish": "es",
    "de": "de",
    "de-de": "de",
    "deutsch": "de",
    "german": "de",
    "ru": "ru",
    "ru-ru": "ru",
    "russian": "ru",
}

LANGUAGE_LABELS = {
    "en": "English",
    "zh": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "vi": "Vietnamese",
    "ar": "Arabic",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "ru": "Russian",
}


def normalize_language(value: str) -> str:
    key = value.strip().lower().replace("_", "-")
    if key in LANGUAGE_ALIASES and LANGUAGE_ALIASES[key] != "original":
        return LANGUAGE_ALIASES[key]
    raise LazyBlogError(f"unsupported language: {value}")


def infer_language_from_path(path: Path) -> str:
    tokens = [path.stem, *re.split(r"[._-]+", path.stem)]
    for token in reversed(tokens):
        try:
            return normalize_language(token)
        except LazyBlogError:
            continue
    raise LazyBlogError(
        f"cannot infer translation language from {path}; pass --translation LANG={path}"
    )


def unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value.replace("''", "'")


def split_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n") or "\n---\n" not in markdown:
        return {}, markdown

    header, body = markdown.split("\n---\n", 1)
    front_matter: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in header.splitlines()[1:]:
        line = raw_line.rstrip()
        if not line:
            continue
        if current_list_key and line.startswith("  - "):
            front_matter.setdefault(current_list_key, []).append(unquote_yaml_scalar(line[4:]))
            continue
        current_list_key = None
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            front_matter[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            try:
                decoded = json.loads(value)
                front_matter[key] = decoded if isinstance(decoded, list) else value
            except json.JSONDecodeError:
                front_matter[key] = [item.strip() for item in value.strip("[]").split(",") if item.strip()]
        else:
            front_matter[key] = unquote_yaml_scalar(value)

    return front_matter, body.lstrip("\n")


def list_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def slugify(value: str, fallback: str = "post") -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered, flags=re.UNICODE)
    lowered = re.sub(r"[\s_-]+", "-", lowered, flags=re.UNICODE).strip("-")
    return lowered or fallback


def make_client(args: argparse.Namespace) -> WPClient:
    load_env_file(ROOT_DIR / ".env")
    site_url = args.site_url or os.environ.get("WP_SITE_URL")
    username = args.username or os.environ.get("WP_USERNAME")
    app_password = args.app_password or os.environ.get("WP_APP_PASSWORD")
    if not site_url:
        raise LazyBlogError("set WP_SITE_URL in BLOG/.env or pass --site-url")
    if not args.dry_run and (not username or not app_password):
        raise LazyBlogError("set WP_USERNAME and WP_APP_PASSWORD in BLOG/.env before publishing")
    return WPClient(site_url=site_url, username=username, app_password=app_password)


def create_post(client: WPClient, payload: dict[str, Any]) -> dict[str, Any]:
    return client.request("POST", "/wp-json/wp/v2/posts", payload)


def find_or_create_term(client: WPClient, endpoint: str, name: str, create: bool) -> int:
    if name.isdigit():
        return int(name)

    query = f"search={client_quote(name)}&per_page=100&context=edit"
    rows = client.request("GET", f"/wp-json/wp/v2/{endpoint}?{query}")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("name", "")).strip().lower() == name.strip().lower():
                return int(row["id"])

    if not create:
        raise LazyBlogError(f"{endpoint[:-1]} not found and --no-create-terms was used: {name}")

    created = client.request("POST", f"/wp-json/wp/v2/{endpoint}", {"name": name})
    return int(created["id"])


def client_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value)


def term_ids(client: WPClient, endpoint: str, names: list[str], create: bool) -> list[int]:
    ids: list[int] = []
    for name in names:
        term_id = find_or_create_term(client, endpoint, name, create)
        if term_id not in ids:
            ids.append(term_id)
    return ids


def build_post_payload(
    *,
    client: WPClient,
    markdown_path: Path,
    status: str | None,
    categories: list[str],
    tags: list[str],
    create_terms: bool,
    dry_run: bool,
) -> dict[str, Any]:
    text = markdown_path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    title = str(front_matter.get("title") or first_heading(body) or markdown_path.stem)
    payload: dict[str, Any] = {
        "title": title,
        "content": markdown_to_html(text),
    }

    final_status = status or str(front_matter.get("status") or "")
    if final_status:
        payload["status"] = final_status
    if front_matter.get("slug"):
        payload["slug"] = str(front_matter["slug"])
    if front_matter.get("excerpt"):
        payload["excerpt"] = str(front_matter["excerpt"])

    category_names = [*list_from_value(front_matter.get("categories")), *categories]
    tag_names = [*list_from_value(front_matter.get("tags")), *tags]
    if dry_run:
        if category_names:
            payload["categories"] = category_names
        if tag_names:
            payload["tags"] = tag_names
    else:
        if category_names:
            payload["categories"] = term_ids(client, "categories", category_names, create_terms)
        if tag_names:
            payload["tags"] = term_ids(client, "tags", tag_names, create_terms)

    return payload


def post_id_from_inputs(args: argparse.Namespace, front_matter: dict[str, Any]) -> int | None:
    if args.post_id:
        return int(args.post_id)
    for key in ("id", "post_id", "wp_post_id"):
        if str(front_matter.get(key, "")).isdigit():
            return int(front_matter[key])
    return None


def parse_translation_spec(spec: str) -> tuple[str | None, Path]:
    if "=" in spec:
        language, path = spec.split("=", 1)
        return normalize_language(language), Path(path).expanduser().resolve()
    if ":" in spec and re.match(r"^[A-Za-z_-]+:", spec):
        language, path = spec.split(":", 1)
        return normalize_language(language), Path(path).expanduser().resolve()
    return None, Path(spec).expanduser().resolve()


def discover_translation_files(specs: list[str], translation_dir: str | None) -> list[tuple[str | None, Path]]:
    files = [parse_translation_spec(spec) for spec in specs]
    if translation_dir:
        root = Path(translation_dir).expanduser().resolve()
        files.extend((None, path) for path in sorted(root.glob("*.md")))
    return files


def translation_payload(markdown_path: Path, fallback_language: str, source_language: str) -> tuple[str, dict[str, Any]]:
    text = markdown_path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    language_value = str(front_matter.get("language") or fallback_language or "")
    language = normalize_language(language_value) if language_value else infer_language_from_path(markdown_path)
    return language, {
        "source_language": source_language,
        "title": str(front_matter.get("title") or first_heading(body) or markdown_path.stem),
        "content": markdown_to_html(text),
        "excerpt": str(front_matter.get("excerpt") or ""),
    }


def copy_inputs_to_archive(
    *,
    source: Path,
    translations: list[tuple[str | None, Path]],
    archive_dir: Path,
) -> tuple[Path, list[tuple[str | None, Path]]]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    source_target = archive_dir / "post.md"
    shutil.copyfile(source, source_target)
    copied_translations: list[tuple[str | None, Path]] = []
    translation_root = archive_dir / "translations"
    translation_root.mkdir(parents=True, exist_ok=True)
    for language, path in translations:
        target_name = f"{language or path.stem}.md"
        target = translation_root / target_name
        shutil.copyfile(path, target)
        copied_translations.append((language, target))
    return source_target, copied_translations


def translation_prompt(source_markdown: str, source_language: str, target_language: str) -> str:
    source_label = LANGUAGE_LABELS.get(source_language, source_language)
    target_label = LANGUAGE_LABELS.get(target_language, target_language)
    return f"""Translate this WordPress blog post from {source_label} to {target_label}.

Return only the complete translated Markdown file, including front matter. Do not add explanations.

Rules:
- Keep Markdown structure, links, images, code blocks, citations, equations, and front matter.
- Translate title and excerpt if present.
- Set front matter `language` to `{target_language}` and `source_language` to `{source_language}`.
- Keep the prose natural and appropriate to the original voice.
- Do not sound like AI. Avoid generic translator notes.
- Do not invent facts or add new sections that are not implied by the source.

Markdown:

{source_markdown}
"""


def run_codex_translation(source_path: Path, output_path: Path, source_language: str, target_language: str) -> None:
    prompt = translation_prompt(source_path.read_text(encoding="utf-8"), source_language, target_language)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "codex",
            "exec",
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


def maybe_migrate_media(client: WPClient, post_dir: Path, files: list[Path], remove_dead: bool, dry_run: bool) -> None:
    manifest = {"media": {}}
    for path in files:
        migrate_markdown_media(
            client=client,
            post_dir=post_dir,
            markdown_path=path,
            apply=not dry_run,
            remove_dead=remove_dead,
            manifest=manifest,
        )
    (post_dir / "lazypub-media.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_publish(args: argparse.Namespace) -> None:
    source = Path(args.markdown).expanduser().resolve()
    if not source.exists():
        raise LazyBlogError(f"source Markdown not found: {source}")

    source_text = source.read_text(encoding="utf-8")
    source_front_matter, source_body = split_front_matter(source_text)
    source_language = normalize_language(args.source_language or str(source_front_matter.get("source_language") or source_front_matter.get("language") or "en"))
    post_id = post_id_from_inputs(args, source_front_matter)
    title = str(source_front_matter.get("title") or first_heading(source_body) or source.stem)

    translation_specs = discover_translation_files(args.translation, args.translation_dir)
    archive_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else ARCHIVE_ROOT / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(title)}"
    source_for_publish = source
    translations_for_publish = translation_specs
    if not args.no_archive or args.upload_media or args.auto_translate:
        source_for_publish, translations_for_publish = copy_inputs_to_archive(
            source=source,
            translations=translation_specs,
            archive_dir=archive_dir,
        )

    for target_language_raw in args.auto_translate:
        target_language = normalize_language(target_language_raw)
        output_path = archive_dir / "translations" / f"{target_language}.md"
        if target_language == source_language:
            continue
        print(f"running Codex translation: {source_language} -> {target_language}")
        run_codex_translation(source_for_publish, output_path, source_language, target_language)
        translations_for_publish.append((target_language, output_path))

    client = make_client(args)
    if args.upload_media:
        maybe_migrate_media(
            client=client,
            post_dir=archive_dir,
            files=[source_for_publish, *[path for _, path in translations_for_publish]],
            remove_dead=args.remove_dead_images,
            dry_run=args.dry_run,
        )

    source_payload = build_post_payload(
        client=client,
        markdown_path=source_for_publish,
        status=args.status,
        categories=args.category,
        tags=args.tag,
        create_terms=not args.no_create_terms,
        dry_run=args.dry_run,
    )
    translation_payloads = [
        translation_payload(path, language or "", source_language)
        for language, path in translations_for_publish
    ]

    if args.dry_run:
        print(json.dumps(
            {
                "action": "update" if post_id else "create",
                "post_id": post_id,
                "source": {
                    "path": str(source_for_publish),
                    "title": source_payload.get("title"),
                    "status": source_payload.get("status"),
                    "source_language": source_language,
                    "content_bytes": len(str(source_payload.get("content", ""))),
                    "categories": source_payload.get("categories", []),
                    "tags": source_payload.get("tags", []),
                },
                "translations": [
                    {"language": language, "title": payload["title"], "content_bytes": len(payload["content"])}
                    for language, payload in translation_payloads
                ],
                "archive_dir": str(archive_dir) if not args.no_archive else None,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return

    if post_id:
        client.update_post(post_id, source_payload)
        print(f"updated WordPress post: {post_id}")
    else:
        if "status" not in source_payload:
            source_payload["status"] = "draft"
        created = create_post(client, source_payload)
        post_id = int(created["id"])
        print(f"created WordPress post: {post_id}")

    client.set_source_language(post_id, source_language)
    if not args.source_only:
        for language, payload in translation_payloads:
            client.update_translation(post_id, language, payload)
            print(f"pushed {language} translation for post {post_id}")

    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "lazypub.json").write_text(json.dumps(
        {
            "post_id": post_id,
            "site_url": client.site_url,
            "source_language": source_language,
            "source_path": str(source),
            "published_source_path": str(source_for_publish),
            "translations": [{"language": language, "path": str(path)} for language, path in translations_for_publish],
            "published_at": int(time.time()),
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n", encoding="utf-8")
    print(f"archive: {archive_dir}")


def teach_text() -> str:
    return f"""# LazyPub Cross-Repo Publishing

Use `lazypub` when this repo has an insight, note, experiment result, or finished article that should become a post on `blog.lazying.art`.

## Minimal publish

```bash
lazypub publish article.md --source-language en --status draft
```

`lazypub article.md` is also accepted as shorthand for `lazypub publish article.md`.

## With reviewed translations

```bash
lazypub publish article.md \\
  --source-language en \\
  --translation ja=translations/article.ja.md \\
  --translation zh=translations/article.zh.md \\
  --status draft
```

Translation files are normal Markdown. Put `language: ja` or `language: zh` in front matter, or pass the language in `LANG=path.md`.

## With Codex-generated translations

```bash
lazypub publish article.md --source-language en --auto-translate ja zh --status draft
```

## Useful front matter

```yaml
---
title: "My Insight"
slug: "my-insight"
status: "draft"
source_language: "en"
categories:
  - Research
tags:
  - notes
  - lazyblog
excerpt: "Short summary."
---
```

## Media

Use this when the Markdown has local images or third-party image URLs:

```bash
lazypub publish article.md --upload-media --remove-dead-images --status draft
```

The tool uploads reachable images to WordPress media, rewrites the archived Markdown copy, and records dead links in the archive folder.

## Where the tool lives

`lazypub` is a shell function loaded from `~/scripts/sourced_lazyblog_tools.sh` and delegates to:

```text
{ROOT_DIR}/lazypub
```

If a Codex session in this repo needs to publish, it should call `lazypub --help` first, then run `lazypub publish ... --dry-run` before real publishing.
"""


def cmd_teach(args: argparse.Namespace) -> None:
    text = teach_text()
    if args.write:
        path = Path(args.write if isinstance(args.write, str) else "LAZYPUB.md").expanduser().resolve()
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(text)


def cmd_doctor(args: argparse.Namespace) -> None:
    load_env_file(ROOT_DIR / ".env")
    checks = {
        "root": str(ROOT_DIR),
        "wp_site_url": bool(os.environ.get("WP_SITE_URL")),
        "wp_username": bool(os.environ.get("WP_USERNAME")),
        "wp_app_password": bool(os.environ.get("WP_APP_PASSWORD")),
        "codex": shutil.which("codex") is not None,
    }
    print(json.dumps(checks, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazypub",
        description="Publish Markdown from any repo to LazyBlog/WordPress, with optional translations.",
        epilog="""Examples:
  lazypub publish article.md --source-language en --status draft
  lazypub article.md --translation ja=article.ja.md --translation zh=article.zh.md --status publish
  lazypub publish notes.md --auto-translate ja zh --upload-media --remove-dead-images
  lazypub teach --write
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--site-url", help="WordPress URL; defaults to WP_SITE_URL from BLOG/.env")
    parser.add_argument("--username", help="WordPress username; defaults to WP_USERNAME from BLOG/.env")
    parser.add_argument("--app-password", help="WordPress application password; defaults to WP_APP_PASSWORD from BLOG/.env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser(
        "publish",
        help="Create or update a WordPress post from Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    publish.add_argument("markdown", help="Source Markdown file")
    publish.add_argument("--post-id", help="Update an existing post; otherwise create a new post")
    publish.add_argument("--source-language", help="Original writing language, e.g. en, zh, ja")
    publish.add_argument("--translation", action="append", default=[], help="Translation Markdown: LANG=path.md, LANG:path.md, or path.md with language front matter")
    publish.add_argument("--translation-dir", help="Directory of *.md translation files")
    publish.add_argument("--auto-translate", nargs="*", default=[], help="Run Codex to generate translation Markdown for target languages")
    publish.add_argument("--status", help="WordPress status: draft, publish, private, pending")
    publish.add_argument("--category", action="append", default=[], help="Category name or id; may be repeated")
    publish.add_argument("--tag", action="append", default=[], help="Tag name or id; may be repeated")
    publish.add_argument("--no-create-terms", action="store_true", help="Do not create missing categories/tags")
    publish.add_argument("--upload-media", action="store_true", help="Upload local/remote image references to WordPress media before publishing")
    publish.add_argument("--remove-dead-images", action="store_true", help="With --upload-media, remove image tokens that cannot be downloaded")
    publish.add_argument("--source-only", action="store_true", help="Only create/update the source post; skip translation metadata")
    publish.add_argument("--work-dir", help="Archive/work directory; defaults to BLOG/content/lazypub/<timestamp-title>")
    publish.add_argument("--no-archive", action="store_true", help="Do not copy source inputs into BLOG/content/lazypub unless needed")
    publish.add_argument("--dry-run", action="store_true", help="Print the planned payload without writing to WordPress")
    publish.set_defaults(func=cmd_publish)

    teach = subparsers.add_parser("teach", help="Print or write instructions for another repo/Codex session")
    teach.add_argument("--write", nargs="?", const="LAZYPUB.md", help="Write instructions to a file, default LAZYPUB.md")
    teach.set_defaults(func=cmd_teach)

    doctor = subparsers.add_parser("doctor", help="Check local LazyPub configuration")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] not in {"publish", "teach", "doctor", "-h", "--help"} and not sys.argv[1].startswith("--"):
        sys.argv.insert(1, "publish")

    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (LazyBlogError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
