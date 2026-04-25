# Publication Workflow

`lazypub` is the public entry point for publishing Markdown to WordPress.

## Minimal Draft

```bash
./lazypub publish article.md --source-language en --status draft --dry-run
./lazypub publish article.md --source-language en --status draft
```

## Source Front Matter

```yaml
---
title: "My Article"
slug: "my-article"
source_language: "en"
categories:
  - Notes
tags:
  - lazyblog
excerpt: "Short summary."
---
```

## Reviewed Translations

```bash
./lazypub publish article.md \
  --source-language en \
  --translation ja=translations/article.ja.md \
  --translation zh=translations/article.zh.md \
  --status draft
```

## Media

```bash
./lazypub publish article.md \
  --upload-media \
  --remove-dead-images \
  --status draft
```

`lazypub` uploads reachable local or remote image references to WordPress media
and rewrites the archived publish copy.
