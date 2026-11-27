"""Дашборд лага репликации.

Раз в секунду опрашивает primary и реплики и рассылает снимок в браузер по SSE (/events).
Отдельные потоки: heartbeat на primary (сквозной лаг по времени) и регулируемая вставка строк (нагрузка с дашборда).
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
from csrf import csrf_error
from docker_control import ALLOWED_ACTIONS, DockerControl, DockerError
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

log = logging.getLogger("dashboard")

PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_DB = os.environ.get("PG_DB", "lab")
PRIMARY_HOST = os.environ.get("PRIMARY_HOST", "primary")
REPLICA_HOSTS = [h for h in os.environ.get("REPLICA_HOSTS", "").split(",") if h]
CONTAINERS_INTERVAL = 2.0
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "0.1"))
PORT = int(os.environ.get("PORT", "8080"))
NETEM_URL = os.environ.get("NETEM_URL", "")
DOCKER_PROXY_URL = os.environ.get("DOCKER_PROXY_URL", "")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "pg-replication-lab")
# Primary с дашборда не управляется: в белом списке только реплики и нагрузка.
CONTROLLED_SERVICES = frozenset({"replica1", "replica2", "replica3", "loader"})
docker = DockerControl(DOCKER_PROXY_URL, COMPOSE_PROJECT, CONTROLLED_SERVICES) if DOCKER_PROXY_URL else None

MAX_ROWS_PER_SEC = 200_000
MAX_BURST_ROWS = 5_000_000
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()

INSERT_ROWS = """
insert into events (account_id, payload)
select (random() * 1000)::int, repeat(md5(random()::text), 8)
from generate_series(1, %s)
"""

PRIMARY_STATS = """
select
  pg_current_wal_lsn()::text as lsn,
  -- insert_lsn — сколько WAL сгенерировано; current_wal_lsn отстаёт, пока WAL ждёт записи на диск.
  pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '0/0')::bigint as wal_bytes,
  pg_wal_lsn_diff(pg_current_wal_insert_lsn(), pg_current_wal_flush_lsn())::bigint as wal_unflushed_bytes,
  (select extract(epoch from clock_timestamp() - ts)::float8 * 1000 from heartbeat where id = 1)
    as heartbeat_age_ms,
  (select (xact_commit + xact_rollback)::bigint
     from pg_stat_database where datname = current_database()) as xacts,
  (select count(*) from pg_stat_activity
     where state = 'active' and backend_type = 'client backend')::int as active,
  pg_database_size(current_database()) as db_size,
  (select n_live_tup from pg_stat_user_tables where relname = 'events') as events_rows,
  current_setting('synchronous_standby_names') as sync_standby_names
"""

PRIMARY_REPLICATION = """
select
  application_name as name,
  client_addr::text as client_addr,
  state,
  sync_state,
  pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)::bigint as sent_lag_bytes,
  pg_wal_lsn_diff(pg_current_wal_lsn(), write_lsn)::bigint as write_lag_bytes,
  pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn)::bigint as flush_lag_bytes,
  pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint as replay_lag_bytes,
  extract(epoch from write_lag)::float8 * 1000 as write_lag_ms,
  extract(epoch from flush_lag)::float8 * 1000 as flush_lag_ms,
  extract(epoch from replay_lag)::float8 * 1000 as replay_lag_ms
from pg_stat_replication
"""

PRIMARY_SLOTS = """
select slot_name, active,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint as retained_bytes
from pg_replication_slots
"""

REPLICA_STATS = """
select
  pg_is_in_recovery() as in_recovery,
  pg_get_wal_replay_pause_state() as pause_state,
  pg_last_wal_receive_lsn()::text as receive_lsn,
  pg_last_wal_replay_lsn()::text as replay_lsn,
  pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint as apply_backlog_bytes,
  extract(epoch from clock_timestamp() - pg_last_xact_replay_timestamp())::float8 * 1000
    as last_replay_age_ms,
  (select extract(epoch from clock_timestamp() - ts)::float8 * 1000 from heartbeat where id = 1)
    as heartbeat_lag_ms,
  current_setting('recovery_min_apply_delay') as apply_delay,
  (select status from pg_stat_wal_receiver) as receiver_status
"""


def dsn(host: str, statement_timeout_ms: int) -> str:
    return make_conninfo(
        host=host,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
        connect_timeout=2,
        application_name="lag-dashboard",
        options=f"-c statement_timeout={statement_timeout_ms}",
    )


class Node:
    """Долгоживущее autocommit-соединение с одним узлом; после ошибки переподключается при следующем запросе."""

    def __init__(self, host: str, statement_timeout_ms: int = 3000) -> None:
        self.host = host
        self.statement_timeout_ms = statement_timeout_ms
        self._conn: psycopg.Connection | None = None

    def fetch(self, query: str, params: tuple | None = None) -> list[dict]:
        try:
            conn = self._conn
            if conn is None or conn.closed:
                conn = self._conn = psycopg.connect(dsn(self.host, self.statement_timeout_ms), autocommit=True, row_factory=dict_row)
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else []
        except psycopg.Error:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            raise


def run_once(host: str, query: Any, params: tuple | None = None, statement_timeout_ms: int = 3000) -> list[dict]:
    """Запрос на отдельном коротком соединении — для действий из UI, чтобы не мешать опросу."""
    with psycopg.connect(dsn(host, statement_timeout_ms), autocommit=True, row_factory=dict_row) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else []


def netem_call(path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Запрос к агенту netem; ответ агента (включая его ошибку) возвращается как есть."""
    if not NETEM_URL:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "NETEM_URL не задан"}
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        NETEM_URL + path, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return HTTPStatus.BAD_GATEWAY, {"error": f"агент netem недоступен: {exc}"}


