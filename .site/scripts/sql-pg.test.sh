#!/usr/bin/env bash
# Проверки .site/scripts/sql-pg.sh. Поднимает свой контейнер reference-pg-test-<pid>:
# параллельные прогоны тестов друг другу не мешают.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/sql-pg.sh"
tmp="$(mktemp -d)"
export SQL_PG_CONTAINER="reference-pg-test-$$"
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
SELECT pg_sleep(20);
${fence}

${fence}sql title="bg-quick.sql"
SELECT 1;
${fence}

${fence}sql title="bg-err.sql"
SET nope_param = 1;
${fence}

${fence}sql title="bg-late.sql"
SET statement_timeout = '20s';
\\! sleep 2
BEGIN;
UPDATE t SET id = id WHERE id = 1;
\\! sleep 6
${fence}
MD

# Страница с CRLF и пробелами после ограждения: блок должен находиться.
printf '%s\r\n' '```sql title="crlf.sql"' 'SELECT 7 AS seven;' '```' > "$tmp/crlf.md"
printf '%s\n' '```sql title="trail.sql"  ' 'SELECT 8 AS eight;' '```  ' > "$tmp/trail.md"
# Концевые пробелы внутри тела блока — часть SQL (здесь строковый литерал): их не трогаем.
printf '%s\n' '```sql title="ws.sql"' "SELECT length('a  " "b') AS len;" '```' > "$tmp/ws.md"

# 1. нет блока
err="$(bash "$script" block "$tmp/page.md" nope.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [ "$err" = "block not found: nope.sql" ]; then ok "нет блока"; else bad "нет блока (код $code, '$err')"; fi

# 2. два блока
err="$(bash "$script" block "$tmp/page.md" dup.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [[ "$err" == *"block not unique: dup.sql"* ]]; then ok "два блока"; else bad "два блока (код $code, '$err')"; fi

# 3. up со страницы, прогон блока
SQL_PG_PAGE="$tmp/page.md" bash "$script" up small >"$tmp/up.out" 2>&1; code=$?
if [ "$code" -eq 0 ] && [ "$(tail -n1 "$tmp/up.out")" = "ready: $SQL_PG_CONTAINER (small)" ]; then ok "up small"; else bad "up small (код $code)"; tail -n5 "$tmp/up.out"; fi

out="$(bash "$script" block "$tmp/page.md" one.sql 2>&1)"; code=$?
want=$' one \n-----\n   1\n(1 row)'
if [ "$code" -eq 0 ] && [ "$out" = "$want" ]; then ok "SELECT 1"; else bad "SELECT 1 ($code: $out)"; fi

# 4. ошибка SQL
bash "$script" block "$tmp/page.md" err.sql >/dev/null 2>&1; code=$?
if [ "$code" -ne 0 ]; then ok "ошибка SQL"; else bad "ошибка SQL дала код 0"; fi

# 5. данные seed-small загружены, расширение на месте
pq() { docker exec "$SQL_PG_CONTAINER" psql -X -q -t -A -U postgres -d library -c "$1"; }
out="$(pq "SELECT count(*) FROM t")"
if [ "$out" = "1" ]; then ok "seed: одна строка"; else bad "seed: одна строка ('$out')"; fi
out="$(pq "SELECT extname FROM pg_extension WHERE extname = 'pg_stat_statements'")"
if [ "$out" = "pg_stat_statements" ]; then ok "расширение"; else bad "расширение ('$out')"; fi

# 5a. CRLF и пробелы после ограждения
out="$(bash "$script" block "$tmp/crlf.md" crlf.sql 2>&1)"; code=$?
if [ "$code" -eq 0 ] && [[ "$out" == *"7"* ]]; then ok "страница с CRLF"; else bad "страница с CRLF (код $code: $out)"; fi
out="$(bash "$script" block "$tmp/trail.md" trail.sql 2>&1)"; code=$?
if [ "$code" -eq 0 ] && [[ "$out" == *"8"* ]]; then ok "пробелы после ограждения"; else bad "пробелы после ограждения (код $code: $out)"; fi
out="$(bash "$script" block "$tmp/ws.md" ws.sql 2>&1)"; code=$?
want=$' len \n-----\n   5\n(1 row)'
if [ "$code" -eq 0 ] && [ "$out" = "$want" ]; then ok "пробелы в теле блока сохранены"; else bad "пробелы в теле блока сохранены (код $code: $out)"; fi

