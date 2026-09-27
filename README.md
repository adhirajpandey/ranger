# Ranger

Ranger moves selected Docker Compose workloads between two hosts when
the host currently serving them stops being healthy.

The two hosts are:

- `black-box`, an arm64 machine.
- `white-box`, an amd64 machine.

Each host already has Docker and its own Cloudflare Tunnel. A workload normally
runs on its preferred host. If that host fails, the controller starts the same
workload on the other host, waits for it to answer, and changes the workload's
Cloudflare DNS record to the other tunnel.

This is active/passive failover for small, stateless services. It is not a
general workload migration system and it does not put a Worker in the request
path.

## Why we are building it

Today, a host failure makes the services on that host unavailable until someone
starts them elsewhere and changes their traffic route. The two hosts are
already available, but there is no small controller that can make that change
without manual work.

The system should handle the routine part of a host failure while leaving data
and application ownership with the existing repositories. The first version
therefore accepts only workloads that can run on either host and do not depend
on local mutable data.

## The first workload

The pilot is Infinite-Memes:

| Setting | Value |
| --- | --- |
| Preferred host | `white-box` |
| Fallback host | `black-box` |
| Hostname | `memes.adhirajpandey.tech` |
| Port | `6704` |
| Readiness path | `/` |
| Accepted HTTP statuses | `[200]` |

The agent probes the existing homepage. No dedicated application endpoint or
JSON response is required. The homepage calls the external meme API, so an
upstream outage can fail readiness on both hosts. The agent timeout bounds its
wait, but does not cancel work inside the application. Configure a more reliable
existing URL later if needed.

Every enrolled workload declares the same kind of information:

```yaml
name: example-app
preferred_node: black-box
fallback_node: white-box
hostname: example.example.com
port: 3000
readiness_path: /
expected_status: [200]
overlap_safe: true
zone_id: <cloudflare-zone-id>
record_id: <cloudflare-dns-record-id>
```

`readiness_path` defaults to `/` and `expected_status` to `[200]`. Both the agent
and controller must use the same policy. Existing endpoints such as `/healthz`
can be configured without requiring a particular response body. Redirects are
not followed; their status is accepted only when explicitly configured.
Unknown configuration keys are rejected.

The record ID is part of the configuration. The controller uses that ID for
every DNS read and update. It never searches for a record by hostname during a
failover.

## How traffic moves

Each tunnel has a static route for every enrolled hostname. The controller does
not rewrite tunnel configuration.

```text
Normal:
  app.example.com
    -> DNS CNAME for tunnel-black
    -> Cloudflare Tunnel on black-box
    -> local workload

Failover:
  app.example.com
    -> DNS CNAME for tunnel-white
    -> Cloudflare Tunnel on white-box
    -> local workload
```

The public request goes directly through Cloudflare to the selected tunnel. The
Worker only runs the controller and its status endpoint. It does not proxy
application requests or decide where each request goes.

## The controller

The controller is a Python Cloudflare Worker with one named Durable Object. The
Durable Object stores the state needed to recover after a controller restart:

- node health and consecutive failure counts;
- preferred-node recovery time;
- desired and observed workload placement;
- the current transition phase;
- DNS target, last completed cycle, and errors.

A Worker cron trigger runs one reconciliation cycle every minute. A node is
marked unhealthy after two consecutive failed checks. The interval, timeout,
failure count, recovery period, and drain period are configuration values. The
cron schedule is generated from the interval.

A Durable Object must run at most one reconciliation cycle at a time. Each cycle
observes and continues persisted transition state instead of starting a second
transition. The public controller API initially contains only an
authenticated, read-only `GET /status` endpoint.

The Cloudflare API token is stored as a Worker secret. It has only DNS read and
DNS edit permissions for the target zone. It is never committed to this
repository.

## The node agents

Each host runs a small Python agent as a systemd service. The agent talks to the
local Docker Compose installation and accepts only workload names from its local
allowlist.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Report agent identity and Docker availability |
| `GET` | `/workloads/:name/health` | Report container and HTTP readiness |
| `POST` | `/workloads/:name/start` | Start an approved Compose service |
| `POST` | `/workloads/:name/stop` | Stop an approved Compose service |

