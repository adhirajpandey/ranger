# Reference

This page lists Ranger's configuration keys, commands, HTTP routes, status
fields, and log events. [How Ranger works](architecture.md) explains the
behaviour behind them.

## Cluster configuration

The controller reads the cluster configuration from the `CLUSTER_CONFIG`
Worker variable. `scripts/configure-worker.py` validates a cluster file and
writes it into the rendered Wrangler configuration.
[`examples/cluster.json`](../examples/cluster.json) is a complete example.
Unknown keys are rejected at every level.

### Top level

| Key | Required | Value |
| --- | --- | --- |
| `nodes` | Yes | Exactly two nodes, keyed by node name |
| `workloads` | Yes | Workloads, keyed by workload name |
| `policy` | No | Timing and threshold overrides |

Node and workload names start with a letter or digit and contain only letters,
digits, `_`, and `-`.

### Node

| Key | Required | Value |
| --- | --- | --- |
| `tunnel_target` | Yes | The node's tunnel hostname, `<tunnel-id>.cfargotunnel.com`. The two nodes must differ. |
| `agent_binding` | Yes | Name of the Workers VPC service binding for the node's agent, in uppercase, for example `NODE_A_AGENT` |
| `vpc_service_id` | No | ID of that Workers VPC service. Only the renderer reads this key. It refuses to render while the value is missing or `REPLACE_WITH_VPC_SERVICE_ID`. |

### Workload

| Key | Required | Default | Value |
| --- | --- | --- | --- |
| `preferred_node` | Yes | | The node that serves the workload when it is healthy |
| `fallback_node` | Yes | | The other node |
| `hostname` | Yes | | Public hostname of the workload |
| `zone_id` | Yes | | Cloudflare zone ID, 32 lowercase hexadecimal characters |
| `record_id` | Yes | | ID of the workload's DNS record in that zone, 32 lowercase hexadecimal characters. Each record belongs to one workload. |
| `overlap_safe` | Yes | | Must be `true`. It confirms that the workload tolerates two copies running at once. |
| `readiness_path` | No | `/` | Path for the public readiness check |
| `expected_status` | No | `[200]` | HTTP statuses that count as ready |

`readiness_path` must start with `/`. It cannot start with `//` or contain a
query, a fragment, a backslash, whitespace, or control characters.
`expected_status` is a nonempty list of integers from 200 through 599. A
redirect status counts as ready only when it is listed, because redirects are
not followed.

Use the same `readiness_path` and `expected_status` in the cluster
configuration and in both agent configurations.

### Policy

All values are positive integers, in seconds or counts.

| Key | Default | Meaning | Constraint |
| --- | --- | --- | --- |
| `check_interval` | `60` | Seconds between cycles | A whole number of minutes that divides an hour |
| `probe_timeout` | `5` | Seconds before any outgoing request times out | Less than `check_interval` |
| `failure_threshold` | `2` | Consecutive failed checks that mark a node unhealthy or start a failover | |
| `readiness_successes` | `2` | Consecutive ready checks before DNS switches | |
| `startup_timeout` | `300` | Seconds a replacement has to become ready | At least 2 × `check_interval` |
| `recovery_stability` | `600` | Seconds of health before a node or preferred copy counts as recovered. It is also the backoff after a failed replacement. | At least `check_interval` |
| `drain_seconds` | `60` | Seconds between public verification and stopping the old copy | At least `check_interval` |

The renderer turns `check_interval` into the cron schedule: `* * * * *` for 60
seconds, and `*/N * * * *` for N minutes.

## Agent configuration

The agent reads `/etc/ranger-agent/agent.json` unless `--config` names another
file. [`examples/agent.json`](../examples/agent.json) is a complete example.
Unknown keys are rejected.

| Key | Required | Value |
| --- | --- | --- |
| `node` | Yes | This node's name, matching a key in the cluster configuration's `nodes` |
| `workloads` | Yes | Workloads this agent may control, keyed by workload name |

Each workload has these keys:

