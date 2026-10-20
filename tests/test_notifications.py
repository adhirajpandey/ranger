from unittest.mock import AsyncMock

from test_controller import Cluster, Store

from ranger.controller import Notifier


async def test_emails_notable_events_but_not_quiet_cycles():
    c = Cluster()
    send = AsyncMock()
    c.controller.notifier = Notifier(Store(), send)
    await c.tick()  # Both nodes become healthy.
    await c.tick(3)
    assert send.await_count == 1
    c.io.healthy["white-box"] = False
    await c.tick(6)
    sent = "\n".join(call.args[1] for call in send.await_args_list)
    assert "failover_started workload=memes source=white-box target=black-box" in sent
    assert "transition_completed workload=memes" in sent
    assert "start_requested" not in sent


async def test_undelivered_events_go_out_with_the_next_email():
    store, send = Store(), AsyncMock(side_effect=RuntimeError("provider down"))
    notifier = Notifier(store, send)
    await notifier.deliver([{"event": "node_status", "node": "a", "status": "unhealthy"}], 0)
    assert store.value == ["1970-01-01T00:00:00+00:00 node_status node=a status=unhealthy"]
    send.side_effect = None
    await notifier.deliver([{"event": "workload_error", "workload": "w", "error": "x"}], 60)
    assert send.await_args.args[1].splitlines() == [
        "1970-01-01T00:00:00+00:00 node_status node=a status=unhealthy",
        "1970-01-01T00:01:00+00:00 workload_error workload=w error=x",
    ]
    assert store.value == []


async def test_notification_failure_does_not_fail_the_cycle():
    c = Cluster()
    c.controller.notifier = Notifier(Store(), AsyncMock())
    c.controller.notifier.store.load = AsyncMock(side_effect=RuntimeError("storage"))
    await c.tick()
    assert c.store.value["last_cycle"] == c.time
