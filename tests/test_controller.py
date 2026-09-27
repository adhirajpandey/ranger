import asyncio
import copy
import json

import pytest

from ranger.config import validate_cluster_config
from ranger.controller import Reconciler

CONFIG = {
    "nodes": {
        "black-box": {
            "tunnel_target": "11111111-1111-1111-1111-111111111111.cfargotunnel.com",
            "agent_binding": "BLACK_BOX_AGENT",
        },
        "white-box": {
            "tunnel_target": "22222222-2222-2222-2222-222222222222.cfargotunnel.com",
            "agent_binding": "WHITE_BOX_AGENT",
        },
    },
    "workloads": {
        "memes": {
            "preferred_node": "white-box",
            "fallback_node": "black-box",
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
        self.record = {}

    async def node(self, node):
        return {"node": node, "docker": self.nodes[node]}

    async def workload(self, node, name):
        if not self.nodes[node]:
            raise OSError("unreachable")
        if not self.running[node]:
            return {"state": "stopped"}
        return {"state": "ready" if self.healthy[node] else "unready"}

    async def dns(self, spec):
        assert spec["zone_id"] == "a" * 32 and spec["record_id"] == "b" * 32
        if isinstance(self.record, Exception):
            raise self.record
        return {
            "name": spec["hostname"],
            "type": "CNAME",
            "proxied": True,
            "content": CONFIG["nodes"][self.routed]["tunnel_target"],
            **self.record,
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
        self.controller = Reconciler(validate_cluster_config(CONFIG), self.store, self.io)

    async def tick(self, cycles=1):
        for _ in range(cycles):
            self.time += 60
            await self.controller.cycle(self.time)

    @property
    def workload(self):
        return self.store.value["workloads"]["memes"]


@pytest.mark.parametrize("failure", ["node", "application"])
async def test_failover_then_stable_failback(failure):
    c = Cluster()
    await c.tick()
    if failure == "node":
        c.io.nodes["white-box"] = False
    else:
        c.io.healthy["white-box"] = False
    await c.tick()
    assert not c.io.running["black-box"]
    await c.tick()  # Second failed check starts the replacement.
    assert c.io.running["black-box"]
    assert not c.io.changes
    await c.tick()  # First readiness success.
    assert not c.io.changes
    await c.tick()  # Second readiness success switches DNS, then drains.
    assert c.io.changes == ["black-box"]
    assert c.workload["transition"]["phase"] == "draining"
    assert c.io.running["white-box"]
    c.io.nodes["white-box"] = True
    c.io.healthy["white-box"] = True
    await c.tick()  # Drain elapsed and the previous node is reachable again.
    assert not c.io.running["white-box"]
    await c.tick()
    assert c.workload["transition"] is None
    # A transient failure resets the stability period.
    c.io.nodes["white-box"] = False
    await c.tick()
    c.io.nodes["white-box"] = True
    await c.tick(10)  # The first reachable cycle starts the ten-minute stability period.
    assert c.io.changes == ["black-box"]
    assert not c.io.running["white-box"]
    await c.tick()
    assert c.io.running["white-box"]  # Failback starts the preferred copy.
    await c.tick(2)
    assert c.io.changes == ["black-box", "white-box"]
    await c.tick()
    assert not c.io.running["black-box"]


@pytest.mark.parametrize("failure", ["start", "readiness", "both"])
async def test_failed_replacement_leaves_dns_unchanged(failure):
    c = Cluster()
    await c.tick()
    c.io.nodes["white-box"] = False
    c.io.fail_start = failure == "start"
    c.io.healthy["black-box"] = failure != "readiness"
    c.io.nodes["black-box"] = failure != "both"
    await c.tick(8)
    assert c.io.changes == []
    assert c.io.routed == "white-box"
    assert c.workload["error"]


async def test_restart_continues_transition_and_public_check_gates_cleanup():
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    await c.tick(2)
    transition = copy.deepcopy(c.workload["transition"])
    assert transition["phase"] == "starting"
    c.restart()
    c.io.public_ok = False
    await c.tick(3)
    assert c.io.changes == ["black-box"]
    assert c.io.running["white-box"]
    assert c.workload["transition"]["phase"] == "verifying"
    assert c.workload["transition"]["deadline"] == transition["deadline"]
    c.io.public_ok = True
    await c.tick(2)
    assert not c.io.running["white-box"]
    await c.tick()
    assert c.workload["transition"] is None


async def test_public_failure_restarts_drain():
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    await c.tick(4)
    assert c.workload["transition"]["phase"] == "draining"
    c.io.public_ok = False
    await c.tick()
    assert c.io.running["white-box"]
    assert c.workload["transition"]["drain_until"] is None
    c.io.public_ok = True
    await c.tick()
    assert c.workload["transition"]["phase"] == "draining"
    assert c.io.running["white-box"]
    await c.tick()
    assert not c.io.running["white-box"]


async def test_workload_probes_run_concurrently():
    c = Cluster()
    started, both = [], asyncio.Event()

    async def workload(node, name):
        # Sequential probes would time out here, waiting for a probe that never starts.
        started.append(node)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return await IO.workload(c.io, node, name)

    c.io.workload = workload
    await c.tick()
    assert all(o["state"] != "error" for o in c.workload["observations"].values())


def events(capsys):
    return [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]


async def test_failover_logs_each_step(capsys):
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    await c.tick(4)
    c.io.healthy["white-box"] = True
    await c.tick(2)
    assert events(capsys) == [
        "node_status",
        "node_status",
        "failover_started",
        "start_requested",
        "dns_switched",
        "public_check_passed",
        "stop_requested",
        "transition_completed",
    ]


async def test_errors_describe_the_current_cycle(capsys):
    c = Cluster()
    await c.tick()
    c.io.record = RuntimeError("Cloudflare DNS request failed")
    await c.tick(2)
    assert c.workload["error"] == "Cloudflare DNS request failed"
    assert c.store.value["last_successful_cycle"] < c.store.value["last_cycle"]
    c.io.record = {}
    await c.tick()
    assert c.workload["error"] is None
    assert c.store.value["last_successful_cycle"] == c.store.value["last_cycle"]
    # A persisting error is logged once, and so is its recovery.
    assert events(capsys)[-2:] == ["workload_error", "workload_error_cleared"]


async def test_failed_replacement_backs_off_explicitly():
    c = Cluster()
    await c.tick()
    c.io.healthy["white-box"] = False
    c.io.healthy["black-box"] = False
    await c.tick(7)
    assert c.workload["transition"] is None
    assert c.workload["backoff"]["reason"].startswith("replacement startup")
    await c.tick()
    assert "failover paused" in c.workload["error"]
    await c.tick(10)
    assert c.workload["backoff"] is None
    assert c.workload["transition"]["phase"] == "starting"


@pytest.mark.parametrize(
    "record",
    [
        {"content": "33333333-3333-3333-3333-333333333333.cfargotunnel.com"},
        {"proxied": False},
        {"type": "A"},
        {"name": "other.example.com"},
    ],
)
async def test_unexpected_dns_record_is_left_untouched(record):
    c = Cluster()
    await c.tick()
    c.io.record = record
    c.io.healthy["white-box"] = False
    await c.tick(6)
    assert c.io.changes == []
    assert not c.io.running["black-box"]
    assert c.workload["error"]
