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
# Заголовок идёт через ENVIRON (awk -v разворачивает обратные слэши); концевые пробелы
# и \r строки отбрасываются перед сравнением, так что страница с CRLF находится.
extract_block() {
  local page="$1" title="$2" n want
  [ -f "$page" ] || { echo "page not found: $page" >&2; return 1; }
  want="\`\`\`sql title=\"$title\""
  n="$(SQL_T="$want" awk '
    { sub(/[ \t\r]+$/, "") }
    $0 == ENVIRON["SQL_T"] { c++ }
    END { print c + 0 }' "$page")"
  if [ "$n" -eq 0 ]; then echo "block not found: $title" >&2; return 1; fi
  if [ "$n" -gt 1 ]; then echo "block not unique: $title" >&2; return 1; fi
  SQL_T="$want" awk '
    { sub(/[ \t\r]+$/, "") }
    $0 == ENVIRON["SQL_T"] { on = 1; next }
    on && $0 == "```" { exit }
    on { print }
  ' "$page"
}

# Блок в фоновой сессии: application_name = имя блока без .sql. Возвращается, когда блок
# начал работу (до 10 с, иначе код 1): сессия вышла за ведущие SET и BEGIN и либо держит
# транзакцию (idle in transaction), либо выполняет запрос (active), либо ждёт блокировку.
# Срок жизни сессии ограничивает сам блок.
bg_fail() {
  docker exec "$container" rm -f "$1" >/dev/null 2>&1 || true
  echo "$2" >&2
  return 1
}

cmd_bg() {
  local page="$1" title="$2" name body file i seen
  body="$(extract_block "$page" "$title")" || return 1
  name="${title%.sql}"
  case "$name" in
    ""|*[!A-Za-z0-9_-]*) echo "bad block name for bg: $title" >&2; return 1 ;;
  esac
  file="/tmp/bg-$name-$$-$RANDOM.sql"
  printf '%s\n' "$body" | docker exec -i "$container" sh -c 'cat > "$1"' sh "$file" \
    || { bg_fail "$file" "cannot copy block to $container: $name"; return 1; }
  # Файл удаляется сразу после psql. Пределы проверки: она ищет только application_name,
  # поэтому оставшаяся сессия с тем же именем даст ложное «готова», а блок, завершившийся
  # раньше опроса, даст ложный код 1. Имена фоновых блоков уникальны.
  docker exec -d -e PGAPPNAME="$name" "$container" sh -c \
    'psql -X -q -v ON_ERROR_STOP=1 -P pager=off -U postgres -d library -f "$1"; rm -f "$1"' sh "$file" \
    || { bg_fail "$file" "cannot start bg session: $name"; return 1; }
  for i in $(seq 1 20); do
    seen="$(docker exec "$container" psql -X -q -t -A -U postgres -d library -c \
      "SELECT count(*) FROM pg_stat_activity
       WHERE application_name = '$name'
         AND (state IN ('active', 'idle in transaction') OR wait_event_type = 'Lock')
         AND query !~* '^[[:space:]]*(set|begin|start[[:space:]]+transaction)\\y'")" \
      || { bg_fail "$file" "cannot poll pg_stat_activity: $name"; return 1; }
    if [ "$seen" -gt 0 ]; then return 0; fi
    sleep 0.5
  done
  bg_fail "$file" "bg session did not start its work in 10s: $name"
}

cmd_down() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}

# Выход из cmd_up с ошибкой после docker run: полусобранная база не остаётся.
up_cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then cmd_down; echo "db not ready, container removed: $container" >&2; fi
}

cmd_up() {
  local seed="${1:-small}" i
  case "$seed" in small|big) ;; *) echo "seed must be small or big" >&2; exit 2 ;; esac
  # Блоки читаем до пересоздания контейнера: ошибка в самой странице (нет блока) не заденет
  # работающую базу. Ошибка после docker run (старт, схема, данные) снимает контейнер.
  local schema seedsql
  schema="$(extract_block "$schema_page" schema.sql)"
  seedsql="$(extract_block "$schema_page" "seed-$seed.sql")"
  cmd_down
  trap up_cleanup EXIT
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
  trap - EXIT
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
