import asyncio

import pytest

from lab.brokers import Brokers
from lab.scenario import ScenarioError


class FakeDocker:
    def __init__(self, states: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fake_states = states or {f"kafka{i}": {"state": "exited"} for i in (1, 2, 3)}
        self.lag = 0

    def act(self, service: str, action: str) -> None:
        self.calls.append((service, action))

    def states(self) -> dict[str, dict]:
        if self.lag > 0:
            self.lag -= 1
            return {k: {"state": "running"} for k in self.fake_states}
        return self.fake_states


class FakeNet:
    def __init__(self, errors: list[str | None]) -> None:
        self.errors = errors
        self.n = 0

    async def state(self) -> dict[str, dict]:
        err = self.errors[min(self.n, len(self.errors) - 1)]
        self.n += 1
        return {f"kafka{i}": {"error": err} for i in (1, 2, 3)}


def make(errors: list[str | None] | None = None, states: dict | None = None) -> tuple[Brokers, FakeDocker, FakeNet]:
    docker, net = FakeDocker(states), FakeNet(errors or [None])
    return Brokers(docker, net, sidecar_timeout=1.0, poll=0.01), docker, net  # type: ignore[arg-type]


def test_start_restarts_sidecar() -> None:
    b, docker, _ = make()
    asyncio.run(b.act("kafka2", "start"))
    assert docker.calls == [("kafka2", "start"), ("netem2", "restart")]


def test_start_waits_for_sidecar_state() -> None:
    b, docker, net = make(errors=["boom", "boom", None])
    asyncio.run(b.act("kafka2", "start"))
    assert net.n == 3


def test_start_fails_when_sidecar_never_ready() -> None:
    b, _, _ = make(errors=["boom"])
    with pytest.raises(RuntimeError, match="netem2"):
        asyncio.run(b.act("kafka2", "start"))


@pytest.mark.parametrize("action", ["kill", "stop"])
def test_kill_does_not_touch_sidecar(action: str) -> None:
    b, docker, _ = make()
    asyncio.run(b.act("kafka1", action))
    assert docker.calls == [("kafka1", action)]


def test_act_rejects_non_broker_and_bad_action() -> None:
    b, docker, _ = make()
    for svc in ("controller", "netem1", "runner", "x"):
        with pytest.raises(ScenarioError):
            asyncio.run(b.act(svc, "kill"))
    with pytest.raises(ScenarioError):
        asyncio.run(b.act("kafka1", "restart"))
    assert docker.calls == []


def test_stopped_lists_non_running_brokers() -> None:
    states = {
        "kafka1": {"state": "running"},
        "kafka2": {"state": "exited"},
        "kafka3": {"state": "missing"},
        "controller": {"state": "exited"},
    }
    b, _, _ = make(states=states)
    assert asyncio.run(b.stopped()) == ["kafka2", "kafka3"]


def test_kill_waits_until_container_is_down() -> None:
    b, docker, _ = make()
    docker.lag = 3
    asyncio.run(b.act("kafka1", "kill"))
    assert docker.lag == 0


def test_kill_fails_if_container_stays_up() -> None:
    b, docker, _ = make(states={"kafka1": {"state": "running"}})
    with pytest.raises(RuntimeError, match="kafka1"):
        asyncio.run(b.act("kafka1", "kill"))
