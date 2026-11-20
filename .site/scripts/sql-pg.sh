#!/usr/bin/env bash
# Прогон SQL на PostgreSQL 17 в Docker: up | run | block | bg | down.
# Схема и данные берутся из страницы «Схема библиотеки».
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
container="${SQL_PG_CONTAINER:-reference-pg}"
image="${SQL_PG_IMAGE:-postgres:17-alpine}"
schema_page="${SQL_PG_PAGE:-$root/Бэкенд/Базы данных/SQL-рецепты/Схема библиотеки.md}"

usage() {
  echo "usage: sql-pg.sh up [small|big] | run FILE | block PAGE TITLE | bg PAGE TITLE | down" >&2
  exit 2
}

psql_run() {
  docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 -P pager=off -U postgres -d library
}

# Печатает тело блока ```sql title="<title>"; код 1, если блока нет или он не один.
extract_block() {
  local page="$1" title="$2" n
  [ -f "$page" ] || { echo "page not found: $page" >&2; return 1; }
  n="$(awk -v t="\`\`\`sql title=\"$title\"" '$0 == t { c++ } END { print c + 0 }' "$page")"
  if [ "$n" -eq 0 ]; then echo "block not found: $title" >&2; return 1; fi
  if [ "$n" -gt 1 ]; then echo "block not unique: $title" >&2; return 1; fi
  awk -v t="\`\`\`sql title=\"$title\"" '
    $0 == t { on = 1; next }
    on && $0 == "```" { exit }
    on { print }
  ' "$page"
}

# Блок в фоновой сессии: application_name = имя блока без .sql. Возвращается, когда сессия
# видна в pg_stat_activity (до 10 с, иначе код 1). Срок жизни сессии ограничивает сам блок.
cmd_bg() {
  local page="$1" title="$2" name body file i seen
  body="$(extract_block "$page" "$title")" || return 1
  name="${title%.sql}"
  case "$name" in
    ""|*[!A-Za-z0-9_-]*) echo "bad block name for bg: $title" >&2; return 1 ;;
  esac
  file="/tmp/bg-$name-$$-$RANDOM.sql"
  printf '%s\n' "$body" | docker exec -i "$container" sh -c 'cat > "$1"' sh "$file"
  docker exec -d -e PGAPPNAME="$name" "$container" \
    psql -X -q -v ON_ERROR_STOP=1 -P pager=off -U postgres -d library -f "$file"
  for i in $(seq 1 20); do
    seen="$(docker exec "$container" psql -X -q -t -A -U postgres -d library \
      -c "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$name'")"
    if [ "$seen" -gt 0 ]; then return 0; fi
    sleep 0.5
  done
  echo "bg session not visible in 10s: $name" >&2
  return 1
}

cmd_down() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}

cmd_up() {
  local seed="${1:-small}" i
  case "$seed" in small|big) ;; *) echo "seed must be small or big" >&2; exit 2 ;; esac
  # Блоки читаем до пересоздания контейнера: ошибка в странице не оставит пустой базы.
  local schema seedsql
  schema="$(extract_block "$schema_page" schema.sql)"
  seedsql="$(extract_block "$schema_page" "seed-$seed.sql")"
  cmd_down
  docker run -d --name "$container" \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=library \
    "$image" -c shared_preload_libraries=pg_stat_statements -c track_io_timing=on >/dev/null
  # TCP слушает только основной сервер, а не временный сервер инициализации.
  for i in $(seq 1 30); do
    if docker exec "$container" pg_isready -q -h 127.0.0.1 -U postgres -d library; then break; fi
    if [ "$i" -eq 30 ]; then echo "postgres not ready in 30s" >&2; exit 1; fi
    sleep 1
  done
  printf '%s\n' "$schema" | psql_run
  printf '%s\n' "$seedsql" | psql_run
  printf '%s\n' "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" "VACUUM ANALYZE;" | psql_run
  echo "ready: $container ($seed)"
}

case "${1:-}" in
  up) cmd_up "${2:-small}" ;;
  run) [ $# -eq 2 ] || usage; psql_run < "$2" ;;
  block) [ $# -eq 3 ] || usage; body="$(extract_block "$2" "$3")"; printf '%s\n' "$body" | psql_run ;;
  bg) [ $# -eq 3 ] || usage; cmd_bg "$2" "$3" ;;
  down) cmd_down ;;
  *) usage ;;
esac
