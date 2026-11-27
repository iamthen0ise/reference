"""Агент обрывов связи брокера Kafka с пирами (другие брокеры, контроллер, раннер).

Контейнер живёт в сетевом namespace брокера (network_mode: service:kafkaN), поэтому iptables действует на его трафик.
Обрыв — правила DROP в цепочке LAB-CUT (подключена в INPUT и OUTPUT): `out` роняет пакеты к пиру, `in` — от пира.
Сверка раз в 3 с заново резолвит пиров: после рестарта контейнер получает новый IP, и правила переустанавливаются.
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

from cuts import CHAIN, DIRECTIONS, pending, restore_input

log = logging.getLogger("netem")

PEERS = [h for h in os.environ.get("PEER_HOSTS", "").split(",") if h]
PORT = int(os.environ.get("PORT", "9000"))
RECONCILE_INTERVAL = 3.0


class Cuts:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cuts: set[tuple[str, str]] = set()
        self.ips: dict[str, str | None] = {}
        self.error: str | None = None
        self.chain_ready = False
        self._applied: str | None = None

    def set(self, peer: Any, direction: Any, enabled: Any) -> None:
        if peer not in PEERS:
            raise ValueError(f"неизвестный peer: {peer}")
        if direction not in DIRECTIONS:
            raise ValueError(f"direction: ожидалось одно из {', '.join(DIRECTIONS)}")
        if not isinstance(enabled, bool):
            raise ValueError("enabled: ожидалось true или false")
        with self._lock:
            if enabled:
                self.cuts.add((peer, direction))
            else:
                self.cuts.discard((peer, direction))
            self._apply(resolve_all())

    def heal(self) -> None:
        with self._lock:
            self.cuts.clear()
            self._apply(resolve_all())

    def reconcile_loop(self) -> None:
        while True:
            time.sleep(RECONCILE_INTERVAL)
            ips = resolve_all()
            with self._lock:
                try:
                    self._apply(ips)  # набор правил не изменился — _apply ничего не делает
                except Exception:  # noqa: BLE001 — поток сверки не должен умирать
                    log.exception("reconcile failed")

    def state(self) -> dict[str, Any]:
        with self._lock:
            order = {(p, d): i for i, p in enumerate(PEERS) for d in DIRECTIONS}
            return {
                "cuts": [{"peer": p, "direction": d} for p, d in sorted(self.cuts, key=order.get)],
                "ips": self.ips,
                "pending": [{"peer": p, "direction": d} for p, d in sorted(pending(self.ips, self.cuts), key=order.get)],
                "error": self.error,
            }

    def _apply(self, ips: dict[str, str | None]) -> None:
        self.ips = ips
        if not self.chain_ready:
            try:
                setup_chain()
                self.chain_ready = True
            except (subprocess.CalledProcessError, OSError) as exc:
                self.error = f"iptables недоступен: {describe_error(exc)}"
                log.error("%s", self.error)
                return
        rules = restore_input(ips, self.cuts)
        if rules == self._applied:
            return
        try:
            subprocess.run(["iptables-restore", "-w", "--noflush"], input=rules, check=True,
                           capture_output=True, text=True)
            self._applied = rules
            self.error = None
            log.info("cuts: %s", sorted(self.cuts) or "none")
        except (subprocess.CalledProcessError, OSError) as exc:
            self._applied = None
            self.error = f"iptables-restore: {describe_error(exc)}"
            log.error("%s", self.error)


def resolve_all() -> dict[str, str | None]:
    ips: dict[str, str | None] = {}
    for name in PEERS:
        try:
            ips[name] = socket.gethostbyname(name)
        except OSError:
            ips[name] = None  # пира нет (например, раннер ещё не запущен) — обрыв ждёт его IP
    return ips


def describe_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"{' '.join(map(str, exc.cmd))}: {(exc.stderr or '').strip()}"
    return str(exc)


def setup_chain() -> None:
    """Своя цепочка, подключённая в INPUT и OUTPUT один раз; чужие правила не трогаем, свою цепочку очищаем."""
    subprocess.run(["iptables", "-w", "-N", CHAIN], capture_output=True)  # уже есть — не ошибка
    for hook in ("INPUT", "OUTPUT"):
        if subprocess.run(["iptables", "-w", "-C", hook, "-j", CHAIN], capture_output=True).returncode != 0:
            subprocess.run(["iptables", "-w", "-I", hook, "-j", CHAIN], check=True, capture_output=True, text=True)
    subprocess.run(["iptables", "-w", "-F", CHAIN], check=True, capture_output=True, text=True)


cuts = Cuts()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — без access-лога
        pass

    def do_GET(self) -> None:
        if self.path == "/state":
            self._json(HTTPStatus.OK, cuts.state())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/cut", "/heal"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            if self.path == "/heal":
                cuts.heal()
            else:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("ожидался JSON-объект")
                cuts.set(body.get("peer"), body.get("direction"), body.get("enabled"))
        except ValueError as exc:  # включает JSONDecodeError
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        state = cuts.state()
        self._json(HTTPStatus.BAD_GATEWAY if state["error"] else HTTPStatus.OK, state)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with cuts._lock:
        cuts._apply(resolve_all())  # очищает остаток прошлого запуска агента в том же namespace
    threading.Thread(target=cuts.reconcile_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on :%d, peers: %s", PORT, ", ".join(PEERS))
    server.serve_forever()


if __name__ == "__main__":
    main()
