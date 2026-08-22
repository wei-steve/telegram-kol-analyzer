# Production Monitor Remediation Design

**Status:** Approved by the user on 2026-08-21

**Goal:** Restore trustworthy production-monitor evidence by removing the UI-cache dependency from composite checks, eliminating capture-writer schema drift, and keeping the monitor's expected Git HEAD synchronized with managed deployments.

## Verified problem statement

The three observed symptoms are independent at the top level, with the first two forming one failure chain:

1. The production monitor ran at approximately 2026-08-22 09:00 CST while `data/web_cache/deepcoin_live_positions.json` had `captured_at=2026-08-22T00:27:55.038234+00:00`. The composite adapter requires a snapshot no more than five minutes old, so it failed with `live_position_snapshot_stale` and the monitor reported `adapter_failure`.
2. The monitor then attempted to submit that failure through `/api/runtime-incidents/monitor-capture`. The client-side adapter allowlist includes `composite` and `coverage`, but the server-side endpoint still accepts only `service`, `head`, `settings`, `journal`, `events`, and `audit`. The server returned HTTP 422 and the monitor logged `Monitor incident capture writer is unavailable`.
3. `/etc/telegram-kol-monitor.env` still pinned `767497010baf1e1db56080fe80b3e619358b64fa`, while the production checkout was at `fdaff6b12d0aa4470e9bfcc63239c8541c01c5ff`. The installer captures HEAD once, but the managed updater does not advance the pin after later deployments. Current evaluation records this mismatch in details only; it does not add a reason code.

The DeepSeek HTTP 402 failures are explicitly outside this remediation and are being handled separately.

## Scope and invariants

This remediation may change only monitor observation, incident capture, and monitor deployment bookkeeping. It must preserve all of the following:

- first-pass recognition decisions;
- contextual strategy resolution;
- position ownership and attribution;
- trading execution and exchange-write semantics;
- `message_lock_mode=global`;
- `message_pipeline_mode=queue`;
- the monitor's dedicated unprivileged identity and read-only database mounts;
- strict loopback and token authentication for monitor-only endpoints;
- capture-writer fail-open behavior relative to trading and the primary monitor result.

There is no schema migration, production data repair, exchange write, order cancellation, order creation, or historical replay in scope.

## Considered approaches

### A. Authenticated on-demand live-position projection — selected

At the beginning of each composite check, the monitor requests a fresh, bounded position-size projection from a token-authenticated loopback endpoint. The main service owns the Deepcoin credential and performs one read-only exchange request; the monitor never receives that credential.

This removes dependence on UI traffic, performs no background polling between monitor runs, and fails closed when the live read is unavailable or incomplete.

### B. Periodic refresh inside the main service — rejected

A background task could refresh the existing cache every few minutes. This would add a permanent task, recurring API traffic, shutdown behavior, and another source of thread or event-loop pressure. A failed refresher could also leave the same stale-cache ambiguity.

### C. Dedicated exchange-snapshot sidecar — rejected

A separate systemd service would provide stronger process isolation, but it would require a new secret-distribution boundary, service lifecycle, and deployment surface. That cost is not justified for one bounded read every monitor cycle.

## Architecture

### 1. Fresh live-position projection

Add an authenticated loopback-only GET endpoint:

```text
/api/runtime-incidents/live-position-sizes
```

The endpoint reuses `require_monitor_capture_auth`, calls the existing Deepcoin client factory in FastAPI's synchronous worker context, and closes the client in `finally`. It returns only this closed projection:

```json
{
  "schema_version": 1,
  "complete": true,
  "captured_at": "2026-08-22T01:00:00+00:00",
  "positions": [
    {"pos_id": "bounded-position-identity", "size_text": "0.123"}
  ]
}
```

The projection must:

- contain no symbol, side, price, order, message, provider response, credential, or raw error;
- include at most 100 unique non-empty position identities;
- accept only finite, non-negative decimal size strings;
- use an aware UTC capture timestamp;
- return `complete=false` with an empty position list and no raw exception when the provider read fails;
- stay below the monitor client's 32 KiB response limit.

`ProductionSafetyAdapters` receives a fixed default URL for this endpoint and reuses `monitor_capture_token`. A bounded HTTP reader uses `trust_env=False`, rejects any non-loopback or unexpected path, validates the exact JSON schema, and rejects incomplete, stale, duplicate, oversized, negative, non-finite, or malformed data.

`read_composite_management_invariants` is extended to accept an already validated `Mapping[str, Decimal]`. Production composite checks use the fresh mapping. The existing file parser may remain for focused legacy tests, but production must not fall back to the web cache after an endpoint failure.

### 2. One adapter-name contract

Rename the private adapter-name collection to an exported immutable constant and make it the single source of truth for:

- adapter-failure normalization;
- incident-capture projection construction;
- `/api/runtime-incidents/monitor-capture` request validation;
- maximum accepted adapter-failure count;
- exhaustive contract tests.

