# Minimal Deployment Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace artifact/fingerprint deployment authorization with two read-only active-write checks, path-based schema protection, exact-SHA deployment, health verification, and rollback.

**Architecture:** A small Python module counts only durable exchange-write-in-progress states and returns `0`, `3`, or `4`. The Bash updater runs it before and after stopping the sole writer, detects schema changes from Git paths, then performs fast-forward/install/health or restores the old service. Once the replacement path is green, delete the old preflight, evidence, writer-surface, artifact CLI, and boundary machinery.

**Tech Stack:** Python 3.12, SQLite read-only URI/`PRAGMA query_only`, Bash, Git, systemd, curl, pytest.

---

## Execution Rules

- Create a new worktree and branch `codex/minimal-deployment-gate` from the
  local planning branch `codex/minimal-deployment-gate-plan`. Verify its merge
  base with production is
  `53d5169038a65af01ac3bf4951efa82cc44fa6fe` and its initial production diff
  contains only the two minimal-gate planning documents.
- Read `AGENTS.md` and
  `docs/plans/2026-08-17-minimal-deployment-gate-design.md` completely.
- Use @test-driven-development for every runtime change and observe RED first.
- Use @systematic-debugging for every unexpected result.
- Do not add fingerprints, artifacts, full state-vocabulary validation,
  timestamps, age/count exceptions, overrides, AST scans, or frozen Git blobs.
- Do not edit production data, replay history, call Deepcoin, or send test
  notifications.
- Use @requesting-code-review after the local suite. Critical/Important findings
  require RED/GREEN, reruns, and rereview.
- One explicit approval after final review covers push and deployment of the
  exact reviewed SHA. Do not push or deploy before it.

### Task 1: Build the read-only active-write checker

**Files:**
- Create: `src/telegram_kol_research/deployment_active_write_check.py`
- Create: `tests/test_deployment_active_write_check.py`

**Step 1: Write the zero-result RED**

Create a SQLite fixture with only the columns needed by these tables:

```text
position_backup_stop_orders(id, status)
execution_order_legs(id, status)
instruction_execution_contracts(id, state)
strategy_management_components(id, status)
strategy_management_batches(id, status)
strategy_revision_batches(id, status, advance_claim_token, advance_claimed_at)
strategy_revision_legs(id, revision_batch_id, status)
entry_revision_replacements(id, revision_batch_id, status)
trigger_protection_intents(id, recovery_state)
position_mutation_intents(id, status)
trade_signals(id, status)
```

Write `test_empty_authority_tables_have_zero_active_writes`, importing
`count_active_exchange_writes` and expecting `0`.

**Step 2: Run the single test and verify RED**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py::test_empty_authority_tables_have_zero_active_writes
```

Expected: FAIL because the module/function does not exist.

**Step 3: Add direct active-state REDs**

Parameterize these `(table, column, status)` cases and require count `1`:

```python
(
    ("position_backup_stop_orders", "status", "submitting"),
    ("execution_order_legs", "status", "submitting"),
    ("execution_order_legs", "status", "cancel_submitting"),
    ("instruction_execution_contracts", "state", "submitting"),
    ("strategy_management_components", "status", "submitting"),
    ("strategy_management_components", "status", "cancel_submitting"),
    ("strategy_management_batches", "status", "executing"),
    ("strategy_revision_batches", "status", "submitting_replacements"),
    ("trigger_protection_intents", "recovery_state", "submitting"),
    ("trigger_protection_intents", "recovery_state", "cancel_submitting"),
    ("position_mutation_intents", "status", "submitting"),
    ("position_mutation_intents", "status", "cancel_submitting"),
    ("trade_signals", "status", "processing"),
    ("trade_signals", "status", "submitting"),
    ("trade_signals", "status", "cancel_submitting"),
)
```

Add separate tests proving `strategy_revision_legs.cancel_submitting` and
`entry_revision_replacements.submit_reserved` count only when the parent batch
has both a non-empty claim token and claimed-at value. Missing, empty, or
one-sided claims return zero.

**Step 4: Add ignored-history and fail-closed REDs**

Representative `pending`, `ready`, `reserved`, `submitted`, `submit_unknown`,
`unknown_exchange_outcome`, `recovery_required`, terminal, and invented future
states must return zero.

Add tests for missing table/column, nonexistent database, directory input,
SQLite error, verified query-only mode, and unchanged database bytes. Errors
must raise `ActiveWriteCheckError`; they must never return zero.

**Step 5: Implement the minimal checker**

Public API:

```python
class ActiveWriteCheckError(ValueError):
    pass