# 6. bg: фоновая сессия видна в pg_stat_activity под именем блока
bash "$script" bg "$tmp/page.md" bg-sleep.sql >"$tmp/bg.out" 2>&1; code=$?
out="$(pq "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'bg-sleep'")"
if [ "$code" -eq 0 ] && [ "$out" = "1" ]; then ok "bg виден в pg_stat_activity"; else bad "bg (код $code, '$out': $(cat "$tmp/bg.out"))"; fi

# 6b. сессия с тем же именем ещё жива: второй запуск отказывается, а не рапортует «готово»
err="$(bash "$script" bg "$tmp/page.md" bg-sleep.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [ "$err" = "bg session already running: bg-sleep" ]; then ok "bg: имя уже занято"; else bad "bg: имя уже занято (код $code, '$err')"; fi

# 6c. блок закончился раньше первого опроса: код блока, а не ложный тайм-аут
start=$SECONDS
bash "$script" bg "$tmp/page.md" bg-quick.sql >"$tmp/bg.out" 2>&1; code=$?
took=$((SECONDS - start))
if [ "$code" -eq 0 ] && [ "$took" -lt 5 ]; then ok "bg: быстрый блок"; else bad "bg: быстрый блок (код $code, ${took} с: $(cat "$tmp/bg.out"))"; fi
err="$(bash "$script" bg "$tmp/page.md" bg-err.sql 2>&1 >/dev/null)"; code=$?
if [ "$code" -eq 1 ] && [ "$err" = "bg block failed with code 3: bg-err" ]; then ok "bg: ошибка блока"; else bad "bg: ошибка блока (код $code, '$err')"; fi

# 6a. bg возвращается, когда блок дошёл до работы, а не при подключении psql
bash "$script" bg "$tmp/page.md" bg-late.sql >"$tmp/bg.out" 2>&1; code=$?
out="$(pq "SELECT state FROM pg_stat_activity WHERE application_name = 'bg-late'")"
if [ "$code" -eq 0 ] && [ "$out" = "idle in transaction" ]; then ok "bg ждёт начала работы блока"; else bad "bg ждёт начала работы блока (код $code, state '$out')"; fi

# 7. bg: нет блока и страницы
bash "$script" bg "$tmp/page.md" nope.sql >/dev/null 2>&1; code=$?
if [ "$code" -eq 1 ]; then ok "bg: нет блока"; else bad "bg: нет блока (код $code)"; fi
bash "$script" bg "$tmp/nope.md" bg-sleep.sql >/dev/null 2>&1; code=$?
if [ "$code" -eq 1 ]; then ok "bg: нет страницы"; else bad "bg: нет страницы (код $code)"; fi

# 7a. сбой схемы после docker run снимает контейнер
printf '%s\n' '```sql title="schema.sql"' 'SELECT * FROM nope;' '```' '' '```sql title="seed-small.sql"' 'SELECT 1;' '```' > "$tmp/broken.md"
SQL_PG_PAGE="$tmp/broken.md" bash "$script" up small >/dev/null 2>&1; code=$?
if [ "$code" -ne 0 ] && [ -z "$(docker ps -aq --filter "name=^${SQL_PG_CONTAINER}\$")" ]; then ok "up: сбой схемы снимает контейнер"; else bad "up: сбой схемы (код $code)"; fi

# 8. down идемпотентен
bash "$script" down >/dev/null 2>&1 && bash "$script" down >/dev/null 2>&1; code=$?
if [ "$code" -eq 0 ]; then ok "down дважды"; else bad "down дважды (код $code)"; fi

exit "$fail"
