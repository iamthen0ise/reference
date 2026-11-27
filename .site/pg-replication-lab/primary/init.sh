#!/usr/bin/env bash
# Первичная настройка primary (один раз, при пустом PGDATA): роль репликации, таблицы стенда, доступ по протоколу репликации.
set -euo pipefail

psql -v ON_ERROR_STOP=1 -v repl_password="$REPL_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'repl_password';

-- Пишется дашбордом несколько раз в секунду; на реплике clock_timestamp() - ts = сквозной лаг.
CREATE TABLE heartbeat (
  id smallint PRIMARY KEY,
  ts timestamptz NOT NULL
);
INSERT INTO heartbeat VALUES (1, clock_timestamp());

CREATE TABLE events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id int NOT NULL,
  payload text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
SQL

# Строка "host all all all" из образа не покрывает подключения репликации: для них нужна отдельная.
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
