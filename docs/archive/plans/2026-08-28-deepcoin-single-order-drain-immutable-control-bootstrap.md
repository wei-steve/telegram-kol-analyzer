# Deepcoin Single-Order Drain and Immutable Control Bootstrap Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the rejected universal legacy drain bridge with three action-specific tools that seed fenced authority, drain one canonical Deepcoin pending trigger per maintenance window, and bootstrap the first fully monitored immutable control runtime.

**Architecture:** A root-owned host lock plus persistent systemd masks isolates the legacy runtime, while one versioned authority row provides `idle`, `held`, and `blocked` generation-CAS fencing. `drain-one` reuses the canonical target constant and existing terminalization transaction; `bootstrap-control` starts web, ingest, worker, and monitor from one immutable release with entry admission closed and separately proven management/protection authority.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy/SQLite, pytest, systemd, immutable Git release manifests, Deepcoin REST client.

---

## Preconditions and Working Rules

- Work only in `/Users/steven/Documents/telegram获取消息` on
  `codex/phase0-deploy-integration`.
- Start from design commit `e6ee10461fa55484c55502386e356ab7948962c0`
  with a clean worktree. Stop on any mismatch; do not repair it automatically.
- Use `@test-driven-development` for every production-code task.
- Use explicit paths for staging. Never run `git add -A`.
- Keep `REVIEWED_PENDING_ENTRY_TARGETS` as the only seven-order source.
- Run focused tests after each change. Run the complete suite once, after the
  final production-code candidate is assembled.
- Do not push, SSH, stage a server release, mask services, seed production,
  activate, or call a Deepcoin write endpoint in this plan.

### Task 1: Prove the crash-and-reboot falsifier with a persistent runtime guard

**Files:**
- Create: `src/telegram_kol_research/maintenance_runtime_guard.py`
- Create: `src/telegram_kol_research/deepcoin_maintenance_actions.py`
- Create: `tests/test_maintenance_runtime_guard.py`
- Create: `tests/test_deepcoin_maintenance_actions.py`

**Step 1: Write the failing persistent-mask tests**

Define a fake systemd adapter with unit enablement, active state, cgroup PIDs,
and persistent mask state. Add tests for these exact contracts:

```python
def test_enter_guard_persistently_masks_before_stopping_all_units():
    runtime = FakeSystemdRuntime.active_legacy()
    guard = MaintenanceRuntimeGuard(runtime=runtime, receipt_path=receipt_path)

    receipt = guard.enter(action_id="drain-001")

    assert runtime.calls[:4] == [
        ("mask", unit) for unit in MAINTENANCE_UNITS
    ]
    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)
    assert receipt.safe_to_restore is False


def test_reconcile_after_process_crash_and_reboot_keeps_units_masked():
    runtime = FakeSystemdRuntime.from_persisted_masks(MAINTENANCE_UNITS)
    guard = MaintenanceRuntimeGuard.load(runtime=runtime, receipt_path=receipt_path)

    result = guard.reconcile_after_restart()

    assert result.status == "blocked"
    assert runtime.start_calls == []
    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)
```

The ordered call assertion must cover monitor timer/service as well as web,
ingest, and worker. Add refusal tests for a busy root lock, a surviving cgroup
PID, a nonzero MainPID, a matching stray process, an unsafe receipt owner/mode,
and receipt drift.

**Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_maintenance_runtime_guard.py -q
```

Expected: collection failure because `maintenance_runtime_guard` does not
exist.

**Step 3: Implement the minimal guard**

Implement these public contracts:

```python
MAINTENANCE_UNITS: tuple[str, ...] = (
    "telegram-kol-web.service",
    "telegram-kol-ingest.service",
    "telegram-kol-worker.service",
    "telegram-kol-monitor.timer",
    "telegram-kol-monitor.service",
)

@dataclass(frozen=True, slots=True)
class UnitPreimage:
    unit: str
    enabled_state: str
    active_state: str
    masked: bool

