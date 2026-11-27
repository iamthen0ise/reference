import pytest

from lab.scenario import (
    Fault,
    ScenarioError,
    TxSpec,
    apply_override,
    effective_settings,
    load_scenario,
)

MINIMAL = """
topic: {partitions: 1, replication: 3}
producer: {acks: "1", rate: 100, seconds: 5}
"""

FULL = """
name: acks1-leader-kill
description: acks=1 теряет подтверждённое при падении лидера
article: "[[Продюсер Kafka на практике#acks и идемпотентный продюсер]]"
topic: {partitions: 1, replication: 3, configs: {min.insync.replicas: "1"}}
producer: {acks: "1", idempotence: false, rate: 1000, seconds: 15, batch: 1, request_timeout_ms: 5000}
observers: [read_committed]
settle_seconds: 30
faults:
  - {at: 12s, start: $old_leader}
  - {at: 500ms, hang_transaction: 20s}
  - {at: 6s, kill: leader(p0), as: old_leader}
  - {at: 3s, cut: followers(p0), peer: leader(p0), one_way: true}
  - {at: 7s, heal: all}
"""

FAULTS = MINIMAL + "faults:\n  - %s\n"


def test_load_minimal_defaults() -> None:
    s = load_scenario("demo", MINIMAL, [])
    assert s.name == "demo"
    assert s.topic.name == "demo"
    assert s.topic.configs == {}
    assert s.producer.idempotence is False
    assert s.producer.batch == 1
    assert s.producer.request_timeout_ms == 30000
    assert s.producer.transactional is None
    assert s.producer.acks == "1"
    assert s.observers == ()
    assert s.faults == ()
    assert s.settle_seconds == 30
    assert s.description == ""
    assert s.article == ""


def test_acks_int_in_yaml_becomes_string() -> None:
    s = load_scenario("d", MINIMAL.replace('acks: "1"', "acks: 0"), [])
    assert s.producer.acks == "0"


def test_transactional_parsed() -> None:
    text = MINIMAL.replace("seconds: 5}", "seconds: 5, transactional: {abort_every: 3}}")
    assert load_scenario("d", text, []).producer.transactional == TxSpec(abort_every=3)


def test_faults_sorted_and_parsed() -> None:
    s = load_scenario("d", FULL, [])
    assert [f.at for f in s.faults] == [0.5, 3.0, 6.0, 7.0, 12.0]
    hang, cut, kill, heal, start = s.faults
    assert hang == Fault(
        at=0.5,
        action="hang_transaction",
        target=None,
        peer=None,
        one_way=False,
        bind=None,
        hang_seconds=20.0,
    )
    assert cut.action == "cut"
    assert cut.target == "followers(p0)"
    assert cut.peer == "leader(p0)"
    assert cut.one_way is True
    assert kill.bind == "old_leader"
    assert (heal.action, heal.target) == ("heal", "all")
    assert start.target == "$old_leader"
    assert s.name == "acks1-leader-kill"


def test_set_overrides_nested() -> None:
    s = load_scenario(
        "d",
        MINIMAL,
        ["producer.acks=all", "topic.configs.min.insync.replicas=2", "producer.rate=7"],
    )
    assert s.producer.acks == "all"
    assert s.producer.rate == 7
    assert s.topic.configs == {"min.insync.replicas": "2"}


def test_set_config_bool_lowercase() -> None:
    s = load_scenario("d", MINIMAL, ["topic.configs.unclean.leader.election.enable=true"])
    assert s.topic.configs == {"unclean.leader.election.enable": "true"}


def test_set_rejects_unknown_key() -> None:
    with pytest.raises(ScenarioError, match="producer"):
        load_scenario("d", MINIMAL, ["bogus=1"])
    with pytest.raises(ScenarioError, match="producer"):
        load_scenario("d", MINIMAL, ["producer.bogus=1"])
    with pytest.raises(ScenarioError, match="partitions"):
        load_scenario("d", MINIMAL, ["topic.bogus=1"])


def test_set_rejects_bad_acks() -> None:
    with pytest.raises(ScenarioError, match="0, 1, all"):
        load_scenario("d", MINIMAL, ["producer.acks=2"])


def test_set_requires_equals() -> None:
    with pytest.raises(ScenarioError, match="="):
        apply_override({}, "producer.acks")


def test_fault_requires_one_action() -> None:
    with pytest.raises(ScenarioError, match="faults.0"):
        load_scenario("d", FAULTS % "{at: 1s}", [])
    with pytest.raises(ScenarioError, match="faults.0"):
        load_scenario("d", FAULTS % "{at: 1s, kill: kafka1, stop: kafka2}", [])


def test_fault_cut_needs_peer_and_peer_only_for_cut() -> None:
    with pytest.raises(ScenarioError, match="peer"):
        load_scenario("d", FAULTS % "{at: 1s, cut: kafka1}", [])
    with pytest.raises(ScenarioError, match="peer"):
        load_scenario("d", FAULTS % "{at: 1s, kill: kafka1, peer: kafka2}", [])


def test_fault_rejects_unknown_key_and_bad_time() -> None:
    with pytest.raises(ScenarioError, match="faults.0"):
        load_scenario("d", FAULTS % "{at: 1s, kill: kafka1, bogus: 1}", [])
    with pytest.raises(ScenarioError, match=r"faults\.0\.at"):
        load_scenario("d", FAULTS % "{at: soon, kill: kafka1}", [])
    with pytest.raises(ScenarioError, match=r"faults\.0\.at"):
        load_scenario("d", FAULTS % "{kill: kafka1}", [])


def test_required_fields_name_the_path() -> None:
    no_rate = "topic: {partitions: 1, replication: 3}\nproducer: {acks: all, seconds: 1}"
    with pytest.raises(ScenarioError, match=r"producer\.rate"):
        load_scenario("d", no_rate, [])
    with pytest.raises(ScenarioError, match=r"topic\.partitions"):
        load_scenario("d", MINIMAL.replace("partitions: 1", "partitions: 0"), [])
    with pytest.raises(ScenarioError, match="observers"):
        load_scenario("d", MINIMAL + "observers: [nope]\n", [])
    with pytest.raises(ScenarioError, match="повтор"):
        load_scenario("d", MINIMAL + "observers: [read_committed, read_committed]\n", [])


def test_bad_yaml_is_scenario_error() -> None:
    with pytest.raises(ScenarioError):
        load_scenario("d", "topic: [", [])
    with pytest.raises(ScenarioError):
        load_scenario("d", "- 1", [])


def test_effective_settings_flat() -> None:
    s = load_scenario("acks1-leader-kill", FULL, ["topic.configs.min.insync.replicas=2"])
    flat = effective_settings(s)
    assert flat["producer.acks"] == "1"
    assert flat["producer.rate"] == 1000
    assert flat["topic.min.insync.replicas"] == "2"
    assert flat["topic.partitions"] == 1
    assert flat["topic.replication"] == 3
    assert flat["observers"] == ("read_committed",)
    assert flat["settle_seconds"] == 30


def test_app_retries_default_parsed_and_validated() -> None:
    assert load_scenario("d", MINIMAL, []).producer.app_retries == 0
    text = MINIMAL.replace("seconds: 5}", "seconds: 5, app_retries: 3}")
    assert load_scenario("d", text, []).producer.app_retries == 3
    assert load_scenario("d", MINIMAL, ["producer.app_retries=2"]).producer.app_retries == 2
    with pytest.raises(ScenarioError, match="producer.app_retries"):
        load_scenario("d", MINIMAL, ["producer.app_retries=-1"])
