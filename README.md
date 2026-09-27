# Ranger

[![CI](https://github.com/adhirajpandey/ranger/actions/workflows/ci.yml/badge.svg)](https://github.com/adhirajpandey/ranger/actions/workflows/ci.yml)

Ranger moves a Docker Compose service to a second host when the host serving it
fails, then moves it back once the first host has been healthy for ten minutes.
It is built for self-hosted side projects that run behind Cloudflare Tunnels,
and it runs entirely on Cloudflare's free plan. Ranger calls each protected
Compose service a workload.

Each host runs its own tunnel. A workload's public hostname is a proxied DNS
CNAME that points at one of the two tunnels. Ranger fails over by starting the
workload on the other host, waiting until it answers, and changing that one DNS
record:

```text
Normal                               After failover

app.example.com                      app.example.com
  -> CNAME to tunnel A                 -> CNAME to tunnel B
  -> cloudflared on host A             -> cloudflared on host B
  -> the workload on host A            -> the workload on host B
```

The controller is a Python Cloudflare Worker with one Durable Object. It runs
outside both hosts, so it survives either of them failing. It is not in the
request path: visitors reach the tunnel directly, and the Worker only runs the
checks and a status endpoint.

## Why it exists

Two small servers at home can run the same apps, but a host failure still
means downtime until someone notices, starts the app on the other machine, and
repoints DNS. That can take hours if it happens at night. Ranger does that
routine part without a person. With the default timings, DNS moves about three
to four minutes after a workload fails.

It deliberately stays small. Ranger handles stateless workloads that are safe
to run on both hosts for a short time. Kubernetes, a load balancer, or a
replicated database would each cost more to run than the apps they protect.

## What it guarantees

- DNS changes only after the replacement is running and has passed two
  consecutive readiness checks.
- If the replacement fails to start or never becomes ready, DNS stays unchanged.
- If both hosts are unhealthy, DNS stays unchanged.
- If the DNS record points anywhere other than the two configured tunnels, or
  is not a proxied CNAME for the configured hostname, Ranger reports it and
  leaves the record alone.
- The old copy is stopped only after DNS reads back the new target, the public
  hostname returns an accepted status, and a one-minute drain has passed.
- Ranger reads and writes DNS records only by their configured zone and record
  IDs. It never searches for records by name.

It does not guarantee that only one copy runs. If the old host is unreachable
after a failover, Ranger cannot stop its copy, so both may run until the old
host returns. Ranger therefore refuses any workload that is not marked
`overlap_safe`. It does not move data, replicate databases, or preserve
in-flight requests.

## How it is built

| Path | What it is |
| --- | --- |
| `src/entry.py` | Worker and Durable Object entrypoints: cron trigger, `GET /status`, HTTP transport |
| `src/ranger/controller.py` | The reconciliation cycle, independent of the Workers runtime |
| `src/ranger/agent.py` | The node agent: an authenticated HTTP API over Docker Compose |
| `src/ranger/config.py` | Validation for the controller and agent configurations |
| `src/ranger/cloudflare.py` | Adapters for the agents, the DNS API, and the public probe |
| `scripts/configure-worker.py` | Renders the Wrangler configuration from a cluster configuration |
| `deploy/ranger-agent@.service` | systemd unit for the agent |
| `examples/` | Example cluster and agent configurations |

The agent uses only the Python standard library. It accepts start, stop, and
health requests for an allowlist of Compose services, and nothing else. The
controller reaches the agents through Workers VPC services over the existing
tunnels, so agents listen only on loopback and have no public hostname.

## Field testing

Ranger runs in production on two hosts with different CPU architectures, an
arm64 Raspberry Pi and an amd64 laptop, protecting a small stateless web app.

In a drill on 28 September 2026, the app on the preferred host was stopped at
02:41:36. With the default timings, the controller:

| Time | Step |
| --- | --- |
| 02:43:23 | Started the replacement after the second failed check |
| 02:45:23 | Switched DNS after two ready checks |
| 02:46:08 | First public request succeeded through the new host |
| 02:47:30 | Last failed request |
| 02:48:23 | Finished the transition |

Visitors saw errors for about 4.5 minutes, then a mix of successes and errors
for 1.5 minutes while Cloudflare's edges picked up the DNS change. The failback
had no failed requests, because both copies ran during the switch. Every Worker
invocation during the drill finished within the free plan's CPU limit.

Host-shutdown and network-loss drills have not run yet.

## Develop

You need [uv](https://docs.astral.sh/uv/) and Node.js 22.

```sh
uv sync --locked
npm ci
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest
```

The Docker Compose integration test is opt-in. It starts a container from
`tests/fixture/compose.yaml` on loopback port 16740 and removes it afterwards:

```sh
RUN_DOCKER_TESTS=1 uv run pytest tests/test_agent.py
```

To check that the Worker still packages, without deploying it:

```sh
uv run pywrangler deploy --dry-run
```

CI runs all of these on every pull request.

## Documentation

- [How Ranger works](docs/architecture.md) explains the design, the failover
  cycle, the timing, and the limits.
- [Operate Ranger](docs/operations.md) covers installing the agents, deploying
  the controller, running drills, and rolling back.
- [Reference](docs/reference.md) lists every configuration key, the agent API,
  the status fields, and the log events.

## Roadmap

- Notifications on failover, failback, and stalled transitions.

## License

[MIT](LICENSE)
