[English](../README.md) · [中文 (简体)](README.zh-Hans.md) · [日本語](README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

# LazyBlog Translations

LazyBlog Translations は、WordPress の投稿に軽量な多言語レイヤーを追加するプラグインです。原文言語と翻訳を投稿 meta に保存し、フロントエンドでは小さなフローティング言語スイッチャーを表示します。

## 主な機能

- 投稿ごとの原文言語を保存。
- 言語ごとのタイトル、本文、抜粋を保存。
- Codex/LazyBlog ローカル API、OpenAI 直接 API、DeepSeek 直接 API を選択可能。
- 同じ投稿と言語への重複翻訳ジョブを防止。
- Markdown 由来の数式と MathJax レンダリングを維持。

## 翻訳プロバイダー

| プロバイダー | 向いている用途 | ローカルサービス | 既定モデル |
| --- | --- | --- | --- |
| Codex / LazyBlog local API | 長い投稿、推敲、Markdown 同期、画像整理、ローカル自動化 | 必要 | `gpt-5.4` |
| OpenAI direct API | WordPress からのシンプルなオンデマンド翻訳 | 不要 | `gpt-4o` |
| DeepSeek direct API | OpenAI 互換のホスト型翻訳経路 | 不要 | `deepseek-v4-flash` |

## Codex ローカル API セットアップ

Codex プロバイダーを使う場合は、プラグインリポジトリのブートストラップを実行できます。

```bash
git clone https://github.com/lazyingart/lazyblog-translations.git
cd lazyblog-translations
tools/install_lazyblog_translation_api.sh
```

このスクリプトは LazyBlog ワークフローリポジトリを用意し、Python、tmux、Codex CLI、`.env`、`LAZYBLOG_API_TOKEN` を確認して、ローカル翻訳 API を起動します。

OpenAI と DeepSeek の直接 API モードでは、このローカルサービスは不要です。

## 国際化

プラグインには WordPress 標準の i18n が含まれます。

- `Text Domain: lazyblog-translations`
- `Domain Path: /languages`
- `load_plugin_textdomain()`
- `languages/lazyblog-translations.pot`

## Support

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

