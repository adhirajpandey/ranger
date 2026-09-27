# How Ranger works

This page explains why Ranger is shaped the way it is: where the controller
runs, how one cycle decides what to do, how the timing was chosen, and what the
design gives up. For configuration keys and API fields, see the
[reference](reference.md).

## Move the DNS record, not the traffic

Each host already runs its own Cloudflare Tunnel, and both tunnels have a
static route for every enrolled hostname. A workload's public hostname is a
proxied CNAME to one tunnel. Failing over is a single DNS change: point the
CNAME at the other tunnel.

Ranger never rewrites tunnel configuration and never proxies requests. That
keeps the request path exactly what it would be without Ranger, and it keeps
the controller small. A Cloudflare Load Balancer could do the switch as well,
but it is a paid feature, and one DNS record per workload is enough for two
hosts.

## Run the controller off both hosts

A controller on host A dies with host A, and a controller on host B cannot tell
whether A is down or only unreachable from B. So the controller runs on
Cloudflare, as a Python Worker with one Durable Object.

The Durable Object stores all controller state and serializes the cycles. A
cron trigger calls it once a minute. Each call runs one cycle and saves the
result before the next can start. If a cycle is interrupted, the next one
reads the saved state and carries on from the same phase.

The controller reaches the agents through Workers VPC services. Each VPC
service routes through the host's existing tunnel to the agent on loopback
port 6720, so no agent needs a public hostname or an open port.

## Keep the agent boring

The agent is a small HTTP server on each host, written against the Python
standard library only. It knows a fixed list of workloads from its local
configuration, each mapped to one Compose service. It accepts four requests:
report host health, report one workload's health, start it, or stop it. It does not accept Compose files,
image names, or arguments from the controller. A leaked agent token can
therefore start or stop the listed workloads and nothing more.

A workload is ready when its container is running, Docker's health check (if it
has one) reports healthy, and an HTTP request to the configured local path
returns one of the accepted status codes. The agent does not follow redirects
or read the response body, so an existing page such as `/` or `/healthz` works
without application changes.

The agent keeps no journal. Docker is the source of truth, so an agent restart
loses nothing that matters.

## One cycle

Every minute, the controller:

1. Asks both agents for host health, asks both for every workload's health,
   and reads every workload's DNS record. These requests run concurrently, so an
   unreachable host costs one timeout per cycle rather than one per request.
2. Updates each host's status. Two failed checks in a row make a host
   `unhealthy`. A host that comes back is `recovering` until it has been
   healthy for ten minutes.
3. For each workload, works out where DNS points and whether that copy is ready.
   Two failed checks in a row on the serving host start a failover. A failback
   starts once the preferred host has been stable for ten minutes while traffic
   was on the other host.
4. Advances any transition already in progress. One cycle can pass through
   several phases, but it counts at most one ready check.

A transition moves through four phases:

```text
starting -> switching -> verifying -> draining -> done
```

- **starting.** Ask the target agent to start the workload, then wait for two
  consecutive ready checks. If that takes longer than five minutes, give up,
  leave DNS alone, and back off for ten minutes before trying again.
- **switching.** Change the DNS record to the target's tunnel, then read it
  back to confirm the change.
- **verifying.** Confirm that DNS still points at the target, then request the
  public hostname and require an accepted status. If either check fails, retry
  on the next cycle. If the target fails two checks in a row, abandon the
  transition and keep both copies running.
- **draining.** Wait one minute, then stop the old copy if its host is
  reachable. If it is not, keep checking every cycle until it is.

The controller saves state before every external change. After a restart in
the middle of a transition, the next cycle continues from the saved phase.

## Timing

The defaults assume small side projects, where a few minutes of downtime is
acceptable and the goal is recovering without a person:

| Step | Default |
| --- | --- |
| Check interval | 1 minute |
| Failed checks before failover | 2 |
| Ready checks before switching DNS | 2 |
| Startup deadline | 5 minutes |
| Stable time before failback | 10 minutes |
| Drain before stopping the old copy | 1 minute |

With these values, DNS moves about three to four minutes after a workload
fails. Failback is deliberately slower: the old host is already serving
nothing, and waiting ten minutes avoids moving traffic back and forth when a
host flaps.

Faster checks would need Durable Object alarms, because cron triggers run at
most once a minute. An earlier version used alarms every 20 seconds. It
recovered faster, but it needed a temporary cron to start the first alarm, and
a lost alarm stopped the controller until someone noticed. A cron trigger has
neither problem, and the extra minute or two do not matter for side projects.

All the values are configurable. The cron schedule is generated from the check
interval, and configurations whose timings do not fit the interval are
rejected before deployment.

## What Ranger gives up

- **Single ownership.** Ranger cannot prove that an unreachable host stopped
  its copy, so two copies may run at once. Only workloads marked
  `overlap_safe` are accepted.
- **State.** Ranger moves processes, not data. Workloads with local databases
  or files need replication that Ranger does not provide.
- **More than two hosts.** The controller assumes exactly two nodes.
- **Serving-host proof.** The public check proves the hostname answers, not
  which host answered. Cloudflare's cache must be bypassed for the probe path,
  or the check may see a cached response.
- **In-flight requests.** Connections open to the old host during the switch
  can fail.
- **DNS propagation.** Cloudflare's edges pick up a CNAME change over a minute
  or two. If the old copy is already down, some requests fail until they do.
- **Free-plan headroom.** A Worker invocation on the free plan can make 50
  outgoing requests. A cycle makes 2, plus 3 for each workload, plus up to 4
  more for each workload that switches DNS in that cycle. That allows 16
  workloads in steady state, or 6 if all of them switch in the same cycle.
- **Beta dependencies.** Workers VPC is in beta.

## Failure visibility

`GET /status` returns the saved controller state. It answers HTTP 503 when no
cycle has completed for three check intervals, so an ordinary uptime monitor
can watch the controller without parsing JSON. Each workload's `error` field
describes the latest cycle only, and it clears when its cause does.

The Worker writes a JSON log line for each host status change, each step of a
transition, and each new or cleared error. The agents write their actions,
failures, and Docker's error output to the systemd journal on each host.
Docker's error output never leaves the host, because it can contain registry
credentials.
