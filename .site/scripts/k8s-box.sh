#!/usr/bin/env bash
# Прогон демонстраций страниц «Docker и Kubernetes» в песочнице kind: up | block PAGE TITLE | down.
# kubectl здесь всегда с песочным kubeconfig; ~/.kube/config (продовый контекст) не читается и не меняется.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
cluster="${K8S_BOX_CLUSTER:-reference}"
kubeconfig="${K8S_BOX_KUBECONFIG:-$root/.site/k8s-box/kubeconfig}"
limit="${K8S_BOX_TIMEOUT:-300}"

# Версия по умолчанию в kind v0.33.0 (release notes): Kubernetes v1.37.0.
node_image="kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
metrics_server_url="https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.1/components.yaml"
busybox_image="busybox:1.37.0"

usage() {
  echo "usage: k8s-box.sh up | block PAGE TITLE | down" >&2
  exit 2
}

# Печатает в stdout тело блока ```sh title="<title>". Код 1, если блока нет или он не один.
# Заголовок идёт через ENVIRON (awk -v разворачивает обратные слэши). Для сравнения с ограждениями
# концевые пробелы и \r отбрасываются, так что страница с CRLF находится; строки тела печатаются без \r.
extract_block() {
  local page="$1" title="$2" n
  [ -f "$page" ] || { echo "page not found: $page" >&2; return 1; }
  n="$(K_T="title=\"$title\"" awk '
    { line = $0; sub(/[ \t\r]+$/, "", line) }
    line == "```sh " ENVIRON["K_T"] { c++ }
    END { print c + 0 }' "$page")"
  if [ "$n" -eq 0 ]; then echo "block not found: $title" >&2; return 1; fi
  if [ "$n" -gt 1 ]; then echo "block not unique: $title" >&2; return 1; fi
  K_T="title=\"$title\"" awk '
    { line = $0; sub(/[ \t\r]+$/, "", line); body = $0; sub(/\r$/, "", body) }
    !on && line == "```sh " ENVIRON["K_T"] { on = 1; next }
    on && line == "```" { exit }
    on { print body }
  ' "$page"
}

# kind и kubectl работают только с локальным Docker Desktop.
check_docker() {
  local name
  if [ -n "${DOCKER_HOST:-}" ]; then
    echo "docker is not local: DOCKER_HOST=$DOCKER_HOST" >&2
    return 1
  fi
  name="$(docker context show 2>/dev/null)" || name="?"
  case "$name" in
    desktop-linux | default) ;;
    *) echo "docker is not local: context $name" >&2; return 1 ;;
  esac
}

kc() { kubectl --kubeconfig "$kubeconfig" "$@"; }

minor() { echo "${1#v}" | cut -d. -f2; }

