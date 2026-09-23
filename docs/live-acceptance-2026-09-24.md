# Live acceptance, 24 September 2026

The app-only drill used the existing Infinite-Memes `/` endpoint and the unchanged
application image from commit `6daab632b4c0819a5ed6ecc0a4b135b49b3bd55e`.
No files in Infinite-Memes changed. The test used `recovery_stability: 120` seconds.
Afterward, the deployed value was restored to 300 seconds.

Times below are UTC on 23 September 2026.

| Time | Observation |
| --- | --- |
| 20:53:49 | Stopped only the white-box app container. |
| 20:54:52 | Controller started black-box after three failed samples. Its `/` probe returned 200. |
| 20:55:05 | DNS readback pointed to the black-box tunnel. |
| 20:57:10 | Controller had accepted public readiness and entered the 45-second drain. Black-box served public HTTP 200. |
| 20:57:49 | Preferred host recovery timer was running. |
| 21:00:15 | DNS readback pointed to white-box during failback. |
| 21:03:28 | No transition or error remained. White-box was ready; black-box was stopped. |

The controller retries public readiness when `/` returns a non-200 response.
During this drill it saw transient failures after both DNS changes. A separate
five-request public sample while white-box was locally ready returned one 502
and four 200 responses. The exact period of public unavailability was not
measured continuously, so this drill does not establish an outage duration.

The stability timer means the preferred host is recoverable. It includes time
while the app is stopped because the controller can start it. A manual restart
during the first drain was stopped by cleanup; the next restart occurred after
that drain. A future drill should leave the preferred app stopped and let the
controller start it during failback.

Only the app container was stopped for this drill. Host shutdown and network
loss remain separate acceptance tests.
