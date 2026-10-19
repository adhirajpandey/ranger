"""Persisted incident notifications, independent of the Workers runtime."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from ranger.controller import event

RETRY_SECONDS = (60, 300, 900, 1800)
SEND_TIMEOUT = 5


def error_code(error):
    """Never put arbitrary exception messages or HTTP response bodies in email."""
    for fragment, code in (
        ("no eligible destination", "no_eligible_destination"),
        ("readiness deadline", "startup_timeout"),
        ("destination failed", "destination_failed"),
        ("previous node unreachable", "cleanup_blocked"),
        ("public readiness", "public_verification_failed"),
        ("DNS", "dns_operation_failed"),
        ("transition stalled", "transition_stalled"),
    ):
        if fragment in str(error):
            return code
    return "operation_failed"


def empty_state():
    return {
        "active": {},
        "pending": {},
        "counts": {},
        "cycle_failures": 0,
        "attempts": 0,
        "retry_at": 0,
        "last_sent_at": None,
        "delivery_error": None,
    }


class Notifications:
    """One coalesced change per component, with persistent acknowledgement and retries."""

    def __init__(self, config, store, send):
        self.config, self.store, self.send = config, store, send

    def change(self, saved, key, code, now, placement=None):
        active = saved["active"].get(key)
        if code:
            if active and active["code"] == code:
                return
            incident = {
                "id": active["id"] if active else str(uuid4()),
                "code": code,
                "time": now,
                "placement": placement,
            }
            saved["active"][key] = incident
        else:
            if not active:
                return
            incident = {**active, "code": "recovered", "time": now, "placement": placement}
            del saved["active"][key]
        old_pending = saved["pending"].get(key)
        # Preserve a notice that a failure happened even if it recovered before delivery.
        incident["coalesced"] = bool(old_pending)
        saved["pending"][key] = incident

    async def update(self, state, events, now, failed=False):
        saved = await self.store.load() or empty_state()
        saved["cycle_failures"] = saved["cycle_failures"] + 1 if failed else 0
        if saved["cycle_failures"] >= 2:
            self.change(saved, "controller", "cycle_failed", now)
        elif not failed:
            self.change(saved, "controller", None, now)

        # A failed cycle may have only partially refreshed observations; do not resolve
        # existing node/workload incidents from those incomplete results.
        if state and not failed:
            self.observe(saved, state, events, now)
        await self.store.save(saved)
        if not saved["pending"] or now < saved["retry_at"]:
            return
        text = self.message(saved, now)
        try:
            await asyncio.wait_for(self.send("Ranger incident update", text), SEND_TIMEOUT)
        except Exception:
            delay = RETRY_SECONDS[min(saved["attempts"], len(RETRY_SECONDS) - 1)]
            saved["attempts"] += 1
            saved["retry_at"] = now + delay
            saved["delivery_error"] = "email_delivery_failed"
            event("notification_delivery_failed")
        else:
            saved["pending"] = {}
            saved["attempts"], saved["retry_at"] = 0, 0
            saved["last_sent_at"], saved["delivery_error"] = now, None
            event("notification_email_sent")
        await self.store.save(saved)

    def observe(self, saved, state, events, now):
        allowed = {"controller"}
        for name in self.config["nodes"]:
            key = f"node:{name}"
            allowed.add(key)
            node = state.get("nodes", {}).get(name, {})
            if node.get("status") == "unhealthy":
                self.change(saved, key, "node_unhealthy", now)
            elif node.get("status") == "healthy":
                self.change(saved, key, None, now)

        for name, spec in self.config["workloads"].items():
            w = state.get("workloads", {}).get(name)
            if not w:
                continue
            failure_key, error_key, placement_key = (
                f"application:{name}",
                f"operation:{name}",
                f"placement:{name}",
            )
            allowed.update((failure_key, error_key, placement_key))
            routed, transition = w.get("current"), w.get("transition")
            own_events = [e for e in events if e.get("workload") == name]
            started = any(e["event"] == "failover_started" for e in own_events)
            unhealthy = (
                w.get("failures", {}).get(routed, 0) >= self.config["policy"]["failure_threshold"]
            )
            if started or unhealthy:
                action = "failover_in_progress" if transition else "active_application_unhealthy"
                self.change(saved, failure_key, action, now, routed)
            elif (
                not transition and w.get("observations", {}).get(routed, {}).get("state") == "ready"
            ):
                self.change(saved, failure_key, None, now, routed)

            operation_error = w.get("error")
            if transition:
                # Allow the startup budget, drain, and two observation cycles before
                # flagging a transition that keeps running without an explicit error.
                grace = (
                    self.config["policy"]["drain_seconds"]
                    + 2 * self.config["policy"]["check_interval"]
                )
                if now >= transition["deadline"] + grace:
                    operation_error = operation_error or "transition stalled"
            saved["counts"][error_key] = (
                saved["counts"].get(error_key, 0) + 1 if operation_error else 0
            )
            abandoned = any(e["event"] == "transition_abandoned" for e in own_events)
            if abandoned or saved["counts"][error_key] >= 2:
                self.change(saved, error_key, error_code(operation_error), now, routed)
            elif not operation_error:
                self.change(saved, error_key, None, now, routed)

            for e in own_events:
                if e["event"] == "transition_completed":
                    code = (
                        "failback_completed"
                        if routed == spec["preferred_node"]
                        else "failover_completed"
                    )
                    # Completion is an event, not an ongoing incident.
                    saved["pending"][placement_key] = {
                        "id": str(uuid4()),
                        "code": code,
                        "time": now,
                        "placement": routed,
                        "coalesced": placement_key in saved["pending"],
                    }
        # Configuration removals must not leave an ever-growing notification backlog.
        for field in ("active", "pending", "counts"):
            saved[field] = {k: v for k, v in saved[field].items() if k in allowed}

    @staticmethod
    def message(saved, now):
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        lines = ["Ranger incident update", f"Time: {timestamp}", ""]
        for key, item in sorted(saved["pending"].items()):
            lines.append(f"{key}: {item['code']} (incident {item['id']})")
            if item.get("placement"):
                lines.append(f"Current placement: {item['placement']}")
            if item.get("coalesced"):
                lines.append("Multiple changes occurred before delivery; this is the latest state.")
        lines.extend(("", "Inspect authenticated /status and Cloudflare Workers Logs for details."))
        return "\n".join(lines)


def notification_health(saved):
    saved = saved or empty_state()
    return {
        "pending_count": len(saved["pending"]),
        "last_sent_at": saved["last_sent_at"],
        "delivery_error": saved["delivery_error"],
    }
