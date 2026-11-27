"""Управление брокерами через Docker: kill/stop/start; start заодно перезапускает сайдкар netem."""

import asyncio
import os
import time
from typing import Literal, Protocol

from lab.docker_control import DockerControl
from lab.net import Net
from lab.scenario import ScenarioError
from lab.targets import BROKERS

Action = Literal["kill", "stop", "start"]
ACTIONS = ("kill", "stop", "start")
SIDECAR_TIMEOUT = 15.0
SIDECAR_POLL = 0.5
SERVICES = frozenset(BROKERS.values()) | frozenset(f"netem{i}" for i in BROKERS)


class _Docker(Protocol):
    def act(self, service: str, action: str) -> None: ...
    def states(self) -> dict[str, dict]: ...


def default_docker() -> DockerControl:
    return DockerControl(
        os.environ["DOCKER_PROXY_URL"], os.environ.get("COMPOSE_PROJECT", "kafka-lab"), SERVICES
    )


class Brokers:
    def __init__(
        self,
        docker: _Docker,
        net: Net,
        sidecar_timeout: float = SIDECAR_TIMEOUT,
        poll: float = SIDECAR_POLL,
    ) -> None:
        self._docker = docker
        self._net = net
        self._sidecar_timeout = sidecar_timeout
        self._poll = poll

    async def act(self, service: str, action: Action) -> None:
        if service not in BROKERS.values():
            raise ScenarioError(
                f"{action} {service}: управляются только брокеры {', '.join(BROKERS.values())}"
            )
        if action not in ACTIONS:
            raise ScenarioError(
                f"{service}: действие {action!r} не поддерживается (kill, stop, start)"
            )
        await asyncio.to_thread(self._docker.act, service, action)
        if action == "start":
            await self._restart_sidecar(service)
        else:
            await self._wait_down(service)

    async def _restart_sidecar(self, service: str) -> None:
        # Правила iptables живут в netns брокера: после его перезапуска сайдкар смотрит в мёртвый netns, состояние устарело.
        sidecar = "netem" + service.removeprefix("kafka")
        await asyncio.to_thread(self._docker.act, sidecar, "restart")
        deadline = time.monotonic() + self._sidecar_timeout
        last = "нет ответа"
        while True:
            entry = (await self._net.state()).get(service, {})
            if not entry.get("error"):
                return
            last = str(entry["error"])
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{sidecar} не ответил за {self._sidecar_timeout:g} с: {last}")
            await asyncio.sleep(self._poll)

    async def _wait_down(self, service: str) -> None:
        # Docker отвечает на kill раньше, чем контейнер меняет состояние: без ожидания stopped() ещё покажет брокер живым.
        deadline = time.monotonic() + self._sidecar_timeout
        while service not in await self.stopped():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{service} не остановился за {self._sidecar_timeout:g} с")
            await asyncio.sleep(self._poll)

    async def stopped(self) -> list[str]:
        states = await asyncio.to_thread(self._docker.states)
        return [s for s in BROKERS.values() if states.get(s, {}).get("state") != "running"]
