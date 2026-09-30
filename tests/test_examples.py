import json
import subprocess
import sys
from pathlib import Path

import pytest

from ranger.config import validate_agent_config, validate_cluster_config

ROOT = Path(__file__).parents[1]


def example(name):
    return json.loads((ROOT / "examples" / name).read_text())


def test_examples_are_valid():
    validate_cluster_config(example("cluster.json"))
    validate_agent_config(example("agent.json"))


def env_keys(name):
    lines = (ROOT / "examples" / name).read_text().splitlines()
    return {line.split("=", 1)[0] for line in lines if line and not line.startswith("#")}


@pytest.mark.parametrize(
    "name,source,keys",
    [
        ("worker.env", "src/entry.py", {"DNS_API_TOKEN", "AGENT_TOKEN", "STATUS_TOKEN"}),
        ("agent.env", "src/ranger/agent.py", {"AGENT_TOKEN"}),
    ],
)
def test_env_examples_list_the_secrets_the_code_reads(name, source, keys):
    assert env_keys(name) == keys
    code = (ROOT / source).read_text()
    assert all(key in code for key in keys)


def render(cluster, tmp_path):
    source, output = tmp_path / "cluster.json", tmp_path / "wrangler.json"
    source.write_text(json.dumps(cluster))
    return subprocess.run(
        [sys.executable, "scripts/configure-worker.py", str(source), "--output", str(output)],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    ), output


def test_renderer_refuses_placeholder_service_ids(tmp_path):
    result, output = render(example("cluster.json"), tmp_path)
    assert result.returncode != 0
    assert "VPC Service ID" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("interval,schedule", [(60, "* * * * *"), (300, "*/5 * * * *")])
def test_renderer_writes_bindings_config_and_schedule(tmp_path, interval, schedule):
    cluster = example("cluster.json")
    for index, node in enumerate(cluster["nodes"].values()):
        node["vpc_service_id"] = f"service-{index}"
    cluster["policy"] = {
        "check_interval": interval,
        "startup_timeout": 2 * interval,
        "drain_seconds": interval,
    }
    result, output = render(cluster, tmp_path)
    assert result.returncode == 0, result.stderr
    wrangler = json.loads(output.read_text())
    assert wrangler["triggers"]["crons"] == [schedule]
    assert wrangler["vpc_services"] == [
        {"binding": "NODE_A_AGENT", "service_id": "service-0"},
        {"binding": "NODE_B_AGENT", "service_id": "service-1"},
    ]
    assert wrangler["secrets"]["required"] == ["DNS_API_TOKEN", "AGENT_TOKEN", "STATUS_TOKEN"]
    embedded = json.loads(wrangler["vars"]["CLUSTER_CONFIG"])
    assert "vpc_service_id" not in embedded["nodes"]["node-a"]
    assert validate_cluster_config(embedded) == embedded
