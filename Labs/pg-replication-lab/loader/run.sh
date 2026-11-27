#!/usr/bin/env bash
# Фоновая нагрузка на primary: при первом запуске pgbench -i, дальше бесконечный pgbench с ограничением TPS.
set -euo pipefail

until pg_isready -q; do sleep 1; done

if [[ -z "$(psql -tAc "select to_regclass('pgbench_accounts')")" ]]; then
  echo "pgbench init, scale ${SCALE}"
  pgbench -i -s "$SCALE" --quiet
fi

exec pgbench -n -c "$CLIENTS" -j 2 -T 31536000 -R "$RATE" -P 10 \
  -b tpcb-like@3 -f /loader/insert.sql@1
