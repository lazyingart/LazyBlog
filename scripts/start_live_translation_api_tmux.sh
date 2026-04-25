#!/usr/bin/env bash
set -euo pipefail

# This script starts only the Codex/LazyBlog local API provider.
# First-time users should run scripts/install_lazyblog_translation_api.sh so
# .env, LAZYBLOG_API_TOKEN, Codex CLI, Python, and tmux are checked together.
# OpenAI and DeepSeek provider modes in the WordPress plugin do not require this
# tmux service.

SESSION_NAME="${LAZYBLOG_TMUX_SESSION:-lazyblog-studio-live}"
ROOT_DIR="${LAZYBLOG_RUNTIME_DIR:-$HOME/webgit/LazyBlog-runtime}"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

PYTHON_BIN="${LAZYBLOG_PYTHON:-$HOME/miniconda3/bin/python3}"
DEFAULT_HOST="${LAZYBLOG_WEBAPP_HOST:-127.0.0.1}"
DEFAULT_PORT="${LAZYBLOG_WEBAPP_PORT:-8765}"
DEFAULT_MODEL="${LAZYBLOG_WEBAPP_MODEL:-gpt-5.4}"
DEFAULT_REASONING="${LAZYBLOG_WEBAPP_REASONING:-low}"
CODEX_BIN_DIR="${LAZYBLOG_CODEX_BIN_DIR:-$HOME/.local/codex-cli/node_modules/.bin}"
NGROK_URL="${LAZYBLOG_NGROK_URL:-}"
NGROK_BIN="${LAZYBLOG_NGROK_BIN:-$(command -v ngrok || echo ngrok)}"
NGROK_POOLING="${LAZYBLOG_NGROK_POOLING:-0}"

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

if [ -n "$NGROK_URL" ]; then
  read -r -d '' NGROK_COMMAND <<EOF || true
cd "$ROOT_DIR"
set -a
if [ -f .env ]; then
  source .env
fi
set +a
if ! command -v "$NGROK_BIN" >/dev/null 2>&1; then
  echo "ngrok binary not found: $NGROK_BIN"
  echo "Install ngrok or set LAZYBLOG_NGROK_BIN=/path/to/ngrok, then restart tmux session '$SESSION_NAME'."
  echo "Requested tunnel: ngrok http --url=\${LAZYBLOG_NGROK_URL:-$NGROK_URL} \${LAZYBLOG_WEBAPP_PORT:-$DEFAULT_PORT}"
  exec bash
fi
NGROK_POOLING_FLAG=""
if [ "\${LAZYBLOG_NGROK_POOLING:-$NGROK_POOLING}" = "1" ] || [ "\${LAZYBLOG_NGROK_POOLING:-$NGROK_POOLING}" = "true" ]; then
  NGROK_POOLING_FLAG="--pooling-enabled"
fi
"$NGROK_BIN" http --url="\${LAZYBLOG_NGROK_URL:-$NGROK_URL}" \$NGROK_POOLING_FLAG "\${LAZYBLOG_WEBAPP_PORT:-$DEFAULT_PORT}"
status=\$?
echo "ngrok exited with status \$status. Fix the issue above, then rerun the same command from this pane."
exec bash
EOF
  tmux split-window -h -t "$SESSION_NAME":0 "bash -lc $(printf '%q' "$NGROK_COMMAND")"
  tmux select-layout -t "$SESSION_NAME":0 even-horizontal >/dev/null
  echo "Ngrok pane is forwarding https://$NGROK_URL -> http://127.0.0.1:$DEFAULT_PORT"
fi

echo "Session '$SESSION_NAME' created. LazyBlog Studio is launching."
