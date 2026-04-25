[English](../README.md) · [中文 (简体)](README.zh-Hans.md) · [日本語](README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

# LazyBlog

LazyBlog は、Markdown、画像、カテゴリー、言語メタデータ、翻訳を
WordPress に同期するための、LazyingArt の Markdown-first ツールキットです。

[![Live Blog](https://img.shields.io/badge/Live-blog.lazying.art-111827?style=for-the-badge&logo=googlechrome&logoColor=white)](https://blog.lazying.art)
[![WordPress](https://img.shields.io/badge/WordPress-translation%20plugin-21759B?style=for-the-badge&logo=wordpress&logoColor=white)](../wordpress-plugins/lazyblog-translations)

## 主な機能

- `lazypub`: 任意のプロジェクトから Markdown を WordPress に公開します。
- `scripts/lazyblog_sync.py`: 原稿 Markdown、画像移行、翻訳同期を管理します。
- `scripts/lazyblog_webapp.py`: チャットから下書き生成とオンデマンド翻訳を行うローカル PWA/API です。
- `wordpress-plugins/lazyblog-translations/`: 翻訳を保存・表示する WordPress プラグインです。
- `docker-compose.yml`: ローカル WordPress テスト環境です。

## クイックスタート

```bash
git clone --recurse-submodules https://github.com/lazyingart/LazyBlog.git
cd LazyBlog
cp .env.example .env
$EDITOR .env
./lazypub publish article.md --source-language en --status draft --dry-run
```

翻訳付きで公開する場合:

```bash
./lazypub publish article.md \
  --source-language en \
  --translation ja=translations/article.ja.md \
  --translation zh=translations/article.zh.md \
  --status draft
```

## プラグインサブモジュール

`wordpress-plugins/lazyblog-translations/` は
https://github.com/lazyingart/lazyblog-translations を指す Git submodule です。
submodule なしで clone した場合は、次を実行してください。

```bash
git submodule update --init --recursive
```

## Support

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |
