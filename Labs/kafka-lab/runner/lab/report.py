"""Отчёт прогона: markdown для reports/ и rich-вывод для терминала."""

from dataclasses import dataclass

from rich.console import Group
from rich.table import Table

from lab.scenario import Scenario
from lab.tally import Tally, Visibility

SHOWN_SEQS = 10
DASH = "—"

LABELS = {
    "sent": "отправлено",
    "acked": "подтверждено",
    "aborted": "откачено",
    "in_topic": "в топике",
    "lost": "потеряно — подтверждены, но в логе нет",
    "duplicated": "задвоено",
    "present_unacked": "в логе без подтверждения",
    "errors": "ошибки отправки",
    "unreadable": "недоступные партиции",
}
VIS_HEADERS = (
    "наблюдатель",
    "увидел",
    "из них откаченных",
    "задержка p50, мс",
    "задержка max, мс",
    "пропало из лога",
)


@dataclass(frozen=True)
class Event:
    t: float
    text: str


def event_line(e: Event) -> str:
    return f"t={int(e.t):02d}s {e.text}"


def _seqs(seqs: list[int] | tuple[int, ...]) -> str:
    head = ", ".join(str(s) for s in seqs[:SHOWN_SEQS])
    return f"{head} …" if len(seqs) > SHOWN_SEQS else head


def _with_seqs(seqs: tuple[int, ...]) -> str:
    return f"{len(seqs)} (seq: {_seqs(seqs)})" if seqs else "0"


def _value(v: object) -> str:
    if v is None:
        return DASH
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (tuple, list)):
        return ", ".join(str(x) for x in v) or DASH
    return str(v)


def _ms(v: float | None) -> str:
    return DASH if v is None else f"{v:.0f}"


def _summary_rows(t: Tally) -> list[tuple[str, str]]:
    dups = ", ".join(f"{seq}×{n}" for seq, n in list(t.duplicated.items())[:SHOWN_SEQS])
    dup_cell = f"{len(t.duplicated)} (seq×раз: {dups})" if t.duplicated else "0"
    errs = ", ".join(f"{name}: {n}" for name, n in t.errors.items()) or "0"
    return [
        (LABELS["sent"], str(t.sent)),
        (LABELS["acked"], str(t.acked)),
        (LABELS["aborted"], str(t.aborted)),
        (LABELS["in_topic"], str(t.in_topic)),
        (LABELS["lost"], _with_seqs(t.lost)),
        (LABELS["duplicated"], dup_cell),
        (LABELS["present_unacked"], _with_seqs(t.present_unacked)),
        (LABELS["errors"], errs),
        (LABELS["unreadable"], ", ".join(str(p) for p in t.unreadable) or DASH),
    ]


def _unreadable_notes(t: Tally) -> list[str]:
    return [
        f"партиция {p} недоступна — её подтверждённые не считаются потерянными"
        for p in t.unreadable
    ]


def _vis_rows(vis: list[Visibility]) -> list[tuple[str, ...]]:
    return [
        (
            v.observer,
            str(v.seen),
            str(v.aborted_seen),
            _ms(v.delay_p50_ms),
            _ms(v.delay_max_ms),
            _with_seqs(v.vanished),
        )
        for v in vis
    ]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _md_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return lines


def render_markdown(
    s: Scenario,
    settings: dict[str, object],
    events: list[Event],
    t: Tally,
    vis: list[Visibility],
    started: str,
) -> str:
    out = [f"# {s.name}", "", s.description, "", f"Статья: {s.article}", f"Запуск: {started}", ""]
    out += ["## Настройки", ""]
    out += _md_table(("параметр", "значение"), [(k, _value(v)) for k, v in settings.items()])
    out += ["", "## События", ""]
    out += ["```", *[event_line(e) for e in events], "```"] if events else ["Событий не было."]
    out += ["", "## Итог", ""]
    out += _md_table(("показатель", "значение"), _summary_rows(t))
    notes = _unreadable_notes(t)
    if notes:
        out += ["", *[f"{n}  " for n in notes]]
    if vis:
        out += ["", "## Видимость", ""]
        out += _md_table(VIS_HEADERS, _vis_rows(vis))
    return "\n".join(out) + "\n"


def render_console(
    s: Scenario,
    settings: dict[str, object],
    events: list[Event],
    t: Tally,
    vis: list[Visibility],
    started: str,
) -> Group:
    """Возвращает rich-объекты; печатает вызывающий: `console.print(render_console(...))`."""
    head = Table(title=f"{s.name} — {started}", show_header=False)
    head.add_row("описание", s.description)
    head.add_row("статья", s.article)
    sett = Table(title="Настройки")
    sett.add_column("параметр")
    sett.add_column("значение")
    for key, value in settings.items():
        sett.add_row(key, _value(value))
    ev = Table(title="События", show_header=False)
    for e in events:
        ev.add_row(event_line(e))
    summary = Table(title="Итог")
    summary.add_column("показатель")
    summary.add_column("значение")
    for label, value in _summary_rows(t):
        summary.add_row(label, value)
    parts: list[object] = [head, sett, ev, summary, *_unreadable_notes(t)]
    if vis:
        vt = Table(title="Видимость")
        for h in VIS_HEADERS:
            vt.add_column(h)
        for row in _vis_rows(vis):
            vt.add_row(*row)
        parts.append(vt)
    return Group(*parts)


def live_line(sent: int, acked: int, errors: int, seen: dict[str, int]) -> str:
    line = f"отправлено {sent}  подтверждено {acked}  ошибки {errors}"
    if seen:
        line += "  увидели: " + ", ".join(f"{name} {n}" for name, n in seen.items())
    return line
