"""Прогон сценария: подготовка, нагрузка, сбои по расписанию, восстановление, сверка и отчёт."""

import asyncio
import contextlib
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from lab import observer, producer, verify
from lab.journal import Journal
from lab.report import Event, event_line, live_line, render_console, render_markdown
from lab.scenario import Fault, Scenario, ScenarioError, effective_settings
from lab.tally import Seen, tally, visibility
from lab.targets import BROKERS, PartitionState, resolve

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
POLL_LAYOUT = 0.5
LIVE_EVERY = 1.0
HEALTHY_AFTER_CREATE = 30.0
READ_TIMEOUT = 30.0
OBSERVER_DRAIN = 1.0
TASK_STOP_TIMEOUT = 20.0
PRODUCER_STOP_EXTRA = 25.0  # drain + producer.stop() (STOP_TIMEOUT) + запас
CONSOLE_WIDTH = 110
LIVE_PREFIX = "\r"  # строка с этим префиксом `out` перезаписывает текущую, без перевода строки

Bindings = dict[str, str]


@dataclass(frozen=True)
class Deps:
    cluster: Any
    brokers: Any
    net: Any
    bootstrap: str
    clock: Callable[[], float]


@dataclass(frozen=True)
class Clients:
    """Клиенты Kafka; в тестах подменяются, чтобы прогон не ходил к брокеру."""

    run_producer: Callable[..., Awaitable[None]] = producer.run_producer
    hang_transaction: Callable[..., Awaitable[None]] = producer.hang_transaction
    run_observer: Callable[..., Awaitable[None]] = observer.run_observer
    read_all: Callable[..., Awaitable[tuple[list[Seen], set[int]]]] = verify.read_all


@dataclass
class _Ctx:
    s: Scenario
    deps: Deps
    out: Callable[[str], None]
    clients: Clients
    journal: Journal
    t0: float = 0.0
    bindings: Bindings = field(default_factory=dict)
    last_layout: dict[int, PartitionState] = field(default_factory=dict)
    hang_tasks: list[asyncio.Task] = field(default_factory=list)

    def log(self, text: str) -> None:
        event = Event(t=self.deps.clock() - self.t0, text=text)
        self.journal.events.append(event)
        self.out(event_line(event))


_DESCRIBE = {"leader": "лидер", "follower": "фолловер", "followers": "фолловеры"}


def _describe(expr: str) -> str:
    kind, sep, rest = expr.strip().partition("(")
    return f" ({_DESCRIBE[kind]} {rest.rstrip(')')})" if sep and kind in _DESCRIBE else ""


async def _layout(ctx: _Ctx) -> dict[int, PartitionState]:
    try:
        ctx.last_layout = await ctx.deps.cluster.layout(ctx.s.topic.name)
    except Exception:  # noqa: BLE001 — метаданные во время сбоя бывают недоступны, берём последние известные
        pass
    return ctx.last_layout


async def _resolve(ctx: _Ctx, expr: str) -> list[str]:
    targets = resolve(expr, await _layout(ctx), ctx.bindings)
    if not targets:
        ctx.log(f"цель не найдена: {expr}")
    return targets


async def _apply_fault(ctx: _Ctx, f: Fault) -> None:
    deps = ctx.deps
    if f.action == "heal":
        ctx.log("heal")
        await deps.net.heal_all()
        return
    if f.action == "hang_transaction":
        assert f.hang_seconds is not None  # noqa: S101 — гарантирует разбор сценария
        ctx.log(f"hang_transaction {f.hang_seconds:g}s")
        ctx.hang_tasks.append(
            asyncio.create_task(
                ctx.clients.hang_transaction(ctx.s.topic.name, deps.bootstrap, f.hang_seconds)
            )
        )
        return
    assert f.target is not None  # noqa: S101
    targets = await _resolve(ctx, f.target)
    if not targets:
        return
    if f.bind:
        ctx.bindings[f.bind] = targets[0]
    if f.action == "cut":
        assert f.peer is not None  # noqa: S101
        peers = await _resolve(ctx, f.peer)
        arrow = "→" if f.one_way else "↔"
        for src in targets:
            for dst in peers:
                ctx.log(f"cut {src} {arrow} {dst}")
                await deps.net.cut(src, dst, f.one_way)
        return
    for name in targets:
        ctx.log(f"{f.action} {name}{_describe(f.target)}")
        await deps.brokers.act(name, f.action)


