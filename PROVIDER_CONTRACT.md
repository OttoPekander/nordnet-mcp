# Arwen provider boundary

This fork adds a private trusted-worker transport. Existing MCP reads remain,
but order mutations and session export/restore are not MCP tools. The worker
must run only on the broker network, with no published port or reverse proxy.
Start with `python -c 'from nordnet_mcp.worker import main; main()'`.
`NORDNET_HOST=www.nordnet.fi` selects the Finland web-session surface.
`NORDNET_BROKER_SECRET_FILE` defaults to `/run/secrets/broker-nordnet`; the file
must contain at least 32 characters. Keep `NORDNET_ENABLE_ORDER_WRITES` unset.
Do not enable it for the authentication/read test round.

## Private HTTP contract

POST `/action`, authenticated with `Authorization: Bearer <file secret>`:

```json
{"operation":"status","arguments":{},"generation":0}
```

Replies contain `{generation, result}` or `{generation, error:{code,message}}`.
Operations are serial. `restore` and `disconnect` require a strictly greater
generation and fence the current session even when restore fails; all other
operations require exactly the current generation. A stale request receives 409.
A fresh worker starts at generation 0; the broker restores with its persisted
current generation. Never reuse a generation following a worker transition.

- `epoch`: private worker generation probe with no provider request.
- `status`: verified authentication, market and whether writes are enabled.
- `auth_start` / `auth_poll`: SDK content blocks under `result.content`.
  QR content is for the owner login view, never an execution authority.
- `checkpoint`: private `session_token`, `client_id`, `market`, and
  the broker encrypts the complete object in its
  vault and never exposes it to the frontend, facade, Hermes, logs or receipts.
- `restore`: those checkpoint fields; market must match this worker and client
  must be NEXT. It verifies the live session and account mapping. Failed restore clears the token.
- `disconnect`: drops the local token and pending QR order. It does not claim
  provider-global logout or cancellation of existing exchange orders.
- `accounts`: verified minimum account mapping for authenticated accounts.
- `account_info`, `positions`, `ledgers`, `orders`: `{account_id:int}`.
- `trades`: `{account_id:int,days:int=0}` (0..7).
- `resolve_listing`, `market_data`, `trading_info`:
  `{market_id:int,identifier:string}`.
- `entitlements`: no arguments.
- `fx_rates`: no arguments; raw provider exchange-rate table.
- `fx_rate`: `{from_currency:string,to_currency:string}`; both currencies must
  be three ASCII uppercase letters. Direction is explicit, never inferred.
- `commission_model`: no arguments; provider price models with
  `include_models=true`. An empty response does not mean zero commission.

These three private reads do not enable a trading capability. The authenticated
Finland trial on 2026-10-06 returned directional USD/EUR, EUR/USD and SEK/EUR
values without publication timestamps, and an empty commission-model list.
Those rates establish response direction, but neither freshness nor an all-in
EUR cost bound. No fee estimate or order is submitted by these GET operations.
- `search`: `{query:string,limit:int=20}`; 1..128 printable characters and
  limit 1..50. Uses the documented `free_text_search` parameter, offset 0 and
  ascending name sort. The provider may return up to twice the requested limit
  across listing groups. Results are candidates, never an automatic venue choice.
- `tick_sizes`: `{tick_size_id:int}`; source decimal fields remain strings.
- `create_limit_order`: account_id, market_id, identifier, side BUY/SELL,
  volume decimal string (whole shares), price decimal string, currency explicit
  uppercase three-letter code, reference broker child identity.
- `modify_limit_order`: account_id, order_id, volume, price, currency.
- `cancel_order`: account_id, order_id.

All account-specific calls first refresh the minimum `/accounts` mapping.
Stable account identities must be unique and valid. Unknown or batch account
IDs are denied. Display names do not restrict read access. Any personal-account
restriction for a transport test is temporary and bound to the exact operator
authorization manifest, not persisted as a production account exclusion.

## Evidence and incomplete capabilities

Provider reads were authored against these primary sources on 2026-10-06:

