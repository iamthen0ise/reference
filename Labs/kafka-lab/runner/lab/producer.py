"""Продюсеры стенда: непрерывная нагрузка (в том числе транзакционная) и зависшая транзакция."""

import asyncio
import time
import uuid

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from lab.journal import Journal
from lab.scenario import ProducerSpec, ScenarioError
from lab.tally import Ack, SendError

KEY_SPACE = 16
ACKS = {"0": 0, "1": 1, "all": "all"}
STOP_TIMEOUT = 15.0
RETRY_PAUSE = 0.5
HANG_SEQ = -1


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


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


def _new_producer(spec: ProducerSpec, bootstrap: str, **extra: object) -> AIOKafkaProducer:
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
        journal.errors.append(SendError(seq=seq, error="Cancelled", t=time.monotonic()))
        return
    exc = fut.exception()
    if exc is not None:
        journal.errors.append(SendError(seq=seq, error=_error_name(exc), t=time.monotonic()))
        return
    meta = fut.result()
    journal.acks.append(Ack(seq=seq, partition=meta.partition, offset=meta.offset, t=time.monotonic()))


async def run_producer(
    spec: ProducerSpec, topic: str, bootstrap: str, stop: asyncio.Event, journal: Journal
) -> None:
    check_producer(spec)
    if spec.transactional is None:
        producer = _new_producer(spec, bootstrap)
    else:
        producer = _new_producer(
            spec, bootstrap, transactional_id=f"lab-{uuid.uuid4().hex[:8]}"
        )
    await producer.start()
    try:
        if spec.transactional is None:
            await _plain_loop(producer, spec, topic, stop, journal)
        else:
            await _tx_loop(producer, spec, topic, stop, journal)
    finally:
        await _stop_quietly(producer)


def _pace(start: float, n: int, rate: int) -> float:
    return max(0.0, start + n / rate - time.monotonic())


async def _plain_loop(
    producer: AIOKafkaProducer, spec: ProducerSpec, topic: str, stop: asyncio.Event, journal: Journal
) -> None:
    in_flight = asyncio.Semaphore(spec.rate)  # max_in_flight = rate
    pending: set[asyncio.Future] = set()
    start = time.monotonic()
    seq = 0
    try:
        while not stop.is_set() and time.monotonic() - start < spec.seconds:
            await asyncio.sleep(_pace(start, seq, spec.rate))
            await in_flight.acquire()
            journal.sent += 1
            try:
                fut = await producer.send(
                    topic, str(seq).encode(), key=str(seq % KEY_SPACE).encode()
                )
            except KafkaError as exc:
                journal.errors.append(SendError(seq=seq, error=_error_name(exc), t=time.monotonic()))
                in_flight.release()
                seq += 1
                continue
            pending.add(fut)
            fut.add_done_callback(_on_done(seq, journal, in_flight, pending))
            seq += 1
        if pending:
            await asyncio.wait(pending, timeout=spec.request_timeout_ms / 1000 + STOP_TIMEOUT)
    finally:
        for fut in pending:
            fut.cancel()


def _on_done(seq: int, journal: Journal, sem: asyncio.Semaphore, pending: set):  # noqa: ANN202
    def callback(fut: asyncio.Future) -> None:
        pending.discard(fut)
        sem.release()
        _record_ack(journal, seq, fut)

    return callback


async def _tx_loop(
    producer: AIOKafkaProducer, spec: ProducerSpec, topic: str, stop: asyncio.Event, journal: Journal
) -> None:
    assert spec.transactional is not None  # noqa: S101 — сужение типа
    abort_every = spec.transactional.abort_every
    start = time.monotonic()
    seq = 0
    tx_number = 0
    while not stop.is_set() and time.monotonic() - start < spec.seconds:
        tx_number += 1
        seqs = list(range(seq, seq + spec.batch))
        seq += spec.batch
        to_abort = tx_number % abort_every == 0
        futures: list[asyncio.Future] = []
        try:
            await producer.begin_transaction()
            for s in seqs:
                await asyncio.sleep(_pace(start, s, spec.rate))
                journal.sent += 1
                futures.append(
                    await producer.send(topic, str(s).encode(), key=str(s % KEY_SPACE).encode())
                )
            if to_abort:
                await producer.abort_transaction()
                journal.aborted.update(seqs)
                continue
            await producer.commit_transaction()
        except KafkaError as exc:
            now = time.monotonic()
            journal.errors.extend(SendError(seq=s, error=_error_name(exc), t=now) for s in seqs)
            await _abort_quietly(producer)
            await asyncio.sleep(RETRY_PAUSE)
            continue
        for s, fut in zip(seqs, futures, strict=True):
            _record_ack(journal, s, fut)


async def _abort_quietly(producer: AIOKafkaProducer) -> None:
    try:
        await producer.abort_transaction()
    except (KafkaError, RuntimeError):
        pass


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
