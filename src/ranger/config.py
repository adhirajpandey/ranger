"""Configuration validation shared by the controller, the agent, and the renderer."""

import copy

READINESS_DEFAULTS = {"readiness_path": "/", "expected_status": [200]}


def check_keys(section, where, required, optional=()):
    if not isinstance(section, dict):
        raise ValueError(f"{where} must be an object")
    missing = set(required) - section.keys()
    unknown = section.keys() - set(required) - set(optional)
    if missing:
        raise ValueError(f"{where} is missing {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{where} has unknown keys {', '.join(sorted(unknown))}")


def validate_agent_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "agent config", {"node", "workloads"})
    for name, item in config["workloads"].items():
        required = {"compose_file", "project", "service", "port"}
        check_keys(item, f"workload {name}", required, {*READINESS_DEFAULTS, "pull"})
        config["workloads"][name] = {**READINESS_DEFAULTS, "pull": "missing", **item}
    return config


def validate_cluster_config(config):
    config = copy.deepcopy(config)
    check_keys(config, "cluster config", {"nodes", "workloads"}, {"notifications"})
    if "notifications" in config:
        check_keys(config["notifications"], "notifications", {"from", "to"})
    nodes = config["nodes"]
    if len(nodes) != 2:
        raise ValueError("Ranger requires exactly two nodes")
    for name, node in nodes.items():
        check_keys(node, f"node {name}", {"tunnel_target", "agent_binding", "vpc_service_id"})
    if len({node["tunnel_target"] for node in nodes.values()}) != 2:
        raise ValueError("the two nodes need distinct tunnel targets")
    for name, workload in config["workloads"].items():
        required = {"preferred_node", "hostname", "zone_id", "record_id"}
        check_keys(workload, f"workload {name}", required, READINESS_DEFAULTS)
        if workload["preferred_node"] not in nodes:
            raise ValueError(f"workload {name} prefers an unknown node")
        config["workloads"][name] = {**READINESS_DEFAULTS, **workload}
    return config
