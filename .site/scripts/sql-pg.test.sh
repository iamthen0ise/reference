#!/usr/bin/env bash
# Проверки .site/scripts/sql-pg.sh. Поднимает свой контейнер reference-pg-test.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/sql-pg.sh"
tmp="$(mktemp -d)"
export SQL_PG_CONTAINER=reference-pg-test
cleanup() { bash "$script" down >/dev/null 2>&1; rm -rf "$tmp"; }
trap cleanup EXIT

fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

fence='```'
cat > "$tmp/page.md" <<MD
# page
${fence}sql title="schema.sql"
CREATE TABLE t(id int);
${fence}

${fence}sql title="seed-small.sql"
INSERT INTO t VALUES (1);
${fence}

${fence}sql title="seed-big.sql"
INSERT INTO t SELECT generate_series(1, 1000);
${fence}

${fence}sql title="one.sql"
SELECT 1 AS one;
${fence}

${fence}sql title="dup.sql"
SELECT 1;
${fence}

${fence}sql title="dup.sql"
SELECT 2;
${fence}

${fence}sql title="err.sql"
SELECT * FROM nope;
${fence}

${fence}sql title="bg-sleep.sql"
SELECT pg_sleep(5);
${fence}
MD

# 1. нет блока
err="$(bash "$script" block "$tmp/page.md" nope.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [ "$err" = "block not found: nope.sql" ]; then ok "нет блока"; else bad "нет блока (код $code, '$err')"; fi

# 2. два блока
err="$(bash "$script" block "$tmp/page.md" dup.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [[ "$err" == *"block not unique: dup.sql"* ]]; then ok "два блока"; else bad "два блока (код $code, '$err')"; fi

# 3. up со страницы, прогон блока
SQL_PG_PAGE="$tmp/page.md" bash "$script" up small >"$tmp/up.out" 2>&1; code=$?
if [ "$code" -eq 0 ] && [ "$(tail -n1 "$tmp/up.out")" = "ready: reference-pg-test (small)" ]; then ok "up small"; else bad "up small (код $code)"; tail -n5 "$tmp/up.out"; fi

out="$(bash "$script" block "$tmp/page.md" one.sql 2>&1)"; code=$?
if [ "$code" -eq 0 ] && [[ "$out" == *" one "* ]] && [[ "$out" == *"1"* ]]; then ok "SELECT 1"; else bad "SELECT 1 ($code: $out)"; fi

# 4. ошибка SQL
bash "$script" block "$tmp/page.md" err.sql >/dev/null 2>&1; code=$?
if [ "$code" -ne 0 ]; then ok "ошибка SQL"; else bad "ошибка SQL дала код 0"; fi

# 5. данные seed-small загружены, расширение на месте
printf '%s\n' "SELECT count(*) AS n FROM t;" "SELECT extname FROM pg_extension WHERE extname='pg_stat_statements';" > "$tmp/check.sql"
out="$(bash "$script" run "$tmp/check.sql" 2>&1)"
if [[ "$out" == *" 1"* ]] && [[ "$out" == *"pg_stat_statements"* ]]; then ok "seed и расширение"; else bad "seed и расширение ($out)"; fi

# 6. bg: фоновая сессия видна в pg_stat_activity под именем блока
bash "$script" bg "$tmp/page.md" bg-sleep.sql >"$tmp/bg.out" 2>&1; code=$?
printf '%s\n' "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'bg-sleep' AND state = 'active';" > "$tmp/bgcheck.sql"
if [ "$code" -eq 0 ] && [[ "$(bash "$script" run "$tmp/bgcheck.sql" 2>&1)" == *" 1"* ]]; then ok "bg виден в pg_stat_activity"; else bad "bg (код $code: $(cat "$tmp/bg.out"))"; fi

# 7. bg: нет блока и страницы
bash "$script" bg "$tmp/page.md" nope.sql >/dev/null 2>&1; code=$?
if [ "$code" -eq 1 ]; then ok "bg: нет блока"; else bad "bg: нет блока (код $code)"; fi
bash "$script" bg "$tmp/nope.md" bg-sleep.sql >/dev/null 2>&1; code=$?
if [ "$code" -eq 1 ]; then ok "bg: нет страницы"; else bad "bg: нет страницы (код $code)"; fi

# 8. down идемпотентен
bash "$script" down >/dev/null 2>&1 && bash "$script" down >/dev/null 2>&1; code=$?
if [ "$code" -eq 0 ]; then ok "down дважды"; else bad "down дважды (код $code)"; fi

exit "$fail"
