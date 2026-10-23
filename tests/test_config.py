import copy

import pytest
from test_controller import CONFIG

from ranger.config import validate_agent_config, validate_cluster_config

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


def mutate(config, change):
    config = copy.deepcopy(config)
    change(config)
    return config


def test_defaults():
    agent = validate_agent_config(AGENT)["workloads"]["memes"]
    assert (agent["readiness_path"], agent["expected_status"], agent["pull"]) == (
        "/",
        [200],
        "missing",
    )
    assert validate_cluster_config(CONFIG)["workloads"]["memes"]["expected_status"] == [200]


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["workloads"]["memes"].update(health_path="/healthz"),
        lambda c: c["workloads"]["memes"].pop("port"),
    ],
)
def test_invalid_agent_config(change):
    with pytest.raises(ValueError):
        validate_agent_config(mutate(AGENT, change))


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(extra=True),
        lambda c: c.update(policy={"failure_threshold": 3}),
        lambda c: c["nodes"]["black-box"].pop("agent_binding"),
        lambda c: c["nodes"].pop("black-box"),
        lambda c: c["workloads"]["memes"].update(port=6704),
        lambda c: c["workloads"]["memes"].update(preferred_node="other"),
        lambda c: c["nodes"]["white-box"].update(
            tunnel_target=c["nodes"]["black-box"]["tunnel_target"]
        ),
        lambda c: c.update(notifications={"to": "owner@example.net"}),
    ],
)
def test_invalid_cluster_config(change):
    with pytest.raises(ValueError):
        validate_cluster_config(mutate(CONFIG, change))
