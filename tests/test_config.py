import copy

import pytest
from test_controller import CONFIG

from ranger.config import configure_readiness, validate_agent_config, validate_cluster_config

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
    "readiness",
    [
        {"readiness_path": "relative"},
        {"readiness_path": "/?query"},
        {"readiness_path": "/#fragment"},
        {"readiness_path": "/health check"},
        {"readiness_path": "/\n"},
        {"expected_status": []},
        {"expected_status": [True]},
        {"expected_status": "200"},
    ],
)
def test_invalid_readiness(readiness):
    with pytest.raises(ValueError):
        configure_readiness(readiness)


def test_agent_defaults():
    item = validate_agent_config(AGENT)["workloads"]["memes"]
    assert item["readiness_path"] == "/"
    assert item["expected_status"] == [200]
    assert item["pull"] == "missing"


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["workloads"]["memes"].update(health_path="/healthz"),
        lambda c: c["workloads"]["memes"].pop("port"),
        lambda c: c["workloads"].update({"bad/name": c["workloads"]["memes"]}),
    ],
)
def test_invalid_agent_config(change):
    config = copy.deepcopy(AGENT)
    change(config)
    with pytest.raises(ValueError):
        validate_agent_config(config)


def test_cluster_defaults():
    config = validate_cluster_config(CONFIG)
    assert config["policy"]["failure_threshold"] == 2
    assert config["workloads"]["memes"]["expected_status"] == [200]


def mutate(change):
    config = copy.deepcopy(CONFIG)
    change(config)
    return config


@pytest.mark.parametrize(
    "config",
    [
        mutate(lambda c: c.update(extra=True)),
        mutate(lambda c: c["nodes"]["black-box"].pop("agent_binding")),
        mutate(lambda c: c["nodes"].pop("black-box")),
        mutate(lambda c: c["workloads"]["memes"].update(port=6704)),
        mutate(lambda c: c["workloads"]["memes"].update(preferred_node="other")),
        mutate(
            lambda c: c["nodes"]["white-box"].update(
                tunnel_target=c["nodes"]["black-box"]["tunnel_target"]
            )
        ),
        mutate(lambda c: c["workloads"].update(copy=dict(c["workloads"]["memes"]))),
        mutate(lambda c: c.update(notifications={"to": "owner@example.net"})),
        mutate(lambda c: c.update(policy={"check_interval": 60})),
        mutate(lambda c: c.update(policy={"failure_threshold": 0})),
        mutate(lambda c: c.update(policy={"probe_timeout": 1.5})),
        mutate(lambda c: c.update(policy={"startup_timeout": 300})),
    ],
)
def test_invalid_cluster_config(config):
    with pytest.raises(ValueError):
        validate_cluster_config(config)
