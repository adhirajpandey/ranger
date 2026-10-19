import copy
from unittest.mock import AsyncMock

import pytest
from test_controller import Cluster

from ranger.notifications import Notifications, notification_health


class NotificationStore:
    def __init__(self):
        self.value = None

    async def load(self):
        return copy.deepcopy(self.value)

    async def save(self, value):
        self.value = copy.deepcopy(value)


def primary(c):
    return c.controller.config["workloads"]["memes"]["preferred_node"]


def secondary(c):
    return c.controller.config["workloads"]["memes"]["fallback_node"]


def setup():
    cluster = Cluster()
    store, send = NotificationStore(), AsyncMock()
    notifier = Notifications(cluster.controller.config, store, send)
    cluster.controller.notifier = notifier
    return cluster, store, send, notifier


async def test_healthy_cycles_and_stopped_standby_are_silent():
    c, store, send, _ = setup()
    await c.tick(4)
    send.assert_not_awaited()
    assert notification_health(store.value)["pending_count"] == 0


@pytest.mark.parametrize("kind", ["node", "application"])
async def test_failure_completion_and_failback_survive_restart(kind):
    c, store, send, notifier = setup()
    await c.tick()
    if kind == "node":
        c.io.nodes[primary(c)] = False
    else:
        c.io.healthy[primary(c)] = False
    await c.tick()
    send.assert_not_awaited()
    await c.tick()
    assert send.await_count == 1
    assert "failover_in_progress" in send.call_args.args[1]
    if kind == "node":
        assert "node_unhealthy" in send.call_args.args[1]
    c.restart()
    c.controller.notifier = Notifications(c.controller.config, store, send)
    await c.tick(2)
    assert send.await_count == 1
    if kind == "node":
        c.io.nodes[primary(c)] = True
    await c.tick(2)
    assert "failover_completed" in send.call_args.args[1]
    assert "recovered" in send.call_args.args[1]
    count = send.await_count
    await c.tick(3)
    assert send.await_count == count
    c.io.nodes[primary(c)] = True
    c.io.healthy[primary(c)] = True
    await c.tick(15)
    assert "failback_completed" in send.call_args.args[1]
    assert not store.value["active"]


async def test_retry_backoff_persists_and_recovery_coalesces():
    c, store, send, notifier = setup()
    await c.tick()
    send.side_effect = RuntimeError("secret transport details")
    c.io.healthy[primary(c)] = False
    await c.tick(2)
    first = c.time
    assert store.value["retry_at"] == first + 60
    await c.tick()
    assert store.value["retry_at"] == c.time + 300
    assert store.value["attempts"] == 2
    calls = send.await_count
    await c.tick(3)  # Failover completes while email transport remains unavailable.
    assert send.await_count == calls
    assert "failover_completed" in str(store.value["pending"])
    assert "secret transport details" not in str(store.value)
    send.side_effect = None
    c.restart()
    c.controller.notifier = Notifications(c.controller.config, store, send)
    await c.tick(2)
    assert not store.value["pending"]
    assert store.value["delivery_error"] is None
    assert "Multiple changes occurred" in send.call_args.args[1]
    assert "recovered" in send.call_args.args[1]


async def test_persistent_errors_escalate_once_without_raw_exception():
    c, store, send, _ = setup()
    await c.tick()
    c.io.record = RuntimeError("DNS failed with Bearer private-token")
    await c.tick()
    send.assert_not_awaited()
    await c.tick()
    assert "dns_operation_failed" in send.call_args.args[1]
    assert "private-token" not in send.call_args.args[1]
    await c.tick(3)
    assert send.await_count == 1
    c.io.record = {}
    await c.tick()
    assert send.await_count == 2
    assert "recovered" in send.call_args.args[1]


async def test_abandoned_transition_alerts_immediately_and_backoff_deduplicates():
    c, store, send, _ = setup()
    await c.tick()
    c.io.healthy[primary(c)] = False
    c.io.healthy[secondary(c)] = False
    await c.tick(7)
    assert "startup_timeout" in send.call_args.args[1]
    count = send.await_count
    await c.tick(3)
    assert send.await_count == count


