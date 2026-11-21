#!/usr/bin/env bash
# Проверки .site/scripts/k8s-box.sh. Кластер и сеть не нужны, нужен запущенный Docker Desktop.
# Прогон идёт с отдельными именем кластера и kubeconfig: песочница и продовый контекст не затрагиваются.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
script="$here/k8s-box.sh"
tmp="$(mktemp -d)"
export K8S_BOX_CLUSTER="reference-test-$$"
export K8S_BOX_KUBECONFIG="$tmp/kubeconfig"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

fence='```'
cat > "$tmp/page.md" <<MD
# page
${fence}sh title="echo.sh"
echo hi
${fence}

${fence}sh title="exit3.sh"
exit 3
${fence}

${fence}sh title="dup"
echo a
${fence}

${fence}sh title="dup"
echo b
${fence}

${fence}sh title="pwd.sh"
pwd
${fence}

${fence}sh title="stdin.sh"
cat; echo done
${fence}

${fence}sh title="sleep.sh"
sleep 31 & sleep 31
${fence}

${fence}sh title="override.sh"
echo RAN; kubectl get pods --context prod
${fence}

${fence}sh title="plain.sh"
echo RAN
${fence}

${fence}sh title="kubectl.sh"
echo RAN; kubectl get pods
${fence}

${fence}sh title="env.sh"
echo "\$KUBECONFIG"
${fence}
MD
sed 's/$/\r/' "$tmp/page.md" > "$tmp/crlf.md"

# fake_kubeconfig NAME: минимальный kubeconfig с одним контекстом.
fake_kubeconfig() {
  cat > "$K8S_BOX_KUBECONFIG" <<YAML
apiVersion: v1
kind: Config
clusters:
- name: $1
  cluster:
    server: https://127.0.0.1:1
users:
- name: $1
  user: {}
contexts:
- name: $1
  context:
    cluster: $1
    user: $1
current-context: $1
YAML
}

# run PAGE TITLE: stdout в $tmp/out, stderr в $tmp/err, код в $rc.
run() {
  bash "$script" block "$1" "$2" > "$tmp/out" 2> "$tmp/err"
  rc=$?
}
no_ran() { ! grep -q RAN "$tmp/out"; }

run "$tmp/page.md" nope.sh
if [ "$rc" -eq 1 ] && grep -q 'block not found: nope.sh' "$tmp/err" && no_ran; then ok not-found; else bad not-found; fi

run "$tmp/page.md" dup
if [ "$rc" -eq 1 ] && grep -q 'block not unique: dup' "$tmp/err" && [ ! -s "$tmp/out" ]; then ok not-unique; else bad not-unique; fi

run "$tmp/page.md" echo.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "hi" ]; then ok sh-runs; else bad sh-runs; fi

run "$tmp/page.md" exit3.sh
if [ "$rc" -eq 3 ]; then ok exit-code; else bad exit-code; fi

run "$tmp/crlf.md" echo.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "hi" ]; then ok crlf; else bad crlf; fi

run "$tmp/page.md" pwd.sh
cwd="$(cat "$tmp/out")"
if [ "$rc" -eq 0 ] && [ -n "$cwd" ] && [ "$cwd" != "$root" ] && [ ! -e "$cwd" ]; then ok tmp-cwd; else bad tmp-cwd; fi

K8S_BOX_TIMEOUT=5 run "$tmp/page.md" stdin.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "done" ]; then ok stdin-null; else bad stdin-null; fi

K8S_BOX_TIMEOUT=2 run "$tmp/page.md" sleep.sh
sleep 6
if [ "$rc" -eq 124 ] && grep -q 'block timed out' "$tmp/err" && ! pgrep -f "sleep 31" >/dev/null; then ok timeout; else bad timeout; fi

run "$tmp/page.md" override.sh
if [ "$rc" -eq 1 ] && grep -q 'block overrides kubeconfig' "$tmp/err" && no_ran; then ok override; else bad override; fi

DOCKER_HOST=tcp://192.0.2.1:2375 run "$tmp/page.md" plain.sh
if [ "$rc" -eq 1 ] && grep -q 'docker is not local' "$tmp/err" && no_ran; then ok docker-host; else bad docker-host; fi

DOCKER_CONTEXT=reference-test-missing run "$tmp/page.md" plain.sh
if [ "$rc" -eq 1 ] && grep -q 'docker is not local' "$tmp/err" && no_ran; then ok docker-context; else bad docker-context; fi

rm -f "$K8S_BOX_KUBECONFIG"
run "$tmp/page.md" kubectl.sh
if [ "$rc" -eq 1 ] && grep -q 'no sandbox kubeconfig' "$tmp/err" && no_ran; then ok no-kubeconfig; else bad no-kubeconfig; fi

fake_kubeconfig prod
run "$tmp/page.md" kubectl.sh
if [ "$rc" -eq 1 ] && grep -q 'wrong kubectl context: prod' "$tmp/err" && no_ran; then ok wrong-context; else bad wrong-context; fi

fake_kubeconfig "kind-reference-test-$$"
run "$tmp/page.md" kubectl.sh
if [ "$rc" -eq 1 ] && grep -q "not running: reference-test-$$" "$tmp/err" && no_ran; then ok not-running; else bad not-running; fi

KUBECONFIG="$tmp/prod.yaml" run "$tmp/page.md" env.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "$tmp/kubeconfig" ]; then ok env-kubeconfig-ignored; else bad env-kubeconfig-ignored; fi

if bash "$script" down > "$tmp/out" 2> "$tmp/err"; then ok down-absent; else bad down-absent; fi

exit "$fail"
