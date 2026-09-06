# Deepcoin Legacy Runtime Drain Bridge Design

## Goal

Provide a locally implemented, default-read-only bridge that can later drain the
seven exact reviewed pending Deepcoin entries while the existing production
worker process and all protection authority remain continuously active. The
bridge must prove that legacy entry-revision exchange authority is quiescent,
execute at most one reviewed cancellation per separately authorized apply, stop
permanently on an unknown result, and preserve complete local terminalization.

This design does not authorize or perform push, deployment, SSH, restart,
settings/database mutation, Deepcoin writes, historical replay or production
execution.

## Production boundary

Production currently runs the pre-lease SHA
`0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`. The local approved base is
`be9d75cdab57ffe57daea03b9eb1cf862cae698b`.

The bridge must work with the old process still running until all reviewed
pending entries are terminal. It may not stop, suspend, restart or replace that
process to manufacture quiescence. Strategy management, stop protection,
backup-stop rescue, take-profit convergence and position reconciliation keep
their existing authority.

## Relevant legacy behavior

The bridge relies on three verified properties of the exact old runtime:

1. The v2 entry-revision executor reloads `entry_revision_v2_mode` inside the
   process-local position-authority lock immediately before it may claim a
   revision batch.
2. The legacy strategy-revision path is invoked from a durable message-processing
   job. A claimed pre-freeze job remains observable until its processing call,
   including the legacy revision call, finishes.
3. A revision batch with a non-empty `advance_claim_token` and
   `advance_claimed_at IS NULL` is treated by the old executor as already
   claimed and is never reclaimed by its five-minute stale-claim logic.

These properties permit a durable fence that the unchanged process already
honors. A fixed quiet period or repeated zero-claim snapshots do not close the
pre-claim race and are explicitly rejected.

## Chosen architecture

Add a closed-schema bridge state under a dedicated `TradingSetting` key and use
exact non-expiring sentinel claims on every active entry-revision batch.

The bridge has two authority layers:

- **Legacy revision fence:** exact sentinel claims understood by the old
  runtime through its existing batch-claim behavior.
- **Lease-aware cancellation authority:** the existing durable
  `reviewed_pending_entry_cancel` lease used by the exact cancellation helper.

The cancellation planner may ignore a revision claim only when every sentinel
field is bound to one valid held bridge state. Any unrelated claim, missing
sentinel, extra sentinel, token mismatch, batch-set drift, malformed state or
unknown child remains fail-closed.

## Closed bridge state

The bridge document contains only bounded operational fields:

- schema version;
- state;
- random bridge token;
- expected production SHA;
- worker PID and Linux process start ticks;
- freeze timestamp and raw-message watermark;
- exact original values of the two governed settings;
- exact fenced revision-batch IDs;
- exact reviewed order IDs;
- whether any reviewed cancellation crossed the exchange-write boundary;
- exact completed reviewed order IDs;
- bounded reason code and update timestamp.

Allowed states are:

- `planned`: read-only projection only and never persisted;
- `frozen`: settings are frozen but the global legacy fence is not proven;
- `fenced`: every legacy revision path is blocked and cancellation may be
  planned;
- `cancelling`: exactly one reviewed cancellation owns the inner lease;
- `unknown_locked`: a write may have started but complete confirmation is
  unavailable;
- `drained`: all reviewed targets are terminal and fresh exchange evidence is
  complete;
- `released_for_deploy`: sentinel claims are released while settings stay
  frozen.

Unknown keys, missing keys, duplicate identifiers, unsupported versions,
unknown states, invalid SHA/PID/start ticks, invalid timestamps or inconsistent
sets fail closed. There is no timeout, stale takeover or automatic recovery.

## Freeze phase

Freeze is a separately authorized future production mutation. It is not part of
default planning and is never implied by running the CLI.

One `BEGIN IMMEDIATE` transaction must:

1. prove there is no existing bridge state or require the exact current token;
2. load and strictly validate the global trading settings;
3. record the original `auto_trade_enabled` and
   `entry_revision_v2_mode` values;
4. set `auto_trade_enabled=false` and
   `entry_revision_v2_mode=disabled`;
5. capture `MAX(raw_messages.id)` as the freeze watermark;
6. record the exact worker PID/start ticks supplied by the runtime witness;
7. persist state `frozen`.

The freeze does not change management or protection modes and does not touch
orders, positions, revision batches or cancellation state. It is not
automatically reversed.

## Fence phase

Fence acquisition is another explicit future mutation and may occur only after
freeze is durable.

The bridge first proves:

- the same worker PID and start ticks remain active;
- the two governed settings remain exactly frozen;
- the production repository SHA is unchanged;
- the message pipeline is the durable queue authority;
- every non-shadow job claimed at or before the freeze boundary has completed;
- there is no active foreign revision claim;
- there is no target-related unknown revision child or replacement write;
- no existing cancellation mutation has an unknown result.

Then one `BEGIN IMMEDIATE` transaction rechecks the same invariants and writes
the exact bridge token to `advance_claim_token` with
`advance_claimed_at=NULL` for every active entry-revision batch. It stores the
exact batch set in bridge state and transitions to `fenced`.