async def test_controller_failures_alert_on_second_cycle_and_preserve_exception():
    c, store, send, _ = setup()
    await c.tick()
    c.controller.reconcile = AsyncMock(side_effect=ValueError("original failure"))
    for i in range(2):
        with pytest.raises(ValueError, match="original failure"):
            await c.tick()
        assert send.await_count == i
    assert "cycle_failed" in send.call_args.args[1]
    c.restart()
    c.controller.notifier = Notifications(c.controller.config, store, send)
    await c.tick()
    assert "recovered" in send.call_args.args[1]


async def test_notification_storage_failure_cannot_fail_reconciliation():
    c, store, send, _ = setup()
    store.load = AsyncMock(side_effect=OSError("unavailable"))
    await c.tick()
    assert c.store.value["last_cycle"] == c.time
    send.assert_not_awaited()


async def test_newly_enabled_notifications_report_existing_unhealthy_nodes():
    c = Cluster()
    await c.tick()
    c.io.nodes[primary(c)] = False
    await c.tick(2)
    store, send = NotificationStore(), AsyncMock()
    c.controller.notifier = Notifications(c.controller.config, store, send)
    await c.tick()
    assert "node_unhealthy" in send.call_args.args[1]


async def test_configuration_removal_bounds_pending_state():
    c, store, send, notifier = setup()
    await c.tick()
    c.io.healthy[primary(c)] = False
    send.side_effect = OSError()
    await c.tick(2)
    notifier.config = {**notifier.config, "workloads": {}}
    await notifier.update(c.store.value, [], c.time + 1)
    assert not store.value["pending"]
    assert not store.value["counts"]


async def test_email_timeout_preserves_saved_failover_progress(monkeypatch):
    import asyncio

    import ranger.notifications as notifications

    c, store, send, _ = setup()
    await c.tick()
    monkeypatch.setattr(notifications, "SEND_TIMEOUT", 0.01)

    async def blocked(*args):
        await asyncio.Event().wait()

    send.side_effect = blocked
    c.io.healthy[primary(c)] = False
    await c.tick(2)
    assert c.io.running[secondary(c)]
    assert c.workload["transition"]["phase"] == "starting"
    assert c.store.value["last_cycle"] == c.time
    assert store.value["delivery_error"] == "email_delivery_failed"


async def test_retry_delay_caps_at_thirty_minutes():
    c, store, send, notifier = setup()
    await c.tick()
    send.side_effect = OSError()
    c.io.healthy[primary(c)] = False
    await c.tick(2)
    for delay in (300, 900, 1800, 1800):
        now = store.value["retry_at"]
        await notifier.update(c.store.value, [], now)
        assert store.value["retry_at"] == now + delay


async def test_no_eligible_destination_and_later_recovery():
    c, store, send, _ = setup()
    await c.tick()
    c.io.nodes[secondary(c)] = False
    c.io.healthy[primary(c)] = False
    await c.tick(3)
    assert "no_eligible_destination" in send.call_args.args[1]
    c.io.healthy[primary(c)] = True
    await c.tick()
    assert "recovered" in send.call_args.args[1]
    assert "operation:memes" not in store.value["active"]


async def test_stuck_transition_without_controller_error_escalates_after_grace():
    c, store, send, notifier = setup()
    await c.tick()
    state = c.store.value
    state["workloads"]["memes"]["transition"] = {
        "phase": "draining",
        "deadline": c.time,
        "drain_until": c.time,
    }
    policy = c.controller.config["policy"]
    due = c.time + policy["drain_seconds"] + 2 * policy["check_interval"]
    await notifier.update(state, [], due - 1)
    send.assert_not_awaited()
    await notifier.update(state, [], due)
    send.assert_not_awaited()
    await notifier.update(state, [], due + policy["check_interval"])
    assert "transition_stalled" in send.call_args.args[1]
    state["workloads"]["memes"]["transition"] = None
    await notifier.update(state, [], due + 2 * policy["check_interval"])
    assert "recovered" in send.call_args.args[1]
