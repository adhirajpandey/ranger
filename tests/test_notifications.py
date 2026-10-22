import json
from unittest.mock import AsyncMock

import pytest
from test_controller import Cluster, Store

from ranger.controller import Notifier, notification_message


def payload(text):
    return json.loads(text.split("Debug payload (JSON)\n", 1)[1])


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
    assert "memes failed repeated checks on " in sent
    assert "memes has moved from " in sent
    events = [e for call in send.await_args_list for e in payload(call.args[1])]
    assert "failover_started" in [e["event"] for e in events]
    assert "transition_completed" in [e["event"] for e in events]
    assert "start_requested" not in sent


async def test_undelivered_events_go_out_with_the_next_email():
    store, send = Store(), AsyncMock(side_effect=RuntimeError("provider down"))
    notifier = Notifier(store, send)
    first = {"event": "node_status", "node": "a", "status": "unhealthy"}
    await notifier.deliver([first], 0)
    assert store.value == [{**first, "timestamp": "1970-01-01T00:00:00+00:00"}]
    send.side_effect = None
    await notifier.deliver([{"event": "workload_error", "workload": "w", "error": "x"}], 60)
    subject, text = send.await_args.args
    assert subject == "Ranger: 2 updates"
    assert "a failed repeated health checks" in text
    assert "problem managing w: x" in text
    assert [e["timestamp"] for e in payload(text)] == [
        "1970-01-01T00:00:00+00:00",
        "1970-01-01T00:01:00+00:00",
    ]
    assert store.value == []


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"event": "node_status", "node": "a", "status": "healthy"}, "a is healthy"),
        ({"event": "node_status", "node": "a", "status": "recovering"}, "a is responding again"),
        (
            {"event": "failback_started", "workload": "w", "source": "a", "target": "b"},
            "w is moving back from a",
        ),
        (
            {"event": "transition_abandoned", "workload": "w", "target": "b", "reason": "timeout"},
            "Ranger stopped moving w to b: timeout",
        ),
        ({"event": "workload_error_cleared", "workload": "w"}, "previous error for w"),
    ],
)
def test_human_summary_precedes_debug_payload(item, expected):
    item["timestamp"] = "1970-01-01T00:00:00+00:00"
    subject, text = notification_message([item])
    assert subject.startswith("Ranger: ")
    assert expected in text.split("Debug payload", 1)[0]
    assert payload(text) == [item]


async def test_notification_failure_does_not_fail_the_cycle():
    c = Cluster()
    c.controller.notifier = Notifier(Store(), AsyncMock())
    c.controller.notifier.store.load = AsyncMock(side_effect=RuntimeError("storage"))
    await c.tick()
    assert c.store.value["last_cycle"] == c.time