Start and stop are idempotent. The agent serializes operations for one
workload, bounds the Compose command, and checks Docker state afterward. It has
no operation IDs, workload generations, or persistent operation journal. Docker
state remains the source of truth if the agent restarts.

The controller reaches agents through authenticated Workers VPC Service
bindings over the existing tunnels. Agent management ports bind to loopback on
the hosts. No public agent hostname is created.

## Failover

With the workload serving from its current host, the controller does this:

1. Count failed node or workload checks until the failure threshold is reached.
2. Confirm that the other node and Docker are healthy.
3. Ask the other node's agent to start the approved workload.
4. Wait for the container and its readiness endpoint to pass two consecutive
   checks.
5. Update the configured Cloudflare DNS record by record ID.
6. Read the record back and probe the public hostname. The response must have
   an accepted HTTP status. This verifies availability, not which host answered.
7. Keep the old copy for a one-minute drain period, then stop it when the old
   node is reachable.

The controller never changes DNS before the replacement is running and ready.
If replacement startup or readiness fails, DNS stays unchanged. If both nodes
are unhealthy, DNS stays unchanged. If the DNS record already points somewhere
outside the two configured tunnels, the controller reports the conflict and
leaves it alone.

If the old node cannot be reached after a successful cutover, the new copy keeps
serving and cleanup remains pending. This is why v1 allows temporary overlap.
The system cannot prove that an unreachable host stopped running its old copy.

## Failback

When the preferred host returns, the controller marks it recovering first. It
must remain reachable and healthy for ten continuous minutes. The controller
then starts and checks the preferred copy, changes the DNS record back, verifies
public traffic, waits for the same drain period, and stops the fallback copy.

A brief recovery does not trigger failback. A failed recovery attempt leaves the
working fallback route in place.

## Images and deployment

The two hosts use different CPU architectures. An enrolled workload must have
compatible arm64 and amd64 images built before a failover occurs.

The replacement host may use a preloaded image or pull an immutable image from
an available registry. It must never build the image during failover. The
controller reports a start failure and leaves DNS unchanged if the image cannot
be obtained.

The application repository owns application code, health endpoints, and image
definitions. Shed owns the host-specific Compose files and tunnel deployment.
This repository owns the Worker, Durable Object, agents, protocol, tests, and
rollout notes.

## What v1 does not cover

The first version does not solve:

- Postgres replication or database failover;
- SQLite or shared filesystem synchronization;
- Immich or media-library replication;
- Kubernetes, Nomad, Docker Swarm, or HAProxy;
- floating IPs or multi-node consensus;
- resource-based scheduling or active-active traffic;
- arbitrary migration of workloads with local state;
- strict single-owner guarantees during a network partition.

An application that must never run twice needs a fencing mechanism. This design
does not have one, so such an application cannot be enrolled in v1.

## What is implemented

The repository contains the controller, agent, Cloudflare adapter, systemd unit,
configuration examples, and rollout documentation. Infinite-Memes has an HTTP
image smoke check. Shed contains the two host deployment examples and the
observed Cloudflare zone and record IDs.

The local checks cover agent convergence and authentication, failover readiness
and DNS safety, both nodes being unhealthy, delayed failback, and controller
restart during a transition. They include a disposable Compose test. The
verification record distinguishes the current homepage image smoke check from
historical arm64 and amd64 checks of the former health endpoint.

Public probe paths require a Cloudflare cache bypass. Request no-cache headers
and unique query strings alone do not guarantee an origin response.

The previous deployment completed an application-only failover drill on
24 September 2026. It was retired on 26 September during the Ranger rename.
Ranger is currently undeployed. Infinite-Memes runs independently on white-box.
See the [retirement record](docs/rename-2026-09-26.md),
[rollout procedure](docs/operations.md), and
[historical verification record](docs/verification.md).
