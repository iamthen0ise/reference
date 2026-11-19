#!/bin/sh
# Поднимает astro preview (base /reference), гоняет check-browser.mjs, всегда гасит превью.
# Astro 7 в неинтерактивной среде уходит в фон (демон), поэтому гасим через `astro preview stop`, а не kill по PID.
# Запуск из .site/: sh scripts/check-browser.sh [порт]
set -eu

PORT="${1:-4321}"
URL="http://127.0.0.1:${PORT}"

npx astro preview --host 127.0.0.1 --port "$PORT" >/dev/null 2>&1 &
PREVIEW_PID=$!
trap 'npx astro preview stop >/dev/null 2>&1 || true; kill "$PREVIEW_PID" 2>/dev/null || true' EXIT INT TERM

i=0
until curl -fs -o /dev/null "${URL}/reference/notes/"; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "preview не поднялся на ${URL}" >&2
    exit 1
  fi
  sleep 0.5
done

node scripts/check-browser.mjs --dist dist --url "$URL" --base /reference --mermaid 32
