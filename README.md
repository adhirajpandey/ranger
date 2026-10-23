# Ranger

[![CI](https://github.com/adhirajpandey/ranger/actions/workflows/ci.yml/badge.svg)](https://github.com/adhirajpandey/ranger/actions/workflows/ci.yml)

Ranger moves a Docker Compose service to a second host when the host serving it
fails, then moves it back once the first host has been healthy for ten minutes.
It is built for self-hosted side projects behind Cloudflare Tunnels, and it
runs on Cloudflare's free plan. Ranger calls each protected service a workload.

Each host runs its own tunnel, and a workload's public hostname is a proxied
CNAME to one of them. Ranger fails over by starting the workload on the other
host, waiting until it answers, and changing that one DNS record:

```text
Normal                               After failover

app.example.com                      app.example.com
  -> CNAME to tunnel A                 -> CNAME to tunnel B
  -> cloudflared on host A             -> cloudflared on host B
  -> the workload on host A            -> the workload on host B
```

Ranger handles stateless workloads that are safe to run on both hosts for a
short time. Kubernetes, a load balancer, or a replicated database would each
cost more to run than the apps they protect.

## How it works

The controller is a Python Cloudflare Worker with one Durable Object, which
stores all state. It runs off both hosts, so it survives either failing, and it
is not in the request path. A cron trigger runs one cycle every five minutes.

Each host runs an agent: a small HTTP server, using only the Python standard
library, that reports health and starts or stops the Compose services in its
local allowlist. It accepts no files, images, or arguments from the
controller. The controller reaches it through a Workers VPC service over the
host's existing tunnel, so the agent listens only on loopback.

A workload is ready when its container runs, its Docker health check (if any)
is healthy, and a local request to its readiness path returns an accepted
status. Redirects are not followed and the body is not read, so an existing
page such as `/` or `/healthz` works.

Each cycle, the controller reads both agents and every DNS record
concurrently, then:

1. Marks a host `unhealthy` after two failed checks in a row. A host that comes
   back is `recovering` until it has been healthy for ten minutes.
2. Starts a failover after two failed checks in a row on the serving host, or a
   failback once the preferred copy has been stable for ten minutes while
   traffic was elsewhere.
3. Advances the transition through three phases:
   - **starting**: start the workload on the target. Once it is ready, point
     DNS at the target's tunnel and read it back. After fifteen minutes
     without a ready check, give up and leave DNS alone.
   - **verifying**: confirm DNS still points at the target and the public
     hostname returns an accepted status. If the target fails two checks in
     a row, give up and keep both copies.
   - **draining**: one cycle later, stop the old copy once its host is
     reachable.

DNS moves 10 to 15 minutes after a failure. State is saved before every start
or stop, so an interrupted cycle resumes where it left off.

## What it guarantees

- DNS changes only after the replacement is running and ready.
- If the replacement never becomes ready, or both hosts are unhealthy, DNS
  stays unchanged.
- If the DNS record is not a proxied CNAME for the hostname that points at one
  of the two tunnels, Ranger reports it and leaves it alone.
- The old copy is stopped only after DNS reads back the new target, the public
  hostname answers, and one more cycle has passed.
- Ranger reads and writes DNS records only by their configured zone and record
  IDs.

It does not guarantee that only one copy runs: if the old host is unreachable,
both may run until it returns. It does not move data, preserve in-flight
requests, or prove which host answered the public check, so Cloudflare must not
cache the readiness path. It supports exactly two hosts, and Workers VPC is in
beta.

A free-plan invocation can make 50 outgoing requests. A cycle makes 2, plus 3
per workload, plus up to 4 per workload that switches in that cycle.

Ranger runs in production on an arm64 Raspberry Pi and an amd64 laptop. App-stop
drills have failed over and back as expected; host-shutdown and network-loss
drills have not run yet.

## Layout

| Path | What it is |
| --- | --- |
| `src/entry.py` | Worker and Durable Object entrypoints: cron trigger, status, HTTP transport |
| `src/ranger/controller.py` | The reconciliation cycle and email notifications |
| `src/ranger/agent.py` | The node agent |
| `src/ranger/config.py` | Configuration validation |
| `src/ranger/cloudflare.py` | Adapters for the agents, the DNS API, and the public probe |
| `scripts/configure-worker.py` | Renders the Wrangler configuration from a cluster configuration |
| `deploy/ranger-agent@.service` | systemd unit for the agent |

## Develop

You need [uv](https://docs.astral.sh/uv/) and Node.js 22.

```sh
uv sync --locked
npm ci
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest
RUN_DOCKER_TESTS=1 uv run pytest tests/test_agent.py  # starts a container on port 16740
uv run pywrangler deploy --dry-run
```

CI runs all of these on every pull request, and deploys the Worker on a push to
`main`.

## Documentation

- [Operate Ranger](docs/operations.md): install, deploy, add workloads, drills,
  notifications, and rollback.
- [Reference](docs/reference.md): configuration keys, the agent API, status
  fields, and log events.

## License

[MIT](LICENSE)