@dataclass(frozen=True, slots=True)
class GuardReceipt:
    schema_version: int
    action_id: str
    entered_at: datetime
    safe_to_restore: bool
    blocked_reason: str | None
    units: tuple[UnitPreimage, ...]
    fingerprint: str

class MaintenanceRuntimeGuard:
    def enter(self, *, action_id: str) -> GuardReceipt: ...
    def prove_quiescent(self) -> None: ...
    def mark_safe_to_restore(self, *, expected_fingerprint: str) -> GuardReceipt: ...
    def restore(self, *, expected_fingerprint: str) -> None: ...
    def block(self, *, reason_code: str) -> GuardReceipt: ...
    def reconcile_after_restart(self) -> GuardReceipt: ...
```

Use `fcntl.flock(..., LOCK_EX | LOCK_NB)` for the host lock. Publish the receipt
with an owner-only temporary file, `fsync`, `os.replace`, and directory `fsync`.
Never mark a receipt safe merely because its deadline elapsed.

`enter()` must record preimages, persistently mask all units, stop them, then
prove empty cgroups/MainPIDs and two stable process scans. `restore()` must be
the only method that unmasks or starts a unit, and it must require the exact
safe receipt fingerprint.

**Step 4: Add the first cross-boundary falsifier**

Use injected authority, exchange, and guard fakes:

```python
def test_cancel_timeout_then_crash_and_reboot_never_retries_or_restores():
    exchange = ExchangeThatAcceptedThenTimedOut()
    authority = FakeAuthority(state="idle", generation=7)
    guard = FakePersistentGuard()

    with pytest.raises(MaintenanceBlocked):
        run_single_order_drain(
            request=one_request(),
            authority=authority,
            exchange=exchange,
            guard=guard,
            terminalizer=FailIfCalled(),
        )

    rebooted = guard.simulate_host_reboot()
    assert exchange.cancel_calls == 1
    assert authority.state in {"held", "blocked"}
    assert rebooted.worker_start_allowed is False
    assert rebooted.restore_calls == 0
```

Implement only the coordinator error boundary needed to make this test pass:
once the exchange call begins, any unclassified exception invokes
`authority.block(...)` and `guard.block(...)`, then raises
`MaintenanceBlocked`. It must never call restore or retry.

**Step 5: Run focused tests and commit**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_maintenance_runtime_guard.py \
  tests/test_deepcoin_maintenance_actions.py -q
```

Expected: PASS.

Commit:

```bash
git add \
  src/telegram_kol_research/maintenance_runtime_guard.py \
  src/telegram_kol_research/deepcoin_maintenance_actions.py \
  tests/test_maintenance_runtime_guard.py \
  tests/test_deepcoin_maintenance_actions.py
git commit -m "feat: add fail-closed maintenance runtime guard"
```

### Task 2: Replace authority v1 with the three-state generation-CAS record

**Files:**
- Modify: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Modify: `tests/test_entry_revision_exchange_authority.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/entry_revision_executor.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_entry_revision_executor.py`
- Modify: `tests/test_recovery_live_submit.py`

**Step 1: Write failing authority parser and CAS tests**

Cover only three accepted states and exact-key validation:

```python
def test_missing_authority_row_fails_closed_instead_of_auto_creating(): ...
def test_idle_acquire_increments_generation_and_binds_owner_identity(): ...
def test_stale_generation_cannot_acquire_release_or_block(): ...
def test_expired_held_authority_becomes_blocked_never_idle(): ...
def test_write_boundary_unknown_blocks_and_retains_token_hash(): ...
def test_release_cas_failure_leaves_exact_held_authority(): ...
def test_ordinary_settings_row_cannot_overwrite_authority_key(): ...
```

The canonical v2 documents must be exact-key JSON objects:

