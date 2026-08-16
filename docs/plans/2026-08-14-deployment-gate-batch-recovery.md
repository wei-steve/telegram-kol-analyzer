# Deployment Gate and Management-Batch Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the false-active Batch 119 state from exact evidence, classify batches 123/127/129 independently, restore a truthful deployment window, and leave production on reviewed recovery code with MiMo v2 dormant.

**Architecture:** Use the existing allowlisted Batch 119 recovery authority at the reviewed candidate lineage rather than weakening the global preflight. Separate local review, stopped-service diagnostic capture, recovery apply, and ordinary code deployment into independently approved windows. Treat the post-recovery production SHA as the only valid base for resuming MiMo work.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite, Typer CLI, pytest, systemd, Git worktrees, SSH, Deepcoin read-only exact-history APIs.

---

## Execution Rules

- Work in `/Users/steven/Documents/telegram获取消息-deployment-gate-recovery-plan` on branch `codex/deployment-gate-batch-recovery-plan`.
- Read `AGENTS.md` before every production operation.
- Read these authoritative designs and plans before running candidate code:
  - `docs/plans/2026-08-14-deployment-gate-batch-recovery-design.md`
  - `docs/plans/2026-08-12-composite-management-batch-119-recovery-design.md`
  - `docs/plans/2026-08-12-composite-management-batch-119-recovery.md`
  - `docs/plans/2026-08-12-batch-119-instruction-disposition-gate-design.md`
  - `docs/plans/2026-08-12-batch-119-instruction-disposition-gate.md`
  - `docs/plans/2026-08-13-batch119-exact-history-recovery-design.md`
  - `docs/plans/2026-08-13-batch119-exact-history-recovery.md`
  - `docs/plans/2026-08-14-batch119-native-backup-role-recovery-design.md`
  - `docs/plans/2026-08-14-batch119-native-backup-role-recovery.md`
  - `docs/runbook.md`, section `Batch 119 composite-management recovery`
- Never reset, force-push, overwrite, delete, or hand-edit production state.
- Never weaken `deployment_preflight.py` to ignore Batch 119.
- Never replay the historical Telegram message.
- Never treat age, `updated_at`, or a raw `submitted`/`unknown` label as proof of terminality.
- Keep `mimo_contract_mode=v1` and preserve the activation watermark.
- A diagnostic approval does not authorize apply. An apply approval does not authorize ordinary deployment. A deployment approval does not authorize MiMo activation.
- Batch 119 recovery and Deepcoin request-governance Stage 1 rollout must not share one quiet window.
- Stop immediately on incomplete pagination, identity drift, unknown exchange outcome, unprotected exposure, cleanup failure, or a production fact that differs from this plan.

## Bound-close-reservation prerequisite discovered during execution

The first approved Batch 119 apply window stopped before backup or mutation
because the unchanged deployment gate found a separate population of nonterminal
`bound_position_close_reservations`. That fact must not be bypassed inside this
plan. Before resuming Task 5, execute the independently reviewed
`docs/plans/2026-08-15-bound-position-close-reservation-convergence.md` on
`codex/bound-close-reservation-recovery`.

Its stopped-service read-only double capture and its later apply are two new,
separately authorized windows. After its apply and query-only postchecks, restore
the original unit states and explicitly stop before returning here. Do not reuse
its approval, capture, fingerprint, confirmation token, backup, or quiet window
for Batch 119. The exact return chain is:

```text
reservation recovery -> Batch119 apply -> stable snapshot -> ordinary preflight
-> deploy exact c50887b -> Phase One canary/cutover
```

The approved Phase One target remains
`c50887b991712340d7d5606fb6916cdbb033926e`; MiMo remains `v1` throughout.

### Task 1: Establish the exact local and remote baseline

**Files:**
- Review: `AGENTS.md`
- Review: the documents listed in `Execution Rules`
- Modify only if evidence requires correction: `docs/plans/2026-08-14-deployment-gate-batch-recovery-design.md`

