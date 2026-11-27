"""Формат сценария стенда Kafka: разбор YAML, переопределения --set и проверка значений."""

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

import yaml

TOP_KEYS = (
    "name",
    "description",
    "article",
    "topic",
    "producer",
    "observers",
    "settle_seconds",
    "faults",
)
TOPIC_KEYS = ("name", "partitions", "replication", "configs")
PRODUCER_KEYS = (
    "acks",
    "idempotence",
    "rate",
    "seconds",
    "batch",
    "request_timeout_ms",
    "transactional",
    "app_retries",
)
ACKS = ("0", "1", "all")
OBSERVERS = ("read_committed", "read_uncommitted")
ACTIONS = ("kill", "stop", "start", "cut", "heal", "hang_transaction")
FAULT_KEYS = (*ACTIONS, "at", "peer", "one_way", "as")

DEFAULT_SETTLE_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_MS = 30000

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s)$")
_MS_IN_S = 1000


class ScenarioError(ValueError):
    """Ошибка сценария: сообщение начинается с пути к полю."""


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    replication: int
    configs: dict[str, str]


@dataclass(frozen=True)
class TxSpec:
    abort_every: int


@dataclass(frozen=True)
class ProducerSpec:
    acks: Literal["0", "1", "all"]
    idempotence: bool
    rate: int
    seconds: float
    batch: int
    request_timeout_ms: int
    transactional: TxSpec | None
    app_retries: int = 0  # повторы send того же seq приложением после ошибки отправки


@dataclass(frozen=True)
class Fault:
    at: float
    action: Literal["kill", "stop", "start", "cut", "heal", "hang_transaction"]
    target: str | None
    peer: str | None
    one_way: bool
    bind: str | None
    hang_seconds: float | None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    article: str
    topic: TopicSpec
    producer: ProducerSpec
    observers: tuple[Literal["read_committed", "read_uncommitted"], ...]
    faults: tuple[Fault, ...]
    settle_seconds: float


def _allowed(keys: tuple[str, ...]) -> str:
    return ", ".join(keys)


def _unknown_key(path: str, key: str, keys: tuple[str, ...]) -> ScenarioError:
    return ScenarioError(f"{path}: неизвестный ключ {key!r}, допустимы: {_allowed(keys)}")


def apply_override(data: dict[str, Any], expr: str) -> None:
    """Применяет `путь.через.точки=значение`; всё после `topic.configs.` — один ключ конфига."""
    path_text, sep, raw = expr.partition("=")
    if not sep or not path_text:
        raise ScenarioError(f"--set {expr!r}: ожидается путь=значение")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"--set {path_text}: значение не разобрано: {exc}") from exc
    if path_text.startswith("topic.configs.") and len(path_text) > len("topic.configs."):
        path = ["topic", "configs", path_text.removeprefix("topic.configs.")]
    else:
        path = path_text.split(".")
    if path[0] not in TOP_KEYS:
        raise _unknown_key("--set", path[0], TOP_KEYS)
    if len(path) > 1 and path[0] in ("topic", "producer"):
        keys = TOPIC_KEYS if path[0] == "topic" else PRODUCER_KEYS
        if path[1] not in keys:
            raise _unknown_key(f"--set {path[0]}", path[1], keys)
    node = data
    for i, key in enumerate(path[:-1]):
        child = node.setdefault(key, {})
        if not isinstance(child, dict):
            raise ScenarioError(f"--set {'.'.join(path[: i + 1])}: это не словарь, глубже не зайти")
        node = child
    node[path[-1]] = value