```python
idle = {
    "schema_version": 2,
    "state": "idle",
    "generation": 4,
    "released_at": "...Z",
}

held = {
    "schema_version": 2,
    "state": "held",
    "generation": 5,
    "owner_kind": "reviewed_pending_entry_cancel",
    "action_id": "drain-001",
    "owner_pid": 4321,
    "owner_start_ticks": 999,
    "token_sha256": "...64 hex...",
    "plan_sha256": "...64 hex...",
    "evidence_sha256": "...64 hex...",
    "acquired_at": "...Z",
    "deadline_at": "...Z",
    "write_boundary_reached": False,
}

blocked = {
    "schema_version": 2,
    "state": "blocked",
    "generation": 5,
    "prior_owner_kind": "reviewed_pending_entry_cancel",
    "action_id": "drain-001",
    "token_sha256": "...64 hex...",
    "blocked_at": "...Z",
    "reason_code": "exchange_outcome_unknown",
    "write_boundary_reached": True,
}
```

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_entry_revision_exchange_authority.py -q
```

Expected: FAIL because the current parser accepts only v1 `idle/held` and
acquisition creates a missing row.

**Step 3: Implement the exact v2 API**

Keep the authority in the existing dedicated `TradingSetting` key so no table
migration is introduced. Add:

```python
def seed_entry_revision_exchange_authority(..., expected_absent: bool) -> SeedResult: ...
def acquire_entry_revision_exchange_authority(..., expected_generation: int,
    action_id: str, owner_identity: ProcessIdentity, deadline_at: datetime,
    token_sha256: str, plan_sha256: str, evidence_sha256: str) -> Acquisition: ...
def mark_entry_revision_exchange_write_boundary(..., token: str,
    expected_generation: int) -> BoundaryResult: ...
def block_entry_revision_exchange_authority(..., token: str,
    expected_generation: int, reason_code: str, blocked_at: datetime) -> BlockResult: ...
def release_entry_revision_exchange_authority(..., token: str,
    expected_generation: int, owner_kind: str, released_at: datetime) -> Release: ...
```

Every mutation uses `BEGIN IMMEDIATE`, parses the exact current document, and
matches state, generation, owner kind, and token. Absence, v1 data, malformed
JSON, expiry, or mismatch is fail-closed.

Update ordinary runtime callers to obtain their process identity and use a
bounded deadline. Preserve the rule that release failure is an error and never
means authority was released.

**Step 4: Run affected tests and commit**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_auto_trade_execution.py \
  tests/test_entry_revision_executor.py \
  tests/test_recovery_live_submit.py -q
```

Expected: PASS.

Commit the eight explicit paths with message:

```bash
git commit -m "refactor: fence entry revision exchange authority"
```

### Task 3: Add the L3 seed planner and backup/integrity executor

**Files:**
- Create: `src/telegram_kol_research/entry_authority_seed.py`
- Create: `tests/test_entry_authority_seed.py`
- Modify: `src/telegram_kol_research/deepcoin_maintenance_actions.py`
- Modify: `tests/test_deepcoin_maintenance_actions.py`

**Step 1: Write failing dry-run, apply, and rollback tests**

Test these contracts:

```python
def test_seed_plan_is_read_only_and_requires_absent_row(): ...
def test_seed_refuses_existing_idle_held_blocked_or_malformed_row(): ...
def test_seed_backup_uses_sqlite_backup_api_and_passes_quick_check(): ...
def test_seed_apply_changes_only_one_trading_setting_count(): ...
def test_seed_post_commit_integrity_failure_restores_verified_backup(): ...
def test_seed_unknown_restore_keeps_runtime_persistently_masked(): ...
```

The plan fingerprint must cover database inode/device, schema version, authority
absence, `quick_check`, foreign-key count, affected counts, critical table
counts, backup destination, and observation time.

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_entry_authority_seed.py -q
```

Expected: FAIL because the seed module does not exist.

**Step 3: Implement the seed executor**

Expose:

```python
def build_entry_authority_seed_plan(database_path: Path, *, now: datetime) -> SeedPlan: ...
def apply_entry_authority_seed_plan(database_path: Path, *, backup_path: Path,
    expected_fingerprint: str, guard: MaintenanceRuntimeGuard,
    now: datetime) -> SeedResult: ...
