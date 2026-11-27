"""Админ-операции кластера (aiokafka admin): пересоздание топика, раскладка партиций, ожидание ISR."""

import asyncio
import os
import time

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError, UnknownTopicOrPartitionError

from lab.scenario import TopicSpec
from lab.targets import BROKERS, PartitionState

REQUEST_TIMEOUT_MS = 10000
DELETE_TIMEOUT = 30.0
POLL = 0.5
DEFAULT_BOOTSTRAP = ",".join(f"{b}:9092" for b in BROKERS.values())


class ClusterError(Exception):
    pass


class Cluster:
    def __init__(self, bootstrap: str | None = None) -> None:
        self._bootstrap = bootstrap or os.environ.get("BOOTSTRAP") or DEFAULT_BOOTSTRAP

    async def _admin(self) -> AIOKafkaAdminClient:
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._bootstrap, request_timeout_ms=REQUEST_TIMEOUT_MS
        )
        try:
            await admin.start()
        except BaseException:
            await admin.close()
            raise
        return admin

    async def recreate_topic(self, spec: TopicSpec, delete_timeout: float = DELETE_TIMEOUT) -> None:
        admin = await self._admin()
        try:
            try:
                await admin.delete_topics([spec.name], timeout_ms=REQUEST_TIMEOUT_MS)
            except UnknownTopicOrPartitionError:
                pass
            deadline = time.monotonic() + delete_timeout
            while spec.name in set(await admin.list_topics()):
                if time.monotonic() >= deadline:
                    raise ClusterError(f"топик {spec.name} не исчез за {delete_timeout:g} с")
                await asyncio.sleep(POLL)
            await admin.create_topics(
                [
                    NewTopic(
                        name=spec.name,
                        num_partitions=spec.partitions,
                        replication_factor=spec.replication,
                        topic_configs=dict(spec.configs),
                    )
                ],
                timeout_ms=REQUEST_TIMEOUT_MS,
            )
        finally:
            await admin.close()

    async def layout(self, topic: str) -> dict[int, PartitionState]:
        admin = await self._admin()
        try:
            return await _layout(admin, topic)
        finally:
            await admin.close()

    async def wait_healthy(self, topic: str, timeout: float) -> bool:  # noqa: ASYNC109 — интерфейс из плана
        """True, когда у каждой партиции есть лидер и ISR == replicas; False по таймауту."""
        deadline = time.monotonic() + timeout
        admin = await self._admin()
        try:
            while True:
                try:
                    state = await _layout(admin, topic)
                except (KafkaError, ClusterError):
                    state = {}
                if state and all(_healthy(p) for p in state.values()):
                    return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(POLL)
        finally:
            await admin.close()


def _healthy(p: PartitionState) -> bool:
    return p.leader is not None and set(p.isr) == set(p.replicas)


async def _layout(admin: AIOKafkaAdminClient, topic: str) -> dict[int, PartitionState]:
    described = await admin.describe_topics([topic])
    if not described or described[0].get("error_code", 0) != 0:
        raise ClusterError(f"топик {topic}: нет в метаданных")
    result: dict[int, PartitionState] = {}
    for p in described[0]["partitions"]:
        leader = p["leader"]
        result[p["partition"]] = PartitionState(
            leader=None if leader is None or leader < 0 else leader,
            replicas=tuple(p["replicas"]),
            isr=tuple(p["isr"]),
        )
    return result
