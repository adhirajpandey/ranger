# Add a workload

Enroll a service in an existing two-host Ranger installation. Reuse the
agents, tunnels, VPC services, and controller. For a first installation, use
[Operate Ranger](operations.md).

## Prepare the service

Confirm that the service tolerates two copies running during a transition.
Ranger does not replicate files or databases. Its start command uses
`--no-deps`, so dependencies must already be available on both hosts.

Follow [Prepare a workload](operations.md#prepare-a-workload) to prepare both
Compose files and images, configure both tunnel routes, and point the proxied
CNAME at the preferred host. Start the service on the preferred host only.
Use the same pinned application version on both hosts, built for their CPU
architectures. Confirm that the public readiness path bypasses caching.

## Configure both agents

Add the same workload name to both agent configurations. For example, add
this entry under `workloads`, adjusting the path and port on each host:

```json
"example-app": {
  "compose_file": "/srv/example-app/compose.yaml",
  "project": "example-app",
  "service": "app",
  "port": 3000
}
```

Add `env_file` if Compose needs an explicit environment file. The defaults
are `readiness_path: "/"`, `expected_status: [200]`, and `pull: "missing"`.
If you override readiness settings, use the same values in both agents and
the controller.

Update each host's agent configuration and restart its agent:

```sh
sudo systemctl restart ranger-agent@USER
curl -H "Authorization: Bearer $AGENT_TOKEN" \
  http://127.0.0.1:6720/workloads/example-app/health
```

Expect `state: ready` on the preferred host and `state: stopped` on the
standby. Restarting the agent does not restart the application containers.

## Check the proposed configuration

Add the workload to your private cluster file, but do not deploy it yet:

```json
"example-app": {
  "preferred_node": "node-a",
  "fallback_node": "node-b",
  "hostname": "app.example.com",
  "zone_id": "0123456789abcdef0123456789abcdef",
  "record_id": "fedcba9876543210fedcba9876543210",
  "overlap_safe": true
}
```

Use your existing node names and the actual DNS IDs. Ensure the controller's
DNS token can read and edit the new workload's zone.

From a Ranger checkout, validate the cluster and both agent files:

```sh
uv run ranger-preflight --cluster /private/cluster.json \
  --agent /private/node-a-agent.json --agent /private/node-b-agent.json \
  --workload example-app
```

The command checks both allowlists and matching readiness settings. It does
not contact Docker, agents, or Cloudflare unless you request additional checks.
Use the same proposed files on both hosts and repeat the command with
`--local-node node-a` on node-a and `--local-node node-b` on node-b. These
checks inspect Compose, require the image to exist locally, verify the
loopback port and restart policy, and check initial readiness. They never
start, stop, build, or pull a container.

To also read the configured DNS record, export `DNS_API_TOKEN` and add
`--dns`. This requires the proxied CNAME to point at the preferred tunnel.

A pass covers only the requested checks. Verify image versions and CPU
architectures, dependency availability, both tunnel routes, and cache bypass
separately. The command reads files; the agent health requests above confirm
that the running agents loaded their allowlists.

## Enroll the workload

After checks pass on both hosts, deploy the changed controller configuration.
For GitHub Actions, update only the cluster secret if other values are unchanged:

```sh
jq -c . /private/cluster.json | gh secret set -R OWNER/REPO RANGER_CLUSTER_CONFIG
gh run list -R OWNER/REPO --workflow CI --branch main --event push --limit 1
gh run rerun RUN_ID -R OWNER/REPO
```

Use the run ID from the list. Alternatively, follow
[Deploy by hand](operations.md#deploy-by-hand).

Read the authenticated `/status` response after a cycle finishes. In
`workloads.example-app`, expect `current` to be the preferred node,
`transition` and `error` to be `null`, and observations to show the preferred
copy ready and the standby stopped. HTTP 200 confirms a recent controller
cycle; it does not mean every workload is healthy.

Run a [failover drill](operations.md#run-a-failover-drill) when a few minutes
of downtime are acceptable. Stop only the enrolled Compose service when
the project contains other services.
