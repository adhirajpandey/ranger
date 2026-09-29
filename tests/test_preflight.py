import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ranger.preflight import check_configs, check_dns, check_local, read_dns

ROOT = Path(__file__).parents[1]


@pytest.fixture
def configs():
    cluster = json.loads((ROOT / "examples/cluster.json").read_text())
    first = json.loads((ROOT / "examples/agent.json").read_text())
    second = copy.deepcopy(first)
    second["node"] = "node-b"
    return cluster, [first, second]


def test_configs_apply_defaults_and_accept_status_order(configs):
    cluster, agents = configs
    cluster["workloads"]["example-app"]["expected_status"] = [200, 204]
    for agent in agents:
        agent["workloads"]["example-app"]["expected_status"] = [204, 200]
        agent["workloads"]["example-app"].pop("readiness_path")
    validated, by_node = check_configs(cluster, agents, ["example-app"])
    assert by_node["node-b"]["workloads"]["example-app"]["readiness_path"] == "/"
    assert validated["policy"]["check_interval"] == 60


@pytest.mark.parametrize("problem", ["missing", "duplicate", "node", "path", "status"])
def test_configs_reject_incomplete_or_inconsistent_allowlists(configs, problem):
    cluster, agents = configs
    if problem == "missing":
        agents[1]["workloads"].clear()
    elif problem == "duplicate":
        agents[1]["node"] = "node-a"
    elif problem == "node":
        agents[1]["node"] = "node-c"
    elif problem == "path":
        agents[1]["workloads"]["example-app"]["readiness_path"] = "/health"
    else:
        agents[1]["workloads"]["example-app"]["expected_status"] = [204]
    with pytest.raises(ValueError):
        check_configs(cluster, agents, ["example-app"])


def test_unknown_workload(configs):
    with pytest.raises(ValueError, match="unknown cluster workload"):
        check_configs(*configs, ["typo"])


class FakeDocker:
    compose = staticmethod(lambda item: ["compose"])

    def __init__(self):
        self.service = {
            "image": "example:v1",
            "restart": "unless-stopped",
            "ports": [{"host_ip": "127.0.0.1", "published": "3000", "target": 80}],
        }
        self.state = "ready"
        self.image_exists = True
        self.calls = []

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["image", "inspect"]:
            if not self.image_exists:
                raise RuntimeError("docker command exited with code 1")
            return "[]"
        assert args == ["compose", "config", "--format", "json"]
        return json.dumps({"services": {"app": self.service}})

    def observe(self, item, node):
        return {"state": self.state}


@pytest.mark.parametrize("node,state", [("node-a", "ready"), ("node-b", "stopped")])
def test_local_checks_only_inspect(configs, node, state):
    cluster, agents = check_configs(*configs, ["example-app"])
    docker = FakeDocker()
    docker.state = state
    check_local(
        docker,
        node,
        "example-app",
        cluster["workloads"]["example-app"],
        agents[node]["workloads"]["example-app"],
    )
    assert docker.calls == [
        ["compose", "config", "--format", "json"],
        ["image", "inspect", "example:v1"],
    ]


@pytest.mark.parametrize(
    "problem", ["service", "restart", "replicas", "binding", "port", "image", "missing", "state"]
)
def test_local_rejects_unprepared_service(configs, problem):
    cluster, agents = check_configs(*configs, ["example-app"])
    item = agents["node-a"]["workloads"]["example-app"]
    docker = FakeDocker()
    if problem == "service":
        item["service"] = "absent"
    elif problem == "restart":
        docker.service["restart"] = "always"
    elif problem == "replicas":
        docker.service["deploy"] = {"replicas": 2}
    elif problem == "binding":
        docker.service["ports"][0]["host_ip"] = "0.0.0.0"
    elif problem == "port":
        docker.service["ports"][0]["published"] = "3001"
    elif problem == "image":
        docker.service.pop("image")
    elif problem == "missing":
        docker.image_exists = False
    else:
        docker.state = "unready"
    with pytest.raises((ValueError, RuntimeError)):
        check_local(docker, "node-a", "example-app", cluster["workloads"]["example-app"], item)


@pytest.mark.parametrize("field", [None, "type", "name", "proxied", "content"])
def test_dns_requires_initial_preferred_placement(configs, field):
    cluster, _ = configs
    spec = cluster["workloads"]["example-app"]
    record = {
        "type": "CNAME",
        "name": spec["hostname"],
        "proxied": True,
        "content": cluster["nodes"]["node-a"]["tunnel_target"],
    }
    if field:
        record[field] = False if field == "proxied" else "unexpected"
        with pytest.raises(ValueError, match="proxied CNAME"):
            check_dns(cluster, spec, record)
    else:
        check_dns(cluster, spec, record)


@pytest.mark.parametrize("success", [True, False])
def test_dns_reads_only_configured_record(configs, monkeypatch, success):
    cluster, _ = configs
    spec = cluster["workloads"]["example-app"]
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            assert request.get_header("Authorization") == "Bearer test-token"
            return io.BytesIO(json.dumps({"success": success, "result": {"id": "record"}}).encode())

    monkeypatch.setattr("ranger.preflight.urllib.request.build_opener", lambda *args: Opener())
    if success:
        assert read_dns(spec, "test-token") == {"id": "record"}
    else:
        with pytest.raises(ValueError, match="Cloudflare DNS read failed"):
            read_dns(spec, "test-token")
    assert calls == [
        (
            f"https://api.cloudflare.com/client/v4/zones/{spec['zone_id']}"
            f"/dns_records/{spec['record_id']}",
            "GET",
            5,
        )
    ]


def test_cli_checks_configs_without_docker_or_network(configs, tmp_path):
    cluster, agents = configs
    paths = [tmp_path / name for name in ("cluster.json", "a.json", "b.json")]
    for path, config in zip(paths, [cluster, *agents]):
        path.write_text(json.dumps(config))
    command = [
        sys.executable,
        "-m",
        "ranger.preflight",
        "--cluster",
        str(paths[0]),
        "--agent",
        str(paths[1]),
        "--agent",
        str(paths[2]),
        "--workload",
        "example-app",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "example-app: passed configuration\n"
    paths[2].write_text("{}")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "preflight failed:" in result.stderr
