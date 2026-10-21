# How Ranger works

This page explains why Ranger is shaped the way it is. For configuration keys
and API fields, see the [reference](reference.md).

## Move the DNS record, not the traffic

Each host runs its own Cloudflare Tunnel, and both tunnels route every enrolled
hostname. A workload's public hostname is a proxied CNAME to one tunnel, so
failing over is one DNS change. Ranger never rewrites tunnel configuration and
never proxies requests. A Cloudflare Load Balancer could do the switch too, but
it is a paid feature, and one DNS record per workload is enough for two hosts.

## Run the controller off both hosts

A controller on host A dies with host A, and a controller on host B cannot tell
whether A is down or only unreachable from B. So the controller is a Python
Worker on Cloudflare with one Durable Object, which stores all state and
serializes the cycles. A cron trigger runs one cycle a minute.

The controller reaches the agents through Workers VPC services, which route
through each host's existing tunnel to the agent on loopback. No agent needs a
public hostname or an open port.

## Keep the agent boring

The agent is a small HTTP server on each host, using only the Python standard
library. It reports host health and workload health, and starts or stops the
Compose services in its local allowlist. It accepts no Compose files, images,
or arguments from the controller, so a leaked agent token can only start or
stop the listed workloads.

A workload is ready when its container runs, its Docker health check (if any)
is healthy, and a local HTTP request to the readiness path returns an accepted
status. The agent does not follow redirects or read the body, so an existing
page such as `/` or `/healthz` works without application changes. The agent
keeps no state of its own: Docker is the source of truth.

## One cycle

Every minute, the controller:

1. Asks both agents for host and workload health, and reads every workload's
   DNS record, all concurrently. An unreachable host costs one timeout.
2. Updates each host's status. Two failed checks in a row make a host
   `unhealthy`. A host that comes back is `recovering` until it has been
   healthy for ten minutes.
3. Starts a failover after two failed checks in a row on the serving host, or a
   failback once the preferred copy has been stable for ten minutes while
   traffic was elsewhere.
4. Advances any transition in progress:

```text
starting -> switching -> verifying -> draining -> done
```

- **starting.** Start the workload on the target and wait for two consecutive
  ready checks, at most one per cycle. After five minutes, give up, leave DNS
  alone, and back off for ten minutes.
- **switching.** Point the DNS record at the target's tunnel and read it back.
- **verifying.** Confirm DNS still points at the target and the public
  hostname returns an accepted status. If the target fails two checks in a
  row, abandon the transition and keep both copies.
- **draining.** Wait one minute, then stop the old copy once its host is
  reachable.

The controller saves state before every external change, so an interrupted
cycle resumes from the saved phase.

## Timing

With the defaults, DNS moves three to four minutes after a workload fails.
Failback is slower on purpose: waiting ten minutes avoids moving traffic back
and forth when a host flaps.

Faster checks would need Durable Object alarms, because cron triggers run at
most once a minute. An earlier version used alarms every 20 seconds, but it
needed a temporary cron to start the first alarm, and a lost alarm stopped the
controller until someone noticed. For side projects the extra minute does not
matter. The thresholds and deadlines are configurable; the interval is not.

## What Ranger gives up

- **Single ownership.** Ranger cannot prove that an unreachable host stopped
  its copy, so two copies may run at once. Enroll only workloads that tolerate
  that.
- **State.** Ranger moves processes, not data.
- **More than two hosts.**
- **Serving-host proof.** The public check proves the hostname answers, not
  which host answered. Cloudflare must not cache the readiness path.
- **In-flight requests and DNS propagation.** Connections to the old host can
  fail during the switch, and Cloudflare's edges take a minute or two to pick
  up the change.
- **Free-plan headroom.** A free-plan invocation can make 50 outgoing requests.
  A cycle makes 2, plus 3 per workload, plus up to 4 per workload that switches
  in that cycle: 16 workloads in steady state, or 6 if all switch at once.
- **Beta dependencies.** Workers VPC is in beta.

## Failure visibility

`GET /status` returns the saved state and answers 503 when no cycle has
finished for three minutes, so an ordinary uptime monitor can watch the
controller. Each workload's `error` describes the latest cycle only.

The Worker logs each host status change, transition step, and new or cleared
error as JSON, and can email the notable ones. Undelivered events go out with
the next email. The agents log to the systemd journal. Docker's error output
stays there, because it can contain registry credentials.