The set is:

```text
service, head, settings, journal, events, audit, composite, coverage, entry_preamble
```

Including `entry_preamble` also fixes the latent case where the collector can append that name but the evaluator currently normalizes it to `unknown`.

Unknown names remain rejected. The endpoint continues to accept only the fixed reason codes `adapter_failure` and `audit_incomplete`, and it still accepts no caller-supplied incident type, business identifier, fingerprint, or arbitrary summary.

### 3. Transactional expected-HEAD synchronization

Integrate monitor lifecycle handling into `deploy/telegram-kol-update` rather than relying on a manual post-deployment reminder.

When the monitor installation is complete and internally consistent, the updater must:

1. Record whether the timer was enabled and active.
2. Stop the timer and wait for all monitor oneshots to be inactive.
3. Validate that `/etc/telegram-kol-monitor.env` is a regular, non-symlink, root-owned `0600` file containing exactly one expected-HEAD line.
4. Atomically normalize that line to `previous_commit` before mutating the application checkout. This repairs an already stale pin while keeping rollback truthful.
5. Perform the existing exact-SHA deployment and service health gate.
6. Atomically advance the line to `EXPECTED_COMMIT` only after the production checkout and running service have reached that exact commit.
7. Restore the timer's prior enabled/active state only after the pin is verified.

If deployment rolls back, cleanup must synchronize the pin to `previous_commit` and restore the prior timer state. If the monitor is completely absent, deployment behavior remains unchanged. A partial or malformed monitor installation fails closed before checkout mutation.

The atomic rewrite must preserve every non-HEAD environment line without printing secrets. It must not use a broad in-place substitution, unresolved path, or glob.

HEAD mismatch remains deployment context rather than a new monitor reason code. This remediation restores the usefulness of the detail without changing alert semantics.

## Error handling

- Live-position endpoint authentication failure remains indistinguishable from absence (`404`).
- Provider failure produces only an incomplete bounded projection; the raw exception is logged without secrets.
- Monitor-side projection failure is recorded as `composite` adapter failure and cannot fall back to stale data.
- Capture writer continues to catch transport/persistence failure after the primary result is formed. It cannot change trading behavior or suppress the primary alert.
- A deployment synchronization failure before checkout mutation restores the timer and stops.
- A synchronization failure after checkout mutation enters the updater's existing rollback path.
- Any incomplete external query receives at most one reasoned retry. A second incomplete result remains unknown and leaves remediation status `in_progress`.

## Verification level

The complete change is **L2**, because it changes managed monitor process lifecycle during deployment. It is not L3: there is no schema change, production-data mutation, or exchange-write change.

Development verification consists of focused tests after each edit and one full suite on the final production-code candidate. Production verification uses no more than four checkpoints:

1. pre-deploy;
2. post-cutover;
3. post-restart (the managed deployment restart is the only application restart);
4. observation end.

Observe 30 continuous minutes and at least five real messages, trying to cover two chats. If five messages do not arrive in 30 minutes, stop, record limited traffic, and leave status `in_progress`. There is no one-week soak requirement. Direct exchange history is not required because this path performs no exchange writes.

## Acceptance criteria

Local acceptance requires:

- strict endpoint authentication and schema tests;
- complete/incomplete/duplicate/stale/oversized live-position projection tests;
- a composite test proving current live sizes are used without the cache file;
- exhaustive equality between collector adapter names and writer-accepted names;
- capture endpoint acceptance of `composite`, `coverage`, and `entry_preamble`;
- updater tests for stop ordering, atomic pin advance, rollback pin restoration, prior timer-state restoration, absent-monitor compatibility, and malformed-monitor fail-closed behavior;
- focused test groups passing;
- one full suite passing on the final code candidate.

Production acceptance requires:

- exact deployed SHA verified locally, remotely, and in the production checkout;
- actual HEAD equals the installed expected HEAD;
- authenticated capture-health, coverage, and live-position endpoints return complete HTTP 200 projections;
- one scheduled monitor cycle completes without `adapter_failure` caused by a stale UI cache;
- the capture POST is accepted and no new `Monitor incident capture writer is unavailable` warning appears;
- monitor timer is active after deployment;
- `message_lock_mode=global` and `message_pipeline_mode=queue` remain unchanged;
- no new queue backlog, duplicate processing, or event-loop stall attributable to this change;
- 30 minutes and at least five real messages are observed, with two chats attempted.

Existing `audit_abnormal` state must be reported independently. It may not be cleared or described as recovered unless a complete healthy audit proves recovery.

## Rollback

Rollback uses the existing exact previous commit through the managed updater. The updater must stop the monitor timer, restore application code and package state, synchronize expected HEAD to the actual rollback commit, restore the prior timer state, and verify the main service is active.

No database restore or exchange action is required. If monitor recovery cannot be proven, leave its timer stopped, keep the trading service state unchanged, mark remediation `in_progress`, and return control to the user with the exact failed gate and evidence path.
