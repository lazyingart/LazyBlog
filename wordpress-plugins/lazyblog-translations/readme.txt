=== LazyBlog Translations ===
Contributors: lazyingart
Tags: multilingual, translation, markdown, lazyblog, openai, deepseek
Requires at least: 6.5
Tested up to: 6.9
Requires PHP: 7.4
Stable tag: 0.4.8
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

WordPress translation storage, provider switching, and language-switcher rendering for LazyBlog Markdown publishing workflows.

== Description ==

LazyBlog Translations stores post source language and per-language translations in WordPress post meta. It renders a lightweight floating language switcher and can request missing translations from Codex/LazyBlog, OpenAI, or DeepSeek.

The Codex/LazyBlog provider calls a local LazyBlog API service for richer Markdown publishing workflows. OpenAI and DeepSeek direct providers call hosted chat APIs from WordPress and do not require the local API service.

Links:

* LazyingArt LLC: https://lazying.art
* Live blog: https://blog.lazying.art
* GitHub: https://github.com/lazyingart/lazyblog-translations

== Features ==

* Source-language metadata for each post.
* Maintained language list: Original, Simplified Chinese, Traditional Chinese, English, Japanese, Korean, Vietnamese, Arabic, French, Spanish, German, and Russian.
* On-demand missing-translation requests with duplicate-job protection.
* Provider switch: Codex/LazyBlog local API, OpenAI direct API, or DeepSeek direct API.
* Signed frontend requests and server-side token/key forwarding.
* MathJax rendering for Markdown-origin math content.
* WordPress i18n support with text domain, domain path, and POT template.
* Admin settings for LazyBlog endpoint/token/model/reasoning, OpenAI endpoint/key/model, and DeepSeek endpoint/key/model.

== Installation ==

1. Upload the `lazyblog-translations` folder to `/wp-content/plugins/`.
2. Activate `LazyBlog Translations` from the WordPress Plugins screen.
3. Open Settings > LazyBlog Translations.
4. Choose a translation provider.
5. For Codex/LazyBlog, run `scripts/install_lazyblog_translation_api.sh` from the LazyBlog repo and configure the endpoint plus bearer token.
6. For OpenAI or DeepSeek, configure the provider API key and model. No local API service is required.
7. Save settings and purge caches if the site uses a page cache.

== Localization ==

The plugin loads `lazyblog-translations` through WordPress textdomain support and ships `languages/lazyblog-translations.pot` for translators.

GitHub README starter translations are available in `i18n/`.

== Frequently Asked Questions ==

= Does this plugin translate posts by itself? =

It can coordinate translation through three provider modes. Codex/LazyBlog mode calls the local LazyBlog API. OpenAI and DeepSeek modes call hosted chat APIs directly from WordPress and store the JSON result in post meta.

= When do I need the local API service? =

Only when using the Codex/LazyBlog provider. Run `scripts/install_lazyblog_translation_api.sh` from the LazyBlog repo. OpenAI and DeepSeek direct providers do not need tmux or a local service.

= Does this plugin replace TranslatePress, Polylang, or WPML? =

It is a narrower tool for LazyBlog-managed posts. It does not try to translate every string in a WordPress site.

= Where should secrets live? =

Use WordPress options for provider keys and local `.env` files for the LazyBlog API. Do not commit credentials to the plugin repository.

== Sponsors ==

* https://github.com/sponsors/lachlanchen
* https://github.com/sponsors/lazyingart

== Changelog ==

= 0.4.8 =
* Lowered the declared PHP requirement to 7.4 to match the plugin code and the live WordPress host.

= 0.4.7 =
* Added WordPress i18n loading, domain path, and POT template.
* Added localized GitHub README starters.
* Added LazyingArt banner, donation panel, and more vivid README presentation.
* Added plugin-repo bootstrap script for Codex local API setup.
* Added direct GitHub setup links in WordPress settings.

= 0.4.6 =
* Added provider switching between Codex/LazyBlog local API, OpenAI direct API, and DeepSeek direct API.
* Added direct provider endpoint, API key, and model settings.
* Documented local API installation for Codex users.

= 0.4.5 =
* Added LazyingArt LLC plugin metadata and fixed update identity with `Update URI`.
* Added a guarded self-migration endpoint for moving accidental installs from a conflicting plugin slug into the canonical plugin folder.

= 0.4.4 =
* Added configurable translation model and reasoning settings.
* Added live API support for on-demand translation generation.