def count_active_exchange_writes(database_path: str | Path) -> int:
    ...
```

Use direct `SELECT COUNT(*)` statements for direct authorities. Claim-aware
queries join each revision child to `strategy_revision_batches` and require:

```sql
typeof(b.advance_claim_token) = 'text'
AND length(b.advance_claim_token) > 0
AND b.advance_claimed_at IS NOT NULL
AND length(CAST(b.advance_claimed_at AS text)) > 0
```

Open only with `Path(...).resolve().as_uri() + "?mode=ro"`, set and verify
`PRAGMA query_only=ON`, then `BEGIN`. Require each count to be a non-negative
`int` that is not `bool`; cap the sum at `1_000_000`. Roll back and close in
`finally`. Convert path/SQLite/input failures into stable short errors without
including SQL, paths, rows, or payloads.

**Step 6: Run all checker tests and verify GREEN**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py
```

**Step 7: Commit**

```bash
git add src/telegram_kol_research/deployment_active_write_check.py \
  tests/test_deployment_active_write_check.py
git commit -m "feat: add minimal active write deployment check"
```

### Task 2: Add the bounded checker CLI

**Files:**
- Modify: `src/telegram_kol_research/deployment_active_write_check.py`
- Modify: `tests/test_deployment_active_write_check.py`

**Step 1: Write CLI REDs**

Invoke the real module with `sys.executable -m`. Assert exactly:

- zero: rc `0`, stdout `active_write_count=0\n`, empty stderr;
- active: rc `3`, stdout `active_write_count=1\n`, empty stderr;
- invalid DB or arguments: rc `4`, empty stdout, stderr
  `ERROR active_write_check_failed\n`;
- no path, table, row identity, or payload appears in output.

**Step 2: Run CLI tests and verify RED**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py -k cli
```

**Step 3: Implement the entry point**

Do not add Typer, JSON, options, timestamps, or artifacts:

```python
def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print("ERROR active_write_check_failed", file=sys.stderr)
        return 4
    try:
        count = count_active_exchange_writes(values[0])
    except ActiveWriteCheckError:
        print("ERROR active_write_check_failed", file=sys.stderr)
        return 4
    print(f"active_write_count={count}")
    return 0 if count == 0 else 3
```

Add the normal `if __name__ == "__main__"` exit.

**Step 4: Run tests and compile**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py
../telegram获取消息/.venv/bin/python -m compileall -q \
  src/telegram_kol_research/deployment_active_write_check.py
```

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deployment_active_write_check.py \
  tests/test_deployment_active_write_check.py
