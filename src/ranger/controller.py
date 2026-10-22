"""Persisted reconciliation, independent of the Workers runtime."""

import asyncio
import json
from datetime import datetime, timezone


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


# Cloudflare's send-email binding takes about five seconds per message.
SEND_TIMEOUT = 30


SUMMARIES = {
    "unhealthy": "{node} failed repeated health checks and is unavailable for failover.",
    "recovering": "{node} is responding again. Ranger is waiting for it to stay healthy.",
    "healthy": "{node} is healthy and available for failover.",
    "failover_started": (
        "{workload} failed repeated checks on {source}. "
        "Ranger is preparing a replacement on {target}."
    ),
    "failback_started": (
        "{workload} is moving back from {source} to its preferred host, {target}, "
        "after the recovery waiting period."
    ),
    "transition_completed": (
        "{workload} has moved from {source} to {target}. "
        "DNS and the public health check passed, and the old copy is stopped."
    ),
    "transition_abandoned": "Ranger stopped moving {workload} to {target}: {reason}.",
    "workload_error": "Ranger encountered a problem managing {workload}: {error}.",
    "workload_error_cleared": "Ranger no longer reports the previous error for {workload}.",
}


def summary(item):
    """Describe an event without requiring the reader to know the log schema."""
    key = item["status"] if item["event"] == "node_status" else item["event"]
    return SUMMARIES[key].format(**item)


def notification_message(items):
    summaries = [summary(item) for item in items]
    subject = f"Ranger: {summaries[0]}" if len(items) == 1 else f"Ranger: {len(items)} updates"
    # Keep subjects on one line even if an adapter supplies a multiline error.
    subject = " ".join(subject.split())[:160]
    paragraphs = []
    for item, text in zip(items, summaries):
        when = datetime.fromisoformat(item["timestamp"]).strftime("%d %b %Y at %H:%M UTC")
        paragraphs.append(f"{when}\n{text}")
    body = "\n\n".join(paragraphs)
    return subject, body + "\n\nDebug payload (JSON)\n" + json.dumps(items, indent=2)


class Notifier:
    """Emails notable events, retaining undelivered events for the next attempt."""

    def __init__(self, store, send):
        self.store, self.send = store, send

    async def deliver(self, events, now):
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        pending = (await self.store.load() or []) + [
            {**item, "timestamp": timestamp} for item in events
        ]
        if not pending:
            return
        try:
            await asyncio.wait_for(self.send(*notification_message(pending)), SEND_TIMEOUT)
            pending = []
            event("notification_sent")
        except Exception as exc:
            event("notification_failed", error=describe(exc)[:200])
        await self.store.save(pending[-100:])


class Reconciler:
    """Runs one check per call. The caller supplies validated configuration and the time."""

    def __init__(self, config, store, io, notifier=None):
        self.config, self.policy = config, config["policy"]
        self.store, self.io = store, io
        self.notifier = notifier
        self.events = []
        self.lock = asyncio.Lock()

    async def cycle(self, now):
        # A queued invocation re-reads persisted state after the preceding cycle finishes.
        async with self.lock:
            self.events = []
            try:
                await self.reconcile(now)
            finally:
                if self.notifier:
                    try:
                        await self.notifier.deliver(self.events, now)
                    except Exception:
                        # Notification failures never change cycle results.
                        event("notification_failed")

    def emit(self, name, **fields):
        """Log an event that is also worth an email."""
        event(name, **fields)
        self.events.append({"event": name, **fields})

    async def reconcile(self, now):
        state = await self.store.load() or {"nodes": {}, "workloads": {}}
        nodes, workloads = self.config["nodes"], self.config["workloads"]
        health, observed, records = await asyncio.gather(
            settle({node: self.io.node(node) for node in nodes}),
            settle(
                {(name, node): self.io.workload(node, name) for name in workloads for node in nodes}
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
                self.emit("node_status", node=node, previous=previous, status=node_state["status"])
        await self.store.save(state)
        for name, spec in workloads.items():
            w = state["workloads"].setdefault(
                name,
                {
                    "current": None,
                    "transition": None,
                    "failures": {},
                    "preferred_since": None,
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
                self.emit("workload_error", workload=name, error=w["error"])
            elif previous and not w["error"]:
                self.emit("workload_error_cleared", workload=name)
            await self.store.save(state)
        state["last_cycle"] = now
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
        self.emit(
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
                w["error"], w["transition"] = "replacement did not become ready in time", None
                self.emit("transition_abandoned", workload=name, target=target, reason=w["error"])
                return
            if not target_ready:
                if self.eligible(state, target) and stopped(observed) and not pending(observed):
                    await self.store.save(state)
                    event("start_requested", workload=name, node=target)
                    await self.io.change(target, name, "start")
                return
            # Switching is idempotent, so an interrupted cycle simply repeats it.
            if routed != target:
                await self.io.set_dns(spec, target_tunnel)
                dns = await self.io.dns(spec)
                if dns["content"] != target_tunnel:
                    raise RuntimeError("DNS readback has not confirmed destination")
                event("dns_switched", workload=name, source=routed, target=target)
            w["current"], w["dns_target"] = target, target_tunnel
            w["failures"][target] = 0
            t["phase"] = "verifying"

        if not target_ready:
            t["phase"] = "verifying"
            if w["failures"].get(target, 0) >= self.policy["failure_threshold"]:
                # Reconsider the observed placement next cycle, keeping both copies.
                w["transition"] = None
                w["error"] = "destination failed after cutover"
                self.emit("transition_abandoned", workload=name, target=target, reason=w["error"])
            return
        # DNS must still point to this target before stopping any previous copy.
        dns = await self.io.dns(spec)
        if dns["content"] != target_tunnel:
            raise RuntimeError("DNS changed during cutover; keeping both copies")
        public = await self.io.public(spec)
        if public.get("http_status") not in spec["expected_status"]:
            t["phase"] = "verifying"
            raise RuntimeError("public readiness did not return an accepted HTTP status")
        if t["phase"] == "verifying":
            # Drain for one cycle while Cloudflare's edges pick up the new target.
            t["phase"] = "draining"
            event("public_check_passed", workload=name, target=target)
            return
        if not state["nodes"][source]["available"]:
            w["error"] = "cutover verified; previous node unreachable for cleanup"
            return
        source_obs = observations[source]
        if stopped(source_obs) and not pending(source_obs):
            w["transition"] = None
            self.emit("transition_completed", workload=name, source=source, target=target)
            return
        await self.store.save(state)
        event("stop_requested", workload=name, node=source)
        await self.io.change(source, name, "stop")
