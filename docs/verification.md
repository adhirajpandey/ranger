# Implementation verification

Captured on 2026-09-22. These results cover code and disposable fixtures.
No controller, agent unit, tunnel mapping, or live DNS change was deployed.

## Completed checks

| Check | Result |
| --- | --- |
| `RUN_DOCKER_TESTS=1 uv run pytest -q` | 9 passed, including the disposable Compose integration |
| `uv run ruff check src tests scripts` | Passed |
| `uv run ruff format --check src tests scripts` | Passed |
| `uv run pywrangler deploy --dry-run` | Python Worker packaged with Wrangler 4.136.3 |
| Controller and agent configuration validation | Both host examples and the cluster example passed |
| `docker compose ... config --quiet` | Both standalone Infinite-Memes deployments passed |
| `systemd-analyze verify deploy/habitat-failover-agent.service` | Passed, with existing unrelated host-unit warnings |
| Infinite-Memes image smoke, arm64 black-box | HTTP readiness, node identity, release identity, and no-store passed |
| Infinite-Memes image smoke, amd64 white-box | Same checks passed |
| Garden documentation validator | Passed |

The four focused test groups cover agent convergence and access control,
failure thresholds and readiness-gated DNS changes, stable recovery and cleanup,
and restart during a transition. The failover cases include both nodes unhealthy
and failed replacement startup or readiness. The adapter test checks that DNS
uses configured record IDs without hostname lookup.

The image smoke tests used temporary containers without external networking or
published ports. The amd64 build ran from a temporary directory on white-box,
not its application checkout. Temporary containers and source staging were
removed. Local candidate images remain available for inspection:

| Host | Image | Image ID |
| --- | --- | --- |
| black-box | `infinite-memes:failover-v1-arm64` | `sha256:85243c4b2e7372c57192bbfc3125da6b58ea51e2c93fe0cc5168dc5f4e35bd63` |
| white-box | `infinite-memes:failover-v1-amd64` | `sha256:85a6c4dd89de8b97071b865469b022516848650c3c5d188f7a996a81cd50fdce` |

Both candidate images identify their release as `failover-v1-candidate`.
Their application code matches Infinite-Memes commit `166c8f6`. Rebuild with the
full source commit as `RELEASE_ID` before enrollment. No image was published.
The image check required pinning setuptools 80.9.0 for the existing Gunicorn
version's `pkg_resources` import. Existing application dependency pins remain.

## Companion commits

| Repository | Commit | Change |
| --- | --- | --- |
| Infinite-Memes | `166c8f6` | Readiness endpoint, image definition, smoke script |
| Shed | `f23ecbc` | Static deployment and VPC configuration examples |
| Garden | `f5af8e3` | Durable architecture and delivery boundary |

Commits are local and use `adhirajpandey <pandey.adhiraj02@gmail.com>`.
The new Infinite-Memes checkout was cloned from white-box without modifying its
live checkout. The new habitat-failover repository has no remote.

## Pending live acceptance

- Workers VPC account permissions and authenticated private routing.
- Deployed Python Worker behavior and Durable Object alarms on Cloudflare.
- Static hostname ingress on both existing tunnels and public cache behavior.
- Controlled failover, five-minute failback, and measured traffic cutover time.
- Host shutdown and network-loss drills in a separate maintenance window.

Workers VPC remains beta. Packaging and simulated Cloudflare responses do not
prove live platform integration. Follow [the rollout procedure](operations.md).