git commit -m "feat: expose minimal deployment check cli"
```

### Task 3: Rewrite the updater around two zero-count checks

**Files:**
- Modify: `deploy/telegram-kol-update`
- Create: `tests/test_minimal_server_updater.py`
- Modify: `tests/test_server_update_scripts.py`

**Step 1: Create a small fault harness**

Copy only reusable fake Git/systemctl/pip/curl setup from
`tests/test_server_updater_phases.py`. Do not copy artifact, fingerprint,
surface, snapshot, or decision helpers. The fake candidate Python recognizes
only:

```text
-m telegram_kol_research.deployment_active_write_check DATABASE_PATH
```

Control its two calls with `HARNESS_PRESTOP_ACTIVE_RC` and
`HARNESS_POSTSTOP_ACTIVE_RC`, both defaulting to zero. Record ordered events and
fail if a third checker invocation occurs.

**Step 2: Write the happy-path RED**

Assert this exact order:

```text
fetch -> worktree-add -> schema-diff -> active-check-1 -> stop -> inactive
-> active-check-2 -> checkout/fast-forward -> pip-install -> start -> is-active
-> http-health -> durable-updater-install -> worktree-remove
```

Assert there is no `deployment_preflight_cli`, surface, snapshot, watermark,
preliminary/final, fingerprint, artifact, WARN, or BLOCK event/file. Candidate
source must load through candidate `PYTHONPATH`; the durable updater is last.

Run the single test and observe RED against the current updater.

**Step 3: Write checker-boundary REDs**

Add these cases:

- pre-stop rc `3`: updater rc `3`, zero stop/checkout/install/start;
- pre-stop rc `4`: updater rc `4`, zero stop/checkout/install/start;
- post-stop rc `3`: no checkout/install; old service restarted and verified;
- post-stop rc `4`: same restoration, updater rc `4`;
- post-stop check occurs only after exact `inactive|failed`;
- `deactivating -> inactive` waits, then checks;
- permanent `deactivating` exits hard without checkout.

**Step 4: Write schema-path REDs**

Changes to `models.py`, `db.py`, or `migrations/001_example.py` must run online
backup, backup `quick_check`, disposable migration, migration `quick_check`,
and watermark comparison before active-check-1. Runtime-only and docs-only
changes skip all schema work. No hash or declared change class is allowed.

**Step 5: Write rollback and health REDs**

Cover remote SHA mismatch, dirty tracked tree, wrong branch/rollback ref,
worktree failure, stop failure, delayed stop, TERM/INT during stop, checkout,
pip, candidate start, HTTP health, durable-updater move, and rollback failures.
Also prove a failed `git status` is not treated as clean and code is never
replaced while systemd reports `active` or `deactivating`.

Do not recreate artifact tampering, rc2/WARN, parent, watermark-artifact, AST,
or fingerprint tests.

**Step 6: Rewrite the updater minimally**

Preserve the correct deployment lock, exact remote SHA, clean/attached rollback
point, mode-0700 detached worktree, strict inactive wait, stop-attempt cleanup,
schema backup cleanup, rollback, and updater-last behavior.

Delete all surface/artifact/fact/two-phase code. Add:

```bash
run_active_write_check() {
  set +e
  output="$("${candidate_python[@]}" -m \
    telegram_kol_research.deployment_active_write_check "$DATABASE_PATH" 2>&1)"
  status=$?
  set -e
  case "$status" in
    0) [ "$output" = "active_write_count=0" ] || exit 4 ;;
    3) echo "Deployment refused: active exchange write." >&2; exit 3 ;;
    *) echo "Deployment active-write check failed." >&2; exit 4 ;;
  esac
}
```

Never print captured failure output. Detect schema paths only with:

```bash
schema_changed=0
if ! git -C "$APP_DIR" diff --quiet "$previous_commit" "$EXPECTED_COMMIT" -- \
  src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  migrations; then
  schema_changed=1
fi
```

After start, poll `http://127.0.0.1:8000/api/trading-settings` with
`curl -fsS --max-time 2 -o /dev/null`, at most 20 attempts separated by 0.5s.

The authorization order is exactly:

```bash
run_active_write_check
stop_writer_service
run_active_write_check
# checkout/install/start/health/updater-last
```

**Step 7: Run focused tests and syntax**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_minimal_server_updater.py tests/test_server_update_scripts.py
bash -n deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh scripts/server_git_update.sh
```

**Step 8: Commit**

```bash
git add deploy/telegram-kol-update \
  tests/test_minimal_server_updater.py tests/test_server_update_scripts.py
