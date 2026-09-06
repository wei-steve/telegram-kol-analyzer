# Production Monitor Deployment-Check Removal Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove deployment, process-identity, topology, and systemd proof from the production safety monitor while preserving every business-risk check and the activation-time business diagnostic gate.

**Architecture:** The activation controller remains the only deployment authority. The monitor becomes an HTTP/database/journal business observer: it proves service availability through the settings endpoint, reads a fixed bounded unit list for error logs, and never queries deployment identities, systemd state, release contents, or other processes.

**Tech Stack:** Python 3.12, Typer, pytest, systemd unit files, immutable-release staging scripts.

---

### Task 1: Remove deployment evidence from the monitor data model

**Files:**
- Modify: `tests/test_production_safety_monitor.py:1-360`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:70-590`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:3250-3410`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:4240-4280`

**Step 1: Write the failing contract tests**

Add:

```python
from dataclasses import fields

def test_monitor_snapshot_has_no_deployment_evidence_contract():
    assert "runtime_release_scope" not in {
        field.name for field in fields(MonitorSnapshot)
    }
    assert not hasattr(monitor_module, "evaluate_runtime_release_scope")

def test_monitor_run_never_reads_runtime_release_scope(tmp_path):
    class DeploymentTrapAdapters(_RecordingAdapters):
        def read_runtime_release_scope(self):
            raise AssertionError("deployment evidence must not be read")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=DeploymentTrapAdapters(),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        notify=False,
    )
    assert outcome.result.healthy is True
```

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py::test_monitor_snapshot_has_no_deployment_evidence_contract \
  tests/test_production_safety_monitor.py::test_monitor_run_never_reads_runtime_release_scope -q
```

Expected: the structural test fails because the snapshot and module still expose runtime-release evidence.

**Step 3: Delete the deployment evaluator and collection path**

- Remove `runtime_release_scope` from `MonitorSnapshot`.
- Delete `evaluate_runtime_release_scope`.
- Delete runtime-release collection from `run_production_safety_monitor`.
- Delete the runtime-release branch in `evaluate_monitor_snapshot`.
- Remove evaluator-only imports and tests.
- Remove fixed/capture reason codes `runtime_release_invalid`, `runtime_release_mixed`, `runtime_identity_unproven`, `runtime_capability_unproven`, and `runtime_unit_hash_drift`.
- Do not alter business invariant evaluation.

**Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_production_safety_monitor.py -q`

Expected: PASS with no removed deployment reason code emitted.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git diff --cached --name-only
git commit -m "refactor(monitor): remove deployment evidence gates"
```

### Task 2: Delete topology, systemd, /proc, and release-file adapters

**Files:**
- Modify: `tests/test_production_safety_monitor.py:320-480`
- Modify: `tests/test_production_safety_monitor.py:3780-3880`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:640-990`

**Step 1: Write failing adapter tests**

Add:

```python
def test_production_adapter_exposes_no_deployment_or_systemd_probes(tmp_path):
    adapters = ProductionSafetyAdapters(database_path=tmp_path / "unused.db")
    for name in (
        "read_runtime_release_scope",
        "_validated_release_evidence",
        "_with_systemd_identity",
        "_release_unit_hashes_valid",
        "_runtime_service_names",
    ):
        assert not hasattr(adapters, name)
```

Change the journal test so loop-health readers raise if called, then assert the journal command contains this bounded unit list exactly once:

```python
expected_units = (
    "telegram-kol.service",
    "telegram-kol-worker.service",
    "telegram-kol-web.service",
    "telegram-kol-ingest.service",
)
```

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py::test_production_adapter_exposes_no_deployment_or_systemd_probes \
  tests/test_production_safety_monitor.py -k journal -q
```

Expected: FAIL because deployment methods and topology probing remain.

**Step 3: Apply the minimal adapter deletion**

- Remove release path, commit, manifest, unit root, loop-health, and deployment-identity fields.
- Delete `read_runtime_release_scope`, `_validated_release_evidence`, `_with_systemd_identity`, `_release_unit_hashes_valid`, and `_runtime_service_names`.
- Make `read_service_state()` call only `read_loopback_settings()`.
- Make journal collection pass `--unit` for the monolith and all three split runtime units without topology discovery.
- Remove now-unused release, identity, systemd, and cross-process imports.
- Preserve bounded timeouts, output limits, settings, journal classification, database checks, contract-spec checks, and management audit.

**Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_production_safety_monitor.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git diff --cached --name-only
git commit -m "refactor(monitor): drop cross-process deployment probes"
```

### Task 3: Simplify the monitor CLI and systemd units

**Files:**
- Modify: `tests/test_production_safety_monitor.py:480-650`
- Modify: `tests/test_server_monitor_installation.py:80-180`
- Modify: `src/telegram_kol_research/cli.py:2579-2750`
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Modify: `deploy/systemd/telegram-kol-monitor-test-notification.service`

**Step 1: Write failing CLI and unit tests**

Add a CLI test that invokes `monitor-production-safety` without `--expected-release-commit`, `--expected-release-manifest-sha256`, `--release-path`, or loop-health URL options and reaches the mocked monitor runner.

Update the unit contract test:

```python
for service in services:
    assert "--expected-release-commit" not in service
    assert "--expected-release-manifest-sha256" not in service
    assert "--release-path" not in service
    assert "--web-loop-health-url" not in service
    assert "--ingest-loop-health-url" not in service
    assert "--worker-loop-health-url" not in service
    assert "InaccessiblePaths=-/run/dbus/system_bus_socket" in service
