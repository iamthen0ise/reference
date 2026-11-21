"""Демо-приложение для страниц «Docker и Kubernetes»: пробы, версия, сбои по переменным окружения."""

import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
MIB = 1024 * 1024
PAGE = 4096

STARTED = time.monotonic()
APP_VERSION = os.environ.get("APP_VERSION", "dev")
READY_DELAY = float(os.environ.get("READY_DELAY_SECONDS", "0"))
IS_NOT_READY = os.environ.get("NOT_READY") == "1"


def is_warm() -> bool:
    return time.monotonic() - STARTED >= READY_DELAY


class Handler(BaseHTTPRequestHandler):
    def reply(self, *, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        if self.path not in ("/healthz", "/ready"):
            print(f"{self.command} {self.path} {code}", flush=True)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.reply(code=200, body="ok")
        elif self.path == "/ready":
            if IS_NOT_READY or not is_warm():
                self.reply(code=503, body="warming up")
            else:
                self.reply(code=200, body="ready")
        elif self.path == "/":
            if is_warm():
                self.reply(code=200, body=f"version={APP_VERSION} pod={socket.gethostname()}")
            else:
                self.reply(code=503, body="warming up")
        else:
            self.reply(code=404, body="not found")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def allocate(*, mib: int) -> bytearray:
    buf = bytearray(mib * MIB)
    for offset in range(0, len(buf), PAGE):
        buf[offset] = 1
    return buf


def burn_cpu() -> None:
    while True:
        pass


def main() -> None:
    if os.environ.get("CRASH_ON_START") == "1":
        print("fatal: CRASH_ON_START=1", file=sys.stderr, flush=True)
        sys.exit(1)

    alloc_mb = int(os.environ.get("ALLOC_MB", "0"))
    held = allocate(mib=alloc_mb) if alloc_mb > 0 else None
    if held is not None:
        print(f"allocated {alloc_mb} MiB", flush=True)

    if os.environ.get("CPU_BURN") == "1":
        threading.Thread(target=burn_cpu, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    if os.environ.get("IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:

        def on_term(signum: int, frame: object) -> None:
            print("got TERM, shutting down", flush=True)
            threading.Thread(target=server.shutdown).start()

        signal.signal(signal.SIGTERM, on_term)

    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
