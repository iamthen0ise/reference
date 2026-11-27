"""Правила iptables для двустороннего обрыва связи primary ↔ реплика (чистые функции, без вызова iptables)."""

CHAIN = "LAB-PARTITION"


def rule_commands(ips: dict[str, str | None], partitioned: set[str]) -> list[list[str]]:
    """Аргументы iptables (без самого iptables) после очистки цепочки.

    Порядок — как в `ips` (то есть в REPLICAS). Реплика без известного IP пропускается.
    """
    commands: list[list[str]] = []
    for name, ip in ips.items():
        if name in partitioned and ip:
            commands.append(["-A", CHAIN, "-s", ip, "-j", "DROP"])
            commands.append(["-A", CHAIN, "-d", ip, "-j", "DROP"])
    return commands


def needs_reapply(any_active: bool, ips: dict[str, str | None], applied_ips: dict[str, str | None]) -> bool:
    """any_active — есть активный netem или обрыв."""
    return any_active and ips != applied_ips


def restore_input(ips: dict[str, str | None], partitioned: set[str]) -> str:
    """Вход для `iptables-restore --noflush`: объявление цепочки очищает её и заполняет новыми правилами одной транзакцией,
    поэтому окна без DROP при перезаписи нет."""
    lines = ["*filter", f":{CHAIN} - [0:0]"]
    lines += [" ".join(args) for args in rule_commands(ips, partitioned)]
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def partition_pending(ips: dict[str, str | None], partitioned: set[str]) -> set[str]:
    """Реплики, для которых обрыв включён, но правила ещё не поставлены: IP неизвестен (контейнер остановлен)."""
    return {name for name in partitioned if not ips.get(name)}
