# Failover design

One named Durable Object owns cluster intent and transition state. At most one
reconciliation cycle runs at a time. Alarms continue persisted transitions.
Agents inspect Docker and execute allowlisted Compose operations. Start and stop
are idempotent, serialized per workload, and bounded. Agents have no journals,
operation IDs, or generations.

Defaults are 20-second checks, five-second probe deadlines, three consecutive
failures, two readiness successes at least five seconds apart, a 120-second
startup deadline, five-minute recovery stability, and a 45-second drain.

Failover starts the alternative copy, verifies readiness, updates DNS by the
configured zone and record IDs, verifies public HTTP status, then drains and
stops the old copy. Both unhealthy leaves DNS unchanged. DNS uncertainty keeps
copies running. An unknown DNS target is reported and left untouched.
Failback follows continuous preferred-node stability and the same cutover steps.

The controller token has DNS read and edit access only to the target zone.
Workers VPC Service bindings reach authenticated agents through node tunnels.
Both tunnels have static application mappings. The controller never changes
tunnel configuration. Images are built before enrollment, then preloaded or
pulled from a registry during startup. Failover never builds images.

Infinite-Memes uses white-box, black-box, port 6704, and
`memes.adhirajpandey.tech`. Agents probe its existing `/` page and accept HTTP 200.
No dedicated endpoint or JSON response is required. This page depends on the
external meme API, so a shared upstream outage can fail readiness on both hosts.
DNS readback and public HTTP status do not prove which host served a response.
Public probe paths require an explicit Cloudflare cache bypass.

Four test groups cover agent convergence and access control, failover and failed
replacement, stable recovery, and restart during transition. Live VPC, tunnel,
cutover latency, and disruptive host tests remain pending rollout acceptance.

## Evidence and limits

On 2026-09-22, source and live read-only inspection confirmed arm64 black-box,
amd64 white-box, separate tunnels, cloudflared 2026.8.2 on both hosts, and no
Infinite-Memes mounts. Its source revision was
`6daab632b4c0819a5ed6ecc0a4b135b49b3bd55e`.
Workers VPC is beta. Account permissions and deployed private routing are untested.

- [Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [Durable Object alarms](https://developers.cloudflare.com/durable-objects/api/alarms/)
- [Workers VPC requirements](https://developers.cloudflare.com/workers-vpc/configuration/tunnel/)
- [Tunnel DNS](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/)

The local implementation includes no live rollout, remote publication, or image
publication. Commit each coherent implementation step with its relevant checks.
