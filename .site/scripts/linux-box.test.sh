#!/usr/bin/env bash
# Проверки .site/scripts/linux-box.sh. Поднимает свой контейнер reference-linux-test-<pid>:
# параллельные прогоны тестов друг другу не мешают.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/linux-box.sh"
tmp="$(mktemp -d)"
export LINUX_BOX_CONTAINER="reference-linux-test-$$"
cleanup() { bash "$script" down >/dev/null 2>&1; rm -rf "$tmp"; }
trap cleanup EXIT

fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

fence='```'
cat > "$tmp/page.md" <<MD
# page
${fence}sh title="one.sh"
echo hi; id -u
${fence}

${fence}sh title="exit3.sh"
exit 3
${fence}

${fence}sh title="dup"
echo a
${fence}

${fence}python title="dup"
print("b")
${fence}

${fence}python title="fork.py"
import os
import sys

print("before")
pid = os.fork()
if pid == 0:
    sys.exit(0)
os.waitpid(pid, 0)
print("after")
${fence}

${fence}sh title="pidns.sh"
unshare --pid --fork --mount-proc sh -c 'echo \$\$'
${fence}

${fence}sh title="sleep.sh"
sleep 10
${fence}

${fence}sh title="stdin.sh"
cat
echo after
${fence}

${fence}sh title="empty.sh"
${fence}

${fence}python title="empty.py"
${fence}
MD
sed 's/$/\r/' "$tmp/page.md" > "$tmp/crlf.md"

# run NAME PAGE TITLE: пишет stdout в $tmp/out, stderr в $tmp/err, код — в $rc.
run() {
  bash "$script" block "$2" "$3" > "$tmp/out" 2> "$tmp/err"
  rc=$?
}

# Контейнер для всех проверок, кроме последних (down-twice, not-running).
if bash "$script" up > "$tmp/up.log" 2>&1 && [ "$(tail -n1 "$tmp/up.log")" = "ready: $LINUX_BOX_CONTAINER" ]; then
  ok up
else
  bad up; cat "$tmp/up.log" >&2
fi

run not-found "$tmp/page.md" nope.sh
if [ "$rc" -eq 1 ] && grep -q 'block not found: nope.sh' "$tmp/err"; then ok not-found; else bad not-found; fi

run not-unique "$tmp/page.md" dup
if [ "$rc" -eq 1 ] && grep -q 'block not unique: dup' "$tmp/err"; then ok not-unique; else bad not-unique; fi

run sh-runs "$tmp/page.md" one.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "$(printf 'hi\n0')" ]; then ok sh-runs; else bad sh-runs; fi

run exit-code "$tmp/page.md" exit3.sh
if [ "$rc" -eq 3 ]; then ok exit-code; else bad exit-code; fi

run fork-no-dup "$tmp/page.md" fork.py
if [ "$rc" -eq 0 ] && [ "$(grep -c '^before$' "$tmp/out")" -eq 1 ] && grep -q '^after$' "$tmp/out"; then
  ok fork-no-dup
else bad fork-no-dup; fi

run privileged "$tmp/page.md" pidns.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "1" ]; then ok privileged; else bad privileged; fi

LINUX_BOX_TIMEOUT=2 run timeout "$tmp/page.md" sleep.sh
if [ "$rc" -eq 124 ] && grep -q 'block timed out' "$tmp/err"; then ok timeout; else bad timeout; fi

run crlf "$tmp/crlf.md" one.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "$(printf 'hi\n0')" ]; then ok crlf; else bad crlf; fi

run stdin-safe "$tmp/page.md" stdin.sh
if [ "$rc" -eq 0 ] && [ "$(cat "$tmp/out")" = "after" ]; then ok stdin-safe; else bad stdin-safe; fi

run empty-block "$tmp/page.md" empty.sh
rc1=$rc; e1="$(cat "$tmp/out" "$tmp/err")"
run empty-block "$tmp/page.md" empty.py
if [ "$rc1" -eq 0 ] && [ "$rc" -eq 0 ] && [ -z "$e1$(cat "$tmp/out" "$tmp/err")" ]; then ok empty-block; else bad empty-block; fi

bash "$script" down >/dev/null 2>&1; r1=$?
bash "$script" down >/dev/null 2>&1; r2=$?
if [ "$r1" -eq 0 ] && [ "$r2" -eq 0 ]; then ok down-twice; else bad down-twice; fi

run not-running "$tmp/page.md" one.sh
if [ "$rc" -eq 1 ] && grep -q 'not running' "$tmp/err"; then ok not-running; else bad not-running; fi

exit "$fail"