async def _run_faults(ctx: _Ctx) -> None:
    for f in ctx.s.faults:
        if f.at >= ctx.s.producer.seconds:
            ctx.log(f"сбой {f.action} пропущен: at={f.at:g} не раньше конца нагрузки")
            continue
        await asyncio.sleep(max(0.0, f.at - (ctx.deps.clock() - ctx.t0)))
        try:
            await _apply_fault(ctx, f)
        except ScenarioError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой не отменяет прогон, он попадает в отчёт
            ctx.log(f"{f.action} не выполнен: {type(exc).__name__}: {exc}")
    await asyncio.sleep(max(0.0, ctx.s.producer.seconds - (ctx.deps.clock() - ctx.t0)))


def _names(ids: tuple[int, ...]) -> str:
    return ",".join(BROKERS.get(i, str(i)) for i in ids)


async def _poll_layout(ctx: _Ctx) -> None:
    prev: dict[int, PartitionState] = {}
    while True:
        try:
            now = await ctx.deps.cluster.layout(ctx.s.topic.name)
        except Exception:  # noqa: BLE001 — недоступные метаданные не событие
            await asyncio.sleep(POLL_LAYOUT)
            continue
        ctx.last_layout = now
        for p, st in now.items():
            old = prev.get(p)
            if old is None:
                continue
            if old.leader != st.leader:
                was = BROKERS.get(old.leader, "нет") if old.leader else "нет"
                now_ = BROKERS.get(st.leader, "нет") if st.leader else "нет"
                ctx.log(f"p{p} лидер {was} → {now_}")
            if set(old.isr) != set(st.isr):
                ctx.log(f"p{p} ISR {_names(old.isr)} → {_names(st.isr)}")
        prev = now
        await asyncio.sleep(POLL_LAYOUT)


async def _live(ctx: _Ctx) -> None:
    while True:
        j = ctx.journal
        seen = {name: len({r.seq for r in recs}) for name, recs in j.seen.items()}
        ctx.out(LIVE_PREFIX + live_line(j.sent, len(j.acks), len(j.errors), seen))
        await asyncio.sleep(LIVE_EVERY)


async def restore(deps: Deps, log: Callable[[str], None]) -> list[str]:
    """Снимает обрывы и запускает остановленные брокеры; каждый шаг сам по себе, сбой не прерывает остальные."""

    async def heal() -> None:
        try:
            failed = await deps.net.heal_all()
            for broker, reason in failed.items():
                log(f"heal {broker}: {reason}")
        except Exception as exc:  # noqa: BLE001 — остальные шаги важнее
            log(f"heal_all не выполнен: {type(exc).__name__}: {exc}")

    await heal()
    started: list[str] = []
    try:
        stopped = list(await deps.brokers.stopped())
    except Exception as exc:  # noqa: BLE001
        log(f"список остановленных брокеров не получен: {type(exc).__name__}: {exc}")
        stopped = []
    for name in stopped:
        try:
            await deps.brokers.act(name, "start")
            started.append(name)
            log(f"start {name} (восстановление)")
        except Exception as exc:  # noqa: BLE001
            log(f"start {name} не выполнен: {type(exc).__name__}: {exc}")
    if started:
        await heal()
    return started


