# Deepcoin Legacy Runtime Drain Bridge Review Findings Repair Design

## Context

Exact candidate `5024a59e97b4328acba101f9bc138d7bf3d47530` attempted to
drain seven reviewed Deepcoin pending-entry triggers while the old production
worker remained active. Independent review found one critical and four
important defects:

1. the global freeze disabled management, protection and rescue together with
   new entries;
2. a proven pre-write refusal became permanently indistinguishable from an
   unknown exchange outcome;
3. exchange-evidence freshness reused one pre-query timestamp;
4. the runtime witness did not bind the observed PID to the exact worker unit
   and checkout;
5. fence acquisition and later sentinel validation used different revision
   claim scopes.

The root constraint is architectural. Production SHA
`0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f` has no entry-only kill switch.
Its `auto_trade_enabled` flag also gates management and protection. Therefore
an unchanged old worker cannot simultaneously stop new entries and keep every
protection authority live indefinitely.

## Chosen approach

Use a bounded compatibility cutover. The old runtime is globally frozen only
for the short, separately authorized fence-and-cutover interval. The candidate
runtime understands an independent durable entry-submission freeze and restores
management/protection authority immediately on startup, before any later
entry-unfreeze.

This design does not claim zero process interruption. It claims:

- Telegram work remains in the durable queue rather than being replayed from
  history;
- the old runtime cannot create a new entry once the legacy freeze commits;
- the new runtime starts with entry submission still frozen;
- management, close, TPSL, rescue and protection are live under the candidate;
- every exchange-write authority transition is serialized in SQLite;
- an incomplete cutover fails closed and has an explicit settings rollback.

## Durable settings contract

Add the internal boolean setting `legacy_entry_submission_frozen`, defaulting
to `False`. It is not a normal operator trading preference and is changed only
by the bridge state machine.

`TradingSettings` exposes two separate decisions:

- `entry_submission_enabled` is true only when `auto_trade_enabled` is true and
  `legacy_entry_submission_frozen` is false;
- management/protection effective modes remain live when their configured modes
  are live and either normal auto trade is enabled or the internal legacy entry
  freeze is active.

The second rule is the compatibility handoff: the old runtime ignores the new
field and is globally quiet during cutover; the candidate runtime recognizes
the field and brings protection authority back without reopening entries.

All new-entry entrypoints, including direct recovery submission, use
`entry_submission_enabled`. Entry revision remains separately disabled and
sentinel-fenced. Management, protection, rescue, close and TPSL paths must not
check `entry_submission_enabled`.

## Entry exchange authority

Extend the existing durable entry-revision exchange authority with the owner
kind `new_entry_worker`. Keep the existing storage key and fail-closed parser so
old malformed or foreign state cannot be silently replaced.

For an `open_position` trade signal, the candidate acquires this authority
after claiming the durable signal and before the first possible exchange write.
Acquisition and the bridge freeze both use `BEGIN IMMEDIATE`:

- a new entry that acquires first completes or reaches an explicit unknown
  state before freeze can proceed;
- a freeze that commits first makes every later new-entry acquisition refuse
  before exchange access.

The authority covers the whole multi-leg entry submission, not one individual
leg. A known pre-write refusal releases it. A completed or otherwise fully
classified result releases it. Any exception after a possible exchange write
retains it, so neither freeze nor cancellation can assume quiescence.

## Bridge state and cutover identity

The bridge document records both the original legacy identity and the current
authority identity. A new explicit `handoff` transition is permitted only from
the fenced, no-write state. It requires:

- the reviewed legacy production SHA and worker identity stored at freeze;
- a fresh candidate identity at an explicitly expected candidate SHA;
- the exact worker service `telegram-kol-worker.service`;
- unchanged frozen settings, revision sentinels and zero active entry exchange
  authority;
- no target-related unknown mutation.

The handoff updates only the current worker identity and bridge phase. It does
not release the entry freeze or revision sentinels. Cancellation, drain and
release subsequently bind to the candidate worker identity.

If candidate startup or handoff fails before any reviewed cancellation write,
an explicit rollback may restore the original settings and exact sentinels.
Rollback remains forbidden after any possible reviewed cancellation write or
unknown outcome.

## Worker identity witness

Remove the arbitrary service-name trust boundary. Runtime identity is valid
only for `telegram-kol-worker.service` and records that service name.

The witness must prove, between two stable MainPID/start-tick reads:

- exact checkout HEAD;
- exact systemd worker MainPID;
- `/proc/<pid>/cwd` is the supplied checkout;
- `/proc/<pid>/cmdline` identifies `telegram-kol-research web` with
  `--runtime-role worker`;
- PID and start ticks remain unchanged after the process evidence is read.

Descriptor-based bounded reads are used for proc files. Any missing, malformed,
oversized or changed evidence is unknown and blocks.

## Pre-write refusal and unknown semantics

Add the durable intent status `prewrite_refused` for the exact allowlisted cases
that prove no exchange call occurred:

- final write-gate refusal;
- worker identity unavailable or changed before bridge cancellation begins;
- bridge begin refusal before its write boundary.

The intent stores a bounded reason and `submitted=false`. Planning and rollback
may ignore only a structurally valid `prewrite_refused` intent. Any submitting,
unknown, malformed, unowned or non-confirmed intent outside this exact contract
remains fail-closed. A retry requires a fresh plan and a new confirmation token.

## Evidence freshness

Exchange evidence is timestamped only after the last required query completes.
The state transition obtains a second current timestamp after the final worker
identity recheck. Freshness compares those two distinct timestamps and rejects
negative age or age greater than 60 seconds.

Query completeness remains independent: a fresh but capped, malformed or
partial response is still unknown.

## Revision claim scope

Use one definition throughout: only nonterminal revision batches participate in
the active sentinel claim set. Fence acquisition rejects every foreign claim on
that active set and writes the exact bridge token with a null claim timestamp.
Later validation compares active IDs and active claimed IDs to the stored set.

Terminal unrelated claim residue is ignored consistently. Target-related or
orphan unknown revision children remain blocked by the separate ambiguity scan;
global active authority remains fail-closed.

## Production sequence boundary

This local repair does not authorize the following future production sequence:

1. fresh read-only preflight;
2. explicit legacy freeze and revision fence;
3. separately authorized candidate deployment and bounded worker cutover;
4. candidate identity handoff and protection-authority proof;
5. seven fresh, single-order cancellation plans and applies;
6. drain proof, sentinel release, deployment completion and future-signal-only
   entry restore.

No historical raw-message replay is part of the sequence. If cutover, identity,
protection, evidence or exchange state is unknown, the phase remains frozen and
requires an explicit disposition; it never retries an exchange write.

## Verification

Each finding receives an observed failing test before production code changes.
Focused verification covers trading settings, new-entry authority, legacy
bridge state transitions, reviewed cancellation, CLI timing, worker identity,
entry submission, entry revision and all protection/management workers.

After the final production-code edit, run one full repository suite. Then review
the exact base-to-candidate diff, run `git diff --check`, and verify the branch,
HEAD and clean worktree. Push, deployment, SSH, service control and all
production or exchange writes remain outside this local phase.
