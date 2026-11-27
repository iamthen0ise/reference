"""Цели сбоев: выражения сценария превращаются в имена контейнеров по текущей раскладке партиций."""

import re
from dataclasses import dataclass

from lab.scenario import ScenarioError

BROKERS = {1: "kafka1", 2: "kafka2", 3: "kafka3"}

_BROKER = re.compile(r"^kafka(\d+)$")
_PARTITION_FN = re.compile(r"^(leader|follower|followers)\(p(\d+)\)$")
_BINDING = re.compile(r"^\$(\w+)$")


@dataclass(frozen=True)
class PartitionState:
    leader: int | None
    replicas: tuple[int, ...]
    isr: tuple[int, ...]


def _name(broker_id: int, expr: str) -> str:
    if broker_id not in BROKERS:
        raise ScenarioError(f"цель {expr!r}: нет брокера с id {broker_id}")
    return BROKERS[broker_id]


def _by_partition(kind: str, state: PartitionState, expr: str) -> list[str]:
    if state.leader is None:
        return []
    if kind == "leader":
        return [_name(state.leader, expr)]
    followers = [_name(b, expr) for b in state.replicas if b != state.leader]
    return followers[:1] if kind == "follower" else followers


def resolve(expr: str, layout: dict[int, PartitionState], bindings: dict[str, str]) -> list[str]:
    """Имена контейнеров цели; пустой список — цель не найдена, неверный синтаксис — ScenarioError."""
    text = expr.strip()
    if text == "runner":
        return ["runner"]
    if match := _BROKER.match(text):
        return [_name(int(match.group(1)), expr)]
    if match := _BINDING.match(text):
        value = bindings.get(match.group(1))
        return [] if value is None else [value]
    if match := _PARTITION_FN.match(text):
        state = layout.get(int(match.group(2)))
        return [] if state is None else _by_partition(match.group(1), state, expr)
    raise ScenarioError(
        f"цель {expr!r}: ожидается kafkaN, runner, leader(pN), follower(pN), followers(pN) или $имя"
    )