**Step 1: Confirm the planning worktree is clean**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git log --oneline --decorate -8
git worktree list --porcelain
```

Expected: branch `codex/deployment-gate-batch-recovery-plan`; no uncommitted files; the branch contains candidate `b27541f` plus documentation commits only.

**Step 2: Fetch without changing any branch**

```bash
git fetch --prune origin
git rev-parse origin/codex/deepcoin-auto-trading-v1
git rev-parse origin/codex/mimo-v2-fast-deploy
git merge-base 2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa b27541f459ab89a18cb617e434f41b962d72b339
```

Expected:

- canonical remote recovery candidate is still `b27541f459ab89a18cb617e434f41b962d72b339`, or any drift is reviewed before continuing;
- MiMo-only remote remains `5702c343a46c89811edc082650330d4eacf39a8f`;
- the merge base with production is `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`.

**Step 3: Prove the candidate does not enable dormant modes**

Run focused settings inspection and tests:

```bash
rg -n 'mimo_contract_mode|protected_entry|request_govern|activation_after' \
  src/telegram_kol_research/trading_settings.py \
  tests/test_trading_settings.py docs/runbook.md
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py \
  tests/test_web_app.py -k 'mimo or protected_entry or request_govern'
```

Expected: MiMo defaults to `v1`; all new writer paths are dormant or guarded; focused tests pass.

**Step 4: Record the candidate scope without rewriting history**

```bash
git diff --stat 2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa..b27541f459ab89a18cb617e434f41b962d72b339
git log --format='%h %s' --reverse \
  2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa..b27541f459ab89a18cb617e434f41b962d72b339
```

Expected: 116 candidate commits are explicitly reviewed as MiMo safe rebuild, Batch 119 recovery, and Deepcoin safety/governance work. Do not squash or rebase them.

### Task 2: Verify the existing recovery implementation locally

**Files:**
- Review: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Review: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Review: `src/telegram_kol_research/deployment_preflight.py`
- Test: `tests/test_composite_management_batch_recovery.py`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_deployment_preflight.py`
- Test: Deepcoin authority suites named below

**Step 1: Run the regression proving exclusive state-machine ownership**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_strategy_management_reconciliation.py \
  -k 'composite or legacy'
```

Expected: composite batches cannot be mutated by legacy reconciliation; traditional batches remain unchanged.

**Step 2: Run the full Batch 119 recovery suite**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_composite_management_batch_recovery.py \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_cli_smoke.py -k 'batch119 or composite_management_batch or recover_composite'
```

Expected: PASS, including native backup-role resolution, exact-history bounds, natural-stop proof, locked CAS, repeat/apply behavior, redaction, and zero-writer absent-position paths.

**Step 3: Run the writer-authority regressions**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deepcoin_request_policy.py \
  tests/test_deepcoin_request_governor.py \
  tests/test_deepcoin_execution_operations.py \
  tests/test_deepcoin_snapshot_authority.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protected_entry_execution.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_deployment_preflight.py
