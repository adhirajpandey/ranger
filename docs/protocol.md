# Controller and agent reference

All responses are JSON. Agents require `Authorization: Bearer TOKEN` on every
route. The Worker requires the separate status token on `GET /status`.

## Agent routes

| Route | Response |
| --- | --- |
| `GET /health` | Node name, Docker availability, configuration revision |
| `GET /workloads` | Configured workload names mapped to observations |
| `GET /workloads/:name/health` | Running state, readiness, node, release, operation state |
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
It requires HTTP 200, `status: ok`, the expected node, a running container, and
healthy Docker health status when a Docker healthcheck exists.

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

Release identity is informational. Readiness excludes the upstream meme API.
Failover does not preserve in-flight requests or existing connections.
