#!/usr/bin/env bash
# Реплика: при пустом PGDATA снимает pg_basebackup с primary (со слотом репликации) и стартует как hot standby.
set -euo pipefail

: "${REPLICA_NAME:?}" "${PRIMARY_HOST:?}" "${REPL_USER:?}" "${REPL_PASSWORD:?}" "${PGDATA:?}"
export PGPASSWORD="$REPL_PASSWORD"
slot="${REPLICA_NAME}_slot"

if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  until pg_isready -q -h "$PRIMARY_HOST" -U "$REPL_USER" -d postgres; do sleep 1; done

  # Слот мог остаться от прошлого запуска, если удалили только том реплики.
  slot_exists=$(psql -h "$PRIMARY_HOST" -U "$REPL_USER" -d postgres -tAc \
    "select count(*) from pg_replication_slots where slot_name = '$slot'")
  create_slot=()
  [[ "$slot_exists" == "0" ]] && create_slot=(--create-slot)

  # Копия снимается во временный каталог: прерванный basebackup не оставит в PGDATA полу-кластер.
  tmp="${PGDATA}.basebackup"
  rm -rf -- "$tmp" "$PGDATA"
  echo "[$REPLICA_NAME] pg_basebackup from $PRIMARY_HOST, slot $slot"
  pg_basebackup -h "$PRIMARY_HOST" -U "$REPL_USER" -D "$tmp" \
    --checkpoint=fast --wal-method=stream --write-recovery-conf --slot="$slot" "${create_slot[@]}"
  mv -- "$tmp" "$PGDATA"
  echo "[$REPLICA_NAME] basebackup done"
fi

# cluster_name становится application_name в pg_stat_replication на primary.
exec postgres \
  -c cluster_name="$REPLICA_NAME" \
  -c hot_standby=on \
  -c hot_standby_feedback=on \
  -c wal_receiver_timeout=10s \
  "$@"
