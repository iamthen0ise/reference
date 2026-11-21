#!/usr/bin/env bash
# Прогон демонстраций страниц «Linux» в контейнере: up | block PAGE TITLE | shell | down.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
container="${LINUX_BOX_CONTAINER:-reference-linux}"
image="reference-linux:bookworm"
limit="${LINUX_BOX_TIMEOUT:-120}"

usage() {
  echo "usage: linux-box.sh up | block PAGE TITLE | shell | down" >&2
  exit 2
}

# Печатает в stdout язык блока (sh или python), затем тело. Код 1, если блока нет или он не один.
# Блок — ограждение с sh или python и title="<title>". Заголовок идёт через ENVIRON
# (awk -v разворачивает обратные слэши). Для сравнения с ограждениями концевые пробелы и \r
# отбрасываются, так что страница с CRLF находится; строки тела печатаются без \r.
extract_block() {
  local page="$1" title="$2" n
  [ -f "$page" ] || { echo "page not found: $page" >&2; return 1; }
  n="$(LB_T="title=\"$title\"" awk '
    { line = $0; sub(/[ \t\r]+$/, "", line) }
    line == "```sh " ENVIRON["LB_T"] || line == "```python " ENVIRON["LB_T"] { c++ }
    END { print c + 0 }' "$page")"
  if [ "$n" -eq 0 ]; then echo "block not found: $title" >&2; return 1; fi
  if [ "$n" -gt 1 ]; then echo "block not unique: $title" >&2; return 1; fi
  LB_T="title=\"$title\"" awk '
    { line = $0; sub(/[ \t\r]+$/, "", line); body = $0; sub(/\r$/, "", body) }
    !on && line == "```sh " ENVIRON["LB_T"] { on = 1; print "sh"; next }
    !on && line == "```python " ENVIRON["LB_T"] { on = 1; print "python"; next }
    on && line == "```" { exit }
    on { print body }
  ' "$page"
}

is_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" = "true" ]
}

cmd_up() {
  docker build -q -t "$image" "$root/.site/linux-box" >/dev/null
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker run -d --privileged --cgroupns=private --hostname reference-linux \
    --name "$container" "$image" tini -- sleep infinity >/dev/null
  echo "ready: $container"
}

cmd_block() {
  [ $# -eq 2 ] || usage
  local page="$1" title="$2" code lang
  code="$(extract_block "$page" "$title")" || exit 1
  is_running || { echo "not running: $container; run make linux-up" >&2; exit 1; }
  lang="${code%%$'\n'*}"
  code="${code#*$'\n'}"
  local rc=0
  case "$lang" in
    sh) printf '%s\n' "$code" | docker exec -i -w /root "$container" timeout "$limit" bash -s || rc=$? ;;
    python) printf '%s\n' "$code" | docker exec -i -w /root "$container" timeout "$limit" python3 -u - || rc=$? ;;
    *) echo "unknown language: $lang" >&2; exit 1 ;;
  esac
  if [ "$rc" -eq 124 ]; then echo "block timed out: $title" >&2; fi
  exit "$rc"
}

cmd_shell() {
  is_running || { echo "not running: $container; run make linux-up" >&2; exit 1; }
  exec docker exec -it "$container" bash
}

cmd_down() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}

[ $# -ge 1 ] || usage
sub="$1"; shift
case "$sub" in
  up) cmd_up ;;
  block) cmd_block "$@" ;;
  shell) cmd_shell ;;
  down) cmd_down ;;
  *) usage ;;
esac
