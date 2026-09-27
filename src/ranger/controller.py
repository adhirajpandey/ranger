"""Persisted reconciliation, independent of the Workers runtime."""

import asyncio
import json


def event(name, **fields):
    """Emit one structured log line. Workers observability indexes JSON output."""
    print(json.dumps({"event": name, **fields}))


def describe(exc):
    # Adapters raise sanitized messages, never token-bearing requests.
    return str(exc) or type(exc).__name__


async def settle(calls):
    """Await calls concurrently and return each result or exception by key."""
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    return dict(zip(calls, results))


def observation(result):
    if isinstance(result, Exception):
        return {"state": "error", "error": describe(result)}
    return result


def ready(observation):
    return observation.get("state") == "ready"


def stopped(observation):
    return observation.get("state") == "stopped"


def pending(observation):
    return bool(observation.get("operation", {}).get("pending"))


class Reconciler:
    """Runs one check per call. The caller supplies validated configuration and the time."""

    def __init__(self, config, store, io):
        self.config, self.policy = config, config["policy"]
        self.store, self.io = store, io
        self.lock = asyncio.Lock()

    async def cycle(self, now):
        # A queued invocation re-reads persisted state after the preceding cycle finishes.
        async with self.lock:
            state = await self.store.load() or {"nodes": {}, "workloads": {}}
            nodes, workloads = self.config["nodes"], self.config["workloads"]
            health, observed, records = await asyncio.gather(
                settle({node: self.io.node(node) for node in nodes}),
                settle(
                    {
                        (name, node): self.io.workload(node, name)
                        for name in workloads
                        for node in nodes
                    }
                ),
                settle({name: self.io.dns(spec) for name, spec in workloads.items()}),
            )
            for node in nodes:
                result = health[node]
                if isinstance(result, Exception):
                    result = {"error": describe(result)}
                node_state = state["nodes"].setdefault(
                    node, {"failures": 0, "status": "unknown", "healthy_since": None}
                )
                available = result.get("node") == node and result.get("docker") is True
                node_state["available"] = available
                node_state["observation"] = result
                previous = node_state["status"]
                self.update_node(node_state, available, now)
                if node_state["status"] != previous:
                    event("node_status", node=node, previous=previous, status=node_state["status"])
            await self.store.save(state)
            for name, spec in workloads.items():
                w = state["workloads"].setdefault(
                    name,
                    {
                        "current": None,
                        "desired": spec["preferred_node"],
                        "transition": None,
                        "failures": {},
                        "preferred_since": None,
                        "backoff": None,
                        "error": None,
                    },
                )
                observations = {node: observation(observed[name, node]) for node in nodes}
                # Errors describe this cycle only; the logs keep their history.
                previous, w["error"] = w["error"], None
                try:
                    await self.workload(state, name, spec, w, observations, records[name], now)
                except Exception as exc:
                    w["error"] = describe(exc)
                if w["error"] and w["error"] != previous:
                    event("workload_error", workload=name, error=w["error"])
                elif previous and not w["error"]:
                    event("workload_error_cleared", workload=name)
                await self.store.save(state)
            state["last_cycle"] = now
            if not any(w.get("error") for w in state["workloads"].values()):
                state["last_successful_cycle"] = now
            await self.store.save(state)

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

    async def workload(self, state, name, spec, w, observations, dns, now):
        w["observations"] = observations
        for node, observed in observations.items():
            healthy = state["nodes"][node]["available"] and ready(observed)
            w["failures"][node] = 0 if healthy else w["failures"].get(node, 0) + 1
        if isinstance(dns, Exception):
            raise dns
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
        # A stopped preferred copy can be started after the stability period. The period
        # counts only while traffic is elsewhere, so every failover waits for all of it.
        recoverable = state["nodes"][preferred]["available"] and (
            stopped(preferred_obs) or ready(preferred_obs)
        )
        if not recoverable or routed == preferred:
            w["preferred_since"] = None
        elif w["preferred_since"] is None:
            w["preferred_since"] = now

        if w["transition"]:
            await self.advance(state, name, spec, w, observations, routed, now)
            return
        w["desired"] = routed
        if w["backoff"]:
            if now < w["backoff"]["until"]:
                w["error"] = f"{w['backoff']['reason']}; failover paused until the backoff ends"
                return
            w["backoff"] = None
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
        event(
            "failover_started" if failed else "failback_started",
            workload=name,
            source=routed,
            target=target,
        )
        w["transition"] = {
            "source": routed,
            "target": target,
            "phase": "starting",
            "deadline": now + self.policy["startup_timeout"],
            "ready_count": 0,
            "drain_until": None,
        }
        await self.store.save(state)
        await self.advance(state, name, spec, w, observations, routed, now)

    async def advance(self, state, name, spec, w, observations, routed, now):
        t = w["transition"]
        target, source = t["target"], t["source"]
        target_tunnel = self.config["nodes"][target]["tunnel_target"]
        observed = observations[target]
        target_ready = self.eligible(state, target) and ready(observed)
        if t["phase"] == "starting":
            if now >= t["deadline"]:
                reason = "replacement startup or readiness deadline exceeded"
                w["error"], w["transition"] = reason, None
                w["backoff"] = {"until": now + self.policy["recovery_stability"], "reason": reason}
                event("transition_abandoned", workload=name, target=target, reason=reason)
                return
            if not target_ready:
                t["ready_count"] = 0
                if self.eligible(state, target) and stopped(observed) and not pending(observed):
                    await self.store.save(state)
                    event("start_requested", workload=name, node=target)
                    await self.io.change(target, name, "start")
                return
            # Each cycle contributes at most one readiness success.
            t["ready_count"] += 1
            if t["ready_count"] < self.policy["readiness_successes"]:
                return
            w["failures"][target] = 0
            t["phase"] = "switching"
            await self.store.save(state)

        if t["phase"] == "switching":
            if not target_ready and routed != target:
                t["phase"], t["ready_count"] = "starting", 0
                return
            if routed != target:
                await self.io.set_dns(spec, target_tunnel)
                dns = await self.io.dns(spec)
                if dns["content"] != target_tunnel:
                    raise RuntimeError("DNS readback has not confirmed destination")
                event("dns_switched", workload=name, source=routed, target=target)
            w["current"] = target
            w["dns_target"] = target_tunnel
            t["phase"] = "verifying"
            await self.store.save(state)

        if t["phase"] in {"verifying", "draining"}:
            if not target_ready:
                t["phase"], t["drain_until"] = "verifying", None
                if w["failures"].get(target, 0) >= self.policy["failure_threshold"]:
                    # Reconsider the observed placement next cycle, keeping both copies.
                    w["transition"] = None
                    w["error"] = "destination failed after cutover"
                    event("transition_abandoned", workload=name, target=target, reason=w["error"])
                return
            # DNS must still point to this target before stopping any previous copy.
            dns = await self.io.dns(spec)
            if dns["content"] != target_tunnel:
                raise RuntimeError("DNS changed during cutover; keeping both copies")
            public = await self.io.public(spec)
            if public.get("http_status") not in spec["expected_status"]:
                t["phase"], t["drain_until"] = "verifying", None
                raise RuntimeError("public readiness did not return an accepted HTTP status")
            if t["phase"] == "verifying":
                t["phase"] = "draining"
                t["drain_until"] = now + self.policy["drain_seconds"]
                event("public_check_passed", workload=name, target=target)
                await self.store.save(state)
            if now < t["drain_until"]:
                return
            if not state["nodes"][source]["available"]:
                w["error"] = "cutover verified; previous node unreachable for cleanup"
                return
            source_obs = observations[source]
            if stopped(source_obs) and not pending(source_obs):
                w["transition"] = None
                event("transition_completed", workload=name, source=source, target=target)
                return
            await self.store.save(state)
            event("stop_requested", workload=name, node=source)
            await self.io.change(source, name, "stop")
