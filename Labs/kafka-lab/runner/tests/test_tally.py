from lab.tally import Ack, Seen, SendError, Tally, tally, visibility


def ack(seq: int, partition: int = 0, t: float = 0.0) -> Ack:
    return Ack(seq=seq, partition=partition, offset=seq, t=t)


def rec(seq: int, partition: int = 0, t: float = 0.0) -> Seen:
    return Seen(seq=seq, partition=partition, offset=seq, t=t)


def run(**kw: object) -> Tally:
    args: dict[str, object] = {
        "sent": 0,
        "acks": [],
        "aborted": set(),
        "errors": [],
        "log": [],
        "unreadable": set(),
    }
    args.update(kw)
    return tally(**args)  # type: ignore[arg-type]


def test_lost_is_acked_minus_log() -> None:
    r = run(sent=4, acks=[ack(3), ack(1), ack(2)], log=[rec(1), rec(2)])
    assert r.lost == (3,)
    assert r.acked == 3
    assert r.in_topic == 2
    assert r.sent == 4


def test_duplicates_counted() -> None:
    r = run(acks=[ack(1), ack(2)], log=[rec(1), rec(2), rec(1), rec(1)])
    assert r.duplicated == {1: 3}
    assert r.in_topic == 4
    assert r.lost == ()


def test_present_unacked_excludes_aborted() -> None:
    r = run(acks=[ack(1)], aborted={2}, log=[rec(1), rec(2), rec(3)])
    assert r.present_unacked == (3,)
    assert r.aborted == 1


def test_errors_grouped_by_type() -> None:
    errs = [
        SendError(1, "NotEnoughReplicas", 0.0),
        SendError(2, "NotEnoughReplicas", 0.0),
        SendError(3, "KafkaTimeoutError", 0.0),
    ]
    assert run(errors=errs).errors == {"NotEnoughReplicas": 2, "KafkaTimeoutError": 1}


def test_unreadable_partition_not_lost() -> None:
    r = run(acks=[ack(1, partition=1), ack(2, partition=0)], log=[rec(2)], unreadable={1})
    assert r.lost == ()
    assert r.unreadable == (1,)


def test_unreadable_does_not_hide_loss_in_readable_partition() -> None:
    r = run(acks=[ack(1, partition=1), ack(2, partition=0)], log=[], unreadable={1})
    assert r.lost == (2,)


def test_acked_aborted_seq_is_not_lost() -> None:
    r = run(acks=[ack(1), ack(2)], aborted={1}, log=[rec(2)])
    assert r.lost == ()


def test_acked_counts_distinct_seqs() -> None:
    r = run(acks=[ack(1), ack(1), ack(2)], log=[rec(1), rec(2)])
    assert r.acked == 2


def test_lost_is_sorted_distinct() -> None:
    r = run(acks=[ack(5), ack(2), ack(5)], log=[])
    assert r.lost == (2, 5)


def test_present_unacked_sorted_distinct() -> None:
    r = run(log=[rec(9), rec(4), rec(9)])
    assert r.present_unacked == (4, 9)


def test_empty_run() -> None:
    r = run()
    assert (r.lost, r.duplicated, r.present_unacked, r.errors, r.unreadable) == ((), {}, (), {}, ())


def test_visibility_delay_and_vanished() -> None:
    acks = [ack(1, t=1.0), ack(2, t=2.0), ack(3, t=3.0)]
    observed = [rec(1, t=1.1), rec(2, t=2.3), rec(3, t=3.5), rec(9, t=4.0)]
    v = visibility("read_committed", acks, set(), observed, log=[rec(1), rec(2)])
    assert v.observer == "read_committed"
    assert v.seen == 4
    assert v.delay_p50_ms is not None and abs(v.delay_p50_ms - 300) < 1e-6
    assert v.delay_max_ms is not None and abs(v.delay_max_ms - 500) < 1e-6
    assert v.vanished == (3, 9)


def test_visibility_aborted_seen_by_uncommitted() -> None:
    v = visibility(
        "read_uncommitted",
        [ack(1, t=0.0)],
        {2, 3},
        [rec(1, t=0.1), rec(2, t=0.1), rec(2, t=0.2)],
        log=[rec(1)],
    )
    assert v.seen == 2
    assert v.aborted_seen == 1


def test_visibility_first_observation_and_negative_delay_kept() -> None:
    v = visibility("o", [ack(1, t=2.0)], set(), [rec(1, t=1.5), rec(1, t=9.0)], log=[rec(1)])
    assert v.delay_p50_ms is not None and abs(v.delay_p50_ms + 500) < 1e-6
    assert v.delay_max_ms is not None and abs(v.delay_max_ms + 500) < 1e-6


def test_visibility_without_delays_gives_none() -> None:
    v = visibility("o", [], {1}, [rec(1, t=1.0)], log=[])
    assert v.delay_p50_ms is None
    assert v.delay_max_ms is None
    assert v.vanished == (1,)


def test_visibility_median_of_even_count() -> None:
    acks = [ack(1, t=0.0), ack(2, t=0.0)]
    v = visibility("o", acks, set(), [rec(1, t=0.1), rec(2, t=0.3)], log=[rec(1), rec(2)])
    assert v.delay_p50_ms is not None and abs(v.delay_p50_ms - 200) < 1e-6


def test_retried_seq_acked_after_errors_is_neither_lost_nor_unacked() -> None:
    log = [Seen(seq=0, partition=0, offset=0, t=1.0), Seen(seq=0, partition=0, offset=1, t=2.0)]
    result = tally(
        1,
        [Ack(seq=0, partition=0, offset=1, t=2.0)],
        set(),
        [SendError(0, "TimeoutError", 1.0)],
        log,
        set(),
    )
    assert result.duplicated == {0: 2} and not result.lost and not result.present_unacked
    assert result.errors == {"TimeoutError": 1}
