"""Командная строка раннера: `lab list|run|state|kill|stop|start|cut|heal`."""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from aiokafka.errors import KafkaError

from lab.brokers import Brokers
from lab.cluster import Cluster, ClusterError
from lab.docker_control import DockerError
from lab.net import Net, NetError
from lab.run import LIVE_PREFIX, Deps, restore, run_scenario
from lab.scenario import ScenarioError, load_scenario
from lab.targets import BROKERS, resolve

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
EXIT_SCENARIO_ERROR = 2
EXIT_FAILED = 1


def build_deps() -> Deps:
    bootstrap = os.environ.get("BOOTSTRAP") or ",".join(f"{b}:9092" for b in BROKERS.values())
    net = Net()
    return Deps(
        cluster=Cluster(bootstrap),
        brokers=Brokers(_docker(), net),
        net=net,
        bootstrap=bootstrap,
        clock=time.monotonic,
    )


def _docker():  # noqa: ANN202
    from lab.brokers import default_docker

    return default_docker()


def out(text: str) -> None:
    """Живая строка (префикс `\\r`) перезаписывается, остальные идут отдельными строками."""
    if text.startswith(LIVE_PREFIX):
        sys.stdout.write(f"\r\033[K{text[1:]}")
    else:
        sys.stdout.write(f"\r\033[K{text}\n")
    sys.stdout.flush()


def scenario_names() -> list[str]:
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))


def _load(name: str, overrides: list[str]):  # noqa: ANN202
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ScenarioError(f"нет сценария {name!r}, доступны: {', '.join(scenario_names())}")
    return load_scenario(name, path.read_text(encoding="utf-8"), overrides)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab", description="Раннер сценариев стенда Kafka")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="список сценариев")
    run = sub.add_parser("run", help="прогнать сценарий")
    run.add_argument("name")
    run.add_argument("--set", dest="overrides", action="append", default=[], metavar="K=V")
    sub.add_parser("state", help="раскладка партиций, ISR, обрывы, остановленные брокеры")
    for action in ("kill", "stop", "start"):
        sub.add_parser(action, help=f"{action} брокера").add_argument("target")
    cut = sub.add_parser("cut", help="оборвать связь между двумя узлами")
    cut.add_argument("a")
    cut.add_argument("b")
    cut.add_argument("--one-way", action="store_true")
    sub.add_parser("heal", help="снять обрывы и запустить остановленные брокеры")
    return parser


async def _state(deps: Deps) -> None:
    try:
        topics = await deps.cluster.topics()
    except (KafkaError, ClusterError) as exc:
        topics = []
        print(f"топики недоступны: {type(exc).__name__}")
    for topic in topics:
        print(f"топик {topic}")
        try:
            layout = await deps.cluster.layout(topic)
        except (KafkaError, ClusterError) as exc:
            print(f"  раскладка недоступна: {type(exc).__name__}")
            continue
        for p, st in sorted(layout.items()):
            leader = BROKERS.get(st.leader, "нет") if st.leader else "нет"
            print(f"  p{p} лидер {leader} реплики {st.replicas} ISR {st.isr}")
    stopped = await deps.brokers.stopped()
    print("остановлены:", ", ".join(stopped) or "нет")
    for broker, entry in (await deps.net.state()).items():
        cuts = entry.get("cuts") or []
        text = entry["error"] if entry.get("error") else json.dumps(cuts, ensure_ascii=False) if cuts else "обрывов нет"
        print(f"  {broker}: {text}")


async def _heal(deps: Deps) -> None:
    started = await restore(deps, print)
    print("запущены:", ", ".join(started) or "никто")


async def _dispatch(args: argparse.Namespace) -> int:
    scenario = _load(args.name, args.overrides) if args.cmd == "run" else None  # до любых объектов кластера
    deps = build_deps()
    match args.cmd:
        case "run":
            assert scenario is not None  # noqa: S101
            await run_scenario(scenario, deps, out)
        case "state":
            await _state(deps)
        case "kill" | "stop" | "start":
            targets = resolve(args.target, {}, {})
            if not targets:
                raise ScenarioError(f"цель не найдена: {args.target}")
            for name in targets:
                await deps.brokers.act(name, args.cmd)
                print(f"{args.cmd} {name}")
        case "cut":
            await deps.net.cut(args.a, args.b, args.one_way)
            print(f"cut {args.a} {'→' if args.one_way else '↔'} {args.b}")
        case "heal":
            await _heal(deps)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "list":
        print("\n".join(scenario_names()))
        return 0
    try:
        return asyncio.run(_dispatch(args))
    except ScenarioError as exc:
        print(f"ошибка сценария: {exc}", file=sys.stderr)
        return EXIT_SCENARIO_ERROR
    except (ClusterError, DockerError, NetError, KafkaError, OSError) as exc:
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130
