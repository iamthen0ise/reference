"""Подсчёт итогов прогона: потери, дубли, записи без подтверждения и видимость для наблюдателей."""

from dataclasses import dataclass

_MS_IN_S = 1000
_HALF = 2


@dataclass(frozen=True)
class Ack:
    seq: int
    partition: int
    offset: int
    t: float


@dataclass(frozen=True)
class SendError:
    seq: int
    error: str
    t: float


@dataclass(frozen=True)
class Seen:
    seq: int
    partition: int
    offset: int
    t: float


@dataclass(frozen=True)
class Tally:
    sent: int
    acked: int
    aborted: int
    in_topic: int
    lost: tuple[int, ...]
    duplicated: dict[int, int]
    present_unacked: tuple[int, ...]
    errors: dict[str, int]
    unreadable: tuple[int, ...]


@dataclass(frozen=True)
class Visibility:
    observer: str
    seen: int
    aborted_seen: int
    delay_p50_ms: float | None
    delay_max_ms: float | None
    vanished: tuple[int, ...]


def tally(
    sent: int,
    acks: list[Ack],
    aborted: set[int],
    errors: list[SendError],
    log: list[Seen],
    unreadable: set[int],
) -> Tally:
    """Сверяет подтверждения с перечитанным логом.

    Потерянное — подтверждённое, не откаченное, отсутствующее в логе; подтверждения в партициях
    из `unreadable` не считаются потерянными: лог по ним не прочитан.
    """
    acked_seqs = {a.seq for a in acks}
    counts: dict[int, int] = {}
    for rec in log:
        counts[rec.seq] = counts.get(rec.seq, 0) + 1
    lost = {
        a.seq
        for a in acks
        if a.seq not in aborted and a.seq not in counts and a.partition not in unreadable
    }
    error_counts: dict[str, int] = {}
    for err in errors:
        error_counts[err.error] = error_counts.get(err.error, 0) + 1
    return Tally(
        sent=sent,
        acked=len(acked_seqs),
        aborted=len(aborted),
        in_topic=len(log),
        lost=tuple(sorted(lost)),
        duplicated={seq: n for seq, n in sorted(counts.items()) if n > 1},
        present_unacked=tuple(
            sorted(s for s in counts if s not in acked_seqs and s not in aborted)
        ),
        errors=error_counts,
        unreadable=tuple(sorted(unreadable)),
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // _HALF
    if len(ordered) % _HALF:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / _HALF


def visibility(
    observer: str,
    acks: list[Ack],
    aborted: set[int],
    observed: list[Seen],
    log: list[Seen],
) -> Visibility:
    """Что и когда увидел наблюдатель; задержка — первое наблюдение минус первое подтверждение."""
    first_seen: dict[int, float] = {}
    for rec in observed:
        if rec.seq not in first_seen or rec.t < first_seen[rec.seq]:
            first_seen[rec.seq] = rec.t
    first_ack: dict[int, float] = {}
    for ack in acks:
        if ack.seq not in first_ack or ack.t < first_ack[ack.seq]:
            first_ack[ack.seq] = ack.t
    delays = [(t - first_ack[seq]) * _MS_IN_S for seq, t in first_seen.items() if seq in first_ack]
    in_log = {rec.seq for rec in log}
    return Visibility(
        observer=observer,
        seen=len(first_seen),
        aborted_seen=sum(1 for seq in first_seen if seq in aborted),
        delay_p50_ms=_median(delays) if delays else None,
        delay_max_ms=max(delays) if delays else None,
        vanished=tuple(sorted(seq for seq in first_seen if seq not in in_log)),
    )
