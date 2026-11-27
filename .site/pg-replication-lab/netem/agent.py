"""Агент netem: задержка, потери, ограничение полосы и полный обрыв связи primary ↔ реплика.

Контейнер живёт в сетевом namespace primary (network_mode: service:primary), поэтому qdisc на eth0
действует на исходящий трафик primary — поток WAL к реплике. Трафик разводится по IP реплики:
prio-qdisc, у каждой реплики свой band с netem и u32-фильтр по адресу назначения; остальное идёт в band 1:1 без задержки.
Обрыв — отдельная iptables-цепочка LAB-PARTITION (INPUT и OUTPUT): DROP пакетов от реплики и к ней. tc и iptables независимы.
"""

import json
import logging
import os
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from partition import CHAIN, needs_reapply, partition_pending, restore_input

log = logging.getLogger("netem")

DEV = os.environ.get("NETEM_DEV", "eth0")
REPLICAS = [h for h in os.environ.get("REPLICA_HOSTS", "").split(",") if h]
PORT = int(os.environ.get("PORT", "9000"))
RECONCILE_INTERVAL = 3.0
ORDER_KEEPING_RATE = "10gbit"

# Поле: (минимум, максимум, целое ли).
FIELDS: dict[str, tuple[float, float, bool]] = {
    # Потолок ниже wal_sender_timeout / wal_receiver_timeout (10 с): иначе репликация рвётся по таймауту, а не из-за задержки.
    "delay_ms": (0, 5_000, True),
    "jitter_ms": (0, 5_000, True),
    "loss_pct": (0, 100, False),
    "rate_mbit": (0, 10_000, False),
}
EMPTY = {name: 0 for name in FIELDS}


class Netem:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.desired: dict[str, dict[str, float]] = {name: dict(EMPTY) for name in REPLICAS}
        self.partitioned: set[str] = set()
        self.applied_ips: dict[str, str | None] = {}
        self.last_ips: dict[str, str | None] = {}
        self.tc_error: str | None = None
        self.partition_error: str | None = None
        self.chain_ready = False
        self._applied_rules: str | None = None
        self._applied_tc: Any = None

    @property
    def error(self) -> str | None:
        return "; ".join(e for e in (self.tc_error, self.partition_error) if e) or None

    def init_chain(self) -> None:
        """Ошибка iptables не должна ронять агента: tc продолжает работать, обрыв сообщает ошибку."""
        try:
            setup_chain()
            self.chain_ready = True
            self.partition_error = None
        except (subprocess.CalledProcessError, OSError) as exc:
            self.chain_ready = False
            self.partition_error = f"iptables недоступен: {describe_error(exc)}"
            log.error("%s", self.partition_error)

    def set(self, replica: str, params: dict[str, Any]) -> None:
        if replica not in REPLICAS:
            raise ValueError(f"неизвестная реплика: {replica}")
        clean = {name: parse_field(name, params.get(name, 0)) for name in FIELDS}
        with self._lock:
            self.desired[replica] = clean
            self._apply(resolve_all())

    def set_partition(self, replica: str, enabled: Any) -> None:
        if replica not in REPLICAS:
            raise ValueError(f"неизвестная реплика: {replica}")
        if not isinstance(enabled, bool):
            raise ValueError("enabled: ожидалось true или false")
        with self._lock:
            if enabled:
                self.partitioned.add(replica)
            else:
                self.partitioned.discard(replica)
            self._apply(resolve_all())

    def reconcile_loop(self) -> None:
        """Реплика после рестарта может получить новый IP — тогда фильтры пересобираются."""
        while True:
            time.sleep(RECONCILE_INTERVAL)
            ips = resolve_all()
            with self._lock:
                any_active = bool(self.partitioned) or any(is_active(p) for p in self.desired.values())
                if needs_reapply(any_active, ips, self.applied_ips):
                    log.info("replica IPs changed: %s -> %s", self.applied_ips, ips)
                    try:
                        self._apply(ips)
                    except Exception:  # noqa: BLE001 — поток сверки не должен умирать
                        log.exception("reconcile failed")

    def state(self) -> dict[str, Any]:
        with self._lock:
            pending = partition_pending(self.last_ips, self.partitioned)
            replicas = {
                name: {**params, "ip": self.applied_ips.get(name), "active": is_active(params),
                       "partitioned": name in self.partitioned,
                       "partition_pending": name in pending}
                for name, params in self.desired.items()
            }
            return {"dev": DEV, "replicas": replicas, "error": self.error}

    def _apply(self, ips: dict[str, str | None]) -> None:
        """Применяет tc и iptables из desired/partitioned; ошибки хранятся раздельно."""
        self.last_ips = ips
        self.tc_error = self._apply_tc(ips)
        self.partition_error = self._apply_partition(ips)
        if not self.tc_error and not self.partition_error:
            self.applied_ips = ips

    def _apply_partition(self, ips: dict[str, str | None]) -> str | None:
        if not self.chain_ready:
            self.init_chain()
            if not self.chain_ready:
                return self.partition_error
        rules = restore_input(ips, self.partitioned)
        if rules == self._applied_rules:
            return None  # набор правил не изменился — цепочку не трогаем
        try:
            subprocess.run(["iptables-restore", "-w", "--noflush"], input=rules, check=True,
                           capture_output=True, text=True)
            self._applied_rules = rules
            log.info("partition: %s", sorted(self.partitioned) or "none")
            return None
        except (subprocess.CalledProcessError, OSError) as exc:
            self._applied_rules = None
            log.error("iptables-restore failed: %s", describe_error(exc))
            return f"iptables-restore: {describe_error(exc)}"

    def _apply_tc(self, ips: dict[str, str | None]) -> str | None:
        active = {n: p for n, p in self.desired.items() if is_active(p) and ips.get(n)}
        key = {n: (ips[n], tuple(sorted(p.items()))) for n, p in active.items()}
        if key == self._applied_tc:
            return None  # tc для активных реплик не изменился (например, переключили только обрыв)
        try:
            # Пересобираем с нуля: так состояние qdisc всегда совпадает с desired.
            subprocess.run(["tc", "qdisc", "del", "dev", DEV, "root"], capture_output=True)  # нет root — не ошибка
            if active:
                tc("qdisc", "add", "dev", DEV, "root", "handle", "1:", "prio",
                   "bands", str(len(REPLICAS) + 1), "priomap", *["0"] * 16)
                for index, name in enumerate(REPLICAS):
                    if name not in active:
                        continue
                    band = index + 2
                    tc("qdisc", "add", "dev", DEV, "parent", f"1:{band}", "handle", f"{band}0:",
                       *netem_args(active[name]))
                    tc("filter", "add", "dev", DEV, "parent", "1:", "protocol", "ip", "prio", "1",
                       "u32", "match", "ip", "dst", f"{ips[name]}/32", "flowid", f"1:{band}")
            self._applied_tc = key
            log.info("applied: %s", {n: (ips.get(n), p) for n, p in active.items()} or "none")
            return None
        except (subprocess.CalledProcessError, OSError) as exc:
            self._applied_tc = None
            log.error("tc failed: %s", describe_error(exc))
            return f"tc: {describe_error(exc)}"


