# Reference

Ranger's configuration keys, HTTP routes, status fields, and log events.
Unknown configuration keys are rejected at every level, so a typo fails
validation instead of being ignored.

## Cluster configuration

`scripts/configure-worker.py` validates a cluster file and writes it into
`wrangler.local.jsonc` as the `CLUSTER_CONFIG` variable, along with the VPC
service bindings and, when configured, the email binding.
[`examples/cluster.json`](../examples/cluster.json) is a complete example.

| Key | Required | Value |
| --- | --- | --- |
| `nodes` | Yes | Exactly two nodes, keyed by node name |
| `workloads` | Yes | Workloads, keyed by workload name |
| `notifications` | No | `from` and `to` email addresses. See [Email notifications](operations.md#email-notifications). |

| Node key | Value |
| --- | --- |
| `tunnel_target` | The node's tunnel hostname, `<tunnel-id>.cfargotunnel.com`. The two nodes must differ. |
| `agent_binding` | Binding name for the node's Workers VPC service, for example `NODE_A_AGENT` |
| `vpc_service_id` | ID of that Workers VPC service |

| Workload key | Default | Value |
| --- | --- | --- |
| `preferred_node` | Required | The node that serves the workload when it is healthy |
| `hostname` | Required | Public hostname of the workload |
| `zone_id` | Required | Cloudflare zone ID |
| `record_id` | Required | ID of the hostname's DNS record. Each record belongs to one workload. |
| `readiness_path` | `/` | Path for the public readiness check, without a query |
| `expected_status` | `[200]` | HTTP statuses that count as ready. Redirects are not followed. |

Use the same `readiness_path` and `expected_status` in the cluster
configuration and in both agent configurations.

Failure thresholds, deadlines, and timeouts are constants at the top of
`src/ranger/controller.py` and `src/entry.py`. The cycle length,
`CYCLE_SECONDS`, must change together with the cron schedule in
`wrangler.jsonc`.

## Agent configuration

The agent reads `/etc/ranger-agent/agent.json` unless `--config` names another
file. [`examples/agent.json`](../examples/agent.json) is a complete example.
`node` names this node, and `workloads` lists the workloads it may control:

| Workload key | Default | Value |
| --- | --- | --- |
| `compose_file` | Required | Absolute path to the Compose file |
| `project` | Required | Compose project name |
| `service` | Required | Compose service name. It must run exactly one container. |
| `port` | Required | Host port that the readiness check requests on `127.0.0.1` |
| `readiness_path` | `/` | Path for the local readiness check |
| `expected_status` | `[200]` | HTTP statuses that count as ready |
| `pull` | `missing` | Compose pull policy on start: `never`, `missing`, or `always` |

## Agent API

The agent listens on `127.0.0.1:6720` and reads its bearer token from
`AGENT_TOKEN`. It exits at startup if the token is missing or shorter than 32
characters, or if the configuration is invalid.

It starts a workload with `docker compose -f FILE -p PROJECT up -d --no-build
--no-deps --pull POLICY SERVICE` and stops it with `docker compose -f FILE -p
PROJECT stop --timeout 10 SERVICE`, each with a 100-second timeout. A start or
stop that is already satisfied does nothing.

Every route requires `Authorization: Bearer AGENT_TOKEN` and answers JSON.

| Route | Response |
| --- | --- |
| `GET /health` | `node`, and `docker`, which is `true` when the Docker daemon answers |
| `GET /workloads/NAME/health` | The workload observation |
| `POST /workloads/NAME/start` | 202. The start runs in the background. |
| `POST /workloads/NAME/stop` | 202. The stop runs in the background. |

A wrong token gets 401, an unknown route or workload 404, and the wrong method
405. A start or stop while the other action is still running gets 409.

An observation's `state` is `stopped`, `unready` (running, but Docker's health
check or the local request fails), `ready`, or `error` (Docker could not be
observed, with `error` saying why). It also has `http_status`, `docker_health`,
and `operation`, which holds the `pending` action and the last action's `error`.

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
controller state. It answers 200 when a cycle finished in the last fifteen
minutes, and 503 otherwise, so an ordinary uptime monitor can watch the
controller.

The public readiness check requests
`https://HOSTNAME/READINESS_PATH?failover_probe=NANOSECONDS` without reading
the body or following redirects.

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
| `transition` | `source`, `target`, `phase`, and `deadline` of the running transition, or `null` |
| `error` | What went wrong in the latest cycle, or `null` |

## Log events

The Worker writes one JSON object per line, with an `event` field and the
fields below. Events marked ✉ are also emailed when notifications are
configured. The agents log to the systemd journal, which also keeps Docker's
error output, because it can contain registry credentials.

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