```

Use `sqlite3.Connection.backup`, never a file copy of a live WAL database. Run
pre/post `PRAGMA quick_check` and `PRAGMA foreign_key_check`. Compare affected
and critical table counts; the only accepted count delta is one new
`trading_settings` row. A failed restore is `blocked`, not rollback success.

**Step 4: Run tests and commit**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_entry_authority_seed.py \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_maintenance_runtime_guard.py \
  tests/test_deepcoin_maintenance_actions.py -q
```

Expected: PASS.

Commit the four explicit paths with message:

```bash
git commit -m "feat: add one-time entry authority seed"
```

### Task 4: Remove the public bridge freeze and preserve the process-local entry gate

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_web_trading_settings.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing ownership tests**

Add tests proving:

```python
def test_ordinary_settings_payload_rejects_legacy_entry_submission_frozen(): ...
def test_settings_api_never_serializes_internal_entry_freeze(): ...
def test_deployment_entry_freeze_blocks_entry_without_enabling_management(): ...
def test_auto_trade_disabled_plus_deployment_freeze_keeps_management_disabled(): ...
def test_recovery_submit_uses_process_local_deployment_gate(): ...
```

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_trading_settings.py \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py -q
```

Expected: FAIL because `legacy_entry_submission_frozen` is still accepted and
affects management authority.

**Step 3: Remove the field and use the existing immutable process gate**

Delete `legacy_entry_submission_frozen` from `TradingSettings`, parsing,
serialization, and reason codes. Make settings-derived management authority
depend only on its real configured authority conditions. Entry call sites must
separately require both:

```python
settings.entry_submission_enabled is True
and deployment_entry_admission_frozen() is False
```

Do not add a replacement settings field. Keep
`TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1` as an immutable unit/drop-in property
whose loaded effect is proven by runtime identity.

**Step 4: Run affected tests and commit**

Run the five test files from Step 2 plus `tests/test_web_app.py`. Expected: PASS.

Commit the nine explicit paths with message:

```bash
git commit -m "refactor: keep entry freeze outside trading settings"
```

### Task 5: Refactor cancellation into an exact fresh single-order drain

**Files:**
- Create: `src/telegram_kol_research/deepcoin_maintenance_evidence.py`
- Create: `tests/test_deepcoin_maintenance_evidence.py`
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`
- Modify: `src/telegram_kol_research/deepcoin_maintenance_actions.py`
- Modify: `tests/test_deepcoin_maintenance_actions.py`
- Modify: `src/telegram_kol_research/repair_confirmation.py`
- Modify: `tests/test_historical_state_repair.py`

**Step 1: Write failing completeness and freshness tests**

Move the bounded Deepcoin query audit out of `cli.py` and test:

```python
def test_evidence_requires_complete_positions_regular_pending_and_exact_target_readback(): ...
def test_incomplete_query_gets_one_reasoned_retry_then_unknown(): ...
def test_evidence_older_than_thirty_seconds_is_rejected(): ...
def test_remaining_pending_set_must_equal_canonical_unfinished_subset(): ...
def test_noncanonical_pending_trigger_stops_without_exchange_write(): ...
def test_active_global_authority_or_target_unknown_stops_prewrite(): ...
def test_fresh_plan_is_rebuilt_after_guard_and_again_under_authority(): ...
```

Do not assert a copied seven-order tuple. Derive expected IDs inside tests from
`REVIEWED_PENDING_ENTRY_TARGETS`.

**Step 2: Write failing terminalization and token-binding tests**

Parameterize failures across intent, leg, binding, lifecycle, protection,
convergence, and event updates. Require one transaction and one event. Test raw
token global single use plus binding to the authority row's action ID, target,
plan hash, evidence hash, and generation.

