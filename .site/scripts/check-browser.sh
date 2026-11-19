#!/bin/sh
# Поднимает astro preview (base /reference), гоняет check-browser.mjs, гасит только то превью, которое поднял сам.
# Astro 7 в неинтерактивной среде уходит в фон (демон), поэтому гасим через `astro preview stop`, а не kill по PID.
# Запуск из .site/: sh scripts/check-browser.sh [порт] (по умолчанию PREVIEW_PORT или 4329)
set -eu

PORT="${1:-${PREVIEW_PORT:-4329}}"
URL="http://127.0.0.1:${PORT}"

# Порт занят — не стартуем и ничего не гасим: иначе проверили бы чужой или устаревший сервер.
# (curl без -f: любой HTTP-ответ, включая 404, значит «занято»; не ответил — порт свободен.)
if curl -s -o /dev/null --max-time 2 "${URL}/"; then
  echo "порт ${PORT} уже занят: остановите сервер или задайте другой PREVIEW_PORT" >&2
  exit 1
fi

# Число диаграмм берём из заметок, а не из константы.
MERMAID=$(node -e "import('./scripts/check-vault.mjs').then(async (m) => console.log((await m.countVault('..')).mermaid))")


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

node scripts/check-browser.mjs --dist dist --url "$URL" --base /reference --mermaid "$MERMAID"
