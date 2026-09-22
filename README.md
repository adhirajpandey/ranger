# Habitat failover

Python controller and host agents for preferred-node Docker Compose failover.
The controller runs in one Cloudflare Durable Object. Application traffic goes
directly through the selected node's existing Cloudflare Tunnel.

The first workload is Infinite-Memes on white-box, with black-box as fallback.
Only workloads that tolerate temporary overlap are eligible. Local mutable data
migration and strict single ownership are outside v1.

This repository owns the controller, agents, protocol, and tests. Shed owns
site deployment files. Infinite-Memes owns its readiness endpoint and image.

See [the design](docs/design.md), [protocol reference](docs/protocol.md),
[rollout procedure](docs/operations.md), and [verification](docs/verification.md).

Status: implemented and locally tested. Live rollout remains pending.
