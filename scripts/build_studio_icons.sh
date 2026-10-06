#!/usr/bin/env bash
# Rebuild committed PNGs from the canonical SVG; not needed on the serving host.
set -euo pipefail
icon_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../web/icons" && pwd)"
command -v convert >/dev/null || { echo 'ImageMagick convert is required to rebuild icons.' >&2; exit 1; }
for icon_size in 192 512; do
  convert -background '#b83e2c' -density 384 "$icon_root/lazyblog.svg" \
    -resize "${icon_size}x${icon_size}" -alpha off -strip \
    "PNG24:$icon_root/lazyblog-${icon_size}.png"
done
