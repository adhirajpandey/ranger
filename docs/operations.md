# Prepare and operate the pilot

This procedure is for a later live rollout. Implementation validation does not
authorize running deployment steps. See [verification](verification.md) for
completed checks and pending live acceptance.

## Check the code

Run these commands from this repository:

```sh
uv sync --locked
npm ci
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
RUN_DOCKER_TESTS=1 uv run pytest tests/test_agent.py -q
uv run pywrangler deploy --dry-run
```

The Docker test owns only its generated Compose project and loopback port 16740.
It removes its containers afterward. Packaging does not deploy.
The locked development environment includes a recent `uv` for Workers tooling.

## Prepare images and hosts

1. Build Infinite-Memes from the same committed revision on both architectures.
   Set the Docker build argument `RELEASE_ID` to that revision.
2. Run `python3 scripts/smoke-image.py IMAGE` in each application checkout.
3. Preload images or make immutable registry references available to both hosts.
   Authenticate the agent's OS user to a private registry if needed.
4. Copy each Shed `failover.env.example` to `failover.env`. Set `FAILOVER_IMAGE`
   to the prebuilt image. Never add build instructions to `compose.failover.yml`.
5. Validate each standalone Compose file:

   ```sh
   docker compose -f compose.failover.yml --env-file failover.env -p infinite-memes config --quiet
   ```

6. Install this repository at `/home/adhiraj/projects/habitat-failover` on each
   host. Create `.venv` with Python 3.12 or newer. The agent has no third-party
   runtime dependencies. Do not replace the host's system Python.
7. Copy the host's Shed `agent.example.json` to
   `/etc/habitat-failover/agent.json`. Use the existing `adhiraj` Docker user.
8. Generate a separate random token of at least 32 characters for each agent.
   Put `AGENT_TOKEN=...` in `/etc/habitat-failover/agent.env`, readable only by
   root. Keep Compose files and agent configuration writable only by trusted
   operators. Docker access grants control of the host.
9. Install `deploy/habitat-failover-agent.service` in `/etc/systemd/system`.
   Run `systemctl daemon-reload` and enable and start the unit on each host.
10. Verify authenticated local `/health` and `/workloads` requests. Confirm that
    the agent listens only on loopback port 6720.

The standalone failover Compose file replaces the legacy deployment only during
enrollment. Both files use the existing `infinite-memes` project and container
name. Do not run both deployment procedures independently after enrollment.

## Prepare Cloudflare

1. Verify each existing tunnel belongs exclusively to its intended node.
   Keep cloudflared using host networking, version 2025.7.0 or later, and QUIC.
2. Add `memes.adhirajpandey.tech -> http://127.0.0.1:6704` to both tunnels as a
   static mapping. Preserve existing mappings and the final fallback.
   Avoid dashboard actions that silently change the current DNS target.
3. Bypass caching for `/healthz`. Preserve `no-store` from the origin and verify
   the public response is not a cached copy.
4. Create one HTTP VPC Service per node, targeting `127.0.0.1:6720` through its
   tunnel. Use Shed's `vpc-services.example.json` as the configuration reference.
   Verify this private route end to end before activation. Do not add public
   agent hostnames or broaden network exposure to bypass a routing failure.
5. Copy Shed's `cluster.example.json` to `cluster.local.json`. Fill both VPC
   Service IDs. Recheck the zone ID, record ID, and tunnel targets against
   Cloudflare. IDs are provisioned once, never discovered during failover.
6. Create a runtime token limited to DNS Read and DNS Edit on the single
   `adhirajpandey.tech` zone. Cloudflare does not scope this token to one record.
   The controller's configured record ID limits which record it accesses.
   Use a separate operator credential for Worker and VPC provisioning.

Workers VPC is beta. Resolve missing account permissions or unsupported private
routing before enrollment. These are not reasons to expose an agent.

## Enroll and initialize

1. Verify the new image on white-box and recreate only Infinite-Memes with
   `compose.failover.yml`. Confirm local and public `/healthz` identify
   white-box. Keep the black-box copy stopped.
2. From this repository, render bootstrap configuration:

   ```sh
   PYTHONPATH=src uv run python scripts/configure-worker.py ../shed/failover/cluster.local.json --bootstrap
   ```

3. Set Worker secrets `DNS_API_TOKEN`, `STATUS_TOKEN`, `BLACK_BOX_TOKEN`, and
   `WHITE_BOX_TOKEN` using `uv run pywrangler secret put NAME --config
   wrangler.local.jsonc`. Match agent tokens and use at least 32 random
   characters for `STATUS_TOKEN`. Never commit secrets or shell transcripts.
4. Deploy with `uv run pywrangler deploy --config wrangler.local.jsonc`.
   The temporary minute Cron invokes an internal Durable Object initialization
   path. It only schedules a missing alarm. There is no public initialization API.
5. Query authenticated `GET /status`. Wait for `last_cycle` and verify both
   observations and the white-box DNS target. Confirm private probes work.
6. Render again without `--bootstrap`, then deploy that configuration. This
   removes the temporary Cron. Subsequent reconciliation uses alarms alone.

The status endpoint never initializes or changes controller state. The temporary
bootstrap procedure can repair a missing alarm after an operational incident.
Keep the Durable Object binding, class, migration history, and singleton name
unchanged across ordinary deployments.

## Run live acceptance

1. Record the normal DNS target and both agent observations.
2. Stop only the preferred application as a controlled failure. Observe the
   threshold, replacement readiness, DNS write, and public node identity.
3. Record the time from first failed request to sustained public recovery.
   DNS API acknowledgement alone does not establish cutover time.
4. Verify the old copy stops after the 45-second drain and the fallback serves.
5. Verify failback waits for five minutes of stability before starting and
   validating the preferred copy. Confirm public traffic returns to white-box.
6. Schedule host shutdown and network-loss drills separately. Avoid stopping
   Docker or cloudflared during application-only acceptance.

Keep live results pending until observed. Do not promise an outage target before
measuring the real Cloudflare route change.

## Roll back

1. Revoke the controller's dedicated DNS token and stop both agent units to
   prevent new controller mutations. Leave the current application running.
2. Save status and determine which copies are actually healthy.
3. Manually start and verify the intended serving copy with approved Compose
   configuration. Restore its DNS target using an operator credential.
4. Verify public readiness and wait 45 seconds before stopping another copy.
5. Keep controller state for diagnosis. For permanent removal, delete the
   Worker after recording status and confirming manual service ownership.

To return to the legacy deployment, stop the enrolled service and recreate it
from Shed's original Compose file. This restores its original restart policy.
Never delete application volumes as part of rollback.