```

Expected: PASS with no network use.

**Step 4: Run repository-wide verification**

```bash
git diff --check
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m compileall -q src tests scripts
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q
```

Expected: all commands succeed. Do not convert environmental or logic failures into skips.

**Step 5: Review before any push**

Review `2274d90..HEAD` for correctness, security, recovery isolation, no credential serialization, no accidental live default, and missing tests. Resolve every Critical or Important finding before continuing. Commit review-driven changes separately and repeat Steps 1–4.

The 2026-08-14 local review identified five Important findings. Resolve them
under TDD before Task 3:

1. Add `deepcoin_execution_operations` to the deterministic deployment
   preflight. Treat every state except `pre_submit_deferred`, `completed`, and
   `submission_failed_no_exposure` as active; classify `entry_unknown`,
   `protection_unknown`, and `recovery_required` as unknown. Add the exact
   candidate-table prior-schema shape without accepting arbitrary missing
   tables.
2. Add a failing runbook regression proving the diagnostic writer query handles
   the known pre-candidate absence of `deepcoin_execution_operations`, and that
   `entry_confirmed` is not accepted as terminal.
3. Add a failing recovery regression proving a stopped-service capture can run
   when `deepcoin_account_write_generations` is absent, but only with the exact
   batch-119 stopped-capture authorization, identical resolved database paths,
   bounded authority evidence, and no generic snapshot change.
4. Add a failing runbook regression proving the diagnostic fetches
   `codex/deployment-gate-batch-recovery-plan` before validating its exact
   remote-tracking ref.
5. Update the apply commands and runbook to require
   `I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE`; do not bootstrap the
   production database.

Run each new test before implementation and observe the intended failure. After
GREEN, rerun Steps 1–4 and repeat the complete review until there are zero
Critical and zero Important findings.

### Task 3: Push an independent reviewed recovery candidate

**Files:**
- No code changes expected

**Step 1: Confirm the production and MiMo branches will not be modified**

```bash
git status --short --branch
git branch --show-current
git rev-parse origin/codex/deepcoin-auto-trading-v1
git rev-parse origin/codex/mimo-v2-fast-deploy
```

Expected: current branch is `codex/deployment-gate-batch-recovery-plan`.

**Step 2: Push only the independent recovery branch**

```bash
git push --set-upstream origin codex/deployment-gate-batch-recovery-plan
git rev-parse HEAD
git rev-parse origin/codex/deployment-gate-batch-recovery-plan
```

Expected: local and remote full SHAs match. Do not push or force-update either existing production branch.

**Step 3: Stop and request diagnostic-window approval**

Report the exact reviewed SHA, test counts, review result, and the fact that the next action stops all listed database/writer units for a read-only double capture. Do not continue in the same turn without explicit approval.

### Task 4: Capture a stopped-service Batch 119 diagnostic twice

**Files:**
- Follow exactly: `docs/runbook.md`, section `当前操作停点：另行审批的停服双 dry-run`
- Use: `scripts/compare_batch119_dry_runs.py`

**Step 1: Reconfirm the approval and immutable inputs**

Require an explicit approval scoped to:

```text
I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_READ_ONLY_DOUBLE_CAPTURE
```

Record the reviewed candidate full SHA. Confirm production is still at the expected pre-recovery SHA and MiMo is still `v1`.

**Step 2: Execute the runbook block verbatim with only these substitutions**

- `REVIEWED_SHA=<full SHA from Task 3>`
- `APPROVED_REF=refs/remotes/origin/codex/deployment-gate-batch-recovery-plan`
- environment `BATCH119_STOP_APPROVAL=I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_READ_ONLY_DOUBLE_CAPTURE`

Do not add `--apply`. Do not point candidate bootstrap at the production database. Do not omit any unit from `QUIESCE_UNITS`.

Expected:

- every listed unit becomes exactly inactive before backup;
- no local Telegram/Deepcoin writer process remains;
- durable active/unknown writer count is zero under the runbook predicate;
- two private database copies pass `PRAGMA quick_check`;
- both dry runs report `status=ready`, `production_writes=0`, and `exchange_calls=0`;
- the comparator prints exactly `{"status":"stable"}`;
- cleanup restores every original unit state and leaves production SHA unchanged.

**Step 3: Stop on any refusal**

If either plan is refused, the two plans differ, or cleanup is imperfect, return the bounded reason code. Do not apply, deploy, bootstrap production, or change settings.

**Step 4: Present only the redacted decision facts**

Report position disposition, bounded evidence counts, source fingerprint, exchange snapshot fingerprint, evidence fingerprint, zero counters, service restoration, and production SHA. Never report raw IDs, order payloads, source text, credentials, or provider responses.

**Step 5: Stop and request a separate apply approval**

The diagnostic fingerprint is evidence for review only. It cannot be reused as the future apply fingerprint.

### Task 5: Apply the exact Batch 119 recovery in a new window

**Files:**
- Follow: `docs/runbook.md`, Batch 119 apply boundary
- Follow: `docs/plans/2026-08-12-composite-management-batch-119-recovery.md`, controlled production recovery task

**Step 1: Require separate explicit apply authorization**

Do not infer authorization from Task 4. The operator must approve a new stopped-service recovery window and the fixed CLI authorization:

```text
I_AUTHORIZE_BATCH_119_TO_REMAINING_19
I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE
```

**Step 2: Stop all database/writer units and create a verified backup**

Use the same exact unit inventory and cleanup trap as Task 4. After all units are inactive and no writer process remains:

```bash
sqlite3 /opt/telegram-kol-analyzer/data/research.db \
  ".backup '/opt/telegram-kol-analyzer/data/backups/research-before-batch119-apply-<UTC>.db'"
