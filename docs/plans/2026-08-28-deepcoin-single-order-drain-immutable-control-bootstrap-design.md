# Deepcoin Single-Order Drain and Immutable Control Bootstrap Design

**Date:** 2026-08-28
**Status:** approved
**Risk:** L3 for the authority seed and each exchange write; L2/L3 for the
runtime authority cutover
**Local design baseline:** `ffb06d19eabfd32dfdab2942b2152fd2809e3d17`

## Objective and Accepted Trade-off

The objective is to terminalize the seven reviewed Deepcoin pending trigger
entries one at a time, then replace the legacy checkout runtime with the first
lease-aware immutable control runtime.

The design explicitly gives up an unprovable zero-interruption handoff. The
legacy runtime cannot independently freeze new entry submission while proving
that management and protection authority remain live. Safety therefore comes
from short, explicit, observable maintenance windows with exact rollback
boundaries. A normal window targets less than 60 seconds and has a hard ceiling
of ten minutes.

The current staged release at the local baseline is not an activation
candidate. It omits the monitor from its activation scope and contains the
rejected legacy bridge. Implementation must produce and independently review a
new commit, then create a new immutable release. The existing release must not
be edited in place.

Historical production observations, including the production SHA, zero
positions, zero regular orders, and seven pending triggers, are routing context
only. Every production window must obtain fresh evidence.

## Non-Negotiable Boundaries

- The only order targets come from
  `REVIEWED_PENDING_ENTRY_TARGETS`; no manifest, document, command, or database
  row may contain a second seven-order list.
- One invocation handles exactly one target order.
- Every order is freshly planned and uses a new single-use confirmation token.
- An incomplete exchange query gets at most one reasoned retry. A second
  incomplete result is `unknown`.
- An exchange-write `unknown` is never retried automatically.
- A confirmed exchange result is not complete until all local intent, leg,
  binding, lifecycle, protection, convergence, and event state is terminal.
- Active global authority and target-related unknown state fail closed.
- Bulk cancel, cancellation loops, historical replay, replacement orders, and
  replay of messages accumulated during maintenance are prohibited.
- Entry freeze does not imply or grant management, protection, close, TPSL, or
  rescue authority. Those capabilities require direct runtime proof.
- Checkout HEAD alone is never runtime version evidence.
- Monitoring is release-aware, covers the complete activation scope, and
  redacts secrets and detailed exchange payloads.
- Push, stage, SSH, production reads, database writes, service control,
  activation, exchange writes, and entry thaw remain separately authorized.

## Considered Architectures

### A. Three action-specific entry points — selected

Use one authority seed action, one strictly single-order drain action, and one
one-time immutable control bootstrap action. Each action has its own manifest,
arguments, evidence, success state, and rollback boundary.

This removes the universal safety gate and the multi-state handoff bridge while
retaining narrow safety gates at the point of each dangerous action.

### B. Retrofit the legacy runtime, then use the ordinary activator

Add candidate identity and entry-only control to the legacy checkout, restart
it, then perform another deployment and activation. This needs an intermediate
runtime, multiple restarts, and another bridge between versions. It preserves
the circular dependency that caused the rejected design and is not selected.

### C. Pure manual maintenance runbook

Have an operator stop services, seed the row, cancel orders, write systemd
drop-ins, and restart services manually. This minimizes code but provides weak
TOCTOU closure, auditability, crash recovery, and deterministic rollback. It is
not acceptable for a real-money runtime.

## Delete, Keep, and Replace

### Delete

- The `legacy_runtime_drain_bridge` persistent document and its seven-state
  workflow.
- Freeze, fence, handoff, cancelling, drain, and deploy-release bridge commands.
- The public/settings-owned `legacy_entry_submission_frozen` field and every
  ordinary API/settings write path for it.
- Activation success rules that infer management or protection authority from
  settings.
- Activation rules that accept a different SHA without a distinct candidate
  process identity.

### Keep

- `REVIEWED_PENDING_ENTRY_TARGETS` as the sole canonical target source.
- The existing single-order cancellation planner and complete local
  terminalization semantics.
- Single-use repair confirmation tokens, strengthened with action and evidence
  binding.
- The durable entry/revision exchange authority concept.
- Immutable release staging, content and manifest hashes, explicit action
  manifests, and rollback release validation.
- Process-local deployment identity and successful-cycle authority evidence.
- Atomic publication and worker ownership for the contract-specification cache.

