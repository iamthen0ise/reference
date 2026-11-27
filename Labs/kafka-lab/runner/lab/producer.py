"""Продюсеры стенда: непрерывная нагрузка (в том числе транзакционная) и зависшая транзакция."""

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from lab.journal import Journal
from lab.scenario import ProducerSpec, ScenarioError
from lab.tally import Ack, SendError

KEY_SPACE = 16
ACKS = {"0": 0, "1": 1, "all": "all"}
STOP_TIMEOUT = 15.0
RETRY_PAUSE = 0.5
APP_RETRY_PAUSE = 0.2  # пауза перед повтором send того же seq
HANG_SEQ = -1
DRAIN_MARGIN = 10.0
UNRESOLVED = "Unresolved"  # исход отправки неизвестен: не подтверждена и не отклонена


def check_producer(spec: ProducerSpec) -> None:
    """Ошибки сочетаний настроек, которые aiokafka отвергнет только при старте продюсера."""
    if spec.idempotence and spec.acks != "all":
        raise ScenarioError(
            f"producer.idempotence: aiokafka требует producer.acks=all, получено {spec.acks!r}"
        )
    if spec.transactional is not None and spec.acks != "all":
        raise ScenarioError(
            f"producer.transactional: транзакции требуют producer.acks=all, получено {spec.acks!r}"
        )
    if spec.transactional is not None and spec.app_retries:
        raise ScenarioError("producer.app_retries: не сочетается с producer.transactional")


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


def drain_budget(spec: ProducerSpec) -> float:
    """Сколько продюсер ждёт неподтверждённые отправки после стопа; раннер ждёт его чуть дольше."""
    attempts = spec.app_retries + 1
    return (spec.request_timeout_ms / 1000 + APP_RETRY_PAUSE) * attempts + DRAIN_MARGIN


def _new_producer(
    spec: ProducerSpec, bootstrap: str, transactional_id: str | None = None
) -> AIOKafkaProducer:
    extra: dict[str, object] = {}
    if transactional_id is not None:
        extra["transactional_id"] = transactional_id
    return AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        acks=ACKS[spec.acks],
        enable_idempotence=spec.idempotence or spec.transactional is not None,
        request_timeout_ms=spec.request_timeout_ms,
        **extra,  # type: ignore[arg-type]
    )


async def _stop_quietly(producer: AIOKafkaProducer) -> None:
    try:
        await asyncio.wait_for(producer.stop(), STOP_TIMEOUT)
    except (TimeoutError, KafkaError):
        pass


def _record_ack(journal: Journal, seq: int, fut: "asyncio.Future") -> None:
    if fut.cancelled():
        journal.errors.append(SendError(seq=seq, error=UNRESOLVED, t=time.monotonic()))
        return
    exc = fut.exception()
    if exc is not None:
        journal.errors.append(SendError(seq=seq, error=_error_name(exc), t=time.monotonic()))
        return
    meta = fut.result()
    journal.acks.append(
        Ack(seq=seq, partition=meta.partition, offset=meta.offset, t=time.monotonic())
    )


async def run_producer(  # noqa: PLR0913 — ready и factory нужны раннеру и тестам
    spec: ProducerSpec,
    topic: str,
    bootstrap: str,
    stop: asyncio.Event,
    journal: Journal,
    *,
    ready: asyncio.Event | None = None,
    factory: Callable[..., Any] = _new_producer,
) -> None:
    """Нагрузка до `stop` или `spec.seconds`; `ready` ставится, когда продюсер стартовал (от него идёт расписание)."""
    check_producer(spec)
    tx_id = None if spec.transactional is None else f"lab-{uuid.uuid4().hex[:8]}"
    producer = factory(spec, bootstrap, tx_id)
    await producer.start()
    if ready is not None:
        ready.set()
    try:
        if spec.transactional is None:
            await _plain_loop(producer, spec, topic, stop, journal)
        else:
            await _tx_loop(producer, spec, topic, stop, journal)
    finally:
        await _stop_quietly(producer)


def _pace(start: float, n: int, rate: int) -> float:
    return max(0.0, start + n / rate - time.monotonic())


async def _deliver(
    producer: Any, spec: ProducerSpec, topic: str, seq: int, journal: Journal
) -> None:
    """Отправляет seq, при ошибке повторяет тот же seq до `app_retries` раз.

    Каждая неудачная попытка — запись в `journal.errors`; итоговое подтверждение — одна запись в `journal.acks`.
    Отмена не записывается здесь: это делает `_on_done` (исход неизвестен).
    """
    for attempt in range(spec.app_retries + 1):
        if attempt:
            await asyncio.sleep(APP_RETRY_PAUSE)
        try:
            fut = await producer.send(topic, str(seq).encode(), key=str(seq % KEY_SPACE).encode())
            meta = await fut
        except Exception as exc:  # noqa: BLE001 — любая ошибка отправки попадает в журнал
            journal.errors.append(SendError(seq=seq, error=_error_name(exc), t=time.monotonic()))
            continue
        journal.acks.append(
            Ack(seq=seq, partition=meta.partition, offset=meta.offset, t=time.monotonic())
        )
        return


