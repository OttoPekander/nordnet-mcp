# Read deadline evidence — 2026-10-07

Read operations accept an optional absolute Unix-millisecond deadline. The
worker bounds lock wait plus execution by the smaller of its existing operation
budget and remaining deadline. Expired deadlines never acquire the worker lock.
Financial requests reject the field and retain their delivery behavior.

This change resolves the worker dependency of confirmed P2 finding 2 in the
single joint Arwen execution-observations formal review:
`20261006-nordnet-execution-observations-d7d3171d` (complete, Ready with fixes).
The full financial program remains incomplete; no second review was run.

Agent-led checks crossed the actual deployed staging worker service, with six
seconds of kernel network delay on the external interface. An expired accounts
deadline returned 503 immediately. A queued accounts deadline returned 503 at
1.50 seconds while the first read ended after 5.00 seconds. A following status
read returned 200 immediately. Finally restored the original noqueue network
state. Normal broker reads recovered to the real session_expired response in
0.14 seconds. No tests with mocks or unit tests were added or run; no orders or
paid subscriptions were created. Authenticated success after this fix remains
unobserved because the saved Nordnet session has expired.

Final staging worker image:
`sha256:d000070afdbbff06db3f5042b81f518890d22b63bd4a155607dd77c2c4a0aa6a`.
Joint owned runtime source SHA256:
`5388818eaf05229ecc77a7739b68b83e5483ad51ec8e5b2990d8630af40afb82`.