### Replace

- Replace the current authority document with a three-state, generation-fenced
  authority record.
- Replace the legacy bridge with `seed-entry-authority`, `drain-one`, and
  `bootstrap-control`.
- Replace checkout-HEAD monitoring with loaded-release and unit-fragment
  monitoring.
- Replace the initial use of the ordinary activator with a one-time bootstrap.
  After bootstrap, the resulting immutable control release becomes the rollback
  base for ordinary future activations.

## Components

### `seed-entry-authority`

This L3 action is callable only from a separately staged immutable release. It
does the following while the production services are persistently masked and
stopped:

1. validates an exact action manifest and host action lock;
2. creates and verifies a SQLite backup;
3. records `PRAGMA quick_check`, foreign-key results, and affected and critical
   table counts;
4. requires the authority row to be absent;
5. inserts exactly one canonical idle authority row in one transaction;
6. repeats integrity and count checks;
7. restores the legacy service state only after every postcondition passes.

An existing, malformed, or unexpected row is a refusal, not a migration hint.

### `drain-one`

The command accepts one canonical target selector, one action manifest, one
fresh confirmation token, and the exact expected plan and evidence hashes. It
has no list argument, wildcard, remaining-set mode, or loop.

It runs the exchange action only after persistent service inhibition, complete
post-stop exchange evidence, a generation CAS, and an in-transaction re-plan.
It releases authority only after confirmed exchange terminal state and complete
local terminalization.

### `bootstrap-control`

This one-time action switches web, ingest, worker, and monitor as one scope to a
new immutable release. It records the exact legacy unit state, persistently
masks and stops the four-component scope, proves quiescence, acquires bootstrap
authority, installs and validates the complete candidate unit/drop-in set, and
starts the candidate with entry admission closed.

The bootstrap remains the authority holder until the candidate proves its
identity and independent management/protection capabilities. It then releases
the row and requires one no-exchange-write authority acquire/release self-test
from the candidate worker.

Bootstrap success never enables new automatic entries.

### Host action lock and persistent service inhibit

All three entry points share one root-owned host `flock`.

Every maintenance window uses persistent systemd masks rather than runtime
masks. Persistent masks survive action-process failure and host reboot. The
action records the exact pre-window enabled, masked, and active state and only
unmasks after a provably safe terminal state.

If a write-boundary unknown or local terminalization failure occurs, the masks
remain. The old worker cannot restart after a reboot and bypass the new
authority row.

## Minimal Persistent State Machine

The single internal authority row has three states:

```text
absent --seed--> idle
idle --generation CAS--> held
held --confirmed success and complete local terminalization--> idle
held --write unknown, expiry, identity drift, or unsafe release--> blocked
```

### `idle`

No process owns entry/revision exchange-write authority. The row includes a
non-negative generation and the last safe release timestamp.

### `held`

The row binds all of the following:

- incremented generation;
- exact owner kind and action ID;
- confirmation-token hash where applicable;
- owner PID and process start ticks;
- acquisition and deadline timestamps;
- plan and fresh-evidence hashes;
- whether the exchange write boundary has been reached.

Deadline expiry never returns the row to idle. The next observer converts it to
`blocked`, or treats an unsuccessful conversion as equivalently blocked while
leaving `held` intact.

### `blocked`

The row records the prior owner, generation, time, reason code, and write-boundary
classification. There is no automatic transition out. Resolution requires a
separately designed and authorized evidence repair.

If a `held -> blocked` CAS fails, the row must not be released. An exact held
row remains fail-closed.

## Cross-Process TOCTOU Closure