**Step 3: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_deepcoin_maintenance_evidence.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_deepcoin_maintenance_actions.py -q
```

Expected: FAIL because bridge identity still participates in plan/apply and
fresh evidence is not a first-class bound object.

**Step 4: Implement the single-order flow**

- Remove all imports and arguments from `legacy_runtime_drain_bridge`.
- Require exactly one `order_id` validated against the canonical constant.
- Build evidence only after `guard.prove_quiescent()`.
- Acquire v2 authority with the new plan/evidence/token hashes.
- Rebuild and compare the plan under authority.
- Mark the write boundary immediately before calling Deepcoin.
- Never loop over actions; reject a plan with zero or more than one applicable
  action.
- Require an exact terminal exchange result before local terminalization.
- On any possible-write unknown, block authority and guard.
- On complete success, commit local terminalization, release authority, mark the
  guard safe, and restore the exact legacy state.

**Step 5: Run focused tests and commit**

Run the four changed test files plus
`tests/test_entry_revision_exchange_authority.py`. Expected: PASS.

Commit all eight explicit paths with message:

```bash
git commit -m "feat: drain one canonical pending entry safely"
```

### Task 6: Expose three strict action-specific CLI commands

**Files:**
- Create: `src/telegram_kol_research/deepcoin_maintenance_manifest.py`
- Create: `tests/test_deepcoin_maintenance_manifest.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `tests/test_deepcoin_maintenance_actions.py`

**Step 1: Write failing manifest tests**

Define three exact action names:

```python
class MaintenanceAction(str, Enum):
    SEED_ENTRY_AUTHORITY = "seed-entry-authority"
    DRAIN_ONE = "drain-one"
    BOOTSTRAP_CONTROL = "bootstrap-control"
```

Test exact-key parsing, maximum file size, owner/mode checks, SHA/hash formats,
expiry no longer than 15 minutes, and action-specific fields. `drain-one` accepts
one target order ID and validates it against the canonical constant. It rejects
lists, wildcard strings, duplicated target fields, and unknown keys.

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_deepcoin_maintenance_manifest.py -q
```

Expected: FAIL because the parser does not exist.

**Step 3: Implement the parser and CLI**

Add Typer commands with dry-run as the default:

```text
seed-entry-authority
drain-one
bootstrap-control
```

Apply mode requires an exact manifest, expected fingerprint, and action-specific
authorization. `drain-one` additionally requires the fresh confirmation token.
No command accepts `--all`, repeated `--order-id`, or a positional order list.

CLI JSON output is bounded and redacted: status, reason code, action ID, target
suffix, generation, hashes, timestamps, and evidence path only.

**Step 4: Run CLI tests and commit**

Run the three changed test files and `tests/test_cli_smoke.py`. Expected: PASS.

Commit the five explicit paths with message:

```bash
git commit -m "feat: add action-specific Deepcoin maintenance CLI"
```

### Task 7: Implement the one-time immutable control bootstrap

**Files:**
- Create: `src/telegram_kol_research/immutable_control_bootstrap.py`
- Create: `tests/test_immutable_control_bootstrap.py`
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `src/telegram_kol_research/deepcoin_maintenance_actions.py`
- Modify: `tests/test_deepcoin_maintenance_actions.py`

**Step 1: Write failing full-scope and rollback tests**

Cover:

```python
def test_bootstrap_requires_web_ingest_worker_and_monitor_exactly(): ...
def test_bootstrap_rejects_ffb_release_or_same_candidate_and_control_sha(): ...
def test_candidate_pid_start_tuple_must_differ_from_legacy(): ...
def test_candidate_starts_while_bootstrap_authority_is_held(): ...
def test_candidate_entry_is_frozen_but_management_protection_close_tpsl_rescue_are_proven(): ...
def test_all_disabled_capabilities_reject_bootstrap(): ...
def test_candidate_completes_no_exchange_write_authority_round_trip(): ...
def test_partial_dropin_or_systemd_verify_failure_rolls_back_before_unmask(): ...
def test_unknown_write_or_rollback_uncertainty_retains_masks_and_blocks(): ...
```

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_immutable_control_bootstrap.py -q
```

Expected: FAIL because the bootstrap module does not exist.