- [Official API](https://www.nordnet.se/externalapi/docs/api): exact listing
  lookup, calendar/order types, account identities, tick tables, limit orders,
  form data mutation fields and OrderReply result codes.
- [Official feeds](https://www.nordnet.se/externalapi/docs/feeds): five-level
  depth and changed-field streaming deltas. The web transport described below is
  distinct from the External API session-key transport.
- The current Finnish public frontend's
  [redux API reducer bundle](https://www.nordnet.fi/static/nn-vendors-node_modules_webapp-next_redux-api-modeler_dist_index_js-node_modules_webapp-next_red-9abac4.7aad161dc5ad184f9e7d.js)
  declares `/api/2/tradables/depth/{tradableId}`,
  `/api/2/tradables/price/{tradableId}`,
  `/api/2/tradables/trading_status/{tradableId}`, `/api/2/realtime_access`.
  Identity is exactly `market_id:identifier`; depth fields include bid1..bid5,
  ask1..ask5 and levels. This public source establishes request paths, not the
  authenticated account's entitlement or current response shape.

`market_data` returns independent raw depth, price and trading_status snapshots,
plus identity-matched five-level fields normalized from the official feed schema.
Prices and quantities are decimal strings; missing levels remain missing.
Continuous status requires C plus CONTINUOUS_TRADING and no halted reason.
Authenticated contract verification remains false until real sample inspection.
It also returns
request paths, received time and explicit unverified capability flags. HTTP
arrival is not source freshness. 403/404/405 are unsupported observations.
Phase normalization, source timestamps, delay, levels, quantities, price units,
fees, FX and entitled scope still require the final authenticated test round.
The observation result cannot enable execution. No quote-only fallback or
invented fresh liquidity is returned.

Order creation uses explicit LIMIT, no extended hours and implicit DAY validity.
It does not implement market orders, stop loss, activation, short selling,
margin or derivatives. Decimal strings remain strings in form data. Every
mutation is attempted once; transport failures, server failures, malformed
responses or missing decisive receipts are uncertain and never retried.
`acknowledged` means a provider OK reply with an order ID, not a fill. Orders
reads include deleted-today orders and preserve raw provider state and cumulative
traded_volume for broker reconciliation. The broker is responsible for agreed
mandates, value/FX/fee gates, holdings/cash reservations, liquidity, calendars,
price movement, reconciliation and idempotent child dispatch records.

The upstream `search_stocks` incorrectly sent `query`, which the provider ignores.
The parent observed an authenticated Nokia request returning unrelated popular
stocks. This fork fixes the parameter to documented `free_text_search`. A public
unauthenticated request with the corrected parameter returned 401; corrected
search behavior still needs the final authenticated worker acceptance.

No order write or authenticated market-data request was made by this authoring worker.
No unit tests or mock acceptance were added or run. Source syntax was checked.
Full U1/U5 live acceptance remains incomplete, and production dispatch must stay
disabled. The owner explicitly prohibited executable real orders for the test
round; rejection or inactive orders are not substitutes for that authority.

## Preliminary costs and booked history

- `cost_estimate`: `{account_id:int,market_id:int,identifier:string,side:BUY|SELL,volume:string,price:string}`.
  Resolves the exact ordinary-share listing and its currency/lot first. Fixed
  commission/liquidation approximation POSTs are reads, separate from order
  transport. Returns raw data and validated decimal charge summaries where
  available, directional preliminary EUR conversion and observation time.
  `cost_bound_verified` and `execution_ready` remain false. No account-specific
  estimate is a guaranteed maximum charge or a fresh FX publication.
- `transaction_history`: `{account_id:int,days:int=7}`, days 0..365.
  Refreshes the account mapping and derives the provider's separate account ID.
  Reads the newest 50 booked rows and the same range's transaction count;
  `range_complete` is true only when the count equals the returned row count.
  No arbitrary account UUID, cloud host, path or transaction ID is accepted.
  The modern cloud JWT is minted using the web session and matching CSRF cookie
  and hidden input; it stays in memory and never leaves the trusted worker.
  Booked rows are not linked to order IDs by this contract. Tax-reference FX
  cannot settle execution accounting.

Authenticated Finland staging reads on 2026-10-06 returned usable BUY and SELL
estimates for Finnish, US and German cash-share listings. A short history range
was complete; a longer one was capped. Booked share rows exposed quantity,
price and total charges, without a verified order identifier. No orders were
submitted. The shared CSRF handshake also replaces the formerly unsuccessful
periodic authorization refresh; success does not prove a longer session lifetime.

Both new multi-request reads have a 25-second worker budget, including waiting
for its serialized action lock. Arwen gives them 30 seconds at the broker;
other broker operations retain their prior timeout. A foreign-currency FX HTTP
or transport failure retains successfully observed native-currency fees and an
explicit unavailable-rate reason. Authentication expiry remains an error.

A real staging history read under temporary kernel network latency succeeded
in 18.44 seconds. Two concurrent slower reads each stopped after 25.01 seconds;
network restoration recovered the normal provider read without a new login.
The one formal review was `20261006-nordnet-joint-0603e2fa`; confirmed findings
were repaired and checked against deployed service/browser behavior. No unit
tests, test doubles, mocks or new financial orders were used.

## Bounded public web-feed observation

`market_data` preserves its existing REST `normalized` and `observations` fields
and adds `web_feed`, containing independently acknowledged price/depth/status
channels and a separate normalized observation. It first verifies the requested
listing through the existing unique tradable lookup. The QR market host must be
one of www.nordnet.fi/se/no/dk and the client must be NEXT. The only socket route
is that host's `/ws/2/public`, with NEXT subprotocol and the current session
cookie; session material never appears in its result. No private feed or paid
data subscription endpoint is used.

The reader waits for the login acknowledgement and exact identity-matched
subscription acknowledgement before accepting each channel. Initial depth must
contain both sides and quantities for the advertised 1..5 levels (five when no
level count is supplied); initial price requires bid/ask and initial status its
phase code. Later identity-matched deltas merge into only that call's initial
snapshot. Provider tick timestamps must be plausible epoch milliseconds and
nondecreasing. Malformed clocks invalidate that channel. Missing acknowledgments,
missing initial snapshots and rejected subscriptions remain explicit unavailable
or unsupported channels without hiding other usable observations.

After the channels settle, it collects deltas until 250 ms of receive inactivity
or the overall budget. Each observation has a four-second socket deadline, at
most 64 messages, 256 KiB
frames and a bounded receive queue. The worker's market-data operation has an
eight-second total deadline including queue wait and listing/REST requests; it
never waits unboundedly behind another operation. A fresh call always makes a
fresh connection. Unexpected disconnect invalidates reconstructed observations;
no reconnection inherits them. Intentional deadline/normal closure retains only
historical observations with raw provider source time and local receipt time.
Heartbeat, socket acknowledgement and receipt are never freshness proof. Raw
delay fields survive delta merging; an absent delay is unknown, not zero.

Live Finland characterization before implementation accepted Aktia's five depth
levels and rejected Apple/Infineon price/depth subscriptions on the tested session.
Those are observed access outcomes, not blanket market entitlements. Full
exchange-depth coverage, phase contract verification, freshness, entitlements and
execution readiness remain false. Actual deployed acceptance must still verify
this implementation; no orders or paid subscription actions are part of it.

### Deployed read-only evidence, 2026-10-06

Worker `sha256:f20d80beb51cd86bd6eeac12c61fdf62a5a3854e56e0d6f5e67daa579c60f5db`
was exercised through the actual staged facade MCP after worker recreation and
encrypted session restoration. Aktia's depth, price and status channels were
acknowledged and returned snapshots; its closed book timestamp remained old,
with unknown depth delay explicit. Five raw slots contained one positive row
on each side after normalization. Apple and Infineon returned explicit rejected
price/depth subscriptions while status remained observable. Calls completed
in 2.42, 0.65 and 0.65 seconds respectively. No financial writes or paid data
changes occurred, and every execution/capability flag remained false.

This establishes the bounded transport for these listings, not current
liquidity, complete exchange depth, entitlement coverage for a market family,
or the provider clock's freshness semantics. Actual malformed/out-of-order
frames and connection-loss transitions were not forced; their rejection paths
were source-reviewed. No unit tests or mocks were used.

Further real scoped MCP reads resolved Investor B (Stockholm), Novo Nordisk B
(Copenhagen), Equinor (Oslo) and Novo Nordisk ADR (NYSE) from Nordnet search,
then looked up each exact returned market/identifier. Stockholm, Copenhagen and
Oslo returned all three web-feed channels with closed phases and old source
clocks; Oslo supplied five positive bid/ask rows. NYSE rejected price/depth while
status remained observable. No Icelandair listing was returned by the bounded
search, so Icelandic instrument access remains unobserved. These samples do not
claim coverage for all listings or fungibility between an ordinary share and
its ADR.

The single formal review (`20261006-nordnet-book-calendar-808ec42b`) identified
identity-versus-rule conflation and loss of independent feed data on optional
REST errors. Both were corrected: unique tradable matching has its own field,
skipped channels return explicit unavailable reasons, and HTTP/transport
failures remain per-source observations. Confirmed session expiry still
propagates. Updated worker
`sha256:c6969867aa8522f4c40cc520b1bcd00e8115a8737f86a46dccf4c1c64be9a9aa`
returned matched identity and observed channels for a real Finnish listing,
explicit unmatched identity/unavailable channels for an actual nonexistent
listing, and matched identity/rejected subscriptions for Apple. Its session
recovered after recreation without owner action. A simultaneous REST outage
with a successful socket, missing tick metadata on an otherwise matched
listing, and malformed/regressing live deltas were source-reviewed, not forced;
no such live outcome is claimed. Financial writes stayed disabled.
