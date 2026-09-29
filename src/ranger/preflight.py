"""Read-only checks before enrolling a workload."""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from ranger.agent import Docker, NoRedirect
from ranger.config import validate_agent_config, validate_cluster_config


def check_configs(cluster, agents, names):
    """Validate both allowlists against the controller, including readiness defaults."""
    cluster = validate_cluster_config(cluster)
    by_node = {}
    for raw in agents:
        agent = validate_agent_config(raw)
        node = agent["node"]
        if node in by_node:
            raise ValueError(f"duplicate agent configuration for {node}")
        by_node[node] = agent
    if set(by_node) != set(cluster["nodes"]):
        raise ValueError("provide exactly one agent configuration for each cluster node")
    for name in names:
        if name not in cluster["workloads"]:
            raise ValueError(f"unknown cluster workload: {name}")
        spec = cluster["workloads"][name]
        for node, agent in by_node.items():
            if name not in agent["workloads"]:
                raise ValueError(f"{node}: workload {name} is missing from the allowlist")
            item = agent["workloads"][name]
            for key in ("readiness_path", "expected_status"):
                # Status ordering does not change which responses are accepted.
                local, public = item[key], spec[key]
                if key == "expected_status":
                    local, public = set(local), set(public)
                if local != public:
                    raise ValueError(f"{node}/{name}: {key} differs from the controller")
    return cluster, by_node


def check_local(docker, node, name, spec, item):
    """Inspect Compose and the image, then check the initial placement locally."""
    compose = json.loads(docker.run([*docker.compose(item), "config", "--format", "json"]))
    service = compose.get("services", {}).get(item["service"])
    if service is None:
        raise ValueError(f"{node}/{name}: Compose service does not exist")
    if service.get("restart", "no") not in {"no", "unless-stopped"}:
        raise ValueError(f"{node}/{name}: use restart no or unless-stopped")
    replicas = service.get("deploy", {}).get("replicas", service.get("scale", 1))
    if replicas != 1:
        raise ValueError(f"{node}/{name}: expected one container per service")
    if not any(
        port.get("host_ip") == "127.0.0.1"
        and str(port.get("published")) == str(item["port"])
        and port.get("protocol", "tcp") == "tcp"
        for port in service.get("ports", [])
    ):
        raise ValueError(f"{node}/{name}: publish port {item['port']} on 127.0.0.1")
    image = service.get("image")
    if not image:
        raise ValueError(f"{node}/{name}: configure an image; Ranger never builds")
    # Require the image locally even if Compose could pull it during failover.
    docker.run(["image", "inspect", image])
    observed = docker.observe(item, node)
    expected = "ready" if node == spec["preferred_node"] else "stopped"
    if observed["state"] != expected:
        raise ValueError(f"{node}/{name}: expected {expected}, got {observed['state']}")


def check_dns(cluster, spec, record):
    if (
        record.get("type") != "CNAME"
        or record.get("name") != spec["hostname"]
        or record.get("proxied") is not True
        or record.get("content") != cluster["nodes"][spec["preferred_node"]]["tunnel_target"]
    ):
        raise ValueError("DNS must be a proxied CNAME to the preferred node's tunnel")


def read_dns(spec, token):
    url = (
        f"https://api.cloudflare.com/client/v4/zones/{spec['zone_id']}"
        f"/dns_records/{spec['record_id']}"
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    # Never follow a redirect with the DNS token or print API response bodies.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=5) as response:
        result = json.load(response)
    if result.get("success") is not True:
        raise ValueError("Cloudflare DNS read failed")
    return result["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True, type=Path)
    parser.add_argument("--agent", required=True, action="append", type=Path)
    parser.add_argument("--workload", required=True, action="append")
    parser.add_argument("--local-node", help="also inspect Docker on this cluster node")
    parser.add_argument("--dns", action="store_true", help="read DNS using DNS_API_TOKEN")
    args = parser.parse_args()
    try:
        cluster, agents = check_configs(
            json.loads(args.cluster.read_text()),
            [json.loads(path.read_text()) for path in args.agent],
            args.workload,
        )
        if args.local_node and args.local_node not in agents:
            raise ValueError("--local-node must name a configured cluster node")
        token = os.environ.get("DNS_API_TOKEN", "")
        if args.dns and not token:
            raise ValueError("--dns requires DNS_API_TOKEN")
        docker = Docker() if args.local_node else None
        for name in args.workload:
            spec = cluster["workloads"][name]
            if args.local_node:
                item = agents[args.local_node]["workloads"][name]
                check_local(docker, args.local_node, name, spec, item)
            if args.dns:
                check_dns(cluster, spec, read_dns(spec, token))
            checks = ["configuration"]
            if args.local_node:
                checks.append(f"local Docker ({args.local_node})")
            if args.dns:
                checks.append("DNS")
            print(f"{name}: passed {', '.join(checks)}")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
