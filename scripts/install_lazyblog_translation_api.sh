#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/install_lazyblog_translation_api.sh [options]

Prepares the local LazyBlog translation API used by the Codex provider in the
LazyBlog Translations WordPress plugin.

OpenAI and DeepSeek provider modes do not need this service. Use this script
only when WordPress Settings > LazyBlog Translations > Translation provider is
set to "Codex / LazyBlog local API".

Options:
  --host <host>       API bind host (default: 127.0.0.1)
  --port <port>       API port (default: 8765)
  --model <model>     Codex model (default: gpt-5.4)
  --reasoning <level> low|medium|high|xhigh (default: low)
  --no-start          prepare files and validate dependencies, but do not start tmux
  -h, --help          show help
USAGE
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PATH="$ROOT_DIR/.env"
HOST="${LAZYBLOG_WEBAPP_HOST:-127.0.0.1}"
PORT="${LAZYBLOG_WEBAPP_PORT:-8765}"
MODEL="${LAZYBLOG_WEBAPP_MODEL:-gpt-5.4}"
REASONING="${LAZYBLOG_WEBAPP_REASONING:-low}"
START_SERVICE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --reasoning) REASONING="${2:-}"; shift 2 ;;
    --no-start) START_SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

need_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing required command: $name" >&2
    return 1
  fi
}

upsert_env() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_PATH"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_PATH"
    rm -f "$ENV_PATH.bak"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_PATH"
  fi
}

need_command python3
need_command tmux
if ! command -v codex >/dev/null 2>&1; then
  CODEX_FALLBACK="${LAZYBLOG_CODEX_BIN_DIR:-$HOME/.local/codex-cli/node_modules/.bin}/codex"
  if [[ -x "$CODEX_FALLBACK" ]]; then
    export PATH="$(dirname "$CODEX_FALLBACK"):$PATH"
  else
    echo "Missing required command: codex" >&2
    echo "Install Codex CLI or set LAZYBLOG_CODEX_BIN_DIR before using the Codex provider." >&2
    exit 1
  fi
fi

if [[ ! -f "$ENV_PATH" ]]; then
  if [[ -f "$ROOT_DIR/.env.example" ]]; then
    cp "$ROOT_DIR/.env.example" "$ENV_PATH"
  else
    touch "$ENV_PATH"
  fi
  chmod 0600 "$ENV_PATH"
fi

if ! grep -q '^LAZYBLOG_API_TOKEN=' "$ENV_PATH" || [[ -z "$(grep '^LAZYBLOG_API_TOKEN=' "$ENV_PATH" | tail -n1 | cut -d= -f2-)" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    TOKEN="$(openssl rand -hex 32)"
  else
    TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
  fi
  upsert_env "LAZYBLOG_API_TOKEN" "$TOKEN"
fi

upsert_env "LAZYBLOG_WEBAPP_HOST" "$HOST"
upsert_env "LAZYBLOG_WEBAPP_PORT" "$PORT"
upsert_env "LAZYBLOG_WEBAPP_MODEL" "$MODEL"
upsert_env "LAZYBLOG_WEBAPP_REASONING" "$REASONING"

python3 -m py_compile \
  "$ROOT_DIR/scripts/lazyblog_webapp.py" \
  "$ROOT_DIR/scripts/lazyblog_sync.py" \
  "$ROOT_DIR/scripts/lazyblog_translate.py"

cat <<EOF
LazyBlog translation API is prepared.

WordPress plugin settings for Codex provider:
  Provider: Codex / LazyBlog local API
  Endpoint on same host: http://${HOST}:${PORT}/api/translate/jobs
  Endpoint from Docker WordPress: http://host.docker.internal:${PORT}/api/translate/jobs
  Token: saved in $ENV_PATH as LAZYBLOG_API_TOKEN

OpenAI and DeepSeek provider modes do not need this local API service.
EOF

if [[ "$START_SERVICE" -eq 1 ]]; then
  exec "$ROOT_DIR/scripts/start_live_translation_api_tmux.sh"
fi
