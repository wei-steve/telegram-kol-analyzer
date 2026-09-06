# Minimal Deployment Gate Design

## Context

The production deployment gate was introduced on 2026-08-11 and expanded on
2026-08-16/17 into a two-phase authorization system. The current implementation
builds and verifies preliminary/final artifacts, fingerprints the writer
surface, classifies historical rows across many execution tables, freezes Git
blobs and modes in boundary tests, and maintains AST-based mutation call-site
inventories.

That system is disproportionate for this repository. One operator controls the
code and every deployment is tied to an explicitly reviewed commit. The broad
historical classifier has repeatedly blocked useful deployments when a valid
producer state was missing from the gate vocabulary. Each such false block has
required diagnosis, new adapters, tests, review, shadow verification, and more
approvals without improving the deployed feature.

The deployment path still needs to protect the small number of failures that
can cause direct operational harm: replacing code during an exchange mutation,
deploying an unintended commit, applying an unverified schema change, or
leaving the service down after an install failure.

## Goals

- Replace the current gate with a small, readable deployment check.
- Prevent code replacement while an exchange write is durably in progress.
- Check for active exchange writes once before stopping the service and once
  after systemd proves the service inactive.
- Keep exact-commit, clean-tree, schema backup/dry-run, health, and rollback
  protections.
- Make historical unknown, queued, paused, and recovery records informational
  only; they never affect deployment authorization.
- Reduce the normal deployment workflow to one approval for one exact SHA.
- Delete fingerprint, artifact, AST inventory, and frozen-blob machinery rather
  than retaining it behind ignored results.

## Non-goals

- No database history repair, reconciliation, replay, or manual row edits.
- No automatic determination that a feature deployment is meaningful.
- No writer-surface, restart-surface, evidence, artifact, row, count, timestamp,
  or Git-blob fingerprint.
- No preliminary/final authorization artifacts or parent binding.
- No general database consistency audit during deployment.
- No ordinary mandatory shadow deployment.
- No change to trading execution policy, MiMo v1 authority, or exchange APIs.

## Considered Approaches

### 1. Dedicated active-write checker (selected)

Create one read-only checker that counts only durable states representing a
request currently entering or leaving the exchange. Run it before and after
the service stop. This retains the one deployment-specific database invariant
that matters while deleting the historical classification system.

### 2. Keep the existing collector but ignore most results

This is a smaller diff, but it leaves the thousand-line adapter registry,
status vocabulary, fingerprints, and most tests in place. It preserves the
maintenance and token cost and is therefore rejected.

### 3. Remove all database checks

Relying only on `systemctl stop` is simpler, but the process could be stopped
between a durable pre-submit transition and the corresponding exchange result.
The small active-write check is worth retaining, so this option is rejected.

## Active-write Check

The new module is
`src/telegram_kol_research/deployment_active_write_check.py`. It opens SQLite
with `mode=ro`, enables `PRAGMA query_only=ON`, starts one read transaction,
and returns one bounded integer: `active_write_count`.

It uses direct indexed `COUNT(*)` queries. It does not load rows, inspect
payloads, validate relationships, classify terminal states, derive unknown
outcomes, or produce a fingerprint. The initial query set preserves only the
current durable pre-submit/submit authorities:

| Table | Active state |
|---|---|
| `position_backup_stop_orders` | `status = 'submitting'` |
| `execution_order_legs` | `status IN ('submitting', 'cancel_submitting')` |
| `instruction_execution_contracts` | `state = 'submitting'` |
| `strategy_management_components` | `status IN ('submitting', 'cancel_submitting')` |
| `strategy_management_batches` | `status = 'executing'` |
| `strategy_revision_batches` | `status = 'submitting_replacements'` |
| `strategy_revision_legs` | `status = 'cancel_submitting'` with a non-empty paired batch claim |
| `entry_revision_replacements` | `status = 'submit_reserved'` with a non-empty paired batch claim |
| `trigger_protection_intents` | `recovery_state IN ('submitting', 'cancel_submitting')` |
| `position_mutation_intents` | `status IN ('submitting', 'cancel_submitting')` |
| `trade_signals` | `status IN ('processing', 'submitting', 'cancel_submitting')` |

Double-counting across tables is acceptable because authorization depends only
on zero versus non-zero. A missing required table/column, unreadable database,
SQLite error, non-integer/negative result, or inability to prove query-only mode
is a gate error and stops deployment. An unfamiliar status is ignored: the
checker is a positive list of active states, not a complete state vocabulary.

The CLI prints only:

```text
active_write_count=N
```

Exit codes are `0` for zero, `3` for non-zero, and `4` for checker/input error.
It writes no artifact and includes no row identity or payload in output.

## Deployment Flow

The updater keeps a single deployment lock and follows this order:

1. Fetch the requested branch and require `FETCH_HEAD == EXPECTED_COMMIT`.
2. Require the production checkout to be attached to the deployment branch,
   at the recorded old commit, with a clean tracked worktree.
