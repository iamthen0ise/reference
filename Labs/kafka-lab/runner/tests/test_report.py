import io

from rich.console import Console

from lab.report import Event, live_line, render_console, render_markdown
from lab.scenario import effective_settings, load_scenario
from lab.tally import Tally, Visibility

SCENARIO = """
description: acks=1 теряет подтверждённое
article: "[[Продюсер Kafka]]"
topic: {partitions: 2, replication: 3}
producer: {acks: "1", rate: 100, seconds: 5}
"""

LABELS = [
    "отправлено",
    "подтверждено",
    "откачено",
    "в топике",
    "потеряно — подтверждены, но в логе нет",
    "задвоено",
    "в логе без подтверждения",
    "ошибки отправки",
    "недоступные партиции",
]


def make_tally(**kw: object) -> Tally:
    args: dict[str, object] = {
        "sent": 100,
        "acked": 98,
        "aborted": 0,
        "in_topic": 95,
        "lost": (4, 7),
        "duplicated": {3: 2},
        "present_unacked": (50,),
        "errors": {"KafkaTimeoutError": 2},
        "unreadable": (),
    }
    args.update(kw)
    return Tally(**args)  # type: ignore[arg-type]


def md(vis: list[Visibility] | None = None, **kw: object) -> str:
    s = load_scenario("demo", SCENARIO, [])
    events = [Event(5.4, "kill kafka2 (лидер p0)"), Event(12.0, "heal")]
    return render_markdown(
        s, effective_settings(s), events, make_tally(**kw), vis or [], "2026-11-27 10:00"
    )


def test_markdown_contains_labels_and_events() -> None:
    text = md()
    for label in LABELS:
        assert label in text
    assert "t=05s kill kafka2 (лидер p0)" in text
    assert "t=12s heal" in text
    assert "demo" in text
    assert "[[Продюсер Kafka]]" in text
    assert "producer.acks" in text


def test_markdown_values() -> None:
    text = md()
    assert "| потеряно — подтверждены, но в логе нет | 2 (seq: 4, 7) |" in text
    assert "KafkaTimeoutError: 2" in text
    assert "| недоступные партиции | — |" in text
    assert "недоступна" not in text


def test_markdown_omits_visibility_without_observers() -> None:
    assert "Видимость" not in md()


def test_markdown_visibility_section() -> None:
    v = Visibility("read_committed", 10, 0, 120.0, None, (3,))
    text = md(vis=[v])
    assert "## Видимость" in text
    assert "read_committed" in text


def test_markdown_unreadable_note() -> None:
    text = md(unreadable=(1,))
    assert "партиция 1 недоступна — её подтверждённые не считаются потерянными" in text


def test_markdown_zero_loss() -> None:
    text = md(lost=(), duplicated={}, present_unacked=(), errors={})
    assert "| потеряно — подтверждены, но в логе нет | 0 |" in text


def test_console_renders_labels_and_events() -> None:
    s = load_scenario("demo", SCENARIO, [])
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(
        render_console(
            s, effective_settings(s), [Event(5, "kill")], make_tally(unreadable=(1,)), [], "x"
        )
    )
    text = console.export_text()
    for label in LABELS:
        assert label in text
    assert "t=05s kill" in text
    assert "партиция 1 недоступна" in text


def test_live_line() -> None:
    line = live_line(10, 8, 1, {"read_committed": 7})
    assert "10" in line and "8" in line and "read_committed 7" in line
    assert "\n" not in line
