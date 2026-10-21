"""Cloudflare entrypoints: the cron trigger, authenticated status, and a test email."""

import asyncio
import hmac
import json
import time
from urllib.parse import urlsplit

from js import AbortSignal, Object
from js import Request as JSRequest
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import DurableObject, Request, Response, WorkerEntrypoint

from ranger.cloudflare import CloudflareIO
from ranger.config import validate_cluster_config
from ranger.controller import SEND_TIMEOUT, Notifier, Reconciler, describe, event

# The single controller instance. Renaming it starts a new, empty controller state.
CLUSTER_NAME = "ranger-v1"
# Status reports unavailable after three missed one-minute cycles.
STALE_AFTER = 180


class DurableStore:
    def __init__(self, storage, key="state"):
        self.storage = storage
        self.key = key

    async def load(self):
        raw = await self.storage.get(self.key)
        return json.loads(raw) if raw else None

    async def save(self, state):
        await self.storage.put(self.key, json.dumps(state))


class Cluster(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.store = DurableStore(ctx.storage)
        self.config = validate_cluster_config(json.loads(env.CLUSTER_CONFIG))
        io = CloudflareIO(self.config, self.request, env.DNS_API_TOKEN, env.AGENT_TOKEN)
        notifier = None
        if "notifications" in self.config:
            notifier = Notifier(DurableStore(ctx.storage, "notifications"), self.send_email)
        self.reconciler = Reconciler(self.config, self.store, io, notifier)

    async def send_email(self, subject, text):
        message = {**self.config["notifications"], "subject": subject, "text": text}
        await self.env.EMAIL.send(to_js(message, dict_converter=Object.fromEntries))

    async def request(
        self, url, method="GET", headers=None, body=None, binding=None, status_only=False
    ):
        options = {
            "method": method,
            "headers": {"Accept": "*/*" if status_only else "application/json", **(headers or {})},
            "redirect": "manual",
            "signal": AbortSignal.timeout(self.config["policy"]["probe_timeout"] * 1000),
        }
        if body is not None:
            options["body"] = json.dumps(body)
            options["headers"]["Content-Type"] = "application/json"
        fetch = getattr(self.env, binding).fetch if binding else js_fetch
        try:
            outgoing = JSRequest.new(url, to_js(options, dict_converter=Object.fromEntries))
            response = await fetch(outgoing)
            if status_only:
                if response.body is not None:
                    await response.body.cancel()
                return {"http_status": response.status}
            if not 200 <= response.status < 300:
                raise RuntimeError(f"HTTP {response.status}")
            return json.loads(await response.text())
        except Exception as exc:
            raise RuntimeError(
                f"HTTP request failed: {type(exc).__name__}: {str(exc)[:200]}"
            ) from None

    async def fetch(self, request):
        if request.method == "POST" and urlsplit(request.url).path == "/test-email":
            if "notifications" not in self.config:
                return Response("email notifications are disabled", status=503)
            try:
                await asyncio.wait_for(
                    self.send_email(
                        "Ranger test email", "Ranger email delivery test. No actions taken."
                    ),
                    SEND_TIMEOUT,
                )
            except Exception as exc:
                event("test_email_failed", error=describe(exc)[:200])
                return Response("email delivery failed", status=502)
            event("test_email_sent")
            return Response("test email accepted")
        if request.method == "POST" and urlsplit(request.url).path == "/cycle":
            # Internal binding only. The public Worker does not forward this route.
            try:
                await self.reconciler.cycle(json.loads(await request.text())["now"])
            except Exception as exc:
                event("cycle_failed", error=describe(exc)[:200])
                return Response("cycle failed", status=500)
            return Response("cycle complete")
        state = await self.store.load() or {"status": "not initialized"}
        # A stalled controller answers 503, so a plain uptime check notices it.
        fresh = time.time() - state.get("last_cycle", 0) <= STALE_AFTER
        return Response(
            json.dumps(state),
            status=200 if fresh else 503,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlsplit(request.url).path
        if (request.method, path) not in {("GET", "/status"), ("POST", "/test-email")}:
            return Response("not found", status=404)
        token = str(self.env.STATUS_TOKEN)
        provided = request.headers.get("Authorization") or ""
        if not token or not hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
            return Response("unauthorized", status=401)
        return await self.env.CLUSTER.getByName(CLUSTER_NAME).fetch(request)

    async def scheduled(self, controller, env, ctx):
        # The cron schedule is the check interval. Its scheduled time keeps the cycle clock exact.
        await self.env.CLUSTER.getByName(CLUSTER_NAME).fetch(
            Request(
                "http://internal/cycle",
                method="POST",
                body=json.dumps({"now": controller.scheduledTime / 1000}),
            )
        )