def _int(value: object, path: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScenarioError(f"{path}: нужно целое число не меньше {minimum}, получено {value!r}")
    return value


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioError(f"{path}: нужно true или false, получено {value!r}")
    return value


def _str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ScenarioError(f"{path}: нужна строка, получено {value!r}")
    return value


def _seconds(value: object, path: str, *, allow_zero: bool = False) -> float:
    """Число секунд или длительность `3s` / `500ms`."""
    if isinstance(value, str):
        match = _DURATION.match(value)
        if not match:
            raise ScenarioError(f"{path}: время — <число>s или <число>ms, получено {value!r}")
        number = float(match.group(1))
        seconds = number / _MS_IN_S if match.group(2) == "ms" else number
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise ScenarioError(
            f"{path}: нужно число секунд или <число>s / <число>ms, получено {value!r}"
        )
    else:
        seconds = float(value)
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ScenarioError(f"{path}: время должно быть положительным, получено {value!r}")
    return seconds


def _mapping(value: object, path: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{path}: нужен словарь, получено {value!r}")
    for key in value:
        if key not in keys:
            raise _unknown_key(path, str(key), keys)
    return value


def _required(section: dict[str, Any], key: str, path: str) -> Any:
    if key not in section:
        raise ScenarioError(f"{path}.{key}: обязательное поле")
    return section[key]


def _config_value(value: object, path: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise ScenarioError(f"{path}: значение конфига — строка, число или bool, получено {value!r}")


def _parse_topic(raw: object, name: str) -> TopicSpec:
    section = _mapping(raw, "topic", TOPIC_KEYS)
    configs_raw = section.get("configs", {})
    if not isinstance(configs_raw, dict):
        raise ScenarioError(f"topic.configs: нужен словарь, получено {configs_raw!r}")
    configs = {str(k): _config_value(v, f"topic.configs.{k}") for k, v in configs_raw.items()}
    return TopicSpec(
        name=_str(section.get("name", name), "topic.name"),
        partitions=_int(_required(section, "partitions", "topic"), "topic.partitions"),
        replication=_int(_required(section, "replication", "topic"), "topic.replication"),
        configs=configs,
    )


def _parse_tx(raw: object) -> TxSpec | None:
    if raw is None:
        return None
    section = _mapping(raw, "producer.transactional", ("abort_every",))
    every = _required(section, "abort_every", "producer.transactional")
    return TxSpec(abort_every=_int(every, "producer.transactional.abort_every"))


def _parse_producer(raw: object) -> ProducerSpec:
    section = _mapping(raw, "producer", PRODUCER_KEYS)
    acks = str(_required(section, "acks", "producer"))
    if acks not in ACKS:
        raise ScenarioError(f"producer.acks: допустимо {_allowed(ACKS)}, получено {acks!r}")
    return ProducerSpec(
        acks=cast("Literal['0', '1', 'all']", acks),
        idempotence=_bool(section.get("idempotence", False), "producer.idempotence"),
        rate=_int(_required(section, "rate", "producer"), "producer.rate"),
        seconds=_seconds(_required(section, "seconds", "producer"), "producer.seconds"),
        batch=_int(section.get("batch", 1), "producer.batch"),
        request_timeout_ms=_int(
            section.get("request_timeout_ms", DEFAULT_REQUEST_TIMEOUT_MS),
            "producer.request_timeout_ms",
        ),
        transactional=_parse_tx(section.get("transactional")),
        app_retries=_int(section.get("app_retries", 0), "producer.app_retries", minimum=0),
    )


def _parse_observers(raw: object) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise ScenarioError(f"observers: нужен список, получено {raw!r}")
    for item in raw:
        if item not in OBSERVERS:
            raise ScenarioError(f"observers: допустимо {_allowed(OBSERVERS)}, получено {item!r}")
    if len(set(raw)) != len(raw):
        raise ScenarioError(
            f"observers: повтор наблюдателя в {raw!r}, у каждого уровня изоляции один журнал"
        )
    return tuple(raw)


def _parse_fault(raw: object, index: int) -> Fault:
    path = f"faults.{index}"
    section = _mapping(raw, path, FAULT_KEYS)
    actions = [key for key in ACTIONS if key in section]
    if len(actions) != 1:
        raise ScenarioError(
            f"{path}: нужен ровно один из ключей {_allowed(ACTIONS)}, найдено {actions}"
        )
    action = actions[0]
    at = _seconds(_required(section, "at", path), f"{path}.at", allow_zero=True)
    peer = section.get("peer")
    if action == "cut" and peer is None:
        raise ScenarioError(f"{path}.peer: обязателен для cut")
    if action != "cut" and peer is not None:
        raise ScenarioError(f"{path}.peer: допустим только для cut")
    target: str | None = None
    hang: float | None = None
    if action == "hang_transaction":
        hang = _seconds(section[action], f"{path}.{action}")
    else:
        target = _str(section[action], f"{path}.{action}")
        if not target:
            raise ScenarioError(f"{path}.{action}: цель не задана")
    bind = section.get("as")
    return Fault(
        at=at,
        action=cast("Any", action),
        target=target,
        peer=None if peer is None else _str(peer, f"{path}.peer"),
        one_way=_bool(section.get("one_way", False), f"{path}.one_way"),
        bind=None if bind is None else _str(bind, f"{path}.as"),
        hang_seconds=hang,
    )


def _parse_faults(raw: object) -> tuple[Fault, ...]:
    if not isinstance(raw, list):
        raise ScenarioError(f"faults: нужен список, получено {raw!r}")
    faults = [_parse_fault(item, i) for i, item in enumerate(raw)]
    return tuple(sorted(faults, key=lambda f: f.at))


def load_scenario(name: str, text: str, overrides: list[str]) -> Scenario:
    """Разбирает YAML, применяет `--set` по очереди и проверяет результат."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{name}: не YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{name}: верхний уровень должен быть словарём")
    for expr in overrides:
        apply_override(data, expr)
    for key in data:
        if key not in TOP_KEYS:
            raise _unknown_key(name, str(key), TOP_KEYS)
    scenario_name = _str(data.get("name", name), "name")
    return Scenario(
        name=scenario_name,
        description=_str(data.get("description", ""), "description"),
        article=_str(data.get("article", ""), "article"),
        topic=_parse_topic(_required(data, "topic", name), scenario_name),
        producer=_parse_producer(_required(data, "producer", name)),
        observers=_parse_observers(data.get("observers", [])),
        faults=_parse_faults(data.get("faults", [])),
        settle_seconds=_seconds(
            data.get("settle_seconds", DEFAULT_SETTLE_SECONDS), "settle_seconds", allow_zero=True
        ),
    )


def effective_settings(s: Scenario) -> dict[str, object]:
    """Плоский словарь настроек для отчёта; `producer.transactional` — abort_every или None."""
    p = s.producer
    flat: dict[str, object] = {
        "producer.acks": p.acks,
        "producer.idempotence": p.idempotence,
        "producer.rate": p.rate,
        "producer.seconds": p.seconds,
        "producer.batch": p.batch,
        "producer.request_timeout_ms": p.request_timeout_ms,
        "producer.app_retries": p.app_retries,
        "producer.transactional": None if p.transactional is None else p.transactional.abort_every,
        "topic.partitions": s.topic.partitions,
        "topic.replication": s.topic.replication,
    }
    for key, value in s.topic.configs.items():
        flat[f"topic.{key}"] = value
    flat["observers"] = s.observers
    flat["settle_seconds"] = s.settle_seconds
    return flat
