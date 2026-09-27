# Controller and agent reference

All responses are JSON. Agents require `Authorization: Bearer TOKEN` on every
route. Both agents share one token. The Worker requires the separate status
token on `GET /status`.

## Agent routes

| Route | Response |
| --- | --- |
| `GET /health` | Node name and Docker availability |
| `GET /workloads/:name/health` | `state`, agent node, `http_status`, operation state |
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
without a query or fragment. Unknown keys are rejected. Configure the same
policy on agents and the controller.

`state` is `stopped` (no running container), `unready` (running, but readiness
fails), `ready`, or `error` (Docker or the agent could not be observed).
`http_status` is the observed status or null if no HTTP response arrived. `node`
is supplied by the agent, not the application. Application release identity is
no longer reported. Redirects are not followed, but an explicitly accepted
redirect or error status can pass readiness.

## Controller state

Status includes `nodes`, `workloads`, `last_cycle`, and
`last_successful_cycle`. The latter advances only when workloads report no error.
`GET /status` returns HTTP 503 before the first cycle and when `last_cycle` is
more than three check intervals old, so an uptime monitor can detect a stalled
controller. A status token shorter than 32 characters returns HTTP 500.

Each workload includes `current`, `desired`, `observations`, `dns_target`,
`transition`, `backoff`, and `error`. `current` is DNS-observed placement. Both
actual process observations remain visible because overlap is permitted.
`error` describes the latest cycle only and clears when its cause does.
Error messages are sanitized and never contain tokens.

Transition phases are `starting`, `switching`, `verifying`, and `draining`.
Intent is persisted before mutations. Each cycle counts at most one failed
check and one readiness success.
Failed startup backs off for the recovery stability period. `backoff` then holds
`until` and `reason`, and `error` reports the pause.

## Logs

The Worker writes one JSON line per event: `node_status`, `failover_started`,
`failback_started`, `start_requested`, `dns_switched`, `public_check_passed`,
`stop_requested`, `transition_completed`, `transition_abandoned`,
`workload_error`, `workload_error_cleared`, `cycle_failed`, and
`status_token_invalid`. A persisting workload error is logged once.

Agents log actions, failures, and rejected requests to standard error, which
systemd writes to the journal. Docker's error output stays in the journal; the
controller receives only the exit code.

An unreachable previous node leaves cleanup pending while the new copy serves.
Returning nodes do not gain traffic merely because their Docker processes start.
DNS updates use configured zone and record IDs with a PATCH of `content` only.
Unknown targets or mismatched record metadata are reported.

Public verification uses DNS readback plus an accepted HTTP status, not serving
node identity. Cache bypass must be configured for the public probe path.
Infinite-Memes probes `/`, which depends on the upstream meme API.
Failover does not preserve in-flight requests or existing connections.