git commit -m "refactor: reduce deployment gate to active writes"
```

### Task 4: Delete the artifact/fingerprint gate

**Files:**
- Delete: `src/telegram_kol_research/deployment_preflight.py`
- Delete: `src/telegram_kol_research/deployment_preflight_cli.py`
- Delete: `src/telegram_kol_research/deployment_work_evidence.py`
- Delete: `src/telegram_kol_research/deployment_writer_surface.py`
- Delete: `tests/test_deployment_preflight.py`
- Delete: `tests/test_deployment_preflight_cli.py`
- Delete: `tests/test_deployment_work_evidence.py`
- Delete: `tests/test_deployment_writer_surface.py`
- Delete: `tests/test_server_updater_phases.py`
- Delete: `tests/test_simple_deployment_gate_boundary.py`
- Modify: `src/telegram_kol_research/cli.py:64-70,2555-2670`
- Modify: `tests/test_cli_smoke.py:150-200`

**Step 1: Write removal REDs**

Use `importlib.util.find_spec` to require the four retired modules to be absent.
Update CLI smoke tests so `deployment-preflight` and
`verify-deployment-preflight` are ordinary unknown commands, not compatibility
wrappers. Assert updater source lacks:

```text
deployment_preflight_cli
preliminary_artifact
final_artifact
writer_fingerprint
evidence_fingerprint
```

Observe RED while the modules still exist.

**Step 2: Delete the runtime and test files**

Use `apply_patch`. Remove the four imports, preflight time/error helpers, and
both Typer commands from `cli.py`. Leave no shims, deprecated commands, copied
constants, or empty compatibility modules.

**Step 3: Search for live references**

```bash
rg -n 'deployment_preflight|deployment_work_evidence|deployment_writer_surface|\
preliminary_artifact|final_artifact|writer_fingerprint|evidence_fingerprint' \
  src deploy scripts tests pyproject.toml
```

Expected: only explicit absence-test data may match. Historical docs may match.

**Step 4: Run focused tests**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py tests/test_cli_smoke.py
```

**Step 5: Measure simplification**

```bash
git diff --stat 53d5169038a65af01ac3bf4951efa82cc44fa6fe..HEAD
wc -l src/telegram_kol_research/deployment_active_write_check.py \
  deploy/telegram-kol-update tests/test_deployment_active_write_check.py \
  tests/test_minimal_server_updater.py
```

Record but do not freeze the numbers. The diff must show substantial net
deletion. If the replacement approaches the deleted system's size, stop and
revisit the design.

**Step 6: Commit**

```bash
git add -A src/telegram_kol_research tests deploy/telegram-kol-update
git commit -m "refactor: remove fingerprint deployment authorization"
```

### Task 5: Replace operator documentation atomically

**Files:**
- Modify: `docs/server-deployment.md:115-220`
- Modify: `docs/runbook.md:deployment gate sections`
- Modify: `docs/migration-handoff.md`
- Modify: `docs/plans/2026-08-16-simple-deployment-safety-gate-design.md`
- Modify: `docs/plans/2026-08-16-simple-deployment-safety-gate.md`
- Modify: `docs/plans/2026-08-17-simple-gate-unknown-policy-design.md`
- Modify: `docs/plans/2026-08-17-simple-gate-unknown-policy.md`

**Step 1: Find every current instruction**

```bash
rg -n 'Phase A|Phase B|preliminary|final artifact|fingerprint|writer surface|\
WARN|BLOCK|shadow approval|push approval|deployment approval' \
  docs/server-deployment.md docs/runbook.md docs/migration-handoff.md
```

Classify each match as current operator instruction or history.

**Step 2: Write the short runbook**

Document only:

```text
review exact SHA -> one approval -> helper
pre-stop active count -> stop/inactive -> post-stop active count
schema path changed -> backup/dry-run
checkout/install/start/HTTP health -> updater last
failure -> old service restored or hard rollback failure
```

State that historical unknown/queued/recovery never block, no artifacts or
fingerprints exist, exit `3` means an active write, exit `4` means checker/input
or updater/rollback failure, and ordinary deployments require no shadow.
Database edits and exchange test writes remain prohibited.

**Step 3: Mark old plans superseded**

Add this banner to the four old gate plans without rewriting their history:

```text
Superseded by 2026-08-17-minimal-deployment-gate-design.md.
Retained only as historical context; do not execute this runbook.
```

**Step 4: Re-run the search**

Artifact/fingerprint/three-approval language may remain only in explicitly
superseded historical plans or explanatory removal text, not active runbooks.

**Step 5: Commit**

