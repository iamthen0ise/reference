import asyncio
import time
from types import SimpleNamespace

import pytest

from lab import producer
from lab.journal import Journal
from lab.scenario import ProducerSpec, TxSpec
from lab.tally import SendError


class FakeProducer:
    """Фейк AIOKafkaProducer: send возвращает future, остальное записывает вызовы."""

    def __init__(
        self,
        fail_send_at: int | None = None,
        hang_sends: bool = False,
        fail_abort: bool = False,
        fail_futures: set[int] | None = None,
    ) -> None:
        self.fail_futures = fail_futures
        self.fail_send_at = fail_send_at
        self.hang_sends = hang_sends
        self.fail_abort = fail_abort
        self.sends = 0
        self.calls: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.calls.append("stop")

    async def send(self, topic: str, value: bytes, key: bytes) -> asyncio.Future:
        self.sends += 1
        if self.fail_send_at is not None and self.sends == self.fail_send_at:
            raise ValueError("не отправилось")
        fut = asyncio.get_running_loop().create_future()
        if self.fail_futures is not None and (
            not self.fail_futures or self.sends in self.fail_futures
        ):
            fut.set_exception(TimeoutError("ответ потерян"))
        elif not self.hang_sends:
            fut.set_result(SimpleNamespace(partition=0, offset=self.sends))
        return fut

    async def begin_transaction(self) -> None:
        self.calls.append("begin")

    async def commit_transaction(self) -> None:
        self.calls.append("commit")

    async def abort_transaction(self) -> None:
        self.calls.append("abort")
        if self.fail_abort:
            raise RuntimeError("abort не удался")


def spec(
    tx: int | None = None,
    batch: int = 5,
    rate: int = 200,
    seconds: float = 0.5,
    app_retries: int = 0,
) -> ProducerSpec:
    return ProducerSpec(
        acks="all",
        idempotence=False,
        rate=rate,
        seconds=seconds,
        batch=batch,
        request_timeout_ms=50,
        transactional=None if tx is None else TxSpec(abort_every=tx),
        app_retries=app_retries,
    )


def assert_exactly_once(j: Journal) -> None:
    acked = [a.seq for a in j.acks]
    errored = [e.seq for e in j.errors]
    everything = [*acked, *errored, *sorted(j.aborted)]
    assert len(everything) == len(set(everything)), "seq учтён дважды"
    assert len(everything) == j.sent, "есть seq без учёта или лишний учёт"


def run(s: ProducerSpec, fake: FakeProducer, j: Journal) -> None:
    asyncio.run(producer.run_producer(s, "t", "x", asyncio.Event(), j, factory=lambda *_: fake))


def test_tx_failure_records_only_sent_seqs() -> None:
    j, fake = Journal(), FakeProducer(fail_send_at=3)
    run(spec(tx=100), fake, j)
    # первая транзакция: отправки 1,2 прошли, 3-я упала -> ошибки ровно по трём seq и откат
    assert [e.seq for e in j.errors][:3] == [0, 1, 2]
    assert fake.calls.count("abort") >= 1
    assert_exactly_once(j)


def test_tx_abort_every_and_commit_accounting() -> None:
    j = Journal()
    run(spec(tx=2), FakeProducer(), j)
    assert j.aborted and j.acks and not j.errors
    assert_exactly_once(j)


def test_tx_cancel_mid_transaction_aborts_and_accounts() -> None:
    j, fake = Journal(), FakeProducer()

    async def go() -> None:
        task = asyncio.create_task(
            producer.run_producer(
                spec(tx=100, batch=50, rate=20, seconds=30),
                "t",
                "x",
                asyncio.Event(),
                j,
                factory=lambda *_: fake,
            )
        )
        while j.sent < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert fake.calls[-2:] == ["abort", "stop"]
    assert len(j.aborted) == j.sent and not j.errors
    assert_exactly_once(j)


def test_tx_cancel_with_failing_abort_records_unresolved() -> None:
    j, fake = Journal(), FakeProducer(fail_abort=True)

    async def go() -> None:
        task = asyncio.create_task(
            producer.run_producer(
                spec(tx=100, batch=50, rate=20, seconds=30),
                "t",
                "x",
                asyncio.Event(),
                j,
                factory=lambda *_: fake,
            )
        )
        while j.sent < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert not j.aborted and {e.error for e in j.errors} == {producer.UNRESOLVED}
    assert_exactly_once(j)


def test_plain_unresolved_sends_are_not_plain_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(producer, "DRAIN_MARGIN", 0.05)
    j, fake = Journal(), FakeProducer(hang_sends=True)
    s = spec(rate=50, seconds=0.3)
    t = time.monotonic()
    run(s, fake, j)
    assert time.monotonic() - t < 2
    assert j.sent > 0 and not j.acks
    assert {e.error for e in j.errors} == {producer.UNRESOLVED}
    assert_exactly_once(j)


def test_plain_send_exception_is_recorded() -> None:
    j = Journal()
    run(spec(seconds=0.2), FakeProducer(fail_send_at=2), j)
    assert SendError(seq=1, error="ValueError", t=j.errors[0].t) in j.errors
    assert_exactly_once(j)


def test_app_retry_resends_same_seq_and_acks_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(producer, "APP_RETRY_PAUSE", 0.01)
    j, fake = Journal(), FakeProducer(fail_futures={1})
    run(spec(seconds=0.2, app_retries=2), fake, j)
    assert [e.seq for e in j.errors] == [0] and j.errors[0].error == "TimeoutError"
    assert sorted(a.seq for a in j.acks) == list(range(j.sent))  # seq 0 подтверждён повтором
    assert fake.sends == j.sent + 1  # одна лишняя отправка того же seq


def test_app_retry_exhausted_records_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(producer, "APP_RETRY_PAUSE", 0.01)
    j, fake = Journal(), FakeProducer(fail_futures=set())
    run(spec(seconds=0.03, rate=20, app_retries=1), fake, j)
    assert not j.acks
    assert [e.error for e in j.errors if e.seq == 0] == ["TimeoutError"] * 2


def test_no_app_retries_by_default() -> None:
    j, fake = Journal(), FakeProducer(fail_futures={1})
    run(spec(seconds=0.2), fake, j)
    assert [e.seq for e in j.errors] == [0]
    assert fake.sends == j.sent
    assert_exactly_once(j)
