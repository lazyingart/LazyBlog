[English](../README.md) · [中文 (简体)](README.zh-Hans.md) · [日本語](README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

# LazyBlog Translations

LazyBlog Translations 是一个轻量的 WordPress 多语言文章插件。它把原文语言和翻译内容存到文章 meta 中，并在前台渲染一个干净的悬浮语言切换器。

## 核心能力

- 为每篇文章记录原始写作语言。
- 保存不同语言的标题、正文和摘要。
- 支持 Codex/LazyBlog 本地 API、OpenAI 直连 API、DeepSeek 直连 API。
- 多个读者同时点击同一个缺失语言时，只启动一个翻译任务。
- 继续支持 Markdown 迁移过来的数学公式和 MathJax 渲染。

## 选择翻译后端

| 后端 | 适合场景 | 是否需要本地服务 | 默认模型 |
| --- | --- | --- | --- |
| Codex / LazyBlog local API | 长文章、润色、Markdown 同步、图片清理、本地自动化 | 需要 | `gpt-5.4` |
| OpenAI direct API | 从 WordPress 直接生成简单翻译 | 不需要 | `gpt-4o` |
| DeepSeek direct API | 使用 OpenAI 兼容接口的托管翻译路径 | 不需要 | `deepseek-v4-flash` |

## Codex 本地 API 安装

如果你选择 Codex 后端，可以直接使用插件仓库里的启动脚本：

```bash
git clone https://github.com/lazyingart/lazyblog-translations.git
cd lazyblog-translations
tools/install_lazyblog_translation_api.sh
```

脚本会克隆或使用 LazyBlog 工作流仓库，检查 Python、tmux、Codex CLI，创建 `.env`，生成 `LAZYBLOG_API_TOKEN`，并启动本地翻译 API。

OpenAI 和 DeepSeek 后端不需要这个本地服务。

## 插件国际化

插件已经包含 WordPress 标准 i18n 支持：

- `Text Domain: lazyblog-translations`
- `Domain Path: /languages`
- `load_plugin_textdomain()`
- `languages/lazyblog-translations.pot`

## 支持

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

