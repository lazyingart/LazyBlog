#!/usr/bin/env bash
set -euo pipefail

# This script starts only the Codex/LazyBlog local API provider.
# First-time users should run scripts/install_lazyblog_translation_api.sh so
# .env, LAZYBLOG_API_TOKEN, Codex CLI, Python, and tmux are checked together.
# OpenAI and DeepSeek provider modes in the WordPress plugin do not require this
# tmux service.

SESSION_NAME="${LAZYBLOG_TMUX_SESSION:-lazyblog-studio-live}"
ROOT_DIR="${LAZYBLOG_RUNTIME_DIR:-$HOME/webgit/LazyBlog-runtime}"
PYTHON_BIN="${LAZYBLOG_PYTHON:-$HOME/miniconda3/bin/python3.12}"
DEFAULT_HOST="${LAZYBLOG_WEBAPP_HOST:-127.0.0.1}"
DEFAULT_PORT="${LAZYBLOG_WEBAPP_PORT:-8765}"
DEFAULT_MODEL="${LAZYBLOG_WEBAPP_MODEL:-gpt-5.4}"
DEFAULT_REASONING="${LAZYBLOG_WEBAPP_REASONING:-low}"
CODEX_BIN_DIR="${LAZYBLOG_CODEX_BIN_DIR:-$HOME/.local/codex-cli/node_modules/.bin}"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Tmux session '$SESSION_NAME' already exists."
  exit 0
fi

if [ ! -d "$ROOT_DIR" ]; then
  echo "LazyBlog runtime directory not found: $ROOT_DIR" >&2
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python runtime not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

read -r -d '' INNER_COMMAND <<EOF || true
cd "$ROOT_DIR"
if [ -f "$HOME/.nvm/nvm.sh" ]; then
  source "$HOME/.nvm/nvm.sh"
fi
export PATH="$CODEX_BIN_DIR:\$PATH"
set -a
if [ -f .env ]; then
  source .env
fi
set +a
exec "$PYTHON_BIN" scripts/lazyblog_webapp.py \
  --host "\${LAZYBLOG_WEBAPP_HOST:-$DEFAULT_HOST}" \
  --port "\${LAZYBLOG_WEBAPP_PORT:-$DEFAULT_PORT}" \
  --model "\${LAZYBLOG_WEBAPP_MODEL:-$DEFAULT_MODEL}" \
  --reasoning "\${LAZYBLOG_WEBAPP_REASONING:-$DEFAULT_REASONING}"
EOF

tmux new-session -d -s "$SESSION_NAME" "bash -lc $(printf '%q' "$INNER_COMMAND")"
echo "Session '$SESSION_NAME' created. LazyBlog translation API is launching."
