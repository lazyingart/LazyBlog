[English](README.md) · [中文 (简体)](i18n/README.zh-Hans.md) · [日本語](i18n/README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

# LazyBlog

Markdown-first tooling for publishing and maintaining a multilingual WordPress
blog with local AI workflows, media cleanup, and a lightweight translation plugin.

[![Live Blog](https://img.shields.io/badge/Live-blog.lazying.art-111827?style=for-the-badge&logo=googlechrome&logoColor=white)](https://blog.lazying.art)
[![LazyingArt](https://img.shields.io/badge/By-LazyingArt%20LLC-0f766e?style=for-the-badge)](https://lazying.art)
[![WordPress](https://img.shields.io/badge/WordPress-translation%20plugin-21759B?style=for-the-badge&logo=wordpress&logoColor=white)](wordpress-plugins/lazyblog-translations)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-ea4aaa?style=for-the-badge&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

LazyBlog is built around a simple idea: write in Markdown, keep the source of
truth close to your thinking, then publish to WordPress with images, categories,
language metadata, and reviewed translations.

## Demo Preview

![LazyBlog Studio chat-to-post preview](demos/lazyblog-studio-chat-to-post.png)

## What Is Inside

| Layer | Path | Purpose |
| --- | --- | --- |
| Markdown publishing | `lazypub`, `scripts/lazypub.py` | Publish a Markdown file from any repo into WordPress |
| Sync engine | `scripts/lazyblog_sync.py` | Maintain post source Markdown, media migration, and translations |
| Translation helper | `scripts/lazyblog_translate.py` | Scaffold and push post translations |
| Category sync | `scripts/sync_live_categories.py` | Pull WordPress category terms and post assignments into local metadata |
| Studio API | `scripts/lazyblog_webapp.py` | Mobile PWA/API for durable chat, chat-to-draft, and on-demand translation jobs |
| WordPress plugin | `wordpress-plugins/lazyblog-translations/` | Store/render translations and request missing ones asynchronously |
| Local test site | `docker-compose.yml`, `scripts/setup_local_wordpress.sh` | Run a disposable WordPress test site with the plugin mounted |
| Schemas | `schemas/` | JSON contracts for prompt-tool output |

## Quick Start

```bash
git clone --recurse-submodules https://github.com/lazyingart/LazyBlog.git
cd LazyBlog
cp .env.example .env
$EDITOR .env
python3 -m py_compile scripts/*.py
```

If you already cloned without submodules:

```bash
git submodule update --init --recursive
```

Publish a draft from any project:

```bash
./lazypub publish article.md --source-language en --status draft --dry-run
./lazypub publish article.md --source-language en --status draft
```

Publish with reviewed translations:

```bash
./lazypub publish article.md \
  --source-language en \
  --translation ja=translations/article.ja.md \
  --translation zh=translations/article.zh.md \
  --status draft
```

Generate first-pass translations with Codex:

```bash
./lazypub publish article.md \
  --source-language en \
  --auto-translate ja zh \
  --upload-media \
  --remove-dead-images \
  --status draft
```

Mirror live category names/slugs before refreshing the Docker test site:

```bash
python3 scripts/sync_live_categories.py --dry-run
python3 scripts/sync_live_categories.py
./scripts/publish_local_wordpress.sh
```

## WordPress Plugin

LazyBlog Translations is included in:

```text
wordpress-plugins/lazyblog-translations/
```

That path is a Git submodule pointing to
https://github.com/lazyingart/lazyblog-translations, so clicking it on GitHub
opens the standalone plugin repository.

It stores translations as post meta, renders a small language switcher, and can
request a missing translation from one of three providers:

- Codex through the local LazyBlog API
- OpenAI direct API
- DeepSeek direct API

Run a local WordPress test site:

```bash
docker compose up -d
scripts/setup_local_wordpress.sh
```

Prepare the local Codex translation API:

```bash
scripts/install_lazyblog_translation_api.sh --model gpt-5.6-sol --reasoning low
```

OpenAI and DeepSeek provider modes do not need the local API service.

## LazyBlog Studio

Studio uses a bright mobile layout with browser speech dictation, attachments,
safe GitHub-flavored Markdown, and KaTeX formula rendering. Unsent composer text
is backed up immediately in the browser and synchronized to an atomic server
draft. Server-sent events propagate draft changes to other logged-in devices,
while version conflicts are preserved instead of silently overwriting text.

The native microphone provides fast composer dictation; long-press opens a
device-persistent language chooser. A separate audio-message control retains a
complete recording in IndexedDB before upload and can hand it to an optional
server-side Whisper `large-v3` API. The browser receives no model credential,
and accepted recordings continue through transcription and the normal chat
queue after refresh or browser closure.

Every user and assistant message is stored as inspectable Markdown and mirrored
synchronously into an ignored SQLite WAL database. The current message table
supports efficient queries, while an append-only event table preserves edits,
processing transitions, and deleted-message snapshots. Startup reconciliation
backfills Markdown records without duplicating unchanged events.

Prompt profiles use two concurrent lanes. Fast chat defaults to `gpt-5.6-sol / low`;
drafting, revision, research, and publish preparation default to
`gpt-5.6-sol / high`. A long controlled task cannot block queued note replies.
Routing and other structured responses use medium reasoning. An optional
AgentShell route can try several local account profiles in order, retry with
`gpt-5.3-codex-spark`, and finally use AgInTi with DeepSeek when that hosted
fallback is explicitly enabled:

```text
LAZYBLOG_CODEX_ACCOUNTS=personal,company,lab
LAZYBLOG_CODEX_FALLBACK_MODEL=gpt-5.3-codex-spark
LAZYBLOG_CODEX_FALLBACK_REASONING=low
LAZYBLOG_AGINTI_DEEPSEEK_FALLBACK=false
LAZYBLOG_AGINTI_DEEPSEEK_MODEL=deepseek-v4-flash
```

AgentShell retains account credentials; only profile names enter LazyBlog.
DeepSeek remains disabled in the example because enabling it sends prompt
contents to a hosted provider.

## Multilingual Documentation

Translations live in `i18n/`:

- [中文 (简体)](i18n/README.zh-Hans.md)
- [日本語](i18n/README.ja.md)

The code and plugin support broader language metadata, including English,
Simplified Chinese, Traditional Chinese, Japanese, Korean, Vietnamese, Arabic,
French, Spanish, German, and Russian.

## Project Links

- Live blog: https://blog.lazying.art
- LazyingArt: https://lazying.art
- Storefront: https://buy.lazying.art
- Public plugin source: https://github.com/lazyingart/lazyblog-translations
- Public workflow source: https://github.com/lazyingart/LazyBlog

## Support

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

Build less. Publish better. Leave a durable trail.
