import copy

import pytest

from habitat_failover.controller import Reconciler

CONFIG = {
    "nodes": {
        "black-box": {"tunnel_target": "11111111-1111-1111-1111-111111111111.cfargotunnel.com"},
        "white-box": {"tunnel_target": "22222222-2222-2222-2222-222222222222.cfargotunnel.com"},
    },
    "workloads": {
        "memes": {
            "preferred_node": "white-box",
            "fallback_node": "black-box",
            "port": 6704,
            "hostname": "memes.example.com",
            "readiness_path": "/",
            "overlap_safe": True,
            "zone_id": "a" * 32,
            "record_id": "b" * 32,
        }
    },
}


class Store:
    value = None

    async def load(self):
        return copy.deepcopy(self.value)

    async def save(self, state):
        self.value = copy.deepcopy(state)


class IO:
    def __init__(self):
        self.nodes = {n: True for n in CONFIG["nodes"]}
        self.running = {"white-box": True, "black-box": False}
        self.healthy = {n: True for n in self.nodes}
        self.fail_start = False
        self.public_ok = True
        self.routed = "white-box"
        self.changes = []

    async def node(self, node):
        return {"node": node, "docker": self.nodes[node]}

    async def workload(self, node, name):
        if not self.nodes[node]:
            raise OSError("unreachable")
        return {"running": self.running[node], "ready": self.running[node] and self.healthy[node]}

    async def dns(self, spec):
        assert spec["zone_id"] == "a" * 32 and spec["record_id"] == "b" * 32
        return {
            "name": spec["hostname"],
            "type": "CNAME",
            "proxied": True,
            "content": CONFIG["nodes"][self.routed]["tunnel_target"],
        }

    async def set_dns(self, spec, target):
        node = next(n for n in self.nodes if CONFIG["nodes"][n]["tunnel_target"] == target)
        assert self.nodes[node] and self.running[node] and self.healthy[node]
        self.changes.append(node)
        self.routed = node

    async def public(self, spec):
        return {"http_status": 200 if self.public_ok else 503}

    async def change(self, node, name, action):
        if action == "start" and self.fail_start:
            raise RuntimeError("start failed")
        self.running[node] = action == "start"


class Cluster:
    def __init__(self):
        self.time = 1000
        self.store, self.io = Store(), IO()
        self.restart()

    def restart(self):
        self.controller = Reconciler(CONFIG, self.store, self.io, lambda: self.time)

    async def tick(self, seconds=20):
        self.time += seconds
        await self.controller.cycle()

    async def advance(self, seconds):
        for _ in range(seconds // 5):
            await self.tick(5)


@pytest.mark.parametrize("failure", ["node", "application"])
async def test_failover_then_stable_failback(failure):
    c = Cluster()
    await c.tick()
    if failure == "node":
        c.io.nodes["white-box"] = False
    else:
        c.io.healthy["white-box"] = False
    await c.tick()
    await c.tick()
    assert not c.io.changes
    await c.tick()
    assert c.io.running["black-box"]
    assert not c.io.changes
    await c.tick(5)
    assert not c.io.changes
    await c.tick(5)
    assert c.io.changes == ["black-box"]
    assert c.io.running["white-box"]  # Drain has not elapsed.
    c.io.nodes["white-box"] = True
    c.io.healthy["white-box"] = True
    await c.advance(60)
    assert not c.io.running["white-box"]
    assert c.io.changes == ["black-box"]
    # A transient recovery resets stability.
    c.io.nodes["white-box"] = False
    await c.advance(60)
    c.io.nodes["white-box"] = True
    await c.advance(280)
    assert c.io.changes == ["black-box"]
    await c.advance(120)
    assert c.io.changes == ["black-box", "white-box"]
    await c.advance(60)
    assert not c.io.running["black-box"]


@pytest.mark.parametrize("failure", ["start", "readiness", "both"])
async def test_failed_replacement_leaves_dns_unchanged(failure):
    c = Cluster()
    await c.tick()
    c.io.nodes["white-box"] = False
    c.io.fail_start = failure == "start"
    c.io.healthy["black-box"] = failure != "readiness"
    c.io.nodes["black-box"] = failure != "both"
    await c.advance(240)
    assert c.io.changes == []
    assert c.io.routed == "white-box"
    assert c.store.value["workloads"]["memes"]["error"]


async def test_restart_continues_transition_and_public_check_gates_cleanup():
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    await c.advance(60)
    transition = copy.deepcopy(c.store.value["workloads"]["memes"]["transition"])
    assert transition["phase"] == "starting"
    c.restart()
    c.io.public_ok = False
    await c.advance(80)
    assert c.io.changes == ["black-box"]
    assert c.io.running["white-box"]
    assert c.store.value["workloads"]["memes"]["transition"]["deadline"] == transition["deadline"]
    c.io.public_ok = True
    await c.advance(60)
    assert not c.io.running["white-box"]
    assert c.store.value["workloads"]["memes"]["transition"] is None


async def test_public_failure_restarts_drain():
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    await c.advance(80)
    assert c.store.value["workloads"]["memes"]["transition"]["phase"] == "draining"
    c.io.public_ok = False
    await c.advance(60)
    assert c.io.running["white-box"]
    assert c.store.value["workloads"]["memes"]["transition"]["drain_until"] is None
    c.io.public_ok = True
    await c.advance(40)
    assert c.io.running["white-box"]
    await c.advance(20)
    assert not c.io.running["white-box"]