chmod 0600 /opt/telegram-kol-analyzer/data/backups/research-before-batch119-apply-<UTC>.db
sqlite3 -readonly /opt/telegram-kol-analyzer/data/backups/research-before-batch119-apply-<UTC>.db \
  'PRAGMA quick_check;'
```

Expected: `ok`.

**Step 3: Generate a new final capture against the production database**

Run the candidate command with both database paths resolving to the production database and without `--apply`. Require a new `status=ready` result. Review its new fingerprint; do not reuse Task 4 output.

```bash
PYTHONPATH="$CANDIDATE_ROOT/src" "$RUNTIME_PYTHON" -m \
  telegram_kol_research.cli recover-composite-management-batch \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --generation-database-path /opt/telegram-kol-analyzer/data/research.db \
  --batch-id 119 \
  --stopped-service-capture-authorization \
    I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE \
  --deepcoin-contract-specs-path \
    /opt/telegram-kol-analyzer/config/deepcoin_contract_specs.yaml
```

Expected for the previously observed incident: `position_absent`, zero dry-run writes, zero exchange calls, and exact owned natural-stop proof. If the disposition differs, stop for a new review.

**Step 4: Apply only the freshly reviewed fingerprint**

```bash
PYTHONPATH="$CANDIDATE_ROOT/src" "$RUNTIME_PYTHON" -m \
  telegram_kol_research.cli recover-composite-management-batch \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --generation-database-path /opt/telegram-kol-analyzer/data/research.db \
  --batch-id 119 \
  --stopped-service-capture-authorization \
    I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE \
  --deepcoin-contract-specs-path \
    /opt/telegram-kol-analyzer/config/deepcoin_contract_specs.yaml \
  --apply \
  --expected-fingerprint '<fresh-evidence-fingerprint>' \
  --authorization I_AUTHORIZE_BATCH_119_TO_REMAINING_19