def error_text(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


class Loader:
    """Вставка строк в events на primary с заданной скоростью, плюс разовые «залпы» одной транзакцией."""

    def __init__(self) -> None:
        self.rows_per_sec = 0
        self.inserted_total = 0
        self.error: str | None = None
        self.burst: dict[str, Any] = {"running": False}
        # Без statement_timeout: при синхронной реплике вставка ждёт её подтверждения.
        self._node = Node(PRIMARY_HOST, statement_timeout_ms=0)
        self._lock = threading.Lock()

    def run(self) -> None:
        tick = 0.1
        carry = 0.0
        while True:
            started = time.monotonic()
            carry += self.rows_per_sec * tick
            rows = int(carry)
            carry -= rows
            if rows:
                try:
                    self._node.fetch(INSERT_ROWS, (rows,))
                    self.inserted_total += rows
                    self.error = None
                except psycopg.Error as exc:
                    self.error = error_text(exc)
                    carry = 0.0
                    time.sleep(1)
            else:
                carry = min(carry, 1.0)
            time.sleep(max(0.0, tick - (time.monotonic() - started)))

    def start_burst(self, rows: int) -> bool:
        with self._lock:
            if self.burst["running"]:
                return False
            self.burst = {"running": True, "rows": rows, "started": time.time()}
        threading.Thread(target=self._burst, args=(rows,), daemon=True).start()
        return True

    def _burst(self, rows: int) -> None:
        started = time.monotonic()
        result: dict[str, Any] = {"running": False, "rows": rows}
        try:
            run_once(PRIMARY_HOST, INSERT_ROWS, (rows,), statement_timeout_ms=0)
            result["seconds"] = round(time.monotonic() - started, 2)
        except psycopg.Error as exc:
            result["error"] = error_text(exc)
        with self._lock:
            self.burst = result

    def state(self) -> dict[str, Any]:
        return {
            "rows_per_sec": self.rows_per_sec,
            "inserted_total": self.inserted_total,
            "error": self.error,
            "burst": dict(self.burst),
        }


def heartbeat_loop() -> None:
    node = Node(PRIMARY_HOST)
    while True:
        try:
            node.fetch("update heartbeat set ts = clock_timestamp() where id = 1")
        except psycopg.Error as exc:
            log.warning("heartbeat: %s", error_text(exc))
            time.sleep(1)
        time.sleep(HEARTBEAT_INTERVAL)


class Poller:
    def __init__(self, loader: Loader) -> None:
        self.loader = loader
        self.primary = Node(PRIMARY_HOST)
        self.replicas = {host: Node(host) for host in REPLICA_HOSTS}
        self._prev: tuple[float, int, int] | None = None
        self._cond = threading.Condition()
        self._seq = 0
        self._snapshot: dict[str, Any] | None = None
        self._containers_lock = threading.Lock()
        self._containers_cache: dict[str, Any] | None = None

    def containers_loop(self) -> None:
        """Состояния контейнеров опрашиваются отдельно: зависший прокси не должен тормозить снимки Postgres."""
        while True:
            try:
                value = docker.states() if docker is not None else None
            except DockerError as exc:
                value = {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — поток опроса не должен умирать
                value = {"error": error_text(exc)}
            with self._containers_lock:
                self._containers_cache = value
            time.sleep(CONTAINERS_INTERVAL)

    def run(self) -> None:
        while True:
            started = time.monotonic()
            snapshot = self.collect()
            with self._cond:
                self._snapshot = snapshot
                self._seq += 1
                self._cond.notify_all()
            time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - started)))

    def wait(self, seen_seq: int, timeout: float) -> tuple[int, dict[str, Any] | None]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seen_seq, timeout)
            return self._seq, self._snapshot

    def collect(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"ts": time.time(), "loader": self.loader.state()}
        replication: dict[str, dict] = {}
        slots: dict[str, dict] = {}
        try:
            stats = self.primary.fetch(PRIMARY_STATS)[0]
            stats.update(self._rates(stats))
            replication = {row["name"]: row for row in self.primary.fetch(PRIMARY_REPLICATION)}
            slots = {row["slot_name"]: row for row in self.primary.fetch(PRIMARY_SLOTS)}
            snapshot["primary"] = stats
        except psycopg.Error as exc:
            self._prev = None
            snapshot["primary"] = {"error": error_text(exc)}

        replicas = []
        for host, node in self.replicas.items():
            item: dict[str, Any] = {
                "name": host,
                "from_primary": replication.get(host),
                "slot": slots.get(f"{host}_slot"),
            }
            try:
                item["local"] = node.fetch(REPLICA_STATS)[0]
            except psycopg.Error as exc:
                item["error"] = error_text(exc)
            replicas.append(item)
        snapshot["replicas"] = replicas
        snapshot["netem"] = netem_call("/state")[1] if NETEM_URL else None
        with self._containers_lock:
            snapshot["containers"] = self._containers_cache
        return snapshot

    def _rates(self, stats: dict) -> dict[str, float | None]:
        now = time.monotonic()
        current = (now, stats["wal_bytes"], stats["xacts"])
        prev, self._prev = self._prev, current
        if prev is None or now <= prev[0]:
            return {"wal_bytes_per_sec": None, "tps": None}
        dt = now - prev[0]
        wal_rate = (current[1] - prev[1]) / dt
        tps = (current[2] - prev[2]) / dt
        return {
            "wal_bytes_per_sec": wal_rate if wal_rate >= 0 else None,
            "tps": tps if tps >= 0 else None,
        }


