import asyncio
import time
from pathlib import Path

import pytest

from lab.journal import Journal
from lab.run import Clients, Deps, run_scenario
from lab.scenario import ScenarioError, load_scenario
from lab.tally import Ack, Seen
from lab.targets import PartitionState

BASE = """
topic: {partitions: 1, replication: 3}
producer: {acks: all, rate: 10, seconds: %(seconds)s}
settle_seconds: 0
observers: [read_committed]
faults:
%(faults)s
"""


def scenario(faults: str, seconds: float = 0.4):
    return load_scenario("t", BASE % {"faults": faults, "seconds": seconds}, [])


class FakeCluster:
    def __init__(self, layouts: list[dict[int, PartitionState]]) -> None:
        self.layouts = layouts
        self.layout_calls = 0
        self.healthy_calls = 0

    async def recreate_topic(self, spec: object) -> None:
        pass

    async def layout(self, topic: str) -> dict[int, PartitionState]:
        i = min(self.layout_calls, len(self.layouts) - 1)
        self.layout_calls += 1
        return self.layouts[i]

    async def wait_healthy(self, topic: str, timeout: float) -> bool:
        self.healthy_calls += 1
        return True


class FakeBrokers:
    def __init__(self, fail_start: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.down: list[str] = []
        self.fail_start = fail_start

    async def act(self, service: str, action: str) -> None:
        self.calls.append((service, action))
        if action in ("kill", "stop"):
            self.down.append(service)
        elif action == "start":
            if self.fail_start:
                raise RuntimeError("не стартует")
            self.down.remove(service)

    async def stopped(self) -> list[str]:
        return list(self.down)


class FakeNet:
    def __init__(self) -> None:
        self.cuts: list[tuple[str, str, bool]] = []
        self.heals = 0

    async def cut(self, src: str, dst: str, one_way: bool) -> None:
        self.cuts.append((src, dst, one_way))

    async def heal_all(self) -> dict[str, str]:
        self.heals += 1
        return {}


def layout(leader: int) -> dict[int, PartitionState]:
    return {0: PartitionState(leader=leader, replicas=(1, 2, 3), isr=(1, 2, 3))}


async def fake_producer(spec, topic, bootstrap, stop, journal: Journal, ready=None) -> None:
    if ready is not None:
        ready.set()
    journal.sent += 1
    journal.acks.append(Ack(seq=0, partition=0, offset=0, t=time.monotonic()))
    await stop.wait()


async def fake_observer(isolation, topic, bootstrap, stop, journal: Journal) -> None:
    journal.seen.setdefault(isolation, []).append(Seen(seq=0, partition=0, offset=0, t=time.monotonic()))
    await stop.wait()


async def fake_read_all(topic, bootstrap, isolation, timeout):
    return [Seen(seq=0, partition=0, offset=0, t=0.0)], set()


async def fake_hang(topic, bootstrap, seconds) -> None:
    await asyncio.sleep(seconds)


CLIENTS = Clients(
    run_producer=fake_producer,
    hang_transaction=fake_hang,
    run_observer=fake_observer,
    read_all=fake_read_all,
)


def deps(cluster: FakeCluster, brokers: FakeBrokers, net: FakeNet) -> Deps:
    return Deps(cluster=cluster, brokers=brokers, net=net, bootstrap="x:9092", clock=time.monotonic)  # type: ignore[arg-type]


def run(s, d: Deps, tmp_path: Path, out: list[str]) -> Path:
    return asyncio.run(run_scenario(s, d, out.append, clients=CLIENTS, reports_dir=tmp_path))


def test_faults_fire_in_order_and_resolve_late(tmp_path: Path) -> None:
    s = scenario(
        "  - {at: 0s, kill: 'leader(p0)'}\n  - {at: 0.1s, kill: 'leader(p0)'}", seconds=0.3
    )
    brokers = FakeBrokers()
    cluster = FakeCluster([layout(1)])

    async def following_layout(topic: str) -> dict[int, PartitionState]:
        # лидер — первый живой брокер: убили лидера, значит лидер сменился
        return layout(next(i for i in (1, 2, 3) if f"kafka{i}" not in brokers.down))

    cluster.layout = following_layout  # type: ignore[method-assign]
    out: list[str] = []
    run(s, deps(cluster, brokers, FakeNet()), tmp_path, out)
    kills = [c for c in brokers.calls if c[1] == "kill"]
    assert kills == [("kafka1", "kill"), ("kafka2", "kill")]
    assert any("kill kafka1 (лидер p0)" in line for line in out)


def test_missing_target_logs_event(tmp_path: Path) -> None:
    s = scenario("  - {at: 0s, kill: '$nobody'}\n  - {at: 0.05s, cut: kafka1, peer: kafka2}", 0.2)
    brokers, net = FakeBrokers(), FakeNet()
    out: list[str] = []
    run(s, deps(FakeCluster([layout(1)]), brokers, net), tmp_path, out)
    assert any("цель не найдена: $nobody" in line for line in out)
    assert net.cuts == [("kafka1", "kafka2", False)]
    assert brokers.calls == []


def test_failing_fault_is_logged_and_run_continues(tmp_path: Path) -> None:
    s = scenario("  - {at: 0s, start: kafka1}\n  - {at: 0.05s, cut: kafka1, peer: kafka2}", 0.2)

    class Failing(FakeBrokers):
        async def act(self, service: str, action: str) -> None:
            raise RuntimeError("Docker 409")

    net = FakeNet()
    out: list[str] = []
    run(s, deps(FakeCluster([layout(1)]), Failing(), net), tmp_path, out)
    assert any("Docker 409" in line for line in out)
    assert net.cuts


def test_cleanup_on_cancel(tmp_path: Path) -> None:
    s = scenario("  - {at: 0s, kill: kafka2}\n  - {at: 0s, cut: kafka1, peer: kafka3}", seconds=60)
    cluster, brokers, net = FakeCluster([layout(1)]), FakeBrokers(), FakeNet()

    async def go() -> None:
        task = asyncio.create_task(
            run_scenario(s, deps(cluster, brokers, net), lambda _: None, clients=CLIENTS, reports_dir=tmp_path)
        )
        while not (net.cuts and brokers.calls):
            await asyncio.sleep(0.01)
        heals_before = net.heals
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert net.heals > heals_before

    asyncio.run(go())
    assert ("kafka2", "start") in brokers.calls
    assert brokers.down == []
    assert cluster.healthy_calls == 2  # после создания и в очистке
    assert list(tmp_path.iterdir()) == []


def test_cleanup_steps_independent(tmp_path: Path) -> None:
    s = scenario("  - {at: 0s, kill: kafka2}", seconds=60)
    brokers, net = FakeBrokers(fail_start=True), FakeNet()
    cluster = FakeCluster([layout(1)])

    async def broken_heal() -> dict[str, str]:
        raise ValueError("агент ответил не JSON")

    net.heal_all = broken_heal  # type: ignore[method-assign]

    async def go() -> None:
        task = asyncio.create_task(
            run_scenario(s, deps(cluster, brokers, net), lambda _: None, clients=CLIENTS, reports_dir=tmp_path)
        )
        while not brokers.calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert ("kafka2", "start") in brokers.calls
    assert cluster.healthy_calls == 2  # после создания и в очистке


def test_report_written(tmp_path: Path) -> None:
    s = scenario("  - {at: 0s, cut: kafka1, peer: kafka2}\n  - {at: 0.1s, heal: all}", 0.3)
    net, out = FakeNet(), []
    path = run(s, deps(FakeCluster([layout(1)]), FakeBrokers(), net), tmp_path, out)
    assert path.parent == tmp_path and path.name.endswith("_t.md")
    text = path.read_text()
    assert "потеряно" in text and "cut kafka1" in text
    assert net.heals >= 2  # сбой heal + очистка


def test_idempotence_without_acks_all_fails_before_cluster(tmp_path: Path) -> None:
    s = load_scenario(
        "t",
        "topic: {partitions: 1, replication: 3}\n"
        "producer: {acks: '1', idempotence: true, rate: 1, seconds: 1}\n",
        [],
    )
    cluster = FakeCluster([layout(1)])
    net = FakeNet()
    with pytest.raises(ScenarioError, match="acks"):
        run(s, deps(cluster, FakeBrokers(), net), tmp_path, [])
    assert cluster.layout_calls == 0 and net.heals == 0


def test_read_all_failure_still_writes_report(tmp_path: Path) -> None:
    async def broken_read_all(topic, bootstrap, isolation, timeout):
        raise RuntimeError("метаданных нет")

    clients = Clients(
        run_producer=fake_producer,
        hang_transaction=fake_hang,
        run_observer=fake_observer,
        read_all=broken_read_all,
    )
    s = scenario("  - {at: 0s, cut: kafka1, peer: kafka2}", 0.2)
    out: list[str] = []
    path = asyncio.run(
        run_scenario(s, deps(FakeCluster([layout(1)]), FakeBrokers(), FakeNet()), out.append, clients=clients, reports_dir=tmp_path)
    )
    text = path.read_text()
    assert "итоговое чтение не удалось: RuntimeError" in text
    assert "партиция 0 недоступна" in text
    assert "| потеряно — подтверждены, но в логе нет | 0 |" in text