SQLite serialization closes both race orderings:

- if an old executor claims first, fence acquisition observes the foreign
  claim and refuses;
- if the bridge fences first, the old executor's conditional claim fails and
  it performs no exchange write.

A v2 batch created after the fence rechecks the disabled mode before execution.
A legacy batch cannot be created by a pre-freeze message call after the
pre-freeze durable job set is proven drained.

## Cancellation phase

Default cancellation planning remains read-only. Each future apply requires:

- state exactly `fenced`;
- the same worker identity and exact production SHA;
- frozen settings;
- an exact sentinel set matching every active revision batch;
- fresh, complete exchange evidence;
- one exact reviewed action;
- a fresh plan fingerprint and action fingerprint;
- one globally single-use confirmation token;
- no prior unknown cancellation outcome.

The existing cancellation lease is acquired after the bridge fence is
validated. Only the sentinel claims bound to that exact bridge token are
excluded from the global revision-authority conflict; all other claims and
unknown revision children still block.

Apply continues to execute one order only. It performs the existing exact
Deepcoin cancellation, terminal readback, unchanged-sibling checks and complete
local terminalization. A confirmed result records the order in the bridge's
completed set and returns state to `fenced` for a fresh next-order plan.

## Unknown-result semantics

Unknown is terminal for automatic execution.

Any transport exception, incomplete or malformed response, incomplete history,
unconfirmed cancellation, changed exchange identity, fill/position ambiguity,
local commit failure, worker identity drift after a possible write, or escaping
exception after the write boundary must:

- retain the cancellation lease;
- retain all legacy sentinel claims;
- persist or preserve sufficient bounded evidence for `unknown_locked`;
- refuse every later apply without accessing the exchange;
- prohibit automatic rollback, retry, deployment and settings restoration.

Only explicitly classified pre-write refusals may release the inner
cancellation lease. They do not release the outer legacy fence.

## Drain and release

The bridge may become `drained` only when a fresh complete snapshot proves:

- all seven reviewed pending triggers are absent;
- no unreviewed or unidentified governed pending trigger exists;
- governed positions and regular open orders are empty;
- no reviewed order has unknown or fill-conflicting evidence;
- all seven exact local targets are completely terminalized;
- no non-confirmed cancellation mutation exists;
- the inner cancellation lease is idle;
- the same worker process is still running;
- settings remain frozen and sentinel ownership is exact.

Reaching a fixed history page boundary is incomplete, not success.

`released_for_deploy` removes only exact sentinel tokens from the exact stored
batch set in one immediate transaction. It requires all `drained` invariants to
still hold. It never restores automatic trading or revision mode. Any changed
batch/token/timestamp refuses the release.

Once released, the unchanged worker remains active but cannot create new entry
or revision exchange writes because the settings stay frozen and all pre-freeze
legacy jobs are proven drained. A later Task 13 deployment, worker restart and
future-signal-only settings restoration require separate authorizations.

## Rollback

Before any reviewed exchange write, an exact-token rollback may release the
sentinels and restore the two recorded settings only if:

- state is `frozen` or `fenced`;
- no cancellation crossed the write boundary;
- no reviewed order is recorded completed;
- all sentinel and worker identities still match;
- no unknown mutation exists.

After any cancellation starts, automatic rollback is forbidden. Successful
partial progress remains frozen and fenced until the whole reviewed set is
resolved or a separately reviewed manual recovery is authorized.

## CLI and observability

The bridge CLI is default-read-only. Mutation subcommands require explicit
action, exact plan fingerprint, exact bridge token and a one-use confirmation
token. Output contains only bounded reason codes, counts, hashes, worker
identity metadata and state names. It never prints credentials, Deepcoin raw
responses, confirmation tokens or full order rows.

The local implementation must not contact production or Deepcoin during tests.
Adapters provide runtime identity and exchange snapshots so unit tests use
closed deterministic fakes.

## Verification

TDD must prove at minimum:

- strict bridge-state parsing and unknown-owner fail-closed behavior;
- atomic freeze and exact original-setting capture;
- PID/start-tick and exact-SHA drift refusal;
- pre-freeze durable job drainage;
- both old-worker claim race orderings;
- non-expiring sentinel behavior beyond the old five-minute lease;
- global active-batch coverage and exact-token release;
- new v2 work remains disabled after fencing;
- protection, rescue and management authority are untouched;
- only exact bridge sentinels are exempted from cancellation conflicts;
- single-order apply and fresh replan remain mandatory;
- every post-write unknown retains both authority layers and is non-retryable;
- complete local terminalization is required for each success;
- capped/incomplete exchange evidence blocks drain and release;
- settings remain frozen after `released_for_deploy`;
- default CLI paths are read-only and redact all sensitive values.

Run focused bridge, authority, revision and reviewed-cancellation tests while
developing. Run adjacent protection and deployment-gate coverage, then one final
full repository suite after the final production-code edit. Independent review
must find no Critical or Important issue before handoff.

## Explicit exclusions

This local phase does not push, deploy, SSH, freeze production, restart a
service, change production settings or databases, call Deepcoin, cancel an
order, replay history, manufacture traffic or restore automatic trading.