def netem_args(p: dict[str, float]) -> list[str]:
    # limit по умолчанию 1000 пакетов: при большой задержке и потоке WAL очередь переполнится и начнутся лишние потери.
    args = ["netem", "limit", "100000"]
    if p["delay_ms"]:
        args += ["delay", f"{int(p['delay_ms'])}ms"]
        if p["jitter_ms"]:
            args += [f"{int(p['jitter_ms'])}ms"]
    if p["loss_pct"]:
        args += ["loss", f"{p['loss_pct']:g}%"]
    if p["rate_mbit"]:
        args += ["rate", f"{p['rate_mbit']:g}mbit"]
    elif p["delay_ms"] and p["jitter_ms"]:
        # Без rate netem с jitter выдаёт пакеты в порядке случайных времён отправки, то есть переупорядочивает их:
        # TCP primary видит reordering 300, спурионные ретрансмиты, cwnd падает до единиц, лаг растёт без предела.
        # С rate очередь становится FIFO (пакет не уходит раньше предыдущего), а 10 Гбит/с на задержку не влияет.
        args += ["rate", ORDER_KEEPING_RATE]
    return args


def is_active(p: dict[str, float]) -> bool:
    return any(p[name] for name in ("delay_ms", "loss_pct", "rate_mbit"))


def parse_field(name: str, value: Any) -> float:
    low, high, integer = FIELDS[name]
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: ожидалось число, получено {value!r}") from None
    if not low <= number <= high:
        raise ValueError(f"{name}: допустимо от {low:g} до {high:g}")
    return int(number) if integer else round(number, 3)


def resolve_all() -> dict[str, str | None]:
    ips: dict[str, str | None] = {}
    for name in REPLICAS:
        try:
            ips[name] = socket.gethostbyname(name)
        except OSError:
            ips[name] = None
    return ips


def describe_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"{' '.join(map(str, exc.cmd))}: {(exc.stderr or '').strip()}"
    return str(exc)


def tc(*args: str) -> None:
    subprocess.run(["tc", *args], check=True, capture_output=True, text=True)


def iptables(*args: str) -> None:
    subprocess.run(["iptables", "-w", *args], check=True, capture_output=True, text=True)


def setup_chain() -> None:
    """Своя цепочка, подключённая в INPUT и OUTPUT один раз; чужие правила не трогаем, свою цепочку очищаем."""
    subprocess.run(["iptables", "-w", "-N", CHAIN], capture_output=True)  # уже есть — не ошибка
    for hook in ("INPUT", "OUTPUT"):
        if subprocess.run(["iptables", "-w", "-C", hook, "-j", CHAIN], capture_output=True).returncode != 0:
            iptables("-I", hook, "-j", CHAIN)
    iptables("-F", CHAIN)


netem = Netem()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — без access-лога
        pass

    def do_GET(self) -> None:
        if self.path == "/state":
            self._json(HTTPStatus.OK, netem.state())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/netem", "/partition"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("ожидался JSON-объект")
            if self.path == "/partition":
                netem.set_partition(str(body.get("replica")), body.get("enabled"))
            else:
                netem.set(str(body.get("replica")), body)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        state = netem.state()
        # Ошибка iptables не должна превращать успешную настройку tc в 502, и наоборот.
        failed = netem.partition_error if self.path == "/partition" else netem.tc_error
        status = HTTPStatus.BAD_GATEWAY if failed else HTTPStatus.OK
        self._json(status, state)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Остаток от прошлого запуска агента в том же namespace — снять, состояние начинается с нуля.
    subprocess.run(["tc", "qdisc", "del", "dev", DEV, "root"], capture_output=True)
    netem.init_chain()
    threading.Thread(target=netem.reconcile_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on :%d, dev %s, replicas: %s", PORT, DEV, ", ".join(REPLICAS))
    server.serve_forever()


if __name__ == "__main__":
    main()