async def _cancel_all(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(t, TASK_STOP_TIMEOUT)


async def _stop_task(
    task: asyncio.Task, event: asyncio.Event, ctx: _Ctx, name: str, timeout: float = TASK_STOP_TIMEOUT
) -> None:
    """Просит задачу остановиться и ждёт; упавшая задача — событие в отчёте, а не обрыв прогона."""
    event.set()
    await asyncio.wait({task}, timeout=timeout)
    if not task.done():
        task.cancel()
        ctx.log(f"{name} не остановился за {timeout:g} с, отменён")
        await asyncio.wait({task}, timeout=TASK_STOP_TIMEOUT)
    if task.done() and not task.cancelled() and task.exception() is not None:
        exc = task.exception()
        ctx.log(f"{name} упал: {type(exc).__name__}: {exc}")


async def run_scenario(
    s: Scenario,
    deps: Deps,
    out: Callable[[str], None],
    *,
    clients: Clients = Clients(),  # noqa: B008 — неизменяемый набор функций
    reports_dir: Path = REPORTS_DIR,
) -> Path | None:
    producer.check_producer(s.producer)  # до любого обращения к кластеру
    ctx = _Ctx(s, deps, out, clients, Journal())
    ctx.t0 = deps.clock()
    started_at = datetime.now()  # noqa: DTZ005 — локальное время для имени файла
    await restore(deps, ctx.log)
    stop_producer, stop_observers = asyncio.Event(), asyncio.Event()
    background: list[asyncio.Task] = []
    producer_task: asyncio.Task | None = None
    observer_tasks: list[asyncio.Task] = []
    topic_created = False
    completed = False
    try:
        await deps.cluster.recreate_topic(s.topic)
        topic_created = True
        if not await deps.cluster.wait_healthy(s.topic.name, HEALTHY_AFTER_CREATE):
            ctx.log("топик не стал здоровым после создания")
        await _layout(ctx)
        topic, bootstrap = s.topic.name, deps.bootstrap
        observer_tasks = [
            asyncio.create_task(clients.run_observer(iso, topic, bootstrap, stop_observers, ctx.journal))
            for iso in s.observers
        ]
        ready = asyncio.Event()
        producer_task = asyncio.create_task(
            clients.run_producer(s.producer, topic, bootstrap, stop_producer, ctx.journal, ready=ready)
        )
        # Расписание сбоев и seconds отсчитываются от старта продюсера, а не от подготовки.
        waiter = asyncio.create_task(ready.wait())
        await asyncio.wait({producer_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not ready.is_set():
            ctx.log("продюсер не стартовал")
        ctx.t0 = deps.clock()
        background = [
            asyncio.create_task(_poll_layout(ctx)),
            asyncio.create_task(_live(ctx)),
        ]
        await _run_faults(ctx)
        await _stop_task(
            producer_task, stop_producer, ctx, "продюсер", producer.drain_budget(s.producer) + PRODUCER_STOP_EXTRA
        )
        completed = True
    finally:
        # Обрывы и остановленные брокеры не должны пережить прогон: следующий опыт соврёт.
        if not completed:
            to_cancel = [*observer_tasks, *ctx.hang_tasks]
            if producer_task is not None:
                to_cancel.append(producer_task)
            await _cancel_all(to_cancel)
        await _cancel_all(background)
        await restore(deps, ctx.log)
        if topic_created:
            try:
                healthy = await deps.cluster.wait_healthy(s.topic.name, s.settle_seconds)
                if not healthy:
                    ctx.log(f"кластер не восстановился за {s.settle_seconds:g} с")
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"wait_healthy не выполнен: {type(exc).__name__}: {exc}")
        if not completed:
            out("прогон прерван, отчёт не записан")
    await asyncio.sleep(OBSERVER_DRAIN if s.observers else 0)
    await asyncio.gather(
        *(_stop_task(t, stop_observers, ctx, f"наблюдатель {iso}") for t, iso in zip(observer_tasks, s.observers, strict=True))
    )
    await _wait_hangs(ctx)
    try:
        log, unreadable = await clients.read_all(
            s.topic.name, deps.bootstrap, "read_committed", READ_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — отчёт важнее: все партиции считаем непрочитанными
        log = []
        unreadable = await _all_partitions(ctx)
        ctx.log(f"итоговое чтение не удалось: {type(exc).__name__}: {exc}")
    return _write_report(ctx, started_at, reports_dir, log, unreadable)


async def _all_partitions(ctx: _Ctx) -> set[int]:
    try:
        return set((await ctx.deps.cluster.layout(ctx.s.topic.name)).keys())
    except Exception:  # noqa: BLE001 — раскладки нет, берём число партиций из сценария
        return set(range(ctx.s.topic.partitions))


async def _wait_hangs(ctx: _Ctx) -> None:
    for t in ctx.hang_tasks:
        with contextlib.suppress(Exception):
            await t


def _write_report(
    ctx: _Ctx, started_at: datetime, reports_dir: Path, log: list[Seen], unreadable: set[int]
) -> Path | None:
    """Итог — в консоль всегда; файл — если каталог отчётов доступен на запись."""
    s, j = ctx.s, ctx.journal
    result = tally(j.sent, j.acks, j.aborted, j.errors, log, unreadable)
    vis = [visibility(name, j.acks, j.aborted, j.seen.get(name, []), log) for name in s.observers]
    started = started_at.strftime("%Y-%m-%d %H:%M:%S")
    settings = effective_settings(s)
    buf = io.StringIO()
    Console(file=buf, width=CONSOLE_WIDTH, force_terminal=False).print(
        render_console(s, settings, j.events, result, vis, started)
    )
    ctx.out(buf.getvalue().rstrip("\n"))
    path = reports_dir / f"{started_at:%Y-%m-%d_%H%M%S}_{s.name}.md"
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_markdown(s, settings, j.events, result, vis, started), encoding="utf-8"
        )
    except OSError as exc:
        ctx.out(f"отчёт не записан в {reports_dir}: {type(exc).__name__}: {exc}")
        return None
    ctx.out(f"отчёт: {path}")
    return path
