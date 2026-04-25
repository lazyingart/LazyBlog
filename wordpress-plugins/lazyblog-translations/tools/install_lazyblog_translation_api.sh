#!/usr/bin/env bash
set -euo pipefail

# Bootstrap helper for users who discover the Codex provider from the plugin
# repository. The API service lives in the LazyBlog workflow repo.

LAZYBLOG_REPO="${LAZYBLOG_REPO:-https://github.com/lazyingart/LazyBlog.git}"
LAZYBLOG_DIR="${LAZYBLOG_DIR:-$HOME/LazyBlog}"

if [[ ! -d "$LAZYBLOG_DIR/.git" ]]; then
  git clone "$LAZYBLOG_REPO" "$LAZYBLOG_DIR"
fi

cd "$LAZYBLOG_DIR"
exec scripts/install_lazyblog_translation_api.sh "$@"
