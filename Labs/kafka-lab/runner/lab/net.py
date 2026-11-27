"""Обрывы связи: HTTP к агентам netem в сетевых namespace брокеров (порт 9000)."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from lab.scenario import ScenarioError
from lab.targets import BROKERS

AGENT_PORT = 9000
HTTP_TIMEOUT = 5.0
_EXTERNAL = ("runner", "controller")

Request = Callable[[str, str, dict | None, float], tuple[int, dict]]


class NetError(Exception):
    pass


def plan_cut(src: str, dst: str, one_way: bool) -> list[tuple[str, str, str]]:
    """Тройки (агент-брокер, peer, direction) для обрыва src -> dst (и обратно, если не one_way)."""
    brokers = set(BROKERS.values())
    if src in brokers:
        plan = [(src, dst, "out"), (src, dst, "in")]
    elif src in _EXTERNAL and dst in brokers:
        plan = [(dst, src, "in"), (dst, src, "out")]
    else:
        raise ScenarioError(
            f"cut {src} -> {dst}: нужен хотя бы один брокер kafkaN (другой конец — брокер, runner или controller)"
        )
    if src in brokers and dst not in brokers and dst not in _EXTERNAL:
        raise ScenarioError(f"cut {src} -> {dst}: peer должен быть брокером, runner или controller")
    if src == dst:
        raise ScenarioError(f"cut {src} -> {dst}: концы совпадают")
    return plan[:1] if one_way else plan


def decode_error(exc: urllib.error.HTTPError) -> tuple[int, dict]:
    try:
        body = json.loads(exc.read())
    except ValueError:
        body = {}
    return exc.code, body if isinstance(body, dict) else {}


def _http(method: str, url: str, body: dict | None, timeout: float) -> tuple[int, dict]:
    # Адрес собирается из фиксированного списка брокеров, не из пользовательского ввода.
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            parsed = json.loads(resp.read() or b"{}")
            return resp.status, parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        return decode_error(exc)


class Net:
    def __init__(self, request: Request = _http) -> None:
        self._request = request

    async def _call(
        self, broker: str, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict]:
        url = f"http://{broker}:{AGENT_PORT}{path}"
        return await asyncio.to_thread(self._request, method, url, body, HTTP_TIMEOUT)

    async def cut(self, src: str, dst: str, one_way: bool) -> None:
        for broker, peer, direction in plan_cut(src, dst, one_way):
            try:
                status, data = await self._call(
                    broker, "POST", "/cut", {"peer": peer, "direction": direction, "enabled": True}
                )
            except OSError as exc:
                raise NetError(f"агент {broker} недоступен: {exc}") from exc
            if status != HTTPStatus.OK:
                raise NetError(f"агент {broker}: HTTP {status}: {data.get('error') or data}")

    async def heal_all(self) -> dict[str, str]:
        """Снимает обрывы на всех агентах; возвращает {брокер: причина} для недоступных или отказавших."""

        async def one(broker: str) -> str | None:
            try:
                status, data = await self._call(broker, "POST", "/heal")
            except OSError as exc:
                return str(exc)
            return None if status == HTTPStatus.OK else f"HTTP {status}: {data.get('error')}"

        names = list(BROKERS.values())
        results = await asyncio.gather(*(one(b) for b in names))
        return {b: r for b, r in zip(names, results, strict=True) if r is not None}

    async def state(self) -> dict[str, dict[str, Any]]:
        async def one(broker: str) -> dict[str, Any]:
            try:
                status, data = await self._call(broker, "GET", "/state")
            except OSError as exc:
                return {"error": str(exc)}
            if status != HTTPStatus.OK:
                return {"error": f"HTTP {status}: {data.get('error')}"}
            return data

        names = list(BROKERS.values())
        results = await asyncio.gather(*(one(b) for b in names))
        return dict(zip(names, results, strict=True))
