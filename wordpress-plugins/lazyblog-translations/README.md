[English](README.md) · [中文 (简体)](i18n/README.zh-Hans.md) · [日本語](i18n/README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

<div align="center">

# LazyBlog Translations

<p>
  <strong>A vivid, lightweight multilingual layer for WordPress posts.</strong><br/>
  <sub>One language switcher, three translation engines, clean post-meta storage.</sub>
</p>

</div>

[![WordPress](https://img.shields.io/badge/WordPress-Plugin-21759B?style=for-the-badge&logo=wordpress&logoColor=white)](https://wordpress.org)
[![PHP](https://img.shields.io/badge/PHP-7.4%2B-777BB4?style=for-the-badge&logo=php&logoColor=white)](#quick-start)
[![LazyingArt LLC](https://img.shields.io/badge/LazyingArt-LLC-0f766e?style=for-the-badge)](https://lazying.art)
[![Live Blog](https://img.shields.io/badge/Live-blog.lazying.art-111827?style=for-the-badge&logo=googlechrome&logoColor=white)](https://blog.lazying.art)
[![i18n](https://img.shields.io/badge/i18n-WordPress%20Textdomain%20%2B%20README-0EA5E9?style=for-the-badge)](#internationalization)
[![Codex](https://img.shields.io/badge/Codex-Local%20API-10A37F?style=for-the-badge)](#codex-local-api-setup)
[![OpenAI](https://img.shields.io/badge/OpenAI-gpt--4o-412991?style=for-the-badge&logo=openai&logoColor=white)](#providers)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-v4--flash-1D4ED8?style=for-the-badge)](#providers)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-ea4aaa?style=for-the-badge&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

| Jump to | Link |
| --- | --- |
| Install | [Quick Start](#quick-start) |
| Choose backend | [Providers](#providers) |
| Run Codex locally | [Codex Local API Setup](#codex-local-api-setup) |
| Translate the plugin | [Internationalization](#internationalization) |
| Support | [Support](#support) |

LazyBlog Translations keeps WordPress multilingual publishing simple: each post owns its source language and translation records, and readers use a compact floating language switcher. Missing translations can be generated on demand without adopting a heavy multilingual suite.

## What It Does

- Stores source language and translations in WordPress post meta.
- Renders the existing LazyBlog-style floating language selector.
- Generates missing translations through Codex/LazyBlog, OpenAI, or DeepSeek.
- Prevents duplicate post/language jobs when multiple readers click the same draft language.
- Preserves Markdown-origin math with MathJax.
- Provides WordPress i18n plumbing through `Text Domain: lazyblog-translations` and `Domain Path: /languages`.

## Providers

| Provider | Best for | Needs local service | Default model |
| --- | --- | --- | --- |
| Codex / LazyBlog local API | Long posts, polishing, Markdown sync, image cleanup, local automation | Yes | `gpt-5.4` |
| OpenAI direct API | Simple on-demand translation from WordPress | No | `gpt-4o` |
| DeepSeek direct API | OpenAI-compatible hosted translation path | No | `deepseek-v4-flash` |

The rendering layer is provider-independent. All providers write the same translation data, so a post can be maintained by local Markdown workflows today and direct hosted APIs later.

## Quick Start

1. Upload `lazyblog-translations` to `/wp-content/plugins/`.
2. Activate `LazyBlog Translations` in WordPress.
3. Open `Settings > LazyBlog Translations`.
4. Choose `Codex / LazyBlog local API`, `OpenAI direct API`, or `DeepSeek direct API`.
5. Save credentials and provider settings.
6. Visit a post and click a missing language in the floating switcher.

## Codex Local API Setup

Use Codex mode when you want the full LazyBlog automation stack.

Plugin repo bootstrap script:

```bash
git clone https://github.com/lazyingart/lazyblog-translations.git
cd lazyblog-translations
tools/install_lazyblog_translation_api.sh
```

LazyBlog workflow repo setup:

```bash
git clone https://github.com/lazyingart/LazyBlog.git
cd LazyBlog
scripts/install_lazyblog_translation_api.sh
```

GitHub script link: https://github.com/lazyingart/lazyblog-translations/blob/main/tools/install_lazyblog_translation_api.sh

The installer checks Python, tmux, and Codex CLI, creates or updates `.env`, ensures `LAZYBLOG_API_TOKEN` exists, validates the local API, and starts the service in tmux through `scripts/start_live_translation_api_tmux.sh`.

OpenAI and DeepSeek modes do not need this service.

## Internationalization

Plugin i18n support includes:

- `Text Domain: lazyblog-translations`
- `Domain Path: /languages`
- `load_plugin_textdomain()` at runtime
- `languages/lazyblog-translations.pot` for translators
- localized GitHub readme starters in `i18n/`

Generate an updated POT after changing UI strings:

```bash
wp i18n make-pot . languages/lazyblog-translations.pot --slug=lazyblog-translations
```

## Security

- Public frontend clicks use signed, short-lived post/language requests.
- Provider keys stay server-side in WordPress options or constants.
- Browser code never receives OpenAI, DeepSeek, or LazyBlog bearer tokens.
- Do not commit `.env`, exported posts, API keys, application passwords, or deployment logs.

## Links

- LazyingArt LLC: https://lazying.art
- Live blog: https://blog.lazying.art
- Plugin source: https://github.com/lazyingart/lazyblog-translations
- LazyBlog workflow source: https://github.com/lazyingart/LazyBlog
- Maintainer: https://github.com/lachlanchen

## Support

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## Release Notes

### 0.4.8

- Lowers the declared PHP requirement to 7.4, matching the plugin code and the live WordPress host.

### 0.4.7

- Adds WordPress i18n loading and a translation template.
- Adds localized README starters.
- Adds the LazyingArt banner and donation panel.
- Adds a plugin-repo bootstrap script for Codex local API setup.
- Adds direct GitHub setup links in WordPress settings.

### 0.4.6

- Adds provider switching between Codex/LazyBlog local API, OpenAI direct API, and DeepSeek direct API.
- Adds direct provider settings for endpoints, API keys, and models.
- Documents local API installation for Codex users.

### 0.4.5

- Adds LazyingArt LLC plugin metadata and a fixed `Update URI`.
- Adds a guarded self-migration endpoint for moving accidental installs from a conflicting plugin slug into `lazyblog-translations/lazyblog-translations.php`.
