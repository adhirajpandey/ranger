"""Persisted reconciliation, independent of the Workers runtime."""

import asyncio
import copy
import re

from habitat_failover.readiness import configure_readiness

DEFAULTS = {
    "check_interval": 20,
    "probe_timeout": 5,
    "failure_threshold": 3,
    "readiness_successes": 2,
    "readiness_interval": 5,
    "startup_timeout": 120,
    "recovery_stability": 300,
    "drain_seconds": 45,
}


def validate_config(config):
    config = copy.deepcopy(config)
    config["policy"] = {**DEFAULTS, **config.get("policy", {})}
    if len(config["nodes"]) != 2:
        raise ValueError("v1 requires two nodes")
    if any(type(v) is not int or v <= 0 for v in config["policy"].values()):
        raise ValueError("policy values must be positive integers")
    records = set()
    for name, workload in config["workloads"].items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
            raise ValueError("invalid workload name")
        if {workload["preferred_node"], workload["fallback_node"]} != set(config["nodes"]):
            raise ValueError("workload must reference both nodes")
        if workload.get("overlap_safe") is not True:
            raise ValueError("only explicitly overlap-safe workloads are eligible")
        record = (workload["zone_id"], workload["record_id"])
        if record in records:
            raise ValueError("duplicate DNS record")
        records.add(record)
        if not all(re.fullmatch(r"[a-f0-9]{32}", value) for value in record):
            raise ValueError("zone_id and record_id must be Cloudflare IDs")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", workload["hostname"]):
            raise ValueError("invalid hostname")
        configure_readiness(workload)
        if not 1 <= workload["port"] <= 65535:
            raise ValueError("invalid port")
    targets = [node["tunnel_target"] for node in config["nodes"].values()]
    if len(set(targets)) != 2 or any(
        not re.fullmatch(r"[a-f0-9-]{36}\.cfargotunnel\.com", t) for t in targets
    ):
        raise ValueError("two distinct tunnel targets are required")
    return config