**Step 3: Extract reusable immutable release operations**

From `scoped_release_activation.py`, expose or move only the narrowly reusable
functions for release validation, canonical drop-in rendering/publication,
authorization consumption, systemd verification, and runtime identity proof.
Do not call `activate_release()` from bootstrap because its rollback/control
precondition creates the first-control circular dependency.

**Step 4: Implement bootstrap sequencing**

Implement:

```python
def build_immutable_control_bootstrap_plan(...) -> BootstrapPlan: ...
def apply_immutable_control_bootstrap_plan(...,
    guard: MaintenanceRuntimeGuard,
    authority: EntryRevisionAuthorityAdapter,
    runtime: RuntimeAdapter) -> BootstrapResult: ...
```

Require complete fresh zero-position/zero-regular/zero-pending evidence and
complete local terminalization before mutation. Capture exact legacy unit and
drop-in preimages. Keep persistent masks while publishing and verifying all
candidate files. Start the candidate with the process-local entry freeze, prove
four-role identity and independent worker capabilities, release bootstrap
authority, then require one no-exchange-write authority round trip. Only then
mark the guard safe and unmask the candidate units.

The result remains entry-frozen. There is no thaw path in this module.

**Step 5: Run focused tests and commit**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_immutable_control_bootstrap.py \
  tests/test_scoped_release_activation.py \
  tests/test_deepcoin_maintenance_actions.py -q
```

Expected: PASS.

Commit the six explicit paths with message:

```bash
git commit -m "feat: bootstrap first immutable control runtime"
```

### Task 8: Make runtime identity and monitor release-aware for the four-role scope

**Files:**
- Modify: `src/telegram_kol_research/runtime_deployment_identity.py`
- Modify: `tests/test_runtime_deployment_identity.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `deploy/systemd/telegram-kol-monitor.timer`
- Modify: `scripts/install_server_monitor.sh`
- Modify: `tests/test_server_monitor_installation.py`

**Step 1: Write failing identity tests**

Require release commit, manifest hash, PID/start ticks, systemd MainPID/start
ticks, role, loaded cwd, command role, and observed time to agree. Add distinct
legacy/candidate tuple tests and successful-cycle tests for management,
protection, close, TPSL, rescue, and the no-write authority self-test.

Entry admission must be reported independently and must not project the other
capabilities.

**Step 2: Write failing monitor tests**

Test that monitor:

- loads expected commit and manifest from the immutable release contract;
- verifies web, ingest, worker, monitor unit fragment/drop-in hashes;
- does not use checkout HEAD as success proof;
- rejects a mixed-release four-role runtime;
- rejects stale or disabled worker capabilities;
- redacts environment values, confirmation tokens, raw settings JSON, raw
  orders, and unbounded error text.

**Step 3: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_runtime_deployment_identity.py \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py -q
```

Expected: FAIL because the installed monitor still binds expected state to the
fixed production checkout HEAD and does not verify the four-role manifest.

**Step 4: Implement identity and monitor changes**

Keep raw evidence in a bounded local evidence file. Return only hashes, counts,
freshness, role status, and reason codes in monitor status. Make the monitor
service itself load from the immutable release and include its unit files in the
release content hash.

The installer must validate an explicit immutable release path, commit, and
manifest hash. It must not print or persist credential values.

**Step 5: Run focused tests and commit**

Run the three test files from Step 3. Expected: PASS.

Commit the eight explicit paths with message:

```bash
git commit -m "fix: monitor loaded immutable runtime scope"
```

### Task 9: Delete the rejected bridge and close ordinary activation scope

**Files:**
- Delete: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Delete: `tests/test_legacy_runtime_drain_bridge.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `src/telegram_kol_research/deployment_action_plan.py`
- Modify: `tests/test_deployment_action_plan.py`
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Write failing absence and four-component-scope tests**

Add tests proving:

