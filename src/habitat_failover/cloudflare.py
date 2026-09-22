"""HTTP adapters. The transport supplies bounded requests and private bindings."""

import time


class CloudflareIO:
    def __init__(self, config, request, dns_token, agent_tokens):
        self.config, self.request = config, request
        self.dns_token, self.agent_tokens = dns_token, agent_tokens

    async def agent(self, node, path, method="GET"):
        return await self.request(
            f"http://agent.internal{path}",
            method=method,
            headers={"Authorization": f"Bearer {self.agent_tokens[node]}"},
            binding=self.config["nodes"][node]["agent_binding"],
        )

    async def node(self, node):
        return await self.agent(node, "/health")

    async def workload(self, node, name):
        return await self.agent(node, f"/workloads/{name}/health")

    async def change(self, node, name, action):
        return await self.agent(node, f"/workloads/{name}/{action}", "POST")

    async def dns_request(self, spec, method="GET", body=None):
        # No list endpoint or hostname search, including during recovery.
        url = (
            f"https://api.cloudflare.com/client/v4/zones/{spec['zone_id']}"
            f"/dns_records/{spec['record_id']}"
        )
        result = await self.request(
            url,
            method=method,
            body=body,
            headers={"Authorization": f"Bearer {self.dns_token}"},
        )
        if not result.get("success"):
            raise RuntimeError("Cloudflare DNS request failed")
        return result["result"]

    async def dns(self, spec):
        return await self.dns_request(spec)

    async def set_dns(self, spec, target):
        return await self.dns_request(spec, "PATCH", {"content": target})

    async def public(self, spec):
        url = f"https://{spec['hostname']}{spec['readiness_path']}?failover_probe={time.time_ns()}"
        return await self.request(
            url, headers={"Cache-Control": "no-cache, no-store"}, status_only=True
        )
