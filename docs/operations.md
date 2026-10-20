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
- A GitHub fork of this repository to deploy from with GitHub Actions, or a
  checkout with uv and Node.js 22 to deploy by hand.

Workers VPC is in beta. If your account cannot create VPC services, stop here.
Do not expose an agent publicly to work around it.

## Prepare a workload

Do this once for each workload, on both hosts.

1. Build the workload's image for both hosts' CPU architectures, or push a
   multi-architecture image to a registry. Ranger never builds images during a
   failover.
2. Use the workload's existing Compose file, or write one. The file must meet
   these requirements:

   - The service runs the image from step 1 and runs exactly one container.
   - The service publishes its port on the host. Bind it to `127.0.0.1` so
     that only the tunnel and the agent reach it.
   - The restart policy is not `always`. With `always`, Docker restarts the
     stopped standby when the host or Docker restarts, and two copies run.
     `unless-stopped` and `"no"` both work.

   Pin an image tag that names one build, so both hosts run the same version.
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
6. Make sure Cloudflare does not cache the hostname's readiness path. Request
   the path and read the `cf-cache-status` header. `DYNAMIC` means Cloudflare
   does not cache it. For any other value, add a Cache Rule that bypasses the
   cache for the path. Otherwise the public readiness check can pass on a
   cached response.
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

4. Write the token to `/etc/ranger-agent/agent.env`, readable only by root, as
   in [`examples/agent.env`](../examples/agent.env):

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

Do this once:

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
3. Copy [`examples/cluster.json`](../examples/cluster.json) to a private
   location outside the repository. Fill in the tunnel targets, the VPC service
   IDs, and each workload's zone ID and record ID.
4. Copy [`examples/worker.env`](../examples/worker.env) next to it and fill in
   the three Worker secrets. `AGENT_TOKEN` is the agents' token. Generate a
   separate `STATUS_TOKEN` with `openssl rand -hex 32`.

### Deploy from GitHub Actions

On every push to `main`, CI deploys the Worker after the checks pass, keeping
the Worker's existing secrets. Before the first CI deployment, follow
[Provision Worker secrets](#provision-worker-secrets).

Create an API token for the deploy with Workers Scripts Edit, Connectivity
Directory Read, and Connectivity Directory Bind on the account. Without
Connectivity Directory Read, the deploy fails with code 10196. Then add these repository secrets:

| Secret | Value |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | The deploy token |
| `CLOUDFLARE_ACCOUNT_ID` | The Cloudflare account ID |
| `RANGER_CLUSTER_CONFIG` | The cluster configuration, compacted to one line |

GitHub masks a multi-line secret one line at a time, so compact the JSON:

```sh
gh secret set CLOUDFLARE_API_TOKEN
gh secret set CLOUDFLARE_ACCOUNT_ID
jq -c . ~/ranger/cluster.json | gh secret set RANGER_CLUSTER_CONFIG
```

GitHub secrets cannot be read back, so keep the private files as the source.
To deploy changed configuration without a code change, rerun the latest CI run
on `main`.

CI does not check workload health. After a deployment, wait for a cycle and
read the status. `SUBDOMAIN` is your account's workers.dev subdomain:

```sh
curl -H "Authorization: Bearer $STATUS_TOKEN" https://ranger.SUBDOMAIN.workers.dev/status
```

HTTP 200 means a recent cycle finished, not that every workload is healthy.
For a first enrollment, expect both nodes `healthy`, each workload's `current`
set to its preferred node, and `error` and `transition` set to `null`.

### Provision Worker secrets

Keep `worker.env` in a private location as the source for provisioning and
recovery. Deployment fails if `DNS_API_TOKEN`, `AGENT_TOKEN`, or `STATUS_TOKEN`
is missing on the Worker, and routine deployments inherit them. A brand-new
Worker has no secrets yet, so bootstrap it once from a checkout:

```sh
uv sync --locked
npm ci
PYTHONPATH=src uv run python scripts/configure-worker.py ~/ranger/cluster.json
uv run pywrangler deploy --config wrangler.local.jsonc --secrets-file ~/ranger/worker.env
```

To rotate secrets later, update `worker.env` and run
`npx wrangler secret bulk ~/ranger/worker.env --config wrangler.local.jsonc`.
Keep `AGENT_TOKEN` synchronized with both agents.

### Deploy by hand

From a checkout with uv and Node.js 22, logged in with `wrangler login` or
with `CLOUDFLARE_API_TOKEN` set:

```sh
PYTHONPATH=src uv run python scripts/configure-worker.py ~/ranger/cluster.json
uv run pywrangler deploy --config wrangler.local.jsonc
```

The renderer writes `wrangler.local.jsonc`. It stops with an error if the
configuration is invalid or a VPC service ID is still a placeholder.

Do not rename the `Cluster` class, the `CLUSTER` binding, or the `ranger-v1`
object name. Any of those changes starts the controller with empty state.

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

The controller upgrades itself: every merge to `main` deploys it. It keeps its
state across deployments. Agents are never upgraded by CI, so upgrade them by
hand after a change to the agent or its API.

## Add a workload

To enroll another service in a running installation:

1. Follow [Prepare a workload](#prepare-a-workload) on both hosts. Its start
   command uses `--no-deps`, so dependencies must already be available on both.
2. Add the workload to both agent configurations under the same name, restart
   both agents, and check that the preferred copy reports `ready` and the
   standby `stopped`:

   ```sh
   sudo systemctl restart ranger-agent@USER
   curl -H "Authorization: Bearer $AGENT_TOKEN" \
     http://127.0.0.1:6720/workloads/example-app/health
   ```

   Restarting an agent does not restart application containers.
3. Add the workload to the private cluster file. Make sure the DNS token can
   edit the new record's zone.
4. Deploy: update `RANGER_CLUSTER_CONFIG` and rerun the latest CI run on
   `main`, or [deploy by hand](#deploy-by-hand).
5. After a cycle, check `/status`: `current` is the preferred node, and
   `transition` and `error` are `null`.

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
   recoverable, so ten minutes after DNS switched away, the controller starts
   it, switches DNS back, and stops the fallback copy.

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

## Email notifications

Ranger can email the events it logs as notable: node status changes, failover
and failback starts and completions, abandoned transitions, and new or cleared
workload errors. Each cycle's events go out as one email. If sending fails, the
lines are kept and go out with the next email. Notifications run inside the
controller, so keep an uptime monitor on `/status` for a controller that stops
running.

Ranger sends through a Cloudflare send-email binding, so it needs no SMTP
credentials. Use a sender domain and recipient that are already verified in
your Cloudflare account.

1. Add the addresses to the private cluster file:

   ```json
   "notifications": {"from": "ranger@example.com", "to": "owner@example.net"}
   ```

2. Deploy. The rendered binding can send only to `to`.
3. Send a test email:

   ```sh
   curl -X POST -H "Authorization: Bearer $STATUS_TOKEN" \
     https://ranger.SUBDOMAIN.workers.dev/test-email
   ```

   HTTP 200 means Cloudflare accepted the message. Check the inbox.

To turn notifications off, remove the `notifications` key and deploy.
