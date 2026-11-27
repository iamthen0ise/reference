import pytest

from lab.scenario import ScenarioError
from lab.targets import BROKERS, PartitionState, resolve

LAYOUT = {0: PartitionState(leader=2, replicas=(1, 2, 3), isr=(1, 2, 3))}


def test_brokers() -> None:
    assert BROKERS == {1: "kafka1", 2: "kafka2", 3: "kafka3"}


def test_resolve_literal() -> None:
    assert resolve("kafka3", LAYOUT, {}) == ["kafka3"]
    assert resolve("runner", LAYOUT, {}) == ["runner"]


def test_resolve_leader() -> None:
    assert resolve("leader(p0)", LAYOUT, {}) == ["kafka2"]


def test_resolve_follower_and_followers() -> None:
    assert resolve("follower(p0)", LAYOUT, {}) == ["kafka1"]
    assert resolve("followers(p0)", LAYOUT, {}) == ["kafka1", "kafka3"]


def test_resolve_binding() -> None:
    assert resolve("$old_leader", LAYOUT, {"old_leader": "kafka2"}) == ["kafka2"]
    assert resolve("$unknown", LAYOUT, {}) == []


def test_resolve_missing_follower() -> None:
    layout = {0: PartitionState(leader=1, replicas=(1,), isr=(1,))}
    assert resolve("follower(p0)", layout, {}) == []
    assert resolve("followers(p0)", layout, {}) == []


def test_resolve_no_leader() -> None:
    layout = {0: PartitionState(leader=None, replicas=(1, 2, 3), isr=())}
    assert resolve("leader(p0)", layout, {}) == []


def test_resolve_unknown_partition() -> None:
    assert resolve("leader(p5)", LAYOUT, {}) == []


@pytest.mark.parametrize(
    "expr", ["", "leader(0)", "leader(p0", "kafka9", "nginx", "$", "leader(p0) x"]
)
def test_resolve_bad_syntax(expr: str) -> None:
    with pytest.raises(ScenarioError):
        resolve(expr, LAYOUT, {})
