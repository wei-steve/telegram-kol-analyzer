# MiMo v2 Retirement And Safety-Gate History Design

**Date:** 2026-08-16

**Status:** Approved by the operator on 2026-08-16

## Objective

Retire the current MiMo v2 implementation after its fixed 12-message isolated
replay failed the required execution-safety gate. Restore the runtime and test
tree to the last commit before MiMo v2 was designed, preserve the production
database and its v1 audit history, record the old and proposed safety-gate
designs, and deploy only through the existing fail-closed deployment preflight.

## Exact Boundaries

- Production baseline: `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`.
- Pre-MiMo-v2 runtime baseline:
  `354c82c8f657c6b1bf0a5b8aec0c7229aec9dd98`.
- The next commit, `a5260e52314646d627bda625aeb8341b1370ae45`,
  began the MiMo v2 design.
- Build an auditable forward rollback commit on top of the production baseline;
  do not reset, rewrite history, or deploy an old detached commit.
- Restore source, tests, templates, static assets, runbook, and prior MiMo-v2
  plans to the pre-v2 tree. Keep this retirement design and its implementation
  plan as the only post-baseline documentation.
- Do not enable MiMo v2, change its activation watermark, replay messages,
  modify exchange state, repair Batch119, or weaken deployment preflight.

## Database Decision

Production already contains the MiMo-era audit schema. A read-only check at
`2026-08-17T01:03:54Z` observed:

- `mimo_recognition_runs`: 692 rows;
- `mimo_recognition_attempts`: 692 rows;
- `mimo_contract_circuit_state`: 0 rows;
- `message_evidence_versions.mimo_recognition_run_id`: 681 non-null links.

Every recorded run is `v1_authoritative`; there are no production v2 runs.
The references are internally complete, with zero orphaned evidence or attempt
links. The row totals grow while the current v1 service records new audits, so
they are observations rather than deployment constants. Deployment captures a
fresh baseline and verifies no loss, no non-v1 run, and no orphaned reference.
The retirement therefore performs no database downgrade. It does not drop
tables, delete rows, clear foreign keys, edit settings, or remove the watermark.
The pre-v2 code ignores the additive legacy schema safely.

The tables are recorded as retired legacy schema. A future recognition redesign
must use a new contract and an explicitly versioned migration; it must not
silently interpret these rows as proof that MiMo v2 ran in production.

## Rollback Architecture

The retirement is a source rollback, not a data rollback:

1. Start from the exact production commit in an isolated worktree.
2. Add a failing retirement-boundary test that detects MiMo v2 modules,
   activation settings, CLI commands, authority routing, and Web controls.
3. Restore the repository tree from the exact pre-v2 commit.
4. Re-add the retirement design, implementation plan, and boundary test.
5. Prove the runtime diff against the pre-v2 commit contains documentation and
   the retirement test only.
6. Run focused boundary/configuration tests, compile checks, and the full suite.
7. Obtain an independent Critical/Important review before push.
8. Push the exact reviewed branch and deploy that exact SHA only if the existing
   deployment preflight returns an allowed decision.

The server deployment may remain blocked by existing active-work evidence. An
operator approval does not override a `BLOCK` or malformed preflight artifact.
If blocked, leave production unchanged and report the reason.

## Safety-Gate History

### Legacy deterministic deployment preflight

The old gate replaced an informal deployment check with a bounded preflight. It
classifies `code`, `schema_compatible`, `execution_writer`, and
`live_promotion` changes, reads durable local state, consumes bounded exchange
snapshots for sensitive changes, emits a short-lived fingerprinted artifact,
and refuses malformed or incomplete input.

Its useful invariants remain authoritative:

- fail closed on fresh writers and unknown execution outcomes;
- separate change classes and require stronger evidence for writer changes;
- never include raw messages, order payloads, position identifiers, or secrets;
- use an exact expected commit and expiring artifact;
- require migration dry-runs on a disposable backup for schema changes.

### Legacy defects

The main liveness defect is that recent `updated_at` was treated as evidence of
active progress. A stuck reconciler can refresh a row without changing durable
authority, making historical residue look permanently active and blocking every
deployment window.

The broader legacy monitor also has these defects:

- systemd execution success and observed production health share one exit code;
- exchange cache freshness can depend on startup or UI-driven refreshes;
- producer and receiver keep separate adapter/reason allowlists that can drift;
- fixed five-, ten-, and fifteen-minute thresholds are not consistently bound
  to each operation's durable deadline or last real progress;
- one temporally incoherent observation can be promoted too early;
- normal notification ownership is split between the monitor and Runtime
  Incident system.

### Proposed replacement design

The later design separates a read-only snapshot refresher, deterministic
sentinel, versioned incident bridge, durable Runtime Incident ledger, and one
normal notification owner. It introduces:

- independent `execution_status` and `observed_health`;
- sealed snapshot generations from a proven read-only credential boundary;
- a shared producer/receiver schema authority;
- per-operation deadlines and `last_progress_at` semantics;
- `STARTING`, `SETTLING`, `CONFIRMED`, `UNKNOWN`, and `RESOLVED` states;
- temporal ordering and normally two distinct complete snapshots before a
  cross-system mismatch is confirmed;
- structured sentinel evidence instead of systemd unit color as deployment
  health.

Those ideas are retained for a future clean redesign, but the current
implementation branch is not reusable as-is. It is built on the Batch119 and
Deepcoin recovery lineage, touches a large operational surface, and has not
passed an independent production rollout. No part of that branch is deployed
by this retirement.

## Verification

Local verification must prove:

- the retirement boundary test is RED before the rollback and GREEN afterward;
- the runtime/source tree matches the exact pre-v2 baseline;
- the additive production schema remains readable by the pre-v2 code on a
  disposable database copy;
- focused tests, compile checks, and the full suite pass;
- no Critical or Important review finding remains;
- the worktree is clean at the reviewed SHA.

Server verification must prove:

- the fetched branch resolves to the exact reviewed SHA;
- deployment preflight allows the `schema_compatible` rollback;
- database backup and migration checks pass without destructive downgrade;
- the production service returns active and healthy after restart;
- the production SHA is the exact retirement SHA;
- MiMo v1 remains the only runtime recognition path;
- fresh audit invariants show no lost rows, non-v1 run, or orphaned reference,
  and the retired audit counts stop growing after the rollback;
- no notification, historical replay, or exchange write is used as a test.

## Rollback Of This Retirement

If the old runtime fails before any exchange request can occur, restore the
previous reviewed Git SHA through the same deployment helper and preflight. Do
not restore an older database after a request might have reached the exchange.
The retirement itself performs no exchange request and no database mutation.
