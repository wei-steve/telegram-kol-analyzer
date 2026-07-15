# Deepcoin Position-History Rate-Limit Design

Date: 2026-07-15

## Goal

Make the read-only `repair-position-attribution` audit reliably load exact
Deepcoin position history without weakening fail-closed attribution rules or
changing any exchange-write path.

## Production evidence

The deployed exact-history audit completed as a dry run and made no database
or exchange mutation. It returned:

- three current live position IDs;
- no current attribution actions;
- no historical cleanup actions;
- one blocking conflict containing 28, then 36, position-history source
  errors; and
- HTTP `401 Unauthorized` for every burst position-history request.

The same authenticated request, using the same server credentials, path,
parameters, and signature implementation, returned HTTP `200` with one exact
row when issued alone. Deliberately signing only the base path returned the
expected `401` and Deepcoin business code `50111 Invalid Sign`. This separates
the batch failure from a persistent credential or request-signing defect.

Deepcoin's default rate-limit table documents
`/deepcoin/account/positions-history` as `10 requests/second` and
`300 requests/minute`, while the endpoint detail page documents a stricter
request frequency of `1 request/second`. The audit currently sends all exact
history candidates without endpoint-specific pacing. The implementation will
honor the stricter published limit because it is safe under both documents and
matches the observed single-request success versus burst failure.

References:

- <https://www.deepcoin.com/docs/zh/rateLimit>
- <https://www.deepcoin.com/docs/zh/DeepCoinAccount/accountPositionsHistory>

## Considered approaches

### A. Per-client position-history pacing (selected)

Add a monotonic, endpoint-specific pace gate to `DeepcoinRestClient`. The first
position-history request runs immediately. Each later position-history request
on the same client waits until at least 1.05 seconds has elapsed since the
previous request began.

Advantages:

- honors the stricter endpoint documentation;
- affects only the new read-only history endpoint;
- preserves exact request and response behavior;
- is deterministic and unit-testable with injected clock and sleep functions;
- remains below both the per-second and per-minute documented limits.

Trade-off: 36 exact candidates take roughly 38 seconds to query.

### B. Pace at 10 requests/second

This follows the default-rate table and is faster, but conflicts with the
endpoint page and the observed burst failure. It is rejected as insufficiently
conservative for a cleanup evidence path.

### C. Retry `401` responses with backoff

This might mask a burst limit, but `401` can also mean an actual authentication
failure. Retrying it generically would blur an important operational signal.
It is rejected. Errors remain explicit and blocking.

## Component design

### Endpoint-specific pace gate

`DeepcoinRestClient` will accept internal injectable monotonic-clock and sleep
callables, plus a position-history minimum interval defaulting to 1.05 seconds.
The gate stores the monotonic start time of the last position-history request.

For each `list_position_history` call:

1. Read monotonic time.
2. If a previous history request exists, compute the remaining interval.
3. Sleep only for a positive remainder.
4. Re-read monotonic time after sleeping and record it as the request start.
5. Execute the existing authenticated GET unchanged.

The first call never sleeps. Negative or zero elapsed time is handled by
sleeping the full remaining interval. The gate is scoped to one client
instance; it introduces no module-global state and does not pace positions,
orders, fills, trigger history, market data, or write endpoints.

The CLI builds one client for a repair run, so all exact-history candidates in
that run share the gate. Production service and CLI processes remain isolated
from each other; the conservative one-request-per-second audit pace stays well
inside the UID minute limit even if a separate process performs a small number
of account reads.

### Error behavior

No automatic retry is added. The existing request exception continues to flow
into `snapshot.errors`. Any HTTP error, malformed response, missing row,
partial close, identity mismatch, live or pending identity, or conflicting row
continues to produce no actions and an unresolved conflict.

The pace gate does not convert errors to empty history and does not alter the
database or exchange. `--apply` remains absent from the verification workflow.

## Testing

Test-driven implementation will first add a failing unit test proving that two
consecutive `list_position_history` calls are not paced today. The test will
use a fake monotonic clock and fake sleeper, avoiding wall-clock delay.

Coverage will prove:

- the first history request does not sleep;
- the second request sleeps exactly the remaining interval;
- elapsed time greater than the interval causes no sleep;
- other Deepcoin endpoints remain unpaced;
- request paths, authentication signatures, and strict list schema remain
  unchanged; and
- existing repair fail-closed tests remain green.

Run focused Deepcoin-client and attribution-repair tests, then the full test
suite, `compileall`, and `git diff --check`.

## Deployment and read-only verification

After review, commit, push, and standard server deployment:

1. Confirm the server HEAD and active service PID/startup log.
2. Confirm the global automatic-trading switch is still false.
3. Confirm the existing backup size and SHA256 remain unchanged.
4. Confirm there are still zero historical-cleanup audit rows.
5. Run `repair-position-attribution --database-path data/research.db` without
   `--apply` and capture its JSON.
6. Verify current actions, historical actions, conflicts, source errors, live
   position IDs, action/live-position intersections, and fingerprints.
7. Stop and report the exact dry-run plan even if every conflict is resolved.

No cleanup action will be applied and automatic trading will not be re-enabled
under this design.