| Race | Closure |
|---|---|
| Two action commands run concurrently | All action-specific commands require the same root-owned host `flock`. |
| systemd restarts a legacy process | Persistently mask the four-component scope before stopping it; retain masks for the full action. |
| The host reboots after an unknown | Persistent masks survive reboot, so the legacy worker cannot start and ignore the authority row. |
| A unit says inactive while a process survives | Require inactive units, empty cgroups, zero MainPIDs, and two stable scans of matching checkout/cmdline processes. |
| The local plan changes after planning | Acquire with generation CAS and re-plan under `BEGIN IMMEDIATE`; compare the exact plan hash. |
| Exchange evidence changes before the write | Query after stop, require complete pagination and a maximum age of 30 seconds, bind the evidence hash, and recheck the exact target immediately before the call. |
| A token is copied or replayed | Store only its hash and bind it to action ID, target, plan, evidence, and generation; consume once in the pre-write transaction. |
| A different process claims candidate identity | Join immutable commit and manifest hash with systemd MainPID, `/proc` start ticks, cwd, cmdline role, and process-local identity. Require a tuple distinct from legacy. |
| Candidate unit configuration is partially installed | Keep every unit stopped and masked while installing; verify the complete unit/drop-in hash set and `systemd-analyze verify` before unmasking any unit. |
| Authority changes after a check | All authority mutations use an exact state-and-generation CAS; mismatches never overwrite or release. |
| Exchange success is followed by a partial local write | Terminalize every required local object and the single event in one SQLite transaction; failure retains authority and persistent masks. |
| A new trade appears between order windows | Restore the legacy runtime only after a safe order result; the next window obtains a completely new snapshot and stops the campaign on any new object. |

The supported authority boundary is the systemd-managed production runtime. A
root operator who deliberately launches an exchange-writing process outside
systemd is an out-of-contract production mutation. The tools still scan and
refuse known stray processes, but cannot defend against a malicious root process
created after the final scan.

## Runtime Identity and Capability Proof

Every activated role must report and externally match:

- release commit;
- immutable release manifest hash;
- PID and `/proc` start ticks;
- systemd MainPID and start ticks;
- role;
- process cwd and expected command line.

Candidate PID/start-tick tuples must be distinct from the corresponding legacy
tuples. A different Git SHA is insufficient.

The worker must derive capabilities from instantiated components and fresh,
successful authority-owning cycles. Bootstrap requires:

- `entry_admission=false`;
- `management=true`;
- `protection=true`;
- `close=true`;
- `tpsl=true`;
- `rescue=true`.

The entry-admission result neither enables nor implies the other capabilities.
Settings fields are not capability evidence.

## Version-Aware Monitor

Monitor is part of the same immutable activation scope as web, ingest, and
worker. Its expected state comes from the loaded immutable release manifest,
not from checkout HEAD.

It validates:

- the four roles use one commit and manifest;
- active unit fragment and drop-in hashes match the manifest;
- systemd and process-local PID/start-tick evidence agree;
- worker capability cycles are successful and fresh;
- entry admission remains closed during bootstrap acceptance;
- authority state is structurally valid;
- the contract cache retains its worker-owned atomic-publication contract.

Monitor output includes only status, counts, hashes, bounded reason codes, and
truncated identifiers. It excludes credentials, confirmation tokens, raw order
payloads, settings JSON, and unbounded logs.

## Production Workflow

There are nine bounded maintenance windows after a new immutable candidate has
been pushed, reviewed, staged, and given fresh read-only preflight evidence.

### Window 1: L3 seed

Preconditions:

- exact production runtime, database path, unit state, and loaded code evidence;
- absent authority row;
- zero active exchange writes;
- complete fresh exchange evidence;
- verified backup destination and rollback procedure.

Success:

- exactly one new idle authority row;
- integrity and before/after counts pass;
- exact legacy runtime state restored and verified.

Failure:

- before commit: database remains unchanged and legacy may be restored;
- after commit with integrity failure: keep services masked and restore the
  verified backup;
- unknown database state: keep services masked for manual L3 resolution.

### Windows 2–8: one canonical order each

Each window selects one still-pending target through the canonical constant and
requires a new plan and confirmation token.

Preconditions for every order:

- legacy runtime identity and capability proof are fresh;
- authority is exact idle at the expected generation;
- no active global or target revision authority exists;
- no target-related unknown exists;
- complete Deepcoin evidence shows zero positions and zero regular open orders;
- pending triggers equal the exact still-pending canonical subset;
- local intent, leg, binding, lifecycle, protection, convergence, and event
  state matches the plan.

Success:

- Deepcoin gives an exact terminal result for the target;
- all local state terminalizes in one transaction with one terminal event;
- the token cannot be reused;
- authority returns to idle;
- the exact legacy service state is restored and verified.

Failure and rollback:

- a pre-write refusal safely releases authority and restores legacy;
- an explicit exchange rejection that proves no action occurred leaves local
  business state unchanged and may restore legacy;
- a possible write with unknown result enters blocked and retains persistent
  masks;
- confirmed exchange success followed by incomplete local terminalization also
  enters blocked and retains persistent masks.

