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
    journal.acks.append(Ack(seq=seq, partition=meta.partition, offset=meta.offset, t=time.monotonic()))


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


async def _plain_loop(
    producer: Any, spec: ProducerSpec, topic: str, stop: asyncio.Event, journal: Journal
) -> None:
    in_flight = asyncio.Semaphore(spec.rate)  # max_in_flight = rate
    pending: dict[asyncio.Future, int] = {}
    retry_tasks: dict[asyncio.Task, int] = {}  # seq, который сейчас ждёт повтора app_retries
    attempts: dict[int, int] = {}  # сколько повторов seq уже сделано
    retries = spec.app_retries

    def settle(fut: asyncio.Future) -> None:
        seq = pending.pop(fut, None)
        if seq is None:  # запись уже сделал finally
            in_flight.release()
            return
        failed = not fut.cancelled() and fut.exception() is not None
        if failed and attempts.get(seq, 0) < retries:
            _record_ack(journal, seq, fut)  # неудачная попытка — запись об ошибке
            start_retry(seq)  # семафор остаётся занятым до исхода seq
            return
        in_flight.release()
        _record_ack(journal, seq, fut)

    def start_retry(seq: int) -> None:
        task = asyncio.create_task(resend(seq))
        retry_tasks[task] = seq
        task.add_done_callback(retry_done)

    def first_failed(seq: int) -> None:
        if retries:
            start_retry(seq)
        else:
            in_flight.release()

    def retry_done(task: asyncio.Task) -> None:
        seq = retry_tasks.pop(task, None)
        if seq is not None and task.cancelled():
            journal.errors.append(SendError(seq=seq, error=UNRESOLVED, t=time.monotonic()))
            in_flight.release()

    async def resend(seq: int) -> None:
        """Повтор того же seq; сама отправка последовательна внутри задачи, исход разбирает `settle`."""
        while True:
            attempts[seq] = attempts.get(seq, 0) + 1
            await asyncio.sleep(APP_RETRY_PAUSE)
            try:
                fut = await producer.send(
                    topic, str(seq).encode(), key=str(seq % KEY_SPACE).encode()
                )
            except Exception as exc:  # noqa: BLE001 — любая ошибка отправки попадает в журнал
                journal.errors.append(
                    SendError(seq=seq, error=_error_name(exc), t=time.monotonic())
                )
                if attempts[seq] >= retries:
                    in_flight.release()
                    return
                continue
            pending[fut] = seq
            fut.add_done_callback(settle)
            return

    start = time.monotonic()
    seq = 0
    try:
        while not stop.is_set() and time.monotonic() - start < spec.seconds:
            await asyncio.sleep(_pace(start, seq, spec.rate))
            await in_flight.acquire()
            journal.sent += 1
            try:
                # Первая отправка идёт прямо в цикле: порядок постановки в очередь равен порядку seq,
                # а переполнение буфера продюсера притормаживает цикл.
                fut = await producer.send(
                    topic, str(seq).encode(), key=str(seq % KEY_SPACE).encode()
                )
            except Exception as exc:  # noqa: BLE001 — любая ошибка отправки попадает в журнал
                journal.errors.append(SendError(seq=seq, error=_error_name(exc), t=time.monotonic()))
                first_failed(seq)
                seq += 1
                continue
            pending[fut] = seq
            fut.add_done_callback(settle)
            seq += 1
        deadline = time.monotonic() + drain_budget(spec)
        if not retries:
            if pending:  # новых отправок нет, ждём подтверждений
                await asyncio.wait(list(pending), timeout=drain_budget(spec))
        else:
            while (pending or retry_tasks) and (left := deadline - time.monotonic()) > 0:
                await asyncio.wait(
                    [*pending, *retry_tasks], timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
    finally:
        # Неразрешившиеся отправки: исход неизвестен, это не обычная ошибка отправки.
        for fut, s in list(pending.items()):
            del pending[fut]
            fut.cancel()
            journal.errors.append(SendError(seq=s, error=UNRESOLVED, t=time.monotonic()))
        tasks = list(retry_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)  # дать отработать retry_done


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
