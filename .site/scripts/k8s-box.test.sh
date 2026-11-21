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

# Уникальные длительности sleep: pgrep -f не совпадёт с чужими процессами.
s1=$((20000 + RANDOM))
s2=$((20000 + RANDOM))
s3=$((20000 + RANDOM))
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
sleep $s1 & sleep $s1
${fence}

${fence}sh title="override.sh"
echo RAN; kubectl get pods --context prod
${fence}

${fence}sh title="ignore-term.sh"
(trap "" TERM; exec sleep $s2) & wait
${fence}

${fence}sh title="long.sh"
sleep $s3 & wait
${fence}

${fence}sh title="unset.sh"
unset KUBECONFIG; kubectl config current-context
${fence}

${fence}sh title="env-i.sh"
env -i PATH="\$PATH" kubectl config current-context
${fence}

${fence}sh title="server.sh"
echo RAN; kubectl get pods --server https://example.invalid
${fence}

${fence}sh title="unset-form.sh"
echo RAN; unset KUBECONFIG; kubectl get pods
${fence}

${fence}sh title="subst.sh"
x=\$(kubectl version); echo RAN
${fence}

${fence}sh title="pipe.sh"
echo RAN | kubectl apply -f -
${fence}

${fence}sh title="bol.sh"
echo RAN
kubectl get pods
${fence}

${fence}sh title="docker-user.sh"
echo RAN docker run --user 1000 busybox id
${fence}

${fence}sh title="kubectl-user.sh"
echo RAN; kubectl get pods --user x
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
if [ "$rc" -eq 124 ] && grep -q 'block timed out' "$tmp/err" && ! pgrep -f "sleep $s1" >/dev/null; then ok timeout; else bad timeout; fi

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

for t in subst.sh pipe.sh bol.sh; do
  fake_kubeconfig prod
  run "$tmp/page.md" $t
  if [ "$rc" -eq 1 ] && grep -q 'wrong kubectl context: prod' "$tmp/err" && no_ran; then ok "kubectl-word-$t"; else bad "kubectl-word-$t"; fi
done

fake_kubeconfig "kind-reference-test-$$"
run "$tmp/page.md" server.sh
if [ "$rc" -eq 1 ] && grep -q 'block overrides kubeconfig' "$tmp/err" && no_ran; then ok override-server; else bad override-server; fi
run "$tmp/page.md" unset-form.sh
if [ "$rc" -eq 1 ] && grep -q 'block overrides kubeconfig' "$tmp/err" && no_ran; then ok override-unset; else bad override-unset; fi

run "$tmp/page.md" docker-user.sh
if [ "$rc" -eq 0 ] && grep -q RAN "$tmp/out"; then ok docker-user-allowed; else bad docker-user-allowed; fi
run "$tmp/page.md" kubectl-user.sh
if [ "$rc" -eq 1 ] && grep -q 'block overrides kubeconfig' "$tmp/err" && no_ran; then ok override-user; else bad override-user; fi

# kind подменён заглушкой, чтобы пройти проверку «кластер запущен»: настоящий кластер тесту не нужен.
mkdir "$tmp/stub"
printf '#!/bin/sh\necho "%s"\n' "$K8S_BOX_CLUSTER" > "$tmp/stub/kind"
chmod +x "$tmp/stub/kind"
PATH="$tmp/stub:$PATH" run "$tmp/page.md" env-i.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "kind-$K8S_BOX_CLUSTER" ]; then ok wrapper-env-i; else bad wrapper-env-i; fi
PATH="$tmp/stub:$PATH" run "$tmp/page.md" unset.sh
if [ "$rc" -eq 1 ] && grep -q 'block overrides kubeconfig' "$tmp/err"; then ok override-unset-plain; else bad override-unset-plain; fi

K8S_BOX_TIMEOUT=2 run "$tmp/page.md" ignore-term.sh
sleep 6
if [ "$rc" -eq 124 ] && ! pgrep -f "sleep $s2" >/dev/null; then ok timeout-kills-descendants; else bad timeout-kills-descendants; fi
if ! grep -q 'Terminated' "$tmp/err"; then ok timeout-quiet; else bad timeout-quiet; fi
pkill -KILL -f "sleep $s2" 2>/dev/null

bash "$script" block "$tmp/page.md" long.sh > "$tmp/out" 2> "$tmp/err" &
bp=$!
sleep 2
kill -TERM "$bp"
wait "$bp" 2>/dev/null
sleep 2
if ! pgrep -f "sleep $s3" >/dev/null; then ok signal-forwarded; else bad signal-forwarded; pkill -KILL -f "sleep $s3"; fi

# Фальшивый HOME: настоящий ~/.kube тест не трогает.
mkdir -p "$tmp/home/.kube"
echo dummy > "$tmp/home/.kube/config"
ln -s "$tmp/home/.kube/config" "$tmp/link-config"
for kcfg in "$tmp/home/.kube/config" "$tmp/link-config"; do
  HOME="$tmp/home" K8S_BOX_KUBECONFIG="$kcfg" bash "$script" down > "$tmp/out" 2> "$tmp/err"
  rc=$?
  if [ "$rc" -eq 1 ] && grep -q 'unsafe sandbox kubeconfig' "$tmp/err" && [ "$(cat "$tmp/home/.kube/config")" = dummy ]; then
    ok "unsafe-kubeconfig-$(basename "$kcfg")"
  else bad "unsafe-kubeconfig-$(basename "$kcfg")"; fi
done

if bash "$script" down > "$tmp/out" 2> "$tmp/err"; then ok down-absent; else bad down-absent; fi

exit "$fail"