```

Expected for `position_absent`: local audited terminalization only; no exchange writer construction and no exchange call.

**Step 5: Verify before restarting**

Use query-only SQLite to confirm:

- Batch 119 and leg 103 reached the exact expected terminal state;
- every component has the expected terminal disposition;
- one immutable recovery execution event binds the applied fingerprint;
- no new position mutation intent, close reservation, order ID, or provider response was created;
- batches 123, 127, and 129 were unchanged;
- `mimo_contract_mode` remains `v1`.

**Step 6: Restore original service states and verify health**

Expected: production checkout SHA is unchanged, `telegram-kol.service` is active if it was initially active, and the Batch 119 row no longer receives reconciliation timestamp updates.

**Step 7: Stop**

Do not deploy code in this recovery window.

### Task 6: Audit batches 123, 127, and 129 independently

**Files:**
- Create only if needed: `docs/runtime-management-batch-recovery-status.md`
- Do not modify production rows in this task

**Step 1: Capture bounded read-only facts for each batch**

For each batch separately, collect:

- batch/leg/component status and bounded reason code;
- lifecycle and execution-binding status;
- durable mutation intents and close reservations;
- exact protection ownership and current position presence;
- whether any writer can still claim the work;
- last actual progress time separately from `updated_at`.

Do not emit raw `posId`, order IDs, request/response JSON, Telegram text, or credentials.

**Step 2: Classify each batch**

Allowed conclusions are:

- `historical_frozen_no_writer`;
- `protected_recovery_required`;
- `active_or_unknown_writer`;
- `identity_conflict`;
- `evidence_incomplete`.

Only `historical_frozen_no_writer` and `protected_recovery_required` may remain nonblocking warnings. Every other conclusion blocks the release and requires a separate approved design.

**Step 3: Verify no Batch 119 authority leaked**

Prove the Batch 119 allowlist, fingerprints, and apply path cannot accept 123, 127, or 129.

**Step 4: Commit only redacted documentation if state needs a durable handoff**

```bash
git add docs/runtime-management-batch-recovery-status.md
git commit -m "docs: record management batch recovery status"
```

Do not commit the file if it would contain operational identifiers or sensitive evidence.

### Task 7: Establish a normal deployment window

**Files:**
- Review: `deploy/telegram-kol-update`
- Review: `src/telegram_kol_research/deployment_preflight.py`

**Step 1: Create two independent read-only Deepcoin captures**

Use the repository's reviewed snapshot mechanism. The two captures must have different versions and capture times and identical canonical account facts. Do not satisfy stability by reading one cache twice.

Expected:

- available: true;
- complete: true;
- fresh: true;
- stable: true;
- unprotected open positions: zero.

**Step 2: Run a candidate deployment preflight without checking out code**

Use the candidate CLI from a detached worktree and `CHANGE_CLASS=schema_compatible`. Produce a mode-0600 artifact under `/run/telegram-kol`.

Expected:

- `fresh_active_work={}`;
- no unprotected open positions;
- schema backup valid;
- schema migration dry-run valid;
- decision `PASS` or reviewed warning-only `WARN`;
- batches 123/127/129 appear only through their reviewed historical classification, if at all.

**Step 3: Stop on any BLOCK**

Do not edit the artifact or retry by changing timestamps/statuses. Return the exact bounded reason codes.

**Step 4: Request a separate ordinary deployment approval**

Report candidate SHA, preflight fingerprint, warnings, snapshot versions, database watermark, and the fact that MiMo and all writer features remain dormant.

### Task 8: Deploy reviewed recovery code in a separate quiet window

**Files:**
- Use: `scripts/server_git_update.sh`

**Step 1: Reconfirm branch tip**

```bash
git fetch origin codex/deployment-gate-batch-recovery-plan
EXPECTED_COMMIT="$(git rev-parse origin/codex/deployment-gate-batch-recovery-plan)"
test "$EXPECTED_COMMIT" = "$(git rev-parse HEAD)"
```

**Step 2: Deploy through the deterministic helper**

```bash
EXPECTED_COMMIT="$EXPECTED_COMMIT" \
BRANCH=codex/deployment-gate-batch-recovery-plan \
CHANGE_CLASS=schema_compatible \
./scripts/server_git_update.sh
```

Expected: both preflights pass or return reviewed warning-only results; the helper stops the service only after the preliminary gate; schema backup and disposable migration succeed; checkout fast-forwards to the exact SHA; editable install succeeds; service returns active.

**Step 3: Verify post-deployment invariants**

Confirm:

```bash
systemctl is-active telegram-kol.service
git -C /opt/telegram-kol-analyzer rev-parse HEAD
curl -fsS http://127.0.0.1:8000/api/trading-settings
```

Parse the settings without printing secrets. Require:

- exact deployed SHA;
- `mimo_contract_mode=v1`;
- MiMo activation watermark unchanged;
- Deepcoin protected-entry/request-governance live modes not enabled;
- Batch 119 remains terminal;
- no new fresh active work;
- all open positions remain protected.

**Step 4: Record the production handoff**

Update the appropriate redacted status/handoff document with deployed SHA, Batch 119 terminal evidence fingerprint, 123/127/129 classifications, preflight decision, snapshot versions, and MiMo dormant state. Commit and push only to the independent recovery branch.

### Task 9: Prepare the MiMo return point

**Files:**
- Update: `docs/migration-handoff.md` or the dedicated redacted recovery status file

**Step 1: Retire the old deployment assumption**

Record that `5702c343a46c89811edc082650330d4eacf39a8f` must not be deployed over the repaired production tree.

**Step 2: Define the new MiMo base**

The final deployed recovery SHA is the base for all later MiMo work. If that SHA already contains the reviewed safe MiMo rebuild, the next MiMo session performs only:

1. focused/full local verification against the deployed SHA;
2. server isolated replay while mode remains `v1`;
3. artifact review;
4. a later, separately authorized future-watermark activation.

Do not replay historical messages through the listener and do not enable v2 in this plan.

**Step 3: Final handoff**

Report:

- independent branch and exact deployed SHA;
- local and server verification results;
- Batch 119 final state;
- independent classifications for 123/127/129;
- stable Deepcoin snapshot proof;
- current MiMo mode and watermark;
- exact next MiMo command boundary.

The recovery task is complete only when ordinary deployment preflight no longer reports false fresh active work and production remains healthy with MiMo dormant.
