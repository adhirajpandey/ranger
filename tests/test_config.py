import copy

import pytest
from test_controller import CONFIG

from ranger.config import (
    configure_readiness,
    cron_schedule,
    validate_agent_config,
    validate_cluster_config,
    validate_policy,
)

AGENT = {
    "node": "black-box",
    "workloads": {
        "memes": {
            "compose_file": "/srv/memes/compose.yaml",
            "project": "memes",
            "service": "app",
            "port": 6704,
        }
    },
}


@pytest.mark.parametrize(
    "policy",
    [
        {"readiness_path": "//other/"},
        {"readiness_path": "/?query"},
        {"readiness_path": "/#fragment"},
        {"readiness_path": "https://other/"},
        {"readiness_path": "/\n"},
        {"expected_status": []},
        {"expected_status": [True]},
        {"expected_status": [199]},
        {"expected_status": [600]},
        {"expected_status": "200"},
    ],
)
def test_invalid_readiness(policy):
    with pytest.raises(ValueError):
        configure_readiness(policy)


def test_readiness_defaults():
    policy = {}
    configure_readiness(policy)
    assert policy == {"readiness_path": "/", "expected_status": [200]}


def test_agent_defaults():
    item = validate_agent_config(AGENT)["workloads"]["memes"]
    assert item["readiness_path"] == "/"
    assert item["expected_status"] == [200]
    assert item["pull"] == "missing"


@pytest.mark.parametrize(
    "path,value",
    [
        (("expected_statuses",), [200]),
        (("health_path",), "/healthz"),
        (("compose_file",), "relative/compose.yaml"),
        (("env_file",), "relative.env"),
        (("port",), 0),
        (("project",), "bad name"),
        (("pull",), "sometimes"),
    ],
)
def test_invalid_agent_config(path, value):
    config = copy.deepcopy(AGENT)
    config["workloads"]["memes"][path[0]] = value
    with pytest.raises(ValueError):
        validate_agent_config(config)


def test_cluster_defaults():
    config = validate_cluster_config(CONFIG)
    assert config["policy"]["check_interval"] == 60
    assert config["workloads"]["memes"]["expected_status"] == [200]


def mutate(change):
    config = copy.deepcopy(CONFIG)
    change(config)
    return config


@pytest.mark.parametrize(
    "config",
    [
        mutate(lambda c: c.update(extra=True)),
        mutate(lambda c: c["nodes"]["black-box"].update(agent_secret="TOKEN")),
        mutate(lambda c: c["nodes"]["black-box"].pop("agent_binding")),
        mutate(lambda c: c["nodes"]["black-box"].update(agent_binding="lowercase")),
        mutate(lambda c: c["nodes"].pop("black-box")),
        mutate(lambda c: c["workloads"]["memes"].update(port=6704)),
        mutate(lambda c: c["workloads"]["memes"].update(overlap_safe=False)),
        mutate(lambda c: c["workloads"]["memes"].update(fallback_node="white-box")),
        mutate(lambda c: c["workloads"]["memes"].update(record_id="not-an-id")),
        mutate(lambda c: c["workloads"]["memes"].update(hostname="bad/host")),
        mutate(
            lambda c: c["nodes"]["white-box"].update(
                tunnel_target=c["nodes"]["black-box"]["tunnel_target"]
            )
        ),
        mutate(lambda c: c["workloads"].update(copy=dict(c["workloads"]["memes"]))),
    ],
)
def test_invalid_cluster_config(config):
    with pytest.raises(ValueError):
        validate_cluster_config(config)


@pytest.mark.parametrize(
    "policy",
    [
        {"check_intervall": 60},
        {"check_interval": 0},
        {"check_interval": 90},
        {"check_interval": 420},
        {"failure_threshold": 1.5},
        {"startup_timeout": 60},
        {"drain_seconds": 30},
        {"recovery_stability": 30},
        {"probe_timeout": 60},
    ],
)
def test_invalid_policy(policy):
    with pytest.raises(ValueError):
        validate_policy(policy)


@pytest.mark.parametrize(
    "interval,schedule", [(60, "* * * * *"), (120, "*/2 * * * *"), (300, "*/5 * * * *")]
)
def test_cron_schedule_follows_check_interval(interval, schedule):
    policy = validate_policy(
        {"check_interval": interval, "startup_timeout": 2 * interval, "drain_seconds": interval}
    )
    assert cron_schedule(policy) == schedule