3. Create a detached mode-0700 worktree for the exact candidate.
4. Detect schema changes using only `git diff --quiet` on:
   - `src/telegram_kol_research/models.py`
   - `src/telegram_kol_research/db.py`
   - any repository migration directory if one is later introduced.
5. If those paths changed, create an online SQLite backup, run backup
   `quick_check`, migrate a disposable copy with candidate code, run its
   `quick_check`, and compare raw/execution watermarks. Otherwise skip all
   schema work.
6. Run the candidate active-write checker while the old service is active. If
   non-zero or invalid, exit without stopping or changing production.
7. Mark the stop as attempted, stop `telegram-kol.service`, and wait until
   systemd reports exactly `inactive` or `failed`.
8. Run the same candidate active-write checker again. Because the sole writer
   is now inactive, no new active write can begin. If non-zero or invalid,
   restart and verify the unchanged old service, then exit without checkout.
9. Fast-forward the production branch to the exact candidate, install the
   editable package, start the service, require active state, and require a
   successful local HTTP health request.
10. Install the reviewed durable updater last.
11. Remove the temporary worktree and disposable migration copy.

There is no Phase A/Phase B terminology. The two checks are ordinary zero-count
checks and do not bind or compare artifacts.

## Rollback and Error Handling

Before stopping the service, the updater records the old commit and verifies it
is a usable rollback point. After any stop attempt, the EXIT trap is responsible
for leaving the old service active unless the candidate has passed health and
the deployment has been finalized.

If checkout, install, start, health, or durable-updater installation fails, the
updater must:

1. stop the candidate and prove it inactive;
2. restore the old commit and branch ref;
3. reinstall the old editable package;
4. restore the prior durable updater;
5. start and verify the old service;
6. return non-zero.

If rollback itself fails, return exit code 4 and report a hard rollback failure.
Do not continue mutating files or claim that the service recovered.

## Schema Handling

Schema detection is path-based, not fingerprint-based. The operator does not
declare a change class. A changed model/database/migration path automatically
enables backup and dry-run requirements. All other changes skip schema work.

The existing online backup and disposable migration logic is retained because
it protects the production database with little routine cost. No backup is
created for ordinary code or documentation deployments.

## Removed Components

Delete the following runtime modules after the replacement checker and updater
tests are green:

- `src/telegram_kol_research/deployment_preflight.py`
- `src/telegram_kol_research/deployment_preflight_cli.py`
- `src/telegram_kol_research/deployment_work_evidence.py`
- `src/telegram_kol_research/deployment_writer_surface.py`

Remove their Typer imports/commands from `src/telegram_kol_research/cli.py` and
delete the corresponding artifact, surface, evidence, and boundary tests.
Retain updater fault tests, but rewrite them around the short flow instead of
artifact/fingerprint behavior.

Historical design documents remain as history and must be labeled superseded;
they are not rewritten to pretend the earlier system never existed.

## Approval Model

For ordinary deployments:

1. finish local tests and independent review;
2. report the exact clean SHA and material change summary;
3. receive one explicit approval;
4. push and deploy that exact SHA with the helper;
5. report production SHA and health.

There is no separate push, shadow, and deployment approval sequence. A separate
shadow remains an optional feature-specific verification tool for a genuinely
high-risk trading or migration change, not part of the deployment gate.

The first rollout is special because the bootstrap helper extracts and runs the
updater from the candidate SHA. The installed old updater does not authorize
that transition. Therefore the candidate minimal updater, checker, bootstrap
hash verification, rollback harness, and server-side focused tests must all
pass before the first deployment. The transition must not claim protection
from the deleted two-phase gate.

## Testing Strategy

Keep tests proportional to the small design:

- each listed active state makes the checker return exit 3;
- representative historical unknown, queued, terminal, and recovery rows do
  not affect the zero result;
- missing schema, unreadable database, and SQL failure return exit 4;
- pre-stop non-zero means zero stop/checkout/install operations;
- an active row appearing between checks aborts after stop and restarts the old
  service without checkout;
- schema paths trigger backup/dry-run and ordinary paths do not;
- stop timeout, delayed stop completion, TERM/INT, checkout, install, start,
  health, and durable-updater failures exercise rollback;
- exact SHA, clean tree, branch rollback point, and remote race checks remain;
- the deleted modules and retired artifact CLI are no longer importable or
  callable.

Do not add tests for artifact re-signing, parent chains, writer fingerprints,
AST aliases, fixed counts/timestamps, or frozen blob/mode entries. Those
features no longer exist.

## Acceptance Criteria

- Normal code deployment performs no hash/fingerprint work beyond Git's normal
  exact commit verification.
- Exactly two read-only active-write checks run, one before and one after stop.
- Historical unknown/queued/recovery evidence cannot block deployment.
- A non-zero active count cannot reach checkout or install.
- Schema backup/dry-run runs only for schema paths.
- Failure after stop either restores and verifies the old service or returns a
  hard rollback failure.
- The production service passes local HTTP health at the deployed exact SHA.
- The old gate modules, artifact CLI, fingerprints, registries, and boundary
  freezes are absent from runtime and tests.
- Operator documentation describes one approval and the short deployment flow.