cmd_up() {
  check_docker || exit 1
  mkdir -p "$(dirname "$kubeconfig")"
  if kind get clusters 2>/dev/null | grep -qx "$cluster"; then
    kind delete cluster --name "$cluster" --kubeconfig "$kubeconfig"
  fi
  kind create cluster --name "$cluster" --image "$node_image" --kubeconfig "$kubeconfig" --wait 120s

  local vers client server skew
  vers="$(kc version)"
  client="$(echo "$vers" | awk '/^Client Version:/ { print $3 }')"
  server="$(echo "$vers" | awk '/^Server Version:/ { print $3 }')"
  skew=$(( $(minor "$client") - $(minor "$server") ))
  [ "$skew" -ge 0 ] || skew=$(( -skew ))
  if [ "$skew" -gt 1 ]; then
    echo "kubectl version skew: client v$(echo "${client#v}" | cut -d. -f1,2), server v$(echo "${server#v}" | cut -d. -f1,2)" >&2
    exit 1
  fi

  # metrics-server: kubelet в kind со самоподписанным сертификатом, отсюда --kubelet-insecure-tls.
  local manifest
  manifest="$(curl -fsSL "$metrics_server_url" | awk '
    { print }
    /^ *- --metric-resolution=/ { match($0, /^ */); printf "%*s- --kubelet-insecure-tls\n", RLENGTH, ""; added = 1 }
    END { if (!added) exit 3 }')" || { echo "metrics-server manifest: no --metric-resolution arg to extend" >&2; exit 1; }
  printf '%s\n' "$manifest" | kc apply -f -
  kc rollout status -n kube-system deploy/metrics-server --timeout=180s

  local v
  for v in 1 2; do
    docker build -q --build-arg "VERSION=$v" -t "reference-app:$v" "$root/.site/k8s-box/app" >/dev/null
    kind load docker-image "reference-app:$v" --name "$cluster"
  done
  # Docker Desktop с containerd хранит мультиплатформенный индекс, и kind load на нём падает
  # (ctr: content digest ... not found). Пересборка с тем же тегом даёт образ одной платформы.
  docker pull -q "$busybox_image" >/dev/null
  printf 'FROM %s\nLABEL reference.k8s-box="single-platform copy for kind load"\n' "$busybox_image" |
    docker build -q -t "$busybox_image" - >/dev/null
  kind load docker-image "$busybox_image" --name "$cluster"

  echo "ready: kind-$cluster (Kubernetes ${server}, kubectl ${client})"
}

cmd_block() {
  [ $# -eq 2 ] && [ -n "$1" ] && [ -n "$2" ] || usage
  local page="$1" title="$2" body ctx
  body="$(extract_block "$page" "$title")" || exit 1

  if printf '%s\n' "$body" | grep -Eq -- '--context|--kubeconfig|KUBECONFIG=|unset[[:space:]]+KUBECONFIG|-u[[:space:]]+KUBECONFIG|-n[[:space:]]+KUBECONFIG' ||
     printf '%s\n' "$body" | grep -E '(^|[^[:alnum:]_.-])kubectl([^[:alnum:]_.-]|$)' |
       grep -Eq -- '--(server|token|cluster|user)([=[:space:]]|$)'; then
    echo "block overrides kubeconfig: $title" >&2
    exit 1
  fi
  check_docker || exit 1

  if printf '%s\n' "$body" | grep -Eq '(^|[^[:alnum:]_.-])kubectl([^[:alnum:]_.-]|$)'; then
    [ -f "$kubeconfig" ] || { echo "no sandbox kubeconfig; run make k8s-up" >&2; exit 1; }
    ctx="$(kubectl config current-context --kubeconfig "$kubeconfig" 2>/dev/null)" || ctx="?"
    [ "$ctx" = "kind-$cluster" ] || { echo "wrong kubectl context: $ctx" >&2; exit 1; }
    kind get clusters 2>/dev/null | grep -qx "$cluster" || { echo "not running: $cluster; run make k8s-up" >&2; exit 1; }
  fi

  local stash work rc=0 pid wd real
  stash="$(mktemp -d)"
  work="$(mktemp -d)"
  printf '%s\n' "$body" > "$stash/block.sh"
  # kubectl в блоке — обёртка с песочным kubeconfig: unset KUBECONFIG не уводит блок в ~/.kube/config.
  mkdir "$stash/bin"
  if real="$(command -v kubectl)"; then
    printf '#!/bin/sh\nexec %q --kubeconfig %q "$@"\n' "$real" "$kubeconfig" > "$stash/bin/kubectl"
    chmod +x "$stash/bin/kubectl"
  fi
  # shellcheck disable=SC2064
  trap "rm -rf -- '$stash' '$work'" EXIT

  # Блок — в своей группе процессов (set -m): сторож шлёт TERM, потом KILL всей группе.
  set -m
  (cd "$work" && KUBECONFIG="$kubeconfig" PATH="$stash/bin:$PATH" exec bash "$stash/block.sh" < /dev/null) &
  pid=$!
  set +m
  (
    waited=0
    while [ "$waited" -lt "$limit" ]; do
      kill -0 "$pid" 2>/dev/null || exit 0
      sleep 1
      waited=$((waited + 1))
    done
    kill -0 "$pid" 2>/dev/null || exit 0
    : > "$stash/timed-out"
    kill -TERM -- "-$pid" 2>/dev/null || true
    sleep 5
    kill -KILL -- "-$pid" 2>/dev/null || true
  ) &
  wd=$!
  # Сигнал скрипту уходит всей группе блока: блок не остаётся без присмотра.
  trap 'kill -TERM -- "-$pid" 2>/dev/null; kill "$wd" 2>/dev/null; sleep 1; kill -KILL -- "-$pid" 2>/dev/null; exit 143' INT TERM
  { wait "$pid"; } 2>/dev/null || rc=$?
  if [ -e "$stash/timed-out" ]; then
    # Лидер мог выйти по TERM, а потомки — игнорировать его: ждём сторожа, он досылает KILL.
    { wait "$wd"; } 2>/dev/null || true
    echo "block timed out: $title" >&2
    exit 124
  fi
  kill "$wd" 2>/dev/null || true
  { wait "$wd"; } 2>/dev/null || true
  exit "$rc"
}

cmd_down() {
  check_docker || exit 1
  if kind get clusters 2>/dev/null | grep -qx "$cluster"; then
    kind delete cluster --name "$cluster" --kubeconfig "$kubeconfig"
  fi
  rm -f -- "$kubeconfig"
}

# Песочный kubeconfig не должен лежать в ~/.kube: kind create/delete переписали бы его, а down удалил.
# Абсолютный путь без симлинков (включая сам файл, даже если его нет: тогда — по каталогу).
resolve_path() {
  local p="$1" dir link n=0
  while [ -L "$p" ] && [ "$n" -lt 20 ]; do
    link="$(readlink "$p")"
    case "$link" in /*) p="$link" ;; *) p="$(dirname "$p")/$link" ;; esac
    n=$((n + 1))
  done
  dir="$(dirname "$p")"
  [ -d "$dir" ] && dir="$(cd "$dir" && pwd -P)"
  printf '%s/%s\n' "$dir" "$(basename "$p")"
}

check_kubeconfig() {
  local path kube kube_real
  [ -n "${HOME:-}" ] || { echo "HOME is not set; cannot check the sandbox kubeconfig" >&2; return 1; }
  path="$(resolve_path "$kubeconfig")"
  kube="$HOME/.kube"
  kube_real="$kube"
  [ -d "$kube" ] && kube_real="$(cd "$kube" && pwd -P)"
  case "$path" in
    "$kube" | "$kube"/* | "$kube_real" | "$kube_real"/*) echo "unsafe sandbox kubeconfig: $kubeconfig" >&2; return 1 ;;
  esac
  case "$kubeconfig" in
    "$kube" | "$kube"/* | "$kube_real" | "$kube_real"/*) echo "unsafe sandbox kubeconfig: $kubeconfig" >&2; return 1 ;;
  esac
}

[ $# -ge 1 ] || usage
sub="$1"; shift
case "$sub" in
  up | block | down) check_kubeconfig || exit 1 ;;
esac
case "$sub" in
  up) cmd_up ;;
  block) cmd_block "$@" ;;
  down) cmd_down ;;
  *) usage ;;
esac