async def _plain_loop(
    producer: Any, spec: ProducerSpec, topic: str, stop: asyncio.Event, journal: Journal
) -> None:
    in_flight = asyncio.Semaphore(spec.rate)  # max_in_flight = rate
    pending: dict[asyncio.Task, int] = {}
    start = time.monotonic()
    seq = 0
    try:
        while not stop.is_set() and time.monotonic() - start < spec.seconds:
            await asyncio.sleep(_pace(start, seq, spec.rate))
            await in_flight.acquire()
            journal.sent += 1
            task = asyncio.create_task(_deliver(producer, spec, topic, seq, journal))
            pending[task] = seq
            task.add_done_callback(_on_done(journal, in_flight, pending))
            seq += 1
        if pending:  # новых отправок нет, ждём исходов
            await asyncio.wait(list(pending), timeout=drain_budget(spec))
    finally:
        # Неразрешившиеся отправки: исход неизвестен, это не обычная ошибка отправки.
        tasks = list(pending)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)  # дать отработать _on_done


def _on_done(journal: Journal, sem: asyncio.Semaphore, pending: dict[asyncio.Task, int]):  # noqa: ANN202
    def callback(task: asyncio.Task) -> None:
        seq = pending.pop(task, None)
        sem.release()
        if seq is not None and task.cancelled():
            journal.errors.append(SendError(seq=seq, error=UNRESOLVED, t=time.monotonic()))

    return callback


def _retrieve(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()  # прочитать, чтобы не было «exception was never retrieved»


async def _try_abort(producer: Any) -> bool:
    try:
        await asyncio.wait_for(producer.abort_transaction(), STOP_TIMEOUT)
    except (KafkaError, RuntimeError, TimeoutError):
        return False
    return True


async def _tx_loop(  # noqa: PLR0912
    producer: Any, spec: ProducerSpec, topic: str, stop: asyncio.Event, journal: Journal
) -> None:
    assert spec.transactional is not None  # noqa: S101 — сужение типа
    abort_every = spec.transactional.abort_every
    start = time.monotonic()
    seq = 0
    tx_number = 0
    while not stop.is_set() and time.monotonic() - start < spec.seconds:
        tx_number += 1
        to_abort = tx_number % abort_every == 0
        sent: list[int] = []  # seq, по которым была попытка отправки в этой транзакции
        futures: list[asyncio.Future] = []
        try:
            await producer.begin_transaction()
            while len(sent) < spec.batch and not stop.is_set():
                s = seq
                seq += 1
                await asyncio.sleep(_pace(start, s, spec.rate))
                journal.sent += 1
                sent.append(s)
                fut = await producer.send(topic, str(s).encode(), key=str(s % KEY_SPACE).encode())
                fut.add_done_callback(_retrieve)
                futures.append(fut)
            if to_abort:
                await producer.abort_transaction()
                journal.aborted.update(sent)
                continue
            await producer.commit_transaction()
        except asyncio.CancelledError:
            # Открытая транзакция не должна висеть до таймаута брокера: откатываем и учитываем отправленное.
            if await _try_abort(producer):
                journal.aborted.update(sent)
            else:
                now = time.monotonic()
                journal.errors.extend(SendError(seq=s, error=UNRESOLVED, t=now) for s in sent)
            raise
        except Exception as exc:  # noqa: BLE001 — сбой транзакции: ошибки только по отправленным seq
            now = time.monotonic()
            journal.errors.extend(SendError(seq=s, error=_error_name(exc), t=now) for s in sent)
            await _try_abort(producer)
            await asyncio.sleep(RETRY_PAUSE)
            continue
        for s, fut in zip(sent, futures, strict=True):
            _record_ack(journal, s, fut)


async def hang_transaction(topic: str, bootstrap: str, seconds: float) -> None:
    """Открывает транзакцию, шлёт одно сообщение (seq -1) и не коммитит: брокер откатит её по таймауту."""
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        acks="all",
        enable_idempotence=True,
        transactional_id=f"lab-hang-{uuid.uuid4().hex[:8]}",
        transaction_timeout_ms=int(seconds * 1000),
    )
    await producer.start()
    try:
        await producer.begin_transaction()
        await producer.send_and_wait(topic, str(HANG_SEQ).encode(), key=b"hang")
        await asyncio.sleep(seconds + 5)
    finally:
        await _stop_quietly(producer)
