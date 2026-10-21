# Reference

This page lists Ranger's configuration keys, HTTP routes, status fields, and
log events. [How Ranger works](architecture.md) explains the behaviour behind
them. Unknown configuration keys are rejected at every level, so a typo fails
validation instead of being ignored.

## Cluster configuration

The renderer, `scripts/configure-worker.py`, validates a cluster file and
writes it into `wrangler.local.jsonc` as the `CLUSTER_CONFIG` variable, along
with the VPC service bindings and, when configured, the email binding.
[`examples/cluster.json`](../examples/cluster.json) is a complete example.

| Key | Required | Value |
| --- | --- | --- |
| `nodes` | Yes | Exactly two nodes, keyed by node name |
| `workloads` | Yes | Workloads, keyed by workload name |
| `policy` | No | Timing and threshold overrides |
| `notifications` | No | `from` and `to` email addresses. See [Email notifications](operations.md#email-notifications). |

Node and workload names start with a letter or digit and contain only letters,
digits, `_`, and `-`.

### Node

| Key | Value |
| --- | --- |
| `tunnel_target` | The node's tunnel hostname, `<tunnel-id>.cfargotunnel.com`. The two nodes must differ. |
| `agent_binding` | Binding name for the node's Workers VPC service, for example `NODE_A_AGENT` |
| `vpc_service_id` | ID of that Workers VPC service |

### Workload

| Key | Required | Default | Value |
| --- | --- | --- | --- |
| `preferred_node` | Yes | | The node that serves the workload when it is healthy. The other node is the fallback. |
| `hostname` | Yes | | Public hostname of the workload |
| `zone_id` | Yes | | Cloudflare zone ID |
| `record_id` | Yes | | ID of the hostname's DNS record. Each record belongs to one workload. |
| `readiness_path` | No | `/` | Path for the public readiness check, without spaces, a query, or a fragment |
| `expected_status` | No | `[200]` | HTTP statuses that count as ready. Redirects are not followed. |

Use the same `readiness_path` and `expected_status` in the cluster
configuration and in both agent configurations.

### Policy

All values are positive integers.

| Key | Default | Meaning |
| --- | --- | --- |
| `probe_timeout` | `5` | Seconds before any outgoing request times out |
| `failure_threshold` | `2` | Consecutive failed checks that mark a node unhealthy or start a failover |
| `readiness_successes` | `2` | Consecutive ready checks before DNS switches |
| `startup_timeout` | `300` | Seconds a replacement has to become ready. At least 60 × `readiness_successes`. |
| `recovery_stability` | `600` | Seconds of health before a node or preferred copy counts as recovered, and the backoff after a failed replacement |
| `drain_seconds` | `60` | Seconds between public verification and stopping the old copy |

Cycles run once a minute from the cron trigger in `wrangler.jsonc`.

## Agent configuration

The agent reads `/etc/ranger-agent/agent.json` unless `--config` names another
file. [`examples/agent.json`](../examples/agent.json) is a complete example.

| Key | Value |
| --- | --- |
| `node` | This node's name, matching a key in the cluster configuration's `nodes` |
| `workloads` | Workloads this agent may control, keyed by workload name |

| Workload key | Required | Default | Value |
| --- | --- | --- | --- |
| `compose_file` | Yes | | Absolute path to the Compose file |
| `project` | Yes | | Compose project name |
| `service` | Yes | | Compose service name. It must run exactly one container. |
| `port` | Yes | | Host port that the readiness check requests on `127.0.0.1` |
| `readiness_path` | No | `/` | Path for the local readiness check |
| `expected_status` | No | `[200]` | HTTP statuses that count as ready |
| `pull` | No | `missing` | Compose pull policy on start: `never`, `missing`, or `always` |

## Agent

```text
ranger-agent [--config PATH]
```

The agent listens on `127.0.0.1:6720`. `AGENT_TOKEN` in the
environment holds the bearer token. The agent exits at startup if the token is
missing or shorter than 32 characters, or if the configuration is invalid.

It runs these Compose commands, with a 100-second timeout. A start or stop
that is already satisfied does nothing.

| Action | Command |
| --- | --- |
| Start | `docker compose -f FILE -p PROJECT up -d --no-build --no-deps --pull POLICY SERVICE` |
| Stop | `docker compose -f FILE -p PROJECT stop --timeout 10 SERVICE` |

Every route requires `Authorization: Bearer AGENT_TOKEN` and answers JSON.

| Route | Response |
| --- | --- |
| `GET /health` | `node`, and `docker`, which is `true` when the Docker daemon answers |
| `GET /workloads/NAME/health` | The workload observation |
| `POST /workloads/NAME/start` | 202. The start runs in the background. |
| `POST /workloads/NAME/stop` | 202. The stop runs in the background. |

A wrong token gets 401, an unknown route or workload 404, and the wrong method
405. A start or stop while the other action is still running gets 409.

A workload observation has a `state` and supporting fields:

| `state` | Meaning |
| --- | --- |
| `stopped` | No container is running |
| `unready` | The container runs, but Docker's health check or the local HTTP request fails |
| `ready` | The container runs, Docker's health is `none` or `healthy`, and the local request returned an accepted status |
| `error` | Docker could not be observed. `error` says why. |

`http_status` is the local response status, `docker_health` the Docker health
status, and `operation` holds the `pending` action and the last action's
`error`.

## Worker

| Name | Kind | Value |
| --- | --- | --- |
| `CLUSTER` | Durable Object binding | The `Cluster` class. The controller uses the object named `ranger-v1`. |
| One per node | Workers VPC service binding | Named by the node's `agent_binding` |
| `CLUSTER_CONFIG` | Variable | The validated cluster configuration |
| `DNS_API_TOKEN` | Secret | Cloudflare API token with DNS Read and DNS Edit on the workloads' zones |
| `AGENT_TOKEN` | Secret | The token shared by both agents |
| `STATUS_TOKEN` | Secret | Bearer token for `GET /status` |
| `EMAIL` | Optional send-email binding | Restricted to the `notifications.to` address |

`GET /status` requires `Authorization: Bearer STATUS_TOKEN` and returns the
controller state. It answers 200 when a cycle finished in the last three
minutes, and 503 otherwise. A wrong token gets 401, and any other route 404.

The controller reads and patches DNS records only at
`/client/v4/zones/ZONE_ID/dns_records/RECORD_ID`, and acts on a record only
when it is a proxied CNAME named `hostname` whose content is one of the two
tunnel targets. The public readiness check requests
`https://HOSTNAME/READINESS_PATH?failover_probe=NANOSECONDS` without reading
the body. No request follows redirects.

## Status fields

Times are Unix seconds. `last_cycle` is the scheduled time of the last
finished cycle.

Each entry in `nodes` has a `status` (`unknown`, `healthy`, `unhealthy`, or
`recovering`), `available` for the latest check, `failures` in a row,
`healthy_since`, and the agent's latest `observation`.

Each entry in `workloads` has:

| Field | Value |
| --- | --- |
| `current` | Node that DNS points at |
| `dns_target` | Content of the DNS record |
| `observations` | Latest observation from each node's agent |
| `failures` | Consecutive failed checks, by node |
| `preferred_since` | Start of the preferred copy's stable period while traffic is elsewhere, or `null` |
| `transition` | The running transition, or `null`: `source`, `target`, `phase`, `deadline`, `ready_count`, and `drain_until` |
| `backoff` | `until` and `reason` after a failed replacement, or `null` |
| `error` | What went wrong in the latest cycle, or `null` |

## Log events

The Worker writes one JSON object per line, with an `event` field and the
fields below. Events marked with ✉ are also emailed when notifications are
configured.

| Event | Fields | When |
| --- | --- | --- |
| `node_status` ✉ | `node`, `previous`, `status` | A node's status changes |
| `failover_started` ✉ | `workload`, `source`, `target` | The serving copy failed its checks |
| `failback_started` ✉ | `workload`, `source`, `target` | The preferred node has been stable long enough |
| `start_requested` | `workload`, `node` | The controller asks an agent to start a workload |
| `dns_switched` | `workload`, `source`, `target` | The DNS record points at the new target |
| `public_check_passed` | `workload`, `target` | The public hostname answered, and the drain starts |
| `stop_requested` | `workload`, `node` | The controller asks an agent to stop the old copy |
| `transition_completed` ✉ | `workload`, `source`, `target` | The old copy has stopped |
| `transition_abandoned` ✉ | `workload`, `target`, `reason` | The replacement missed its deadline or failed after the switch |
| `workload_error` ✉ | `workload`, `error` | A workload reports a new error |
| `workload_error_cleared` ✉ | `workload` | A workload's error has cleared |
| `cycle_failed` | `error` | A cycle failed outside any single workload |
| `notification_sent`, `notification_failed` | `error` on failure | Result of emailing a cycle's events |

The agent logs to standard error, which systemd writes to the journal.
