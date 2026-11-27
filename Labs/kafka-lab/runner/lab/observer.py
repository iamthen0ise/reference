"""Наблюдатель без группы: читает топик с начала и пишет, что и когда увидел."""

import asyncio
import time

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.errors import KafkaError

from lab.journal import Journal
from lab.tally import Seen

POLL_MS = 200
RETRY_PAUSE = 0.5
METADATA_TRIES = 40


async def partitions_of(consumer: AIOKafkaConsumer, topic: str) -> list[TopicPartition]:
    """Партиции топика; метаданные клиент грузит только для топиков, на которые есть подписка."""
    consumer.subscribe([topic])
    try:
        for _ in range(METADATA_TRIES):
            parts = consumer.partitions_for_topic(topic)
            if parts:
                return [TopicPartition(topic, p) for p in sorted(parts)]
            await asyncio.sleep(RETRY_PAUSE)
    finally:
        consumer.unsubscribe()  # дальше партиции назначаются вручную, `assign`
    raise RuntimeError(f"топик {topic}: нет партиций в метаданных")


def parse_seq(value: bytes | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def run_observer(
    isolation: str, topic: str, bootstrap: str, stop: asyncio.Event, journal: Journal
) -> None:
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        isolation_level=isolation,  # type: ignore[arg-type]
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        tps = await partitions_of(consumer, topic)
        consumer.assign(tps)
        await consumer.seek_to_beginning(*tps)
        seen = journal.seen.setdefault(isolation, [])
        while not stop.is_set():
            try:
                batches = await consumer.getmany(timeout_ms=POLL_MS)
            except KafkaError:
                await asyncio.sleep(RETRY_PAUSE)
                continue
            now = time.monotonic()
            for tp, messages in batches.items():
                for msg in messages:
                    seq = parse_seq(msg.value)
                    if seq is not None and seq >= 0:  # seq -1 — зависшая транзакция, не нагрузка
                        seen.append(Seen(seq=seq, partition=tp.partition, offset=msg.offset, t=now))
    finally:
        await consumer.stop()
