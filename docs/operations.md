# Operate Ranger

These guides set up Ranger on two hosts, deploy the controller, watch it, test
a failover, and remove it again. The [reference](reference.md) lists every key
and field that the steps mention.

## Before you start

You need:

- Two Linux hosts with Docker Engine and the Compose plugin.
- A Cloudflare Tunnel on each host, run by `cloudflared` 2025.7.0 or later with
  host networking. Each tunnel must belong to one host only.
- A Cloudflare account on the free plan or higher, with the workload's zone.
- [uv](https://docs.astral.sh/uv/) on both hosts.
- A checkout of this repository on the machine you deploy from, with uv and
  Node.js 22.

Workers VPC is in beta. If your account cannot create VPC services, stop here.
Do not expose an agent publicly to work around it.

## Prepare a workload

Do this once for each workload, on both hosts.

1. Build the workload's image for both hosts' CPU architectures, or push a
   multi-architecture image to a registry. Ranger never builds images during a
   failover.
2. Write a Compose file that runs the image. Bind the published port to
   `127.0.0.1` and set `restart: "no"`. Without that, Docker starts the standby
   copy on every boot, and two copies run when nobody asked for them.
3. Check that the Compose file is valid:

   ```sh
   docker compose -f /srv/example-app/compose.yaml -p example-app config --quiet
   ```

4. In both tunnels, route the workload's hostname to
   `http://127.0.0.1:PORT`. Keep the tunnels' other routes.
5. Make the hostname's DNS record a proxied CNAME to the preferred host's
   tunnel, `<tunnel-id>.cfargotunnel.com`. Note the zone ID and the record ID.
   Some dashboard actions on tunnel routes rewrite this record, so recheck it
   after changing a route.
6. Add a Cache Rule that bypasses the cache for the hostname's readiness path.
   Without it, the public readiness check can pass on a cached response.
7. Start the workload on the preferred host only.

## Install the agent

Do this on both hosts.

1. As the user who runs the agent, install it. That user must be in the
   `docker` group. uv installs Python 3.12 or newer if the host lacks it.

   ```sh
   uv tool install git+https://github.com/adhirajpandey/ranger
   ```

2. Write the agent configuration to `/etc/ranger-agent/agent.json`. Start from
   [`examples/agent.json`](../examples/agent.json). Set `node` to this host's
   node name, and use the same `readiness_path` and `expected_status` as the
   cluster configuration.
3. Generate one token for both agents. Use the same value on the second host.

   ```sh
   openssl rand -hex 32
   ```

4. Write the token to `/etc/ranger-agent/agent.env`, readable only by root:

   ```text
   AGENT_TOKEN=the-token-from-step-3
   ```

5. Copy [`deploy/ranger-agent@.service`](../deploy/ranger-agent@.service) to
   `/etc/systemd/system/`. Then enable the agent for the user from step 1:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now ranger-agent@USER
   ```

6. Check that the agent answers on loopback:

   ```sh
   curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6720/health
   curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6720/workloads/example-app/health
   ```

   The first request returns `"docker": true`. The second returns the
   workload's `state`: `ready` on the preferred host and `stopped` on the other.

Keep the Compose files and `/etc/ranger-agent/` writable only by trusted
users. Anyone who can change them can run containers through Docker, which is
equivalent to root on the host.

## Deploy the controller

1. In Cloudflare, create one Workers VPC service for each host:

   | Setting | Value |
   | --- | --- |
   | Type | HTTP |
   | Host | `127.0.0.1` |
   | HTTP port | `6720` |
   | Network | The host's tunnel |

2. Create an API token with DNS Read and DNS Edit on the workloads' zone. A
   zone-wide token can edit any record in the zone. Ranger limits itself to
   the configured record IDs.
3. Copy [`examples/cluster.json`](../examples/cluster.json) to a file outside
   the repository. Fill in the tunnel targets, the VPC service IDs, and each
   workload's zone ID and record ID.
4. Render the Wrangler configuration:

   ```sh
   PYTHONPATH=src uv run python scripts/configure-worker.py ~/ranger/cluster.json
   ```

   The renderer writes `wrangler.local.jsonc`. It stops with an error if the
   configuration is invalid or a VPC service ID is still a placeholder.
5. Deploy the Worker:

   ```sh
   uv run pywrangler deploy --config wrangler.local.jsonc
   ```

6. Set the three secrets. Enter each value when prompted.

   ```sh
   uv run pywrangler secret put DNS_API_TOKEN --config wrangler.local.jsonc
   uv run pywrangler secret put AGENT_TOKEN --config wrangler.local.jsonc
   uv run pywrangler secret put STATUS_TOKEN --config wrangler.local.jsonc
   ```

   `AGENT_TOKEN` is the agents' token. Generate a separate `STATUS_TOKEN` with
   `openssl rand -hex 32`. Until all three secrets exist, cycles fail and the
   Worker logs errors.

7. Wait two minutes, then read the status:

   ```sh
   curl -H "Authorization: Bearer $STATUS_TOKEN" https://ranger.SUBDOMAIN.workers.dev/status
   ```

   `SUBDOMAIN` is your account's workers.dev subdomain. Expect HTTP 200. Both
   nodes are `healthy`, each workload's `current` is its preferred node, and
   `error` is `null`.

To change the configuration later, edit the cluster file, then repeat steps 4
and 5. Do not rename the `Cluster` class, the `CLUSTER` binding, or the
`ranger-v1` object name. Any of those changes starts the controller with empty
state.

## Watch the controller

- Point an uptime monitor at `GET /status` with the status token. HTTP 503
  means no cycle has finished for three check intervals.
- Read the controller's events in Workers Logs in the Cloudflare dashboard. The
  [reference](reference.md#log-events) lists them.
- Read an agent's log on its host:

  ```sh
  journalctl -u ranger-agent@USER
  ```

## Upgrade

To upgrade an agent, run this on its host:

```sh
uv tool upgrade ranger
sudo systemctl restart ranger-agent@USER
```

To upgrade the controller, pull the repository, then render and deploy as in
steps 4 and 5 of [Deploy the controller](#deploy-the-controller). The
controller keeps its state across deployments.

## Run a failover drill

Run the drill when a few minutes of downtime are acceptable.

1. Read the status and note each workload's `current` node.
2. On the preferred host, stop the workload:

   ```sh
   docker compose -f /srv/example-app/compose.yaml -p example-app stop
   ```

3. Watch the events. Expect `failover_started` one to two minutes after the
   stop, then `start_requested`, `dns_switched`, `public_check_passed`,
   `stop_requested`, and `transition_completed`.
4. Request the public hostname every few seconds throughout. Note the first
   failed request and the start of sustained success. Those two times give the
   outage length. The time of the DNS change alone does not.
5. Leave the preferred copy stopped. A stopped copy on a healthy host counts as
   recoverable, so about ten minutes after the stop, the controller starts it,
   switches DNS back, and stops the fallback copy.

Test a host shutdown and a network loss as separate drills, one at a time.

## Roll back

To take Ranger out of control of a workload:

1. Stop both agents, so that the controller can no longer start or stop
   anything:

   ```sh
   sudo systemctl disable --now ranger-agent@USER
   ```

2. Read the last status to see which copies are running and where DNS points.
3. Start the copy you want to keep, and check that it answers locally.
4. If DNS does not point at that host's tunnel, change the record in the
   Cloudflare dashboard. Check that the public hostname answers.
5. Wait one minute, then stop the other copy.
6. Set the workload's Compose restart policy back to what it was before
   enrollment, if you changed it.

To remove Ranger entirely, delete the Worker, the VPC services, and the DNS API
token. Deleting the Worker deletes its stored state, so save the last status
first if you need it.
