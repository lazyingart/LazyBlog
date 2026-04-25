[English](../README.md) · [中文 (简体)](README.zh-Hans.md) · [日本語](README.ja.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://lazying.art)

# LazyBlog

LazyBlog 是 LazyingArt 的 Markdown-first 博客工具库，用来把 Markdown、
图片、分类、语言元数据和译文同步到 WordPress。

[![Live Blog](https://img.shields.io/badge/Live-blog.lazying.art-111827?style=for-the-badge&logo=googlechrome&logoColor=white)](https://blog.lazying.art)
[![WordPress](https://img.shields.io/badge/WordPress-translation%20plugin-21759B?style=for-the-badge&logo=wordpress&logoColor=white)](../wordpress-plugins/lazyblog-translations)

## 核心能力

- `lazypub`：从任意项目发布 Markdown 到 WordPress。
- `scripts/lazyblog_sync.py`：维护文章源文件、图片迁移和译文同步。
- `scripts/lazyblog_webapp.py`：本地 PWA/API，用于聊天生成草稿和按需翻译。
- `wordpress-plugins/lazyblog-translations/`：WordPress 翻译插件，负责存储和渲染译文。
- `docker-compose.yml`：本地 WordPress 测试环境。

## 快速开始

```bash
git clone --recurse-submodules https://github.com/lazyingart/LazyBlog.git
cd LazyBlog
cp .env.example .env
$EDITOR .env
./lazypub publish article.md --source-language en --status draft --dry-run
```

带译文发布：

```bash
./lazypub publish article.md \
  --source-language en \
  --translation ja=translations/article.ja.md \
  --translation zh=translations/article.zh.md \
  --status draft
```

## 插件子模块

`wordpress-plugins/lazyblog-translations/` 是指向
https://github.com/lazyingart/lazyblog-translations 的 Git submodule。
如果已经 clone 过仓库但没有拉取 submodule，请运行：

```bash
git submodule update --init --recursive
```

## 支持

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |
