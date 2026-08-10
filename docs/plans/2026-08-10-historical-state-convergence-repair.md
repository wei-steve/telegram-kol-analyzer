# Historical State Convergence Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop future deletion-exit and take-profit convergence rows from becoming permanently dirty, then safely terminalize the proven historical rows without any Deepcoin writes or audit-row deletion.

**Architecture:** Correct the runtime state machines at their decision boundaries, and add a separate dry-run-first repair planner whose output is bound to database and exchange snapshot fingerprints. Applying a plan requires an exact fingerprint, action count, and derived one-time confirmation token; it runs only local database transitions, records a non-notifying audit summary, and refuses all ambiguous or live exchange identities.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, Deepcoin read-only REST adapters, systemd production deployment.

---

### Task 1: Make source-deletion terminal transitions portable and observable

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/source_message_deletion_worker.py`
- Test: `tests/test_source_message_deletion_worker.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Write the failing partial-index transition test**

Create a compatibility-schema database whose `execution_events.notification_fingerprint` uniqueness is provided by the production partial unique index. Insert and process a deletion exit whose transition enqueues an outcome event, then assert the transition commits and a duplicate notification fingerprint is ignored.

```python
def test_outcome_transition_supports_production_partial_notification_index(...):
    create_partial_notification_index_database(database_path)
    result = run_source_message_deletion_worker_tick(...)
    assert result.finalized == 1
    assert load_exit(session).state == "succeeded"
```

**Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion_worker.py -k partial_notification_index -q`

Expected: FAIL with SQLite `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint`.

**Step 3: Make conflict handling index-shape independent**

Change outcome insertion from a column-targeted conflict clause to targetless `on_conflict_do_nothing()`. Define the ORM index with the same `notification_fingerprint IS NOT NULL` predicate as the compatibility migration so fresh and upgraded databases share the same schema contract.

**Step 4: Run the focused transition and bootstrap tests**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion_worker.py -k partial_notification_index -q && .venv/bin/python -m pytest tests/test_db_bootstrap.py -k notification_fingerprint -q`

Expected: PASS.

**Step 5: Write the failing loop-observability test**

Run the async loop with a worker that raises once and a cancelled sleep boundary. Capture logs and assert the exception and traceback are emitted while `CancelledError` still propagates.

**Step 6: Run the loop test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion_worker.py -k logs_worker_loop_exception -q`

Expected: FAIL because the current loop silently discards the exception.

**Step 7: Add bounded exception logging**

Add a module logger and replace the silent `pass` with `logger.exception("source message deletion worker tick failed")`. Do not change retry timing or cancellation semantics.

**Step 8: Run Task 1 tests and commit**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion_worker.py tests/test_db_bootstrap.py -q`

Expected: PASS.

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/source_message_deletion_worker.py tests/test_source_message_deletion_worker.py tests/test_db_bootstrap.py
git commit -m "fix: make deletion outcome transitions portable"
```

### Task 2: Converge non-strategy and already-terminal deletion exits without exchange mutations

**Files:**
- Modify: `src/telegram_kol_research/source_message_deletion.py`
- Modify: `src/telegram_kol_research/source_message_deletion_worker.py`
- Test: `tests/test_source_message_deletion.py`
- Test: `tests/test_source_message_deletion_worker.py`

**Step 1: Write the failing non-strategy binding test**

Record deletion of an archived raw message with no lifecycle, binding, trade signal, or execution leg. Assert binding completes it immediately as:

```python
assert deletion_exit.state == "succeeded"
assert deletion_exit.reason_code == "non_strategy_or_unlinked"
assert event.processing_status == "ignored"
assert event.binding_state == "bound"
```

Also assert no exchange client factory and no notification event is used.

**Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion.py -k non_strategy_or_unlinked -q`

Expected: FAIL because the exit is currently set to `pending`.

**Step 3: Implement evidence-gated ignored convergence**

In `_bind_deletion_event_in_session`, after resolving lifecycle/binding, check for any durable trade or execution evidence. If none exists, set the immutable target identity, then terminalize the exit and source event with `non_strategy_or_unlinked`. Preserve the raw row and all timestamps; do not enqueue outcome notification work.

**Step 4: Run the focused test and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion.py -k non_strategy_or_unlinked -q`

Expected: PASS.

**Step 5: Write the failing already-terminal routing test**

Create an exited lifecycle, closed binding, and terminal exact entry legs. Run one worker tick with exchange mutation fakes that fail if called and a complete read-only snapshot. Assert it routes to reconciliation/finalization and never calls cancel or close.

**Step 6: Run the test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion_worker.py -k already_terminal_without_exchange_mutation -q`

Expected: FAIL because the current path enters `cancelling_entries`.

**Step 7: Add terminal-target routing**

At the claimed-job boundary, recognize only exact terminal lifecycle/binding/entry-leg identity and move directly to `reconciling`. Ambiguous or active identity keeps the existing mutation path.

