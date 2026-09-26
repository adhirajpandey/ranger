"""Cloudflare entrypoints. Only GET /status is publicly exposed."""

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
from ranger.controller import Reconciler, validate_config


class DurableStore:
    def __init__(self, storage):
        self.storage = storage

    async def load(self):
        raw = await self.storage.get("state")
        return json.loads(raw) if raw else None

    async def save(self, state):
        await self.storage.put("state", json.dumps(state))


class Cluster(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.store = DurableStore(ctx.storage)
        self.config = validate_config(json.loads(env.CLUSTER_CONFIG))
        tokens = {
            node: getattr(env, config["agent_secret"])
            for node, config in self.config["nodes"].items()
        }
        io = CloudflareIO(self.config, self.request, env.DNS_API_TOKEN, tokens)
        self.reconciler = Reconciler(self.config, self.store, io, time.time)

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
            detail = str(exc)
            authorization = (headers or {}).get("Authorization")
            if authorization:
                detail = detail.replace(authorization, "[redacted]")
            raise RuntimeError(
                f"HTTP request failed: {type(exc).__name__}: {detail[:200]}"
            ) from None

    async def fetch(self, request):
        if urlsplit(request.url).path == "/initialize":
            # Internal binding only. The public Worker does not forward this route.
            async with self.reconciler.lock:
                if await self.ctx.storage.getAlarm() is None:
                    await self.ctx.storage.setAlarm(int(time.time() * 1000) + 1000)
            return Response("initialized")
        return Response(
            json.dumps(await self.store.load() or {"status": "not initialized"}),
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        )

    async def alarm(self, alarm_info=None):
        delay = self.config["policy"]["check_interval"]
        # Schedule before external work so abrupt termination also leaves a future wakeup.
        await self.ctx.storage.setAlarm(int(time.time() * 1000) + delay * 1000)
        try:
            delay = await self.reconciler.cycle()
        except Exception as exc:
            print(json.dumps({"event": "cycle_failed", "error": type(exc).__name__}))
        finally:
            await self.ctx.storage.setAlarm(int(time.time() * 1000) + delay * 1000)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method != "GET" or urlsplit(request.url).path != "/status":
            return Response("not found", status=404)
        token = str(self.env.STATUS_TOKEN)
        provided = request.headers.get("Authorization") or ""
        if len(token) < 32 or not hmac.compare_digest(
            provided.encode(), f"Bearer {token}".encode()
        ):
            return Response("unauthorized", status=401)
        return await self.env.CLUSTER.getByName("ranger-v1").fetch(request)

    async def scheduled(self, controller, env, ctx):
        # A temporary deployment bootstrap Cron creates the first alarm, then is removed.
        await self.env.CLUSTER.getByName("ranger-v1").fetch(
            Request("http://internal/initialize", method="POST")
        )