Unknown permanently stops the campaign. There is no next order, automatic
retry, or automatic rollback.

Between successful order windows the legacy runtime is fully restored. Because
it has no provable entry-only freeze, a new position, regular order, noncanonical
trigger, or related unknown in the next fresh snapshot stops the campaign. The
target list is never expanded.

### Window 9: immutable control bootstrap

Preconditions:

- all seven canonical targets have complete local terminal state;
- complete fresh Deepcoin evidence shows zero positions, zero regular orders,
  and zero pending triggers;
- authority is exact idle;
- no related unknown or active revision scope exists;
- candidate and rollback artifacts, manifests, unit snapshots, and database
  health checks pass;
- candidate scope is exactly web, ingest, worker, and monitor.

Success:

- all four candidate identities match one immutable release;
- the worker identity is distinct from legacy;
- entry admission is closed;
- required management and protection capabilities have fresh successful-cycle
  evidence;
- a no-exchange-write authority self-test completes;
- the version-aware monitor passes;
- the new immutable release becomes the control and future rollback base.

Rollback boundary:

- before any possible exchange write, restore the exact legacy unit snapshot,
  return authority to idle, and verify the legacy runtime;
- after an unknown possible write or uncertain local state, retain persistent
  masks and blocked authority;
- after a later, separately authorized entry thaw, rollback is a new migration
  action and is not part of bootstrap.

## Acceptance Tests

### Protection authority

- Entry admission false does not make any management or protection capability
  true.
- Disabled, missing, failed, or stale management, protection, close, TPSL, or
  rescue cycles reject bootstrap.
- Capability evidence must come from the candidate process and match its
  PID/start ticks.

### Worker identity

- A different SHA with the same PID/start ticks is rejected.
- Correct checkout HEAD with a wrong cwd, manifest, unit hash, role, or command
  line is rejected.
- A missing monitor role or a monitor outside candidate scope is rejected.
- Identity rechecks detect PID reuse and process replacement.

### Evidence freshness

- Evidence older than 30 seconds is rejected.
- Incomplete pagination, missing completion markers, or malformed responses get
  at most one reasoned retry, then unknown.
- Target-state, remaining-set, plan, or evidence drift is rejected before the
  exchange call.

### Revision and authority scope

- Active global authority, a target-related revision batch, a target unknown,
  or a noncanonical pending trigger rejects the action.
- Unrelated terminal historical rows do not silently widen the action scope.
- Generation and owner mismatches never release authority.

### Local terminalization

- Inject a failure in every required local table update and prove that the
  entire transaction rolls back, the single event is not duplicated, authority
  does not become idle, and services remain persistently masked.
- A repeated invocation cannot reuse the token or terminalize twice.
- Confirmed exchange success without complete local terminal state is blocked,
  not success.

### Seed, systemd, bootstrap, and monitor

- Seed accepts only an absent row and verifies backup, integrity, and counts.
- Persistent masks survive action-process failure and a simulated host reboot.
- Partial drop-ins, missing unit hashes, failed systemd verification, or a
  failed rollback keep the runtime stopped.
- A candidate with all management/protection modes disabled cannot pass.
- Monitor proves loaded release identity without relying on checkout HEAD and
  redacts sensitive material.

## First Falsifier

The first RED integration test simulates Deepcoin receiving the exact cancel
request while the client times out before reading the response. It then crashes
the action process and simulates a host reboot.

The design is falsified if any of the following occurs:

- authority returns to idle;
- the cancel request is sent again;
- the old worker can start;
- persistent masks are removed;
- the action reports success or a safe rollback.

The only acceptable result is held-or-blocked authority, retained persistent
masks, no retry, and an explicit manual-resolution requirement.

## Authorization Phases

The approved local phase includes design documentation, implementation
planning, local code and tests, and local commits.

Separate future authorizations are required for:

1. pushing the new reviewed commit;
2. staging a new immutable release;
3. SSH and fresh production read-only evidence;
4. obtaining and rehearsing on a production database copy;
5. the L3 authority seed, database backup/write, and service control;
6. each of seven single-order Deepcoin cancellation windows, each with a fresh
   exact token and explicit order-level approval;
7. the immutable control bootstrap and service activation;
8. entry thaw or restoration of automatic trading;
9. any manual resolution or rollback after unknown.

No authorization in this design permits bulk exchange writes, replay, automatic
retry, automatic entry thaw, or mutation of the staged historical release.