| Key | Required | Default | Value |
| --- | --- | --- | --- |
| `compose_file` | Yes | | Absolute path to the Compose file |
| `env_file` | No | | Absolute path to an env file passed to Compose with `--env-file` |
| `project` | Yes | | Compose project name |
| `service` | Yes | | Compose service name. The service must run exactly one container. |
| `port` | Yes | | Host port that the readiness check requests on `127.0.0.1` |
| `readiness_path` | No | `/` | Path for the local readiness check. Same rules as the cluster configuration. |
| `expected_status` | No | `[200]` | HTTP statuses that count as ready |
| `pull` | No | `missing` | Compose pull policy on start: `never`, `missing`, or `always` |

## Commands

### `ranger-agent`

```text
ranger-agent [--config PATH] [--host HOST] [--port PORT]
```

| Option | Default |
| --- | --- |
| `--config` | `/etc/ranger-agent/agent.json` |
| `--host` | `127.0.0.1` |
| `--port` | `6720` |

The `AGENT_TOKEN` environment variable holds the bearer token. It must be at
least 32 characters. The agent exits at startup if the token is missing, is
too short, or if the configuration is invalid.

The agent runs these Compose commands:

| Action | Command |
| --- | --- |
| Start | `docker compose -f FILE -p PROJECT [--env-file ENV] up -d --no-build --no-deps --pull POLICY SERVICE` |
| Stop | `docker compose -f FILE -p PROJECT [--env-file ENV] stop --timeout 10 SERVICE` |

A start or stop that is already satisfied does nothing. Compose commands time
out after 100 seconds. Docker and HTTP probes time out after 5 seconds.

### `scripts/configure-worker.py`

```text
PYTHONPATH=src uv run python scripts/configure-worker.py CLUSTER_FILE [--output PATH]
```

The script reads `wrangler.jsonc`, adds the VPC service bindings, the
`CLUSTER_CONFIG` variable, and the cron schedule, and writes the result to
`wrangler.local.jsonc` by default. Git ignores that file. The script does not
call Cloudflare.

## Agent API

Every route requires `Authorization: Bearer AGENT_TOKEN`. Responses are JSON.

| Route | Success |
| --- | --- |
| `GET /health` | 200 with `node` and `docker`, which is `true` when the Docker daemon answers |
| `GET /workloads/NAME/health` | 200 with the workload observation |
| `POST /workloads/NAME/start` | 202. The start runs in the background. |
| `POST /workloads/NAME/stop` | 202. The stop runs in the background. |

| Status | When |
| --- | --- |
| 401 | The token is missing or wrong |
| 404 | The route or workload name is unknown |
| 405 | The method does not match the route |
| 409 | A different action for the same workload is still running. Repeating the running action returns 202. |

### Workload observation

| Field | Value |
| --- | --- |
| `node` | The agent's node name |
| `state` | `stopped`, `unready`, `ready`, or `error` |
| `http_status` | Status of the local readiness request, or `null` if none arrived |
| `docker_health` | Docker health status, or `none` without a health check. Present when a container exists. |
| `error` | What failed. Present when `state` is `error`. |
| `operation` | `pending`, the running action or `null`, and `error`, the last action's failure or `null` |

`stopped` means no container is running. `unready` means the container runs
but the readiness check fails. `ready` means the container runs, Docker's
health is `none` or `healthy`, and the local request returned an accepted
status. `error` means Docker or the agent could not be observed.

## Worker

### Bindings, variables, and secrets

| Name | Kind | Value |
| --- | --- | --- |
| `CLUSTER` | Durable Object binding | The `Cluster` class. The controller uses the object named `ranger-v1`. |
| One per node | Workers VPC service binding | Named by the node's `agent_binding` |
| `CLUSTER_CONFIG` | Variable | The validated cluster configuration, written by the renderer |
| `DNS_API_TOKEN` | Secret | Cloudflare API token with DNS Read and DNS Edit on the workloads' zones |
| `AGENT_TOKEN` | Secret | The token shared by both agents |
| `STATUS_TOKEN` | Secret | Bearer token for `GET /status`, at least 32 characters |

### Routes

