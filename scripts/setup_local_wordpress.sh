#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

docker compose up -d db wordpress

printf 'waiting for local WordPress'
installed=0
for _ in $(seq 1 60); do
  if docker compose run --rm wpcli wp core is-installed --allow-root >/dev/null 2>&1; then
    printf '\n'
    installed=1
    break
  fi

  if docker compose run --rm wpcli wp core version --allow-root >/dev/null 2>&1; then
    printf '\n'
    docker compose run --rm wpcli wp core install \
      --url=http://localhost:8088 \
      --title=LazyBlog \
      --admin_user=admin \
      --admin_password=admin \
      --admin_email=admin@example.test \
      --skip-email \
      --allow-root
    installed=1
    break
  fi

  printf '.'
  sleep 3
done

if [ "$installed" -ne 1 ]; then
  printf '\nerror: local WordPress did not become ready in time\n' >&2
  exit 1
fi

docker compose run --rm wpcli wp rewrite structure '/html/%category%/%post_id%/%postname%.html' --hard --allow-root
docker compose run --rm wpcli sh -lc 'wp theme is-installed twentyfifteen --allow-root || wp theme install twentyfifteen --allow-root'
docker compose run --rm wpcli wp theme activate twentyfifteen --allow-root
docker compose run --rm wpcli wp plugin activate lazyblog-translations --allow-root
docker compose run --rm wpcli sh -lc 'wp plugin is-installed wp-quicklatex --skip-plugins=wp-quicklatex --allow-root || wp plugin install wp-quicklatex --skip-plugins=wp-quicklatex --allow-root'
docker compose run --rm -T -v "$ROOT_DIR:/lazyblog:ro" wpcli wp eval-file /lazyblog/scripts/configure_local_quicklatex.php --skip-plugins=wp-quicklatex --allow-root
docker compose run --rm wpcli wp plugin activate wp-quicklatex --allow-root

cat <<'EOF'
Local WordPress is ready:
  URL:      http://localhost:8088
  Admin:    http://localhost:8088/wp-admin
  Username: admin
  Password: admin

Twenty Fifteen is activated to match blog.lazying.art.
LazyBlog Translations is activated.
WP QuickLaTeX is activated and configured with the live-site settings.
EOF
