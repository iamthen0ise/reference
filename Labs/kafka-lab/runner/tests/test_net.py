import asyncio
import io
import json
import urllib.error

import pytest

from lab import net
from lab.net import Net, plan_cut
from lab.scenario import ScenarioError


def test_plan_cut_broker_to_broker_two_way() -> None:
    assert plan_cut("kafka1", "kafka2", one_way=False) == [
        ("kafka1", "kafka2", "out"),
        ("kafka1", "kafka2", "in"),
    ]


def test_plan_cut_one_way_out() -> None:
    assert plan_cut("kafka1", "kafka2", one_way=True) == [("kafka1", "kafka2", "out")]


def test_plan_cut_runner_to_broker_one_way() -> None:
    assert plan_cut("runner", "kafka1", one_way=True) == [("kafka1", "runner", "in")]


def test_plan_cut_controller_to_broker_two_way() -> None:
    assert plan_cut("controller", "kafka2", one_way=False) == [
        ("kafka2", "controller", "in"),
        ("kafka2", "controller", "out"),
    ]


@pytest.mark.parametrize(
    ("src", "dst"), [("runner", "controller"), ("kafka1", "kafka1x"), ("a", "b"), ("kafka1", "runner2")]
)
def test_plan_cut_rejects_non_brokers(src: str, dst: str) -> None:
    with pytest.raises(ScenarioError):
        plan_cut(src, dst, one_way=False)


class FakeAgents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.down: set[str] = set()
        self.reply: dict[str, tuple[int, dict]] = {}

    def __call__(self, method: str, url: str, body: dict | None, timeout: float) -> tuple[int, dict]:
        host = url.split("//")[1].split(":")[0]
        self.calls.append((method, host, url.rsplit("/", 1)[1], body))
        if host in self.down:
            raise OSError("connection refused")
        return self.reply.get(host, (200, {"cuts": [], "error": None}))


def make() -> tuple[Net, FakeAgents]:
    fake = FakeAgents()
    return Net(request=fake), fake


def test_cut_posts_to_agents() -> None:
    n, fake = make()
    asyncio.run(n.cut("kafka1", "kafka2", one_way=False))
    assert fake.calls == [
        ("POST", "kafka1", "cut", {"peer": "kafka2", "direction": "out", "enabled": True}),
        ("POST", "kafka1", "cut", {"peer": "kafka2", "direction": "in", "enabled": True}),
    ]


def test_cut_raises_with_agent_error_text() -> None:
    n, fake = make()
    fake.reply["kafka1"] = (400, {"error": "неизвестный peer: x"})
    with pytest.raises(net.NetError, match="неизвестный peer"):
        asyncio.run(n.cut("kafka1", "kafka2", one_way=True))
    fake.reply["kafka1"] = (502, {"error": "iptables сломан", "cuts": []})
    with pytest.raises(net.NetError, match="iptables сломан"):
        asyncio.run(n.cut("kafka1", "kafka2", one_way=True))


def test_heal_all_tolerates_unreachable_agent() -> None:
    n, fake = make()
    fake.down.add("kafka2")
    failures = asyncio.run(n.heal_all())
    assert sorted(h for _, h, _, _ in fake.calls) == ["kafka1", "kafka2", "kafka3"]
    assert list(failures) == ["kafka2"]


def test_state_reports_error_instead_of_raising() -> None:
    n, fake = make()
    fake.down.add("kafka3")
    state = asyncio.run(n.state())
    assert state["kafka1"]["error"] is None
    assert "refused" in state["kafka3"]["error"]


def test_decode_helper_handles_http_error() -> None:
    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(json.dumps({"error": "x"}).encode()))  # type: ignore[arg-type]
    assert net.decode_error(err) == (400, {"error": "x"})
