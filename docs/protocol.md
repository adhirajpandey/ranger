# Controller and agent reference

All responses are JSON. Agents require `Authorization: Bearer TOKEN` on every
route. The Worker requires the separate status token on `GET /status`.

## Agent routes

| Route | Response |
| --- | --- |
| `GET /health` | Node name, Docker availability, configuration revision |
| `GET /workloads` | Configured workload names mapped to observations |
| `GET /workloads/:name/health` | Running state, readiness, agent node, http_status, operation state |
| `POST /workloads/:name/start` | HTTP 202 while the idempotent start operation runs |
| `POST /workloads/:name/stop` | HTTP 202 while the idempotent stop operation runs |

Unknown workloads return 404. Missing or invalid authentication returns 401.
A conflicting in-flight operation returns 409. Repeating the same in-flight
operation returns 202. Calls accept no deployment settings or shell arguments.

The observation's `operation` contains `pending` and `error`. These fields are
in memory only. Docker state remains authoritative after an agent restart.
HTTP 202 acknowledges work, not successful completion. The controller polls
observations to verify the result.

The agent owns one Compose service container per workload. It runs `up -d
--no-build --no-deps` with the configured pull policy. Supporting services must
already be independently available. It stops containers without deleting data.

Agent configuration permits an optional absolute `env_file` for Compose
interpolation. Readiness always targets loopback at the configured host port.
It requires an accepted HTTP status, a running container, and healthy Docker
health status when a Docker healthcheck exists. No application body is parsed.
`readiness_path` defaults to `/`; `expected_status` defaults to `[200]` and accepts
a nonempty list of integer statuses from 200 through 599. The path must be local,
without a query or fragment. Legacy `health_path` is rejected with a migration
message. Configure the same policy on agents and the controller.

`http_status` is the observed status or null if no HTTP response arrived. `node`
is supplied by the agent, not the application. Application release identity is
no longer reported. Redirects are not followed, but an explicitly accepted
redirect or error status can pass readiness.

## Controller state

Status includes `nodes`, `workloads`, `next_check`, `last_cycle`, and
`last_successful_cycle`. The latter advances only when workloads report no error.
Each workload includes `current`, `desired`, `observations`, `dns_target`,
`transition`, and `error`. `current` is DNS-observed placement. Both actual
process observations remain visible because overlap is permitted.

Transition phases are `starting`, `switching`, `verifying`, and `draining`.
Intent is persisted before mutations. Readiness successes are separated in time.
Failed checks count at the normal sample interval, not once per alarm retry.
Failed startup backs off for the recovery stability period.

An unreachable previous node leaves cleanup pending while the new copy serves.
Returning nodes do not gain traffic merely because their Docker processes start.
DNS updates use configured zone and record IDs with a PATCH of `content` only.
Unknown targets or mismatched record metadata are reported.

Public verification uses DNS readback plus an accepted HTTP status, not serving
node identity. Cache bypass must be configured for the public probe path.
Infinite-Memes probes `/`, which depends on the upstream meme API.
Failover does not preserve in-flight requests or existing connections.