```

Keep the assertion that `PYTHONPATH=${TELEGRAM_KOL_MONITOR_RELEASE_PATH}/src` loads monitor code from the immutable release.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py -k 'cli and monitor' \
  tests/test_server_monitor_installation.py -q
```

Expected: FAIL because CLI deployment arguments remain and two monitor units still expose the system bus.

**Step 3: Remove deployment-only CLI wiring**

- Delete expected release commit, manifest, release path, and three loop-health URL options.
- Delete immutable-monitor argument validation and expected-head wiring.
- Stop passing deleted values into `MonitorExpectations` and `ProductionSafetyAdapters`.
- Remove deployment-check and loop-health arguments from all three unit files.
- Add or retain `InaccessiblePaths=-/run/dbus/system_bus_socket` in all three units.
- Retain immutable `PYTHONPATH`, business expectations, HTTP sources, database binding, credentials, and notification flags.
- Do not change activation-controller identity checks.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py \
  deploy/systemd/telegram-kol-monitor.service \
  deploy/systemd/telegram-kol-monitor-diagnostic.service \
  deploy/systemd/telegram-kol-monitor-test-notification.service \
  tests/test_production_safety_monitor.py tests/test_server_monitor_installation.py
git diff --cached --name-only
git commit -m "refactor(monitor): isolate business safety observer"
```

### Task 4: Prove A-group failures and the activation hard gate remain

**Files:**
- Verify: `tests/test_production_safety_monitor.py`
- Verify: `tests/test_scoped_release_activation.py`

**Step 1: Identify the retained tests before editing**

Keep the existing tests that exercise `auto_trade_enabled_drift`,
`contract_spec_ownership_drift`, `stale_entry_preamble_unresolved`, and
`audit_abnormal`. Keep
`test_monitor_release_proof_runs_the_actual_diagnostic_unit`, which proves the
activation adapter starts the diagnostic unit. Do not add a duplicate
characterization test that would pass before implementation.

**Step 2: Run the retained tests before deletion**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py \
  -k 'auto_trade_enabled_drift or contract_spec_ownership_drift or stale_entry_preamble_unresolved or audit_abnormal' -q
.venv/bin/python -m pytest \
  tests/test_scoped_release_activation.py::test_monitor_release_proof_runs_the_actual_diagnostic_unit -q
```

Expected: PASS. These are retained-behavior baselines, not tests for new code.

**Step 3: Make only compatibility fixes required by the tests**

Do not weaken, bypass, or catch activation diagnostic failures. Remove only stale references to deleted deployment reason codes.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py \
  tests/test_scoped_release_activation.py tests/test_server_monitor_installation.py -q
```

Expected: PASS.

**Step 5: Commit only if compatibility edits were required**

If no files changed in this task, make no empty commit. Otherwise stage only
the exact changed tests and commit with
`test(monitor): preserve business activation gates`.

### Task 5: Final verification, review, integration, and immutable stage

**Files:**
- Review all files changed since `1c671c92`.
- Use existing stage manifest `/tmp/codex-deepcoin-vacuum-cutover-21314fc4/stage-action.json`.

**Step 1: Run static and focused verification**

Run:

```bash
git diff --check 1c671c92..HEAD
.venv/bin/python -m pytest tests/test_production_safety_monitor.py \
  tests/test_scoped_release_activation.py tests/test_server_monitor_installation.py -q
```

Expected: PASS.

**Step 2: Request code review**

Use `requesting-code-review` against `1c671c92..HEAD`. Resolve every P1/P2 finding test-first. If production code changes, rerun focused tests.

**Step 3: Run the final full suite once**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass with only known skips and warnings.

**Step 4: Verify and push the exact tip**

```bash
git status --short
git rev-parse HEAD
git merge-base --is-ancestor origin/codex/deepcoin-auto-trading-v1 HEAD
git push origin HEAD:codex/deepcoin-auto-trading-v1
git ls-remote origin refs/heads/codex/deepcoin-auto-trading-v1
```

Expected: clean tree and identical local/remote SHA.

**Step 5: Stage the exact candidate**

```bash
ACTION_MANIFEST=/tmp/codex-deepcoin-vacuum-cutover-21314fc4/stage-action.json \
EXPECTED_COMMIT=<exact-reviewed-sha> \
./scripts/server_git_update.sh stage
```

Expected: `status=staged` with exact commit, content hash, manifest hash, and tree. Verify no `.pyc` files and immutable ownership/modes.

**Step 6: Stop at the known A-group boundary**

Do not create activation authorization or mutate production data. Record that `entry_preambles.id=13` remains pending and intentionally blocks activation. Confirm all eight controlled units remain inactive with PID 0 and exact maintenance inhibit files.
