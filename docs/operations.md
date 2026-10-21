# Operate Ranger

These guides set up Ranger on two hosts, deploy the controller, test a
failover, and remove it again. The [reference](reference.md) lists every key
and field they mention.

## Before you start

You need:

- Two Linux hosts with Docker Engine, the Compose plugin, and
  [uv](https://docs.astral.sh/uv/).
- A Cloudflare Tunnel on each host, run by `cloudflared` 2025.7.0 or later with
  host networking. Each tunnel belongs to one host only.
- A Cloudflare account, free plan or higher, with the workload's zone.
- A GitHub fork of this repository to deploy from, or a checkout with uv and
  Node.js 22 to deploy by hand.

Workers VPC is in beta. If your account cannot create VPC services, stop here.
Do not expose an agent publicly to work around it.

## Prepare a workload

Do this for each workload, on both hosts. Its start command uses `--no-deps`,
so any dependencies must already be available on both.

1. Make the same image available on both hosts, built for each host's CPU
   architecture. Ranger never builds images. Pin a tag that names one build.
2. In the workload's Compose file, the service must run exactly one container
   and publish its port on `127.0.0.1`. Its restart policy must not be
   `always`, or Docker restarts the stopped standby whenever Docker restarts.
   `unless-stopped` and `"no"` both work.
3. In both tunnels, route the hostname to `http://127.0.0.1:PORT`.
4. Make the hostname's DNS record a proxied CNAME to the preferred host's
   tunnel, `<tunnel-id>.cfargotunnel.com`, and note its zone ID and record ID.
   Some dashboard actions on tunnel routes rewrite this record, so recheck it
   after changing a route.
5. Request the readiness path through Cloudflare and read `cf-cache-status`.
   If it is not `DYNAMIC`, add a Cache Rule that bypasses the cache for the
   path, or the public check can pass on a cached response.
6. Start the workload on the preferred host only.

## Install the agent

Do this on both hosts.

1. As a user in the `docker` group, install the agent:

   ```sh
   uv tool install git+https://github.com/adhirajpandey/ranger
   ```

2. Write `/etc/ranger-agent/agent.json`, starting from
   [`examples/agent.json`](../examples/agent.json), with this host's node name.
3. Generate one token with `openssl rand -hex 32` and use it on both hosts.
   Write it to `/etc/ranger-agent/agent.env`, readable only by root, as in
   [`examples/agent.env`](../examples/agent.env).
4. Copy [`deploy/ranger-agent@.service`](../deploy/ranger-agent@.service) to
   `/etc/systemd/system/` and enable it for the user from step 1:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now ranger-agent@USER
   ```

5. Check that the agent answers. The workload reports `ready` on the preferred
   host and `stopped` on the other:

   ```sh
   curl -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:6720/workloads/example-app/health
   ```

Keep the Compose files and `/etc/ranger-agent/` writable only by trusted users.
Anyone who can change them can run containers, which is root on the host.

## Deploy the controller

1. In Cloudflare, create one Workers VPC service per host: type HTTP, host
   `127.0.0.1`, port `6720`, on the host's tunnel.
2. Create an API token with DNS Read and DNS Edit on the workloads' zone.
   Ranger touches only the configured record IDs.
3. Copy [`examples/cluster.json`](../examples/cluster.json) and
   [`examples/worker.env`](../examples/worker.env) to a private location
   outside the repository, and fill them in. `AGENT_TOKEN` is the agents'
   token. Generate a separate `STATUS_TOKEN`.
4. Bootstrap the Worker once from a checkout, logged in with `wrangler login`
   or with `CLOUDFLARE_API_TOKEN` set. This uploads the secrets:

   ```sh
   uv sync --locked
   npm ci
   PYTHONPATH=src uv run python scripts/configure-worker.py ~/ranger/cluster.json
   uv run pywrangler deploy --config wrangler.local.jsonc --secrets-file ~/ranger/worker.env
   ```

Later deployments keep the Worker's secrets, so omit `--secrets-file`. To
rotate them, update `worker.env` and run
`npx wrangler secret bulk ~/ranger/worker.env --config wrangler.local.jsonc`,
keeping `AGENT_TOKEN` in step with both agents.

Do not rename the `Cluster` class, the `CLUSTER` binding, or the `ranger-v1`
object name. Any of those starts the controller with empty state.

### Deploy from GitHub Actions

After the bootstrap, CI deploys the Worker on every push to `main` once the
checks pass. Create a deploy token with Workers Scripts Edit, Connectivity
Directory Read, and Connectivity Directory Bind on the account. Without
Connectivity Directory Read, the deploy fails with code 10196. Then set the
repository secrets, compacting the cluster file because GitHub masks
multi-line secrets one line at a time:

```sh
gh secret set CLOUDFLARE_API_TOKEN
gh secret set CLOUDFLARE_ACCOUNT_ID
jq -c . ~/ranger/cluster.json | gh secret set RANGER_CLUSTER_CONFIG
```

To deploy a configuration change without a code change, update the secret and
rerun the latest CI run on `main`.

### Check the deployment

Wait for a cycle and read the status. `SUBDOMAIN` is your workers.dev
subdomain:

```sh
curl -H "Authorization: Bearer $STATUS_TOKEN" https://ranger.SUBDOMAIN.workers.dev/status
```

HTTP 200 means a recent cycle finished, not that every workload is healthy.
Expect both nodes `healthy`, each workload's `current` on its preferred node,
and `error` and `transition` set to `null`.

## Watch the controller

- Point an uptime monitor at `GET /status` with the status token. HTTP 503
  means no cycle has finished for three minutes.
- Read the controller's events in Workers Logs in the Cloudflare dashboard.
- Read an agent's log with `journalctl -u ranger-agent@USER`.

## Add a workload

1. [Prepare the workload](#prepare-a-workload) on both hosts.
2. Add it to both agent configurations under the same name, restart both
   agents with `sudo systemctl restart ranger-agent@USER`, and check its
   health on each. Restarting an agent does not restart containers.
3. Add it to the cluster file, then deploy.
4. After a cycle, check `/status` as in
   [Check the deployment](#check-the-deployment).

## Upgrade

Every merge to `main` deploys the controller, which keeps its state. Agents
are upgraded by hand after a change to the agent:

```sh
uv tool upgrade ranger
sudo systemctl restart ranger-agent@USER
```

## Run a failover drill

Run the drill when a few minutes of downtime are acceptable.

1. Read the status and note each workload's `current` node.
2. Stop the workload on the preferred host:

   ```sh
   docker compose -f /srv/example-app/compose.yaml -p example-app stop
   ```

3. Watch the events. Expect `failover_started` one to two minutes later, then
   `start_requested`, `dns_switched`, `public_check_passed`, `stop_requested`,
   and `transition_completed`.
4. Request the public hostname every few seconds throughout. The first failed
   request and the start of sustained success give the outage length.
5. Leave the preferred copy stopped. Ten minutes after DNS switched away, the
   controller starts it, switches back, and stops the fallback copy.

Test a host shutdown and a network loss as separate drills.

## Roll back

To take a workload out of Ranger's control:

1. Stop both agents with `sudo systemctl disable --now ranger-agent@USER`.
2. Read the last status to see which copies run and where DNS points.
3. Keep one copy running. If DNS does not point at its host's tunnel, change
   the record in the dashboard and check the public hostname.
4. Wait one minute, then stop the other copy, and restore the restart policy if
   you changed it.

To remove Ranger entirely, save the last status, then delete the Worker, the
VPC services, and the DNS API token.

## Email notifications

Ranger can email the events marked ✉ in the
[log events](reference.md#log-events). Each cycle's events go out as one
plain-text email with a summary, timestamps, and a JSON debug payload. If
sending fails, the events go out with the next email. Notifications run inside
the controller, so keep an uptime monitor on `/status` as well.

Ranger sends through a Cloudflare send-email binding, so it needs no SMTP
credentials. Use a sender domain and recipient already verified in your
Cloudflare account.

1. Add the addresses to the cluster file:

   ```json
   "notifications": {"from": "ranger@example.com", "to": "owner@example.net"}
   ```

2. Deploy. The binding can send only to `to`.
3. Send a test email, then check the inbox:

   ```sh
   curl -X POST -H "Authorization: Bearer $STATUS_TOKEN" \
     https://ranger.SUBDOMAIN.workers.dev/test-email
   ```

To turn notifications off, remove the key and deploy.