**Step 8: Run Task 2 tests and commit**

Run: `.venv/bin/python -m pytest tests/test_source_message_deletion.py tests/test_source_message_deletion_worker.py tests/test_shuqin_deleted_repost_regression.py -q`

Expected: PASS.

```bash
git add src/telegram_kol_research/source_message_deletion.py src/telegram_kol_research/source_message_deletion_worker.py tests/test_source_message_deletion.py tests/test_source_message_deletion_worker.py
git commit -m "fix: converge terminal source deletion exits"
```

### Task 3: Preserve definite rejection semantics and terminalize historical TP ledgers

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/position_take_profit_orders.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_position_take_profit_orders.py`

**Step 1: Write the failing definite-rejection test**

Make `submit_exact_position_sltp` raise `DeepcoinDefiniteRejection`. Assert the convergence becomes `conflicted/convergence_submit_rejected`, while the existing timeout test remains `submit_unknown/convergence_submit_unknown`.

**Step 2: Run the rejection test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_trigger_take_profit_convergence_executor.py -k 'definite_rejection or submit_timeout' -q`

Expected: the definite-rejection assertion FAILS because all exceptions are mapped to unknown.

**Step 3: Implement exception-specific classification**

Catch `DeepcoinDefiniteRejection` before the generic exception handler and freeze as `convergence_submit_rejected`; preserve the current unknown path for all genuinely uncertain outcomes.

**Step 4: Run executor tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_trigger_take_profit_convergence_executor.py -k 'definite_rejection or submit_timeout' -q`

Expected: PASS.

**Step 5: Write failing terminal-binding and rejected-no-order reconciliation tests**

Cover three cases:

1. A terminal binding with cleared `binding.pos_id`, matching immutable leg/convergence `pos_id`, complete snapshots, no live position, and absent TP orders becomes completed/expired.
2. The same identity on an active binding remains blocked.
3. A `conflicted/convergence_submit_rejected` row with no TP orders becomes `completed/convergence_submit_rejected_position_terminal` only after complete position and pending snapshots prove the exact position absent.

**Step 6: Run reconciliation tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_position_take_profit_orders.py -k 'terminal_binding_cleared_pos_id or rejected_position_terminal' -q`

Expected: FAIL because terminal identity requires `binding.pos_id` and no-order convergences are skipped.

**Step 7: Implement the narrow terminal identity rule**

Keep binding membership mandatory for nonterminal bindings. Permit matching immutable convergence/entry-leg position identity after the binding is terminal. Extend the reconciliation candidate query and no-order branch only for `convergence_submit_rejected`, with complete snapshots and exact position absence.

**Step 8: Run Task 3 tests and commit**

Run: `.venv/bin/python -m pytest tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py tests/test_trigger_take_profit_convergence.py -q`

Expected: PASS.

```bash
git add src/telegram_kol_research/trigger_take_profit_convergence_executor.py src/telegram_kol_research/position_take_profit_orders.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py
git commit -m "fix: converge definite TP rejections and terminal ledgers"
```

### Task 4: Build a fingerprint-gated, read-only-exchange historical repair planner

**Files:**
- Create: `src/telegram_kol_research/historical_state_repair.py`
- Create: `tests/test_historical_state_repair.py`

**Step 1: Write failing pure-planner classification tests**

Construct database fixtures for the three deletion-exit categories, a terminal submitted TP convergence with active orders, a terminal definite-rejection convergence without orders, and a current live convergence. Feed complete synthetic exchange snapshots and assert exact actions, reasons, exclusions, and conflicts.

**Step 2: Run planner tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair.py -k plan -q`

Expected: ERROR because the repair module does not exist.

**Step 3: Implement immutable plan models and deterministic fingerprints**

Use frozen dataclasses for actions, exclusions, conflicts, and the overall plan. Canonicalize JSON with sorted keys and compact separators. Include relevant row versions/states in the database fingerprint and normalized live position/pending-order identities in the exchange fingerprint.

**Step 4: Run planner tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair.py -k plan -q`

Expected: PASS.

**Step 5: Write failing safety-gate tests**

Assert plan construction or apply refuses incomplete snapshots, live exact positions/orders, unresolved identity, changed fingerprints, changed action counts, wrong confirmation tokens, or a second use of an already-applied plan.

**Step 6: Run safety tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair.py -k 'refuses or fingerprint or confirmation or idempotent' -q`

Expected: FAIL until gates and token verification exist.

**Step 7: Implement guarded local-only application**

Rebuild the plan immediately before apply. Require `expected_fingerprint`, `expected_action_count`, and `confirmation_token == sha256("historical-state-repair:" + fingerprint)[:16]`. Apply all local transitions in one transaction, preserve every row, add terminalization/flat-proof evidence, clear stale claims, and insert one `ExecutionEvent` audit summary with `notification_status="not_needed"`. The module must accept already-loaded read-only snapshot data and must not expose any exchange mutation dependency.

**Step 8: Run Task 4 tests and commit**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair.py -q`