The Worker has one public route, `GET /status`, with
`Authorization: Bearer STATUS_TOKEN`.

| Status | When |
| --- | --- |
| 200 | The last cycle finished within three check intervals |
| 503 | No cycle has finished yet, or the last one is older than three check intervals |
| 401 | The token is missing or wrong |
| 404 | Any other method or path |
| 500 | `STATUS_TOKEN` is missing or shorter than 32 characters |

The cron trigger runs one cycle through an internal Durable Object route that
the public Worker does not forward.

### Cloudflare requests

| Request | Details |
| --- | --- |
| Read a DNS record | `GET /client/v4/zones/ZONE_ID/dns_records/RECORD_ID` |
| Switch a DNS record | `PATCH` on the same URL with `{"content": TUNNEL_TARGET}` |
| Public readiness check | `GET https://HOSTNAME` followed by `READINESS_PATH?failover_probe=NANOSECONDS`, with `Cache-Control: no-cache, no-store`. The body is not read. |

No request follows redirects. The controller acts on a record only when it is
a proxied CNAME named `hostname` whose content is one of the two tunnel
targets.

## Status fields

`GET /status` returns the saved controller state. Times are Unix seconds.

| Field | Value |
| --- | --- |
| `last_cycle` | Scheduled time of the last finished cycle |
| `last_successful_cycle` | Last cycle in which no workload reported an error |
| `nodes` | Node state, keyed by node name |
| `workloads` | Workload state, keyed by workload name |

Each node has these fields:

| Field | Value |
| --- | --- |
| `status` | `unknown`, `healthy`, `unhealthy`, or `recovering` |
| `available` | Whether the latest health check passed |
| `failures` | Consecutive failed health checks |
| `healthy_since` | Start of the current healthy period, or `null` |
| `observation` | The agent's latest `GET /health` response, or an `error` |

Each workload has these fields:

| Field | Value |
| --- | --- |
| `current` | Node that DNS points at |
| `desired` | Node the controller is moving the workload to, or `current` |
| `dns_target` | Content of the DNS record |
| `observations` | Latest observation from each node's agent |
| `failures` | Consecutive failed checks, by node |
| `preferred_since` | Start of the preferred copy's current stable period, or `null` |
| `transition` | The running transition, or `null` |
| `backoff` | `until` and `reason` after a failed replacement, or `null` |
| `error` | What went wrong in the latest cycle, or `null` |

A transition has these fields:

| Field | Value |
| --- | --- |
| `source` | Node the workload is moving from |
| `target` | Node the workload is moving to |
| `phase` | `starting`, `switching`, `verifying`, or `draining` |
| `deadline` | Time by which the target must be ready |
| `ready_count` | Consecutive ready checks so far |
| `drain_until` | End of the drain, or `null` before draining |

## Log events

The Worker writes one JSON object per line, with an `event` field and the
fields below.

| Event | Fields | When |
| --- | --- | --- |
| `node_status` | `node`, `previous`, `status` | A node's status changes |
| `failover_started` | `workload`, `source`, `target` | The serving copy failed its checks |
| `failback_started` | `workload`, `source`, `target` | The preferred node has been stable long enough |
| `start_requested` | `workload`, `node` | The controller asks an agent to start a workload |
| `dns_switched` | `workload`, `source`, `target` | The DNS record points at the new target |
| `public_check_passed` | `workload`, `target` | The public hostname answered, and the drain starts |
| `stop_requested` | `workload`, `node` | The controller asks an agent to stop the old copy |
| `transition_completed` | `workload`, `source`, `target` | The old copy has stopped |
| `transition_abandoned` | `workload`, `target`, `reason` | The replacement missed its deadline or failed after the switch |
| `workload_error` | `workload`, `error` | A workload reports a new error |
| `workload_error_cleared` | `workload` | A workload's error has cleared |
| `cycle_failed` | `error` | A cycle failed outside any single workload |
| `status_token_invalid` | | `STATUS_TOKEN` is missing or too short |

The agent logs to standard error, which systemd writes to the journal. Read it
with `journalctl -u ranger-agent@USER`.
