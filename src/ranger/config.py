"""Configuration validation shared by the controller, the agent, and the renderer."""

import copy
import re

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")

POLICY_DEFAULTS = {
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
    # Names become URL path segments in the agent API.
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r}")


def configure_readiness(workload):
    """Apply readiness defaults. Agents and the controller must use the same values."""
    path = workload.setdefault("readiness_path", "/")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(c in path for c in "?# ")
        or not path.isprintable()
    ):
        raise ValueError("readiness_path must be a path without spaces, query, or fragment")
    statuses = workload.setdefault("expected_status", [200])
    if not isinstance(statuses, list) or not statuses or any(type(s) is not int for s in statuses):
        raise ValueError("expected_status must be a nonempty list of HTTP statuses")


def validate_agent_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "agent config", {"node", "workloads"})
    check_name(config["node"], "node")
    for name, item in config["workloads"].items():
        check_name(name, "workload name")
        check_keys(
            item,
            f"workload {name}",
            {"compose_file", "project", "service", "port"},
            {"readiness_path", "expected_status", "pull"},
        )
        configure_readiness(item)
        item.setdefault("pull", "missing")
    return config


def validate_cluster_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "cluster config", {"nodes", "workloads"}, {"policy", "notifications"})
    check_keys(config.get("policy", {}), "policy", (), POLICY_DEFAULTS)
    config["policy"] = {**POLICY_DEFAULTS, **config.get("policy", {})}
    if any(type(value) is not int or value <= 0 for value in config["policy"].values()):
        raise ValueError("policy values must be positive integers")
    # Each one-minute cycle counts at most one ready check.
    if config["policy"]["startup_timeout"] < 60 * config["policy"]["readiness_successes"]:
        raise ValueError("startup_timeout must allow one minute per required ready check")
    if "notifications" in config:
        check_keys(config["notifications"], "notifications", {"from", "to"})
    nodes = config["nodes"]
    if len(nodes) != 2:
        raise ValueError("Ranger requires exactly two nodes")
    for name, node in nodes.items():
        check_name(name, "node")
        check_keys(node, f"node {name}", {"tunnel_target", "agent_binding", "vpc_service_id"})
    if len({node["tunnel_target"] for node in nodes.values()}) != 2:
        raise ValueError("the two nodes need distinct tunnel targets")
    records = set()
    for name, workload in config["workloads"].items():
        check_name(name, "workload name")
        check_keys(
            workload,
            f"workload {name}",
            {"preferred_node", "hostname", "zone_id", "record_id"},
            {"readiness_path", "expected_status"},
        )
        if workload["preferred_node"] not in nodes:
            raise ValueError(f"workload {name} prefers an unknown node")
        record = (workload["zone_id"], workload["record_id"])
        if record in records:
            raise ValueError(f"workload {name} duplicates another workload's DNS record")
        records.add(record)
        configure_readiness(workload)
    return config