Expected: PASS.

```bash
git add src/telegram_kol_research/historical_state_repair.py tests/test_historical_state_repair.py
git commit -m "feat: add guarded historical state repair planner"
```

### Task 5: Expose the repair CLI and prove its operator contract

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_historical_state_repair_cli.py`
- Modify: `docs/plans/2026-08-10-historical-state-convergence-repair-design.md`

**Step 1: Write failing CLI tests**

Using `typer.testing.CliRunner`, assert default invocation prints JSON dry-run data and never writes; `--apply` without all three gates exits 2; correct gates apply once and print the audit result; any conflict exits 2.

**Step 2: Run CLI tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair_cli.py -q`

Expected: FAIL because the command is missing.

**Step 3: Add `repair-historical-state-convergence`**

Load the database and a complete read-only Deepcoin reconciliation snapshot, build the plan, print stable JSON, and call the guarded apply function only when all explicit gates are present. Never call order-submission, cancellation, or close-position APIs.

**Step 4: Run CLI tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair_cli.py -q`

Expected: PASS.

**Step 5: Add the exact production runbook commands and rollback checks**

Document safe-window proof, stop, SQLite backup/integrity check, dry-run, gated apply, zero-action rerun, exchange fingerprint comparison, restart, and backup restore procedure.

**Step 6: Run focused suites and commit**

Run: `.venv/bin/python -m pytest tests/test_historical_state_repair.py tests/test_historical_state_repair_cli.py tests/test_source_message_deletion.py tests/test_source_message_deletion_worker.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py -q`

Expected: PASS.

```bash
git add src/telegram_kol_research/cli.py tests/test_historical_state_repair_cli.py docs/plans/2026-08-10-historical-state-convergence-repair-design.md
git commit -m "feat: expose supervised historical state repair"
```

### Task 6: Review, push, deploy, repair, and verify production

**Files:**
- Modify only if verification discovers a defect.

**Step 1: Run local regression checks**

Run:

```bash
git diff --check
.venv/bin/python -m pytest tests/test_db_bootstrap.py tests/test_source_message_deletion.py tests/test_source_message_deletion_worker.py tests/test_shuqin_deleted_repost_regression.py tests/test_trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_position_take_profit_orders.py tests/test_historical_state_repair.py tests/test_historical_state_repair_cli.py -q
```

Expected: PASS with no warnings or whitespace errors.

**Step 2: Request independent code review**

Review the complete change range against this plan and the design document. Fix all critical/important findings with a new failing regression test, then rerun Step 1.

**Step 3: Push the reviewed branch**

Run: `git push origin codex/deepcoin-auto-trading-v1`

Expected: the remote branch advances to the reviewed SHA.

**Step 4: Prove a safe production window**

Read only: verify the current service SHA, live positions, live pending orders, management batches/mutation intents, and that no time-sensitive operation is active. Record normalized exchange snapshot fingerprints and the live convergence IDs that must remain excluded.

**Step 5: Stop service and create a verified backup**

Stop `telegram-kol.service`. Use SQLite's backup API to create a timestamped copy of `/opt/telegram-kol-analyzer/data/research.db`; run `PRAGMA integrity_check` against both source and backup and retain the backup path.

**Step 6: Pull, install, and test while stopped**

Pull the reviewed SHA from GitHub, reinstall the editable package, and run the focused server suites. Do not start the service yet.

**Step 7: Dry-run and verify the exact repair plan**

Run the new CLI without `--apply`. Confirm there are no conflicts or snapshot errors, the expected historical categories match production evidence, and every current live position/convergence is explicitly excluded.

**Step 8: Apply once with all gates**

Run the CLI with `--apply`, the exact dry-run fingerprint, exact action count, and derived confirmation token. Immediately rerun dry-run and require zero actions.

**Step 9: Verify database and exchange invariants**

Assert deletion exits have no active/stale claims, historical TP rows are terminal, one non-notifying audit summary exists, and no history row was deleted. Reload Deepcoin read-only snapshots and require before/after live position and pending-order fingerprints to match exactly.

**Step 10: Restart and monitor**

Start `telegram-kol.service`. Verify active state, recent journal output, application monitor, current live position, current protection orders, and no recurrence of `ON CONFLICT` or worker-loop errors. If any pre-start verification fails, restore the verified database backup before restarting; if code health fails, also return to the previous production SHA.

**Step 11: Record production evidence**

Append the deployed SHA, backup path, plan/application fingerprints, before/after counts, exchange fingerprints, and service health outcome to the design/runbook document without recording credentials or sensitive payloads. Commit and push this evidence-only update; no additional restart is required.