class Reconciler:
    def __init__(self, config, store, io, clock):
        self.config = validate_config(config)
        self.policy = self.config["policy"]
        self.store, self.io, self.clock = store, io, clock
        self.lock = asyncio.Lock()

    async def cycle(self):
        # A queued invocation re-reads persisted state after the preceding cycle finishes.
        async with self.lock:
            state = await self.store.load() or {"nodes": {}, "workloads": {}}
            now = self.clock()
            sampled = now >= state.get("next_check", 0)
            if sampled:
                state["next_check"] = now + self.policy["check_interval"]
            for node in self.config["nodes"]:
                health = await self.safe(self.io.node(node))
                node_state = state["nodes"].setdefault(
                    node,
                    {
                        "failures": 0,
                        "status": "unknown",
                        "healthy_since": None,
                    },
                )
                available = health.get("node") == node and health.get("docker") is True
                node_state["available"] = available
                node_state["observation"] = health
                if not available:
                    node_state["healthy_since"] = None
                if sampled:
                    self.update_node(node_state, available, now)
            await self.store.save(state)
            for name, workload in self.config["workloads"].items():
                w = state["workloads"].setdefault(
                    name,
                    {
                        "current": None,
                        "desired": workload["preferred_node"],
                        "transition": None,
                        "failures": {},
                        "preferred_since": None,
                        "retry_after": 0,
                        "error": None,
                    },
                )
                try:
                    await self.workload(state, name, workload, w, now, sampled)
                except Exception as exc:
                    # External adapters expose sanitized exceptions, never token-bearing requests.
                    w["error"] = str(exc)
                await self.store.save(state)
            state["last_cycle"] = now
            if not any(w.get("error") for w in state["workloads"].values()):
                state["last_successful_cycle"] = now
            await self.store.save(state)
            active = any(w["transition"] for w in state["workloads"].values())
            return self.policy["readiness_interval"] if active else self.policy["check_interval"]

    @staticmethod
    async def safe(awaitable):
        try:
            return await awaitable
        except Exception as exc:
            return {"error": type(exc).__name__}

    def update_node(self, node, available, now):
        if not available:
            node["failures"] += 1
            node["healthy_since"] = None
            if node["failures"] >= self.policy["failure_threshold"]:
                node["status"] = "unhealthy"
        else:
            node["failures"] = 0
            if node["healthy_since"] is None:
                node["healthy_since"] = now
            if node["status"] in {"unhealthy", "recovering"}:
                node["status"] = "recovering"
                if now - node["healthy_since"] >= self.policy["recovery_stability"]:
                    node["status"] = "healthy"
            else:
                node["status"] = "healthy"

    @staticmethod
    def eligible(state, node):
        health = state["nodes"][node]
        return health["available"] and health["status"] == "healthy"

    @staticmethod
    def ready(observation):
        return observation.get("running") is True and observation.get("ready") is True

    async def workload(self, state, name, spec, w, now, sampled):
        observations = {
            node: await self.safe(self.io.workload(node, name)) for node in self.config["nodes"]
        }
        w["observations"] = observations
        for node, observation in observations.items():
            if sampled:
                healthy = state["nodes"][node]["available"] and self.ready(observation)
                w["failures"][node] = 0 if healthy else w["failures"].get(node, 0) + 1
        dns = await self.io.dns(spec)
        w["dns_target"] = dns["content"]
        if dns["type"] != "CNAME" or dns["name"] != spec["hostname"] or not dns["proxied"]:
            raise RuntimeError("configured DNS record is not the expected proxied CNAME")
        targets = {v["tunnel_target"]: k for k, v in self.config["nodes"].items()}
        if dns["content"] not in targets:
            raise RuntimeError("DNS target outside configured tunnels; leaving record untouched")
        routed = targets[dns["content"]]
        w["current"] = routed
        preferred = spec["preferred_node"]
        preferred_obs = observations[preferred]
        # A stopped preferred copy can be started after the stability period.
        recoverable = (
            state["nodes"][preferred]["available"]
            and (preferred_obs.get("running") is False or self.ready(preferred_obs))
            and "error" not in preferred_obs
        )
        if not recoverable:
            w["preferred_since"] = None
        elif w["preferred_since"] is None:
            w["preferred_since"] = now

        transition = w["transition"]
        if transition:
            await self.advance(state, name, spec, w, observations, routed, now)
            return
        w["desired"] = routed
        if now < w["retry_after"]:
            return
        failed = w["failures"].get(routed, 0) >= self.policy["failure_threshold"]
        other = next(node for node in self.config["nodes"] if node != routed)
        failback = (
            routed != preferred
            and w["preferred_since"] is not None
            and (now - w["preferred_since"] >= self.policy["recovery_stability"])
        )
        if not failed and not failback:
            return
        target = other if failed else preferred
        if not self.eligible(state, target):
            w["error"] = "no eligible destination; DNS unchanged"
            return
        w["desired"] = target
        w["transition"] = {
            "source": routed,
            "target": target,
            "phase": "starting",
            "deadline": now + self.policy["startup_timeout"],
            "ready_count": 0,
            "last_ready": None,
            "drain_until": None,
        }
        w["error"] = None
        await self.store.save(state)
        await self.advance(state, name, spec, w, observations, routed, now)

    async def advance(self, state, name, spec, w, observations, routed, now):
        t = w["transition"]
        target, source = t["target"], t["source"]
        observation = observations[target]
        ready = self.eligible(state, target) and self.ready(observation)
        if t["phase"] == "starting":
            if now >= t["deadline"]:
                w["error"] = "replacement startup or readiness deadline exceeded"
                w["transition"] = None
                w["retry_after"] = now + self.policy["recovery_stability"]
                return
            if not ready:
                t["ready_count"], t["last_ready"] = 0, None
                if self.eligible(state, target) and observation.get("running") is False:
                    if not observation.get("operation", {}).get("pending"):
                        await self.store.save(state)
                        await self.io.change(target, name, "start")
                return
            if (
                t["last_ready"] is None
                or now - t["last_ready"] >= self.policy["readiness_interval"]
            ):
                t["ready_count"] += 1
                t["last_ready"] = now
            if t["ready_count"] < self.policy["readiness_successes"]:
                return
            w["failures"][target] = 0
            t["phase"] = "switching"
            await self.store.save(state)

        if t["phase"] == "switching":
            if not ready and routed != target:
                t["phase"] = "starting"
                t["ready_count"], t["last_ready"] = 0, None
                return
            if routed != target:
                await self.io.set_dns(spec, self.config["nodes"][target]["tunnel_target"])
                dns = await self.io.dns(spec)
                if dns["content"] != self.config["nodes"][target]["tunnel_target"]:
                    raise RuntimeError("DNS readback has not confirmed destination")
            w["current"] = target
            w["dns_target"] = self.config["nodes"][target]["tunnel_target"]
            t["phase"] = "verifying"
            await self.store.save(state)

        if t["phase"] in {"verifying", "draining"}:
            if not ready:
                t["phase"], t["drain_until"] = "verifying", None
                if w["failures"].get(target, 0) >= self.policy["failure_threshold"]:
                    # Reconsider the observed placement next cycle, keeping both copies.
                    w["transition"] = None
                    w["error"] = "destination failed after cutover"
                return
            # DNS must still point to this target before stopping any previous copy.
            dns = await self.io.dns(spec)
            if dns["content"] != self.config["nodes"][target]["tunnel_target"]:
                raise RuntimeError("DNS changed during cutover; keeping both copies")
            public = await self.io.public(spec)
            if public.get("http_status") not in spec["expected_status"]:
                t["phase"], t["drain_until"] = "verifying", None
                raise RuntimeError("public readiness did not return an accepted HTTP status")
            if t["phase"] == "verifying":
                t["phase"] = "draining"
                t["drain_until"] = now + self.policy["drain_seconds"]
                w["error"] = None
                await self.store.save(state)
            if now < t["drain_until"]:
                return
            if not state["nodes"][source]["available"]:
                w["error"] = "cutover verified; previous node unreachable for cleanup"
                return
            if (
                observations[source].get("running") is False
                and "error" not in observations[source]
                and not observations[source].get("operation", {}).get("pending")
            ):
                w["transition"] = None
                w["error"] = None
                return
            await self.store.save(state)
            await self.io.change(source, name, "stop")
