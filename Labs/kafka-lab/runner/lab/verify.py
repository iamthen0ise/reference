"""Итоговое перечитывание лога: до конца каждой партиции на момент старта, без зависаний."""

import asyncio
import contextlib
import time

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.errors import KafkaError

from lab.observer import parse_seq, partitions_of
from lab.tally import Seen

POLL_MS = 200
OFFSETS_TIMEOUT = 10.0
REQUEST_TIMEOUT_MS = 10000
STOP_TIMEOUT = 5.0


async def _end_offset(consumer: AIOKafkaConsumer, tp: TopicPartition, timeout: float) -> int | None:
    try:
        ends = await asyncio.wait_for(consumer.end_offsets([tp]), min(timeout, OFFSETS_TIMEOUT))
    except (TimeoutError, KafkaError):
        return None
    return ends[tp]


async def read_all(
    topic: str, bootstrap: str, isolation: str, timeout: float  # noqa: ASYNC109 — интерфейс из плана
) -> tuple[list[Seen], set[int]]:
    """Возвращает записи и номера недоступных партиций (нет лидера или не дочитаны за timeout)."""
    deadline = time.monotonic() + timeout
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        isolation_level=isolation,  # type: ignore[arg-type]
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        request_timeout_ms=REQUEST_TIMEOUT_MS,
    )
    try:
        await asyncio.wait_for(consumer.start(), _left(deadline))
        tps = await asyncio.wait_for(partitions_of(consumer, topic), _left(deadline))
        ends: dict[TopicPartition, int] = {}
        unreadable: set[int] = set()
        for tp in tps:
            end = await _end_offset(consumer, tp, _left(deadline))
            if end is None:
                unreadable.add(tp.partition)
            else:
                ends[tp] = end
        log = await _drain(consumer, ends, deadline, unreadable)
        return log, unreadable
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(consumer.stop(), STOP_TIMEOUT)


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.1)


async def _drain(
    consumer: AIOKafkaConsumer,
    ends: dict[TopicPartition, int],
    deadline: float,
    unreadable: set[int],
) -> list[Seen]:
    log: list[Seen] = []
    todo = {tp for tp, end in ends.items() if end > 0}
    if not todo:
        return log
    try:
        consumer.assign(sorted(todo))
        await asyncio.wait_for(consumer.seek_to_beginning(*todo), _left(deadline))
    except (TimeoutError, KafkaError):
        unreadable.update(tp.partition for tp in todo)
        return log
    while todo and time.monotonic() < deadline:
        try:
            batches = await consumer.getmany(timeout_ms=POLL_MS)
            now = time.monotonic()
            for tp, messages in batches.items():
                for msg in messages:
                    seq = parse_seq(msg.value)
                    if seq is not None and seq >= 0 and msg.offset < ends[tp]:
                        log.append(Seen(seq=seq, partition=tp.partition, offset=msg.offset, t=now))
            for tp in list(todo):
                if await asyncio.wait_for(consumer.position(tp), _left(deadline)) >= ends[tp]:
                    todo.discard(tp)
        except TimeoutError:
            break
        except KafkaError:
            await asyncio.sleep(POLL_MS / 1000)
    unreadable.update(tp.partition for tp in todo)
    return log
