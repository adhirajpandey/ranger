"""Configuration validation shared by the controller, the agent, and the renderer."""

import copy
import re
from urllib.parse import urlsplit

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")

POLICY_DEFAULTS = {
    "check_interval": 60,
    "probe_timeout": 5,
    "failure_threshold": 2,
    "readiness_successes": 2,
    "startup_timeout": 300,
    "recovery_stability": 600,
    "drain_seconds": 60,
}


def check_keys(section, where, required, optional=()):
    if not isinstance(section, dict):
        raise ValueError(f"{where} must be an object")
    missing = set(required) - section.keys()
    unknown = section.keys() - set(required) - set(optional)
    if missing:
        raise ValueError(f"{where} is missing {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{where} has unknown keys {', '.join(sorted(unknown))}")


def check_name(value, what):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r}")


def check_port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"invalid port: {value!r}")


def configure_readiness(workload):
    """Apply readiness defaults. Agents and the controller must use the same values."""
    path = workload.setdefault("readiness_path", "/")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(c.isspace() or ord(c) < 32 for c in path)
        or any(c in path for c in "?#\\")
        or urlsplit(path).netloc
    ):
        raise ValueError("readiness_path must be a local path without query or fragment")
    statuses = workload.setdefault("expected_status", [200])
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(type(status) is not int or not 200 <= status <= 599 for status in statuses)
    ):
        raise ValueError("expected_status must be a nonempty list of HTTP statuses 200..599")


def validate_agent_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "agent config", {"node", "workloads"})
    check_name(config["node"], "node")
    for name, item in config["workloads"].items():
        where = f"workload {name}"
        check_name(name, "workload name")
        check_keys(
            item,
            where,
            {"compose_file", "project", "service", "port"},
            {"env_file", "readiness_path", "expected_status", "pull"},
        )
        check_name(item["project"], f"{where} project")
        check_name(item["service"], f"{where} service")
        for key in ("compose_file", "env_file"):
            if key in item and not (isinstance(item[key], str) and item[key].startswith("/")):
                raise ValueError(f"{where} {key} must be an absolute path")
        check_port(item["port"])
        configure_readiness(item)
        if item.setdefault("pull", "missing") not in {"never", "missing", "always"}:
            raise ValueError(f"{where} has an invalid pull policy")
    return config


def validate_policy(policy):
    check_keys(policy, "policy", (), POLICY_DEFAULTS)
    policy = {**POLICY_DEFAULTS, **policy}
    if any(type(value) is not int or value <= 0 for value in policy.values()):
        raise ValueError("policy values must be positive integers")
    interval = policy["check_interval"]
    # The interval becomes a cron schedule, so it must divide an hour into whole minutes.
    if interval % 60 or 60 % (interval // 60):
        raise ValueError("check_interval must be a whole number of minutes that divides an hour")
    if policy["startup_timeout"] < 2 * interval:
        raise ValueError("startup_timeout must cover at least two check intervals")
    if policy["recovery_stability"] < interval or policy["drain_seconds"] < interval:
        raise ValueError("recovery_stability and drain_seconds must cover a check interval")
    if policy["probe_timeout"] >= interval:
        raise ValueError("probe_timeout must be shorter than check_interval")
    return policy


def validate_cluster_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "cluster config", {"nodes", "workloads"}, {"policy"})
    config["policy"] = validate_policy(config.get("policy", {}))
    nodes = config["nodes"]
    if len(nodes) != 2:
        raise ValueError("Ranger requires exactly two nodes")
    for name, node in nodes.items():
        check_name(name, "node")
        check_keys(node, f"node {name}", {"tunnel_target", "agent_binding"}, {"vpc_service_id"})
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", node["agent_binding"]):
            raise ValueError(f"node {name} agent_binding must be an uppercase binding name")
    targets = [node["tunnel_target"] for node in nodes.values()]
    if len(set(targets)) != 2 or any(
        not re.fullmatch(r"[a-f0-9-]{36}\.cfargotunnel\.com", target) for target in targets
    ):
        raise ValueError("two distinct tunnel targets are required")
    records = set()
    for name, workload in config["workloads"].items():
        where = f"workload {name}"
        check_name(name, "workload name")
        check_keys(
            workload,
            where,
            {
                "preferred_node",
                "fallback_node",
                "hostname",
                "zone_id",
                "record_id",
                "overlap_safe",
            },
            {"readiness_path", "expected_status"},
        )
        if {workload["preferred_node"], workload["fallback_node"]} != set(nodes):
            raise ValueError(f"{where} must reference both nodes")
        if workload["overlap_safe"] is not True:
            raise ValueError(f"{where}: only explicitly overlap-safe workloads are eligible")
        record = (workload["zone_id"], workload["record_id"])
        if not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v) for v in record):
            raise ValueError(f"{where} zone_id and record_id must be Cloudflare IDs")
        if record in records:
            raise ValueError(f"{where} duplicates another workload's DNS record")
        records.add(record)
        if not isinstance(workload["hostname"], str) or not re.fullmatch(
            r"[a-zA-Z0-9.-]+", workload["hostname"]
        ):
            raise ValueError(f"{where} has an invalid hostname")
        configure_readiness(workload)
    return config


def cron_schedule(policy):
    minutes = policy["check_interval"] // 60
    return "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