loader = Loader()
poller = Poller(loader)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — без access-лога на каждый запрос
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
        elif self.path == "/events":
            self._stream()
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if (denied := csrf_error(self.headers)) is not None:
            self._json(HTTPStatus.FORBIDDEN, {"error": denied})
            return
        try:
            body = self._read_json()
            parts = self.path.strip("/").split("/")
            match parts:
                case ["api", "replicas", name, ("pause" | "resume") as action]:
                    self._replay(name, action)
                case ["api", "replicas", name, "netem"]:
                    if name not in REPLICA_HOSTS:
                        raise ValueError(f"неизвестная реплика: {name}")
                    status, result = netem_call("/netem", {**body, "replica": name})
                    self._json(HTTPStatus(status), result)
                case ["api", "replicas", name, "partition"]:
                    if name not in REPLICA_HOSTS:
                        raise ValueError(f"неизвестная реплика: {name}")
                    status, result = netem_call("/partition", {"replica": name, "enabled": body.get("enabled")})
                    self._json(HTTPStatus(status), result)
                case ["api", "services", service, action]:
                    if service not in CONTROLLED_SERVICES:
                        raise ValueError(f"неизвестный сервис: {service}")
                    if action not in ALLOWED_ACTIONS:
                        raise ValueError(f"неизвестное действие: {action}")
                    if docker is None:
                        raise DockerError("управление контейнерами не настроено")
                    docker.act(service, action)
                    self._json(HTTPStatus.OK, {"ok": True})
                case ["api", "load"]:
                    loader.rows_per_sec = clamp(body.get("rows_per_sec"), 0, MAX_ROWS_PER_SEC)
                    self._json(HTTPStatus.OK, {"ok": True})
                case ["api", "burst"]:
                    started = loader.start_burst(clamp(body.get("rows"), 1, MAX_BURST_ROWS))
                    if started:
                        self._json(HTTPStatus.OK, {"ok": True})
                    else:
                        self._json(HTTPStatus.CONFLICT, {"error": "предыдущий залп ещё идёт"})
                case ["api", "sync"]:
                    self._set_sync(body.get("standby") or "")
                case ["api", "truncate"]:
                    run_once(PRIMARY_HOST, "truncate events")
                    self._json(HTTPStatus.OK, {"ok": True})
                case _:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except DockerError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        except psycopg.Error as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": error_text(exc)})

    def _replay(self, name: str, action: str) -> None:
        if name not in REPLICA_HOSTS:
            raise ValueError(f"неизвестная реплика: {name}")
        func = "pg_wal_replay_pause" if action == "pause" else "pg_wal_replay_resume"
        run_once(name, sql.SQL("select {}()").format(sql.Identifier(func)))
        self._json(HTTPStatus.OK, {"ok": True})

    def _set_sync(self, standby: str) -> None:
        if standby and standby not in REPLICA_HOSTS:
            raise ValueError(f"неизвестная реплика: {standby}")
        value = f"FIRST 1 ({standby})" if standby else ""
        # ALTER SYSTEM не принимает параметры запроса; значение собрано из белого списка и экранировано Literal.
        run_once(PRIMARY_HOST, sql.SQL("alter system set synchronous_standby_names = {}").format(sql.Literal(value)))
        run_once(PRIMARY_HOST, "select pg_reload_conf()")
        self._json(HTTPStatus.OK, {"ok": True})

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        seq = -1
        try:
            while True:
                new_seq, snapshot = poller.wait(seq, timeout=15)
                if new_seq == seq or snapshot is None:
                    self.wfile.write(b": ping\n\n")
                else:
                    seq = new_seq
                    self.wfile.write(f"data: {json.dumps(snapshot, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("ожидался JSON-объект")
        return data

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"ожидалось целое число, получено {value!r}") from None
    return max(low, min(high, number))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for target in (poller.run, poller.containers_loop, loader.run, heartbeat_loop):
        threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on :%d, replicas: %s", PORT, ", ".join(REPLICA_HOSTS))
    server.serve_forever()


if __name__ == "__main__":
    main()
