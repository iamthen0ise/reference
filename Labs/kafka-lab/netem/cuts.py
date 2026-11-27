"""Правила iptables для направленных обрывов связи брокера с пирами (чистые функции, без вызова iptables)."""

CHAIN = "LAB-CUT"
AGENT_PORT = 9000

# Цепочка подключена и в INPUT, и в OUTPUT: `-d peer` срабатывает только на исходящих пакетах, `-s peer` — только на входящих.
_FLAGS = {"out": "-d", "in": "-s"}
DIRECTIONS = tuple(_FLAGS)


def rule_args(ips: dict[str, str | None], cuts: set[tuple[str, str]]) -> list[list[str]]:
    """Аргументы iptables (без самого iptables). Порядок — как в `ips`, внутри пира сначала out. Пир без IP пропускается."""
    args: list[list[str]] = []
    for peer, ip in ips.items():
        if not ip:
            continue
        for direction in DIRECTIONS:
            if (peer, direction) in cuts:
                args.append(["-A", CHAIN, _FLAGS[direction], ip, "-j", "DROP"])
    return args


def restore_input(ips: dict[str, str | None], cuts: set[tuple[str, str]], agent_port: int = AGENT_PORT) -> str:
    """Вход для `iptables-restore --noflush`: объявление цепочки очищает её и заполняет заново одной транзакцией,
    поэтому окна без DROP при перезаписи нет.

    Порт агента исключён из обрывов (RETURN до всех DROP): иначе обрыв до раннера глушит и ответы агента, и `/heal` не дойдёт."""
    lines = ["*filter", f":{CHAIN} - [0:0]",
             f"-A {CHAIN} -p tcp --dport {agent_port} -j RETURN",
             f"-A {CHAIN} -p tcp --sport {agent_port} -j RETURN"]
    lines += [" ".join(a) for a in rule_args(ips, cuts)]
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def pending(ips: dict[str, str | None], cuts: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Обрывы, для которых правила ещё не поставлены: IP пира неизвестен (контейнер остановлен или его нет)."""
    return {cut for cut in cuts if not ips.get(cut[0])}