```bash
git add docs/server-deployment.md docs/runbook.md docs/migration-handoff.md \
  docs/plans/2026-08-16-simple-deployment-safety-gate-design.md \
  docs/plans/2026-08-16-simple-deployment-safety-gate.md \
  docs/plans/2026-08-17-simple-gate-unknown-policy-design.md \
  docs/plans/2026-08-17-simple-gate-unknown-policy.md
git commit -m "docs: replace deployment gate runbook"
```

### Task 6: Complete local verification and independent review

**Files:** No intended runtime changes.

**Step 1: Run the focused safety suite**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_active_write_check.py \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py tests/test_cli_smoke.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_terminal_entry_cleanup.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_source_message_deletion.py
```

**Step 2: Run static and full checks**

```bash
../telegram获取消息/.venv/bin/python -m compileall -q src tests
bash -n deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh scripts/server_git_update.sh
git diff --check 53d5169038a65af01ac3bf4951efa82cc44fa6fe..HEAD
../telegram获取消息/.venv/bin/python -m pytest -q
git status --short
```

Expected: all pass and worktree clean.

**Step 3: Request independent Critical/Important review**

The reviewer must construct and run:

- every listed active authority -> rc `3`;
- unknown/queued/recovery/future states -> rc `0`;
- an active row appearing between checks prevents checkout and restores old
  service;
- missing table/column and unreadable DB -> rc `4`;
- schema/non-schema path behavior;
- delayed stop, signal, install, health, and rollback faults;
- exact remote SHA and clean rollback-point rejection;
- no live import or call of the deleted modules;
- no artifact/fingerprint/AST/frozen-blob mechanism reintroduced;
- meaningful net line deletion.

Critical/Important findings require RED/GREEN and rereview.

**Step 4: Stop for one explicit approval**

Report exact reviewed SHA, focused/full results, review verdict, net deletion,
clean status, and server plan. Ask once for approval to push and deploy that
exact SHA; the approval covers both actions.

### Task 7: Push and deploy the reviewed minimal gate

**Files:** No intended repository changes.

**Step 1: Verify the approved SHA and baseline**

Require local HEAD equals approved SHA, clean worktree, remote production branch
and server HEAD both equal `53d5169...`, candidate descends from it, production
branch/tree is attached/clean, service active, MiMo v1 authoritative, and v2
watermark zero. Stop on any divergence; normal approval never authorizes force.

**Step 2: Fast-forward the exact SHA**

```bash
git push origin "$approved_sha:refs/heads/codex/deepcoin-auto-trading-v1"
test "$(git ls-remote origin refs/heads/codex/deepcoin-auto-trading-v1 | \
  awk '{print $1}')" = "$approved_sha"
```

**Step 3: Run server-focused candidate tests**

Stage the exact SHA detached and mode 0700 under
`/opt/telegram-kol-candidates/`. With production venv plus candidate
`PYTHONPATH`, run the active-checker, minimal-updater, server-script, and CLI
smoke tests. This step authorizes no production DB write, notification, service
stop, or exchange call. Stop on failure.

**Step 4: Deploy with the SHA-verified candidate updater**

Run the normal helper with the exact approved SHA and production branch. The
bootstrap must fetch the candidate updater, verify its reviewed SHA-256, and
execute the new two-check flow. The installed old updater does not protect this
transition. Stop on any bootstrap, pre-stop check, stop, post-stop check,
install, health, or rollback failure; do not fall back to manual deployment.

**Step 5: Verify production**

Require:

- server HEAD and remote production ref equal approved SHA;
- attached branch, tracked tree clean, service active, new healthy invocation;
- local `/api/trading-settings` HTTP 200;
- durable updater equals reviewed deployed updater;
- MiMo v1 and v2 watermark zero;
- settings hash unchanged and no deployment notification/execution increase;
- no post-restart error log;
- four old modules absent;
- new checker on production DB returns rc `0`, `active_write_count=0`;
- durable updater has no preliminary/final artifact, fingerprint, or old CLI
  token.

Normal incoming messages may advance raw-message and MiMo v1 counters; record
them separately from deployment side effects.

**Step 6: Report completion**

Report production SHA, service/API health, active count, schema-path result,
rollback status, net deletion, and that future normal deployments use the
one-approval short flow.