```python
def test_cli_has_no_bridge_reviewed_pending_entries_command(): ...
def test_authority_activation_requires_web_ingest_worker_and_monitor(): ...
def test_partial_monitorless_authority_activation_is_rejected(): ...
def test_ordinary_activate_still_cannot_write_exchange_or_thaw_entry(): ...
```

Search tests must fail if production code imports
`legacy_runtime_drain_bridge` or contains `legacy_entry_submission_frozen`.

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_cli_smoke.py \
  tests/test_deployment_action_plan.py \
  tests/test_scoped_release_activation.py -q
```

Expected: FAIL while the bridge command and monitorless authority scope remain.

**Step 3: Remove bridge code and tighten future activation**

Delete the bridge module and its dedicated test file. Remove the CLI command,
bridge imports, and compatibility arguments. For any future authority-changing
ordinary activation, require the complete component set
`web, ingest, worker, monitor` while continuing to prohibit exchange writes,
historical replay, bulk actions, settings mutation, and automatic thaw.

Update the canonical status document to record:

- the rejected `ffb06d19...` release remains inactive;
- the bridge was replaced locally;
- exact local test evidence and candidate commit are pending until Task 10;
- no push, production read, seed, cancellation, activation, or thaw occurred.

**Step 4: Run focused tests and commit**

Run the three files from Step 2 plus all new maintenance, authority, identity,
monitor, and reviewed-cancel tests. Expected: PASS.

Use `git rm` for the two deleted files, explicitly add the seven modified paths,
verify `git diff --cached --name-only`, then commit:

```bash
git commit -m "refactor: remove rejected legacy drain bridge"
```

### Task 10: Final candidate verification and local handoff

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Test: all focused files from Tasks 1–9
- Test: complete repository suite

**Step 1: Run static and import checks**

Run:

```bash
git diff --check
rg -n "legacy_runtime_drain_bridge|legacy_entry_submission_frozen" \
  src scripts deploy tests
PYTHONPATH=src .venv/bin/python -m telegram_kol_research.cli --help
```

Expected: no bridge/freeze matches, no whitespace errors, CLI help exits zero
and lists only the three new maintenance actions.

**Step 2: Run the complete focused safety suite**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest \
  tests/test_maintenance_runtime_guard.py \
  tests/test_deepcoin_maintenance_actions.py \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_entry_authority_seed.py \
  tests/test_deepcoin_maintenance_evidence.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_deepcoin_maintenance_manifest.py \
  tests/test_immutable_control_bootstrap.py \
  tests/test_runtime_deployment_identity.py \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_scoped_release_activation.py \
  tests/test_deployment_action_plan.py \
  tests/test_trading_settings.py \
  tests/test_web_trading_settings.py \
  tests/test_auto_trade_execution.py \
  tests/test_entry_revision_executor.py \
  tests/test_recovery_live_submit.py -q
```

Expected: PASS.

**Step 3: Run the final full suite once**

Run:

```bash
PYTHONPATH=src:tests .venv/bin/python -m pytest -q
```

Expected: PASS with the repository's documented skips only. If production code
changes afterward, rerun the affected focused tests and this full suite once on
the new final candidate.

**Step 4: Update the local status evidence**

Record:

- final commit and tree;
- focused and full-suite counts;
- first-falsifier result;
- files removed/replaced;
- no production-dependent acceptance was claimed;
- exact future authorizations still required: push, stage, read-only preflight,
  DB-copy rehearsal, L3 seed, seven independent order writes, bootstrap, and
  entry thaw.

Do not copy the seven IDs into the status document.

**Step 5: Commit the final status only**

```bash
git add docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record immutable bootstrap local evidence"
```

**Step 6: Review the final branch without pushing**

Run:

```bash
git status --short
git log --oneline --decorate -12
git diff --stat e6ee10461fa55484c55502386e356ab7948962c0..HEAD
```

Expected: clean worktree, only the planned commits and files, and no remote or
production mutation.

Stop here. Push, stage, SSH, production DB rehearsal, service control, Deepcoin
writes, bootstrap activation, and entry thaw each require later independent
authorization.
