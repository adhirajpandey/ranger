import json
import subprocess
import sys
from pathlib import Path

from ranger.config import validate_agent_config, validate_cluster_config

ROOT = Path(__file__).parents[1]


def example(name):
    return json.loads((ROOT / "examples" / name).read_text())


def test_examples_are_valid():
    validate_cluster_config(example("cluster.json"))
    validate_agent_config(example("agent.json"))


def render(cluster, tmp_path):
    source, output = tmp_path / "cluster.json", tmp_path / "wrangler.json"
    source.write_text(json.dumps(cluster))
    result = subprocess.run(
        [sys.executable, "scripts/configure-worker.py", str(source), "--output", str(output)],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text())


def test_renderer_writes_bindings_and_config(tmp_path):
    wrangler = render(example("cluster.json"), tmp_path)
    assert wrangler["vpc_services"] == [
        {"binding": "NODE_A_AGENT", "service_id": "REPLACE_WITH_VPC_SERVICE_ID"},
        {"binding": "NODE_B_AGENT", "service_id": "REPLACE_WITH_VPC_SERVICE_ID"},
    ]
    assert wrangler["secrets"]["required"] == ["DNS_API_TOKEN", "AGENT_TOKEN", "STATUS_TOKEN"]
    embedded = json.loads(wrangler["vars"]["CLUSTER_CONFIG"])
    assert validate_cluster_config(embedded) == embedded
    assert "send_email" not in wrangler


def test_renderer_restricts_email_to_the_configured_recipient(tmp_path):
    cluster = example("cluster.json")
    cluster["notifications"] = {"from": "ranger@example.com", "to": "owner@example.net"}
    wrangler = render(cluster, tmp_path)
    assert wrangler["send_email"] == [{"name": "EMAIL", "destination_address": "owner@example.net"}]
