#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATUS=${1:-publish}

cd "$ROOT_DIR"

"$ROOT_DIR/scripts/setup_local_wordpress.sh"

docker compose run --rm -T \
  -v "$ROOT_DIR:/lazyblog:ro" \
  -e LAZYBLOG_LOCAL_CONTENT_DIR=/lazyblog/content/posts \
  -e LAZYBLOG_LOCAL_STATUS="$STATUS" \
  wpcli wp eval-file /lazyblog/scripts/publish_local_wordpress.php --allow-root

cat <<'EOF'
Local LazyBlog import finished.
  Site:  http://localhost:8088
  Admin: http://localhost:8088/wp-admin
EOF
