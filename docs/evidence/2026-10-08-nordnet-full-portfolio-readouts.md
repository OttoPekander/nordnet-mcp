# Nordnet full portfolio read acceptance — 8 October 2026

## Scope and acceptance contract

Hermes must retrieve every current holding and preserve all fields returned by the authenticated Nordnet account endpoints, rather than expose metadata alone. It must retrieve native dated portfolio returns and distinguish daily return from accumulated return, cash flows and current cost basis. Native portfolio metrics and complete paged booked transactions must remain accessible. Reads use verified stable account identity and must never activate trading, credit or paid data.

This read unit supplements the trading execution plan. It does not establish every market entitlement or complete the larger trading program.

## Deployed revision

Production: https://arwen.arkki.app/ . Privileged roles were rebuilt from their freshly inspected running images, preserving unrelated incoming connector code, networks, environment and mounts. Existing production credentials were checkpointed and restored on the same owner connection. No staging credential was copied. Hermes retained its existing image/provider and received the three additional semantic tools; its gateway was restarted to discover them.

Owned source SHA256: 5a0971b0b819bbc78b70fefebdd88b1ffe9555cfe93724567054cb5c7a91fe7a

- nordnet-worker: `sha256:640a821366d3b6a5d1c065400df667b39d9a7f13a1035e395130dcc4bb3dacab`
- broker: `sha256:e3ce1b390ad21cc40497d4c6a470055c6fcfdf389cb1f9917360ac6f9ce5f6f1`
- facade: `sha256:b77f14f64227b41c7f0203734f7f223f0ade85ad0f0cd87f1a202a826f78d793`

## Agent-led acceptance

| Journey | Actual observation | Limits |
| --- | --- | --- |
| Deployed MCP → broker → worker → Nordnet, personal account | All five portfolio sections observed; all nine positions and all returned fields retained, decimal quantities as strings. Final snapshot took 0.22 seconds. | Independent section reads are not atomic; receipt time is not a quote timestamp. Executed-trade snapshot covers seven days. |
| Exact dated history, 6 October 2026 | One requested dated row observed with daily/accumulated monetary and percentage returns, own capital, securities value, balance and cumulative flow; native response retained with adjoining baseline. | Account history does not supply historical positions. Missing dates remain unavailable. |
| Native metrics | All six sections observed: today/period development, 17 annual rows, risk metrics, own capital and currency balance overview. | Undated DAY_1 is not substituted for a dated historical return. |
| Booked transaction paging | 365-day range returned 50 then 40 rows, matching total 90; next offset becomes null. All provider transaction fields retained. | Pages are not atomic. Echo the first page's date window on subsequent calls; rows are not verified order settlement. |
| Unknown account | Non-owned account request denied. | No negative order test performed. |
| Actual production Hermes chat, desktop | Read-only request produced all nine holdings with quantity/currency/value and the native dated daily euro/percentage return. It distinguished accumulated returns, cash flows and current versus historical holdings; no screenshot/export request. | Private portfolio content and screenshots remain local, mode 0600; no financial content published here. |
| Actual production Hermes chat, 390×844 IAB | Repeat read-only request again returned all nine holdings and the same native dated return. Rendered document width equalled viewport width, 390px. | Native iPhone behavior unverified. Temporary viewport reset afterwards. |
| Real cloud connection fault and recovery | On the actual provider route (eth1), cloud-only 600ms packet delay returned all six observed sections in 2.53s. Cloud-only 100% packet loss returned partial with all six explicitly unavailable/provider_unavailable in 15.62s. Restoring the original noqueue qdisc yielded all six observed in 0.84s. | No mocked provider. Does not quantify incidence of real slow endpoints or mixed-success partial responses. An initial eth0 injection did not affect the provider route and is not counted as fault evidence. |

Acceptance scripts and captures are local under `/tmp/nordnet-portfolio-*20261008*`. Authentication material is never included in receipts. Financial writes remained disabled throughout; no orders or other financial mutations were submitted. The existing owner session sufficed; acceptance required no owner action.

## Review and resolution

One formal `ce-code-review` receipt: status complete, run `20261008-portfolio`, artifact `/tmp/compound-engineering-501/ce-code-review/20261008-portfolio/review.json`. Source-only local lenses and an independent Claude review covered the coordinated change; the reviewer did not independently exercise the browser. Product acceptance above was collected by the implementing agent.

Confirmed P2: six sequential metrics calls could exhaust the 25-second worker operation budget and discard successful sections. Resolved by bounded fixed-set concurrent reads for metrics and portfolio snapshots. Each section reports observed/unavailable; authentication and identity errors still deny the entire request. Remaining tasks are cancelled and joined before the HTTP client closes on failure or cancellation. The real network checks above verify bounded failure and recovery after this change; no second formal review was run.

Response sizes stay bounded to 1MiB. Oversize-history incidence and long-read disconnect contention remain unmeasured, as recorded by review; no confirmed blocker was established. Dates and transaction pages can be narrowed. Market quotes, depth entitlements, fees and FX execution bounds require their own evidence. These read responses deliberately retain execution_ready=false.

No unit tests, test doubles or mocked internals were added or run. Syntax validation, diff checks and immutable role builds support the real deployed acceptance. The fork CI now performs syntax/package build checks instead of invoking its inherited unit suite, in accordance with the task's testing instruction.
