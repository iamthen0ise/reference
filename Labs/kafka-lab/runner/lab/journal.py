"""Журнал прогона: то, что продюсер и наблюдатели пишут, а раннер потом сверяет с логом."""

from dataclasses import dataclass, field

from lab.report import Event
from lab.tally import Ack, Seen, SendError


@dataclass
class Journal:
    acks: list[Ack] = field(default_factory=list)
    errors: list[SendError] = field(default_factory=list)
    aborted: set[int] = field(default_factory=set)
    seen: dict[str, list[Seen]] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    sent: int = 0  # попытки отправки, не подтверждения
