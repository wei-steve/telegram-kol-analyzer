# Production Monitor And Runtime Agent Decoupling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the noisy legacy production monitor with a temporally coherent read-only snapshot refresher and deterministic sentinel that hands confirmed incidents to the Runtime Incident system, then delete every superseded monitor path.

**Architecture:** A dedicated read-only Deepcoin refresher seals three bounded snapshot generations on a two-minute timer. A five-minute sentinel evaluates coherent local and exchange facts through one reason-policy registry, persists `COMPLETED/FAILED` separately from `HEALTHY/UNHEALTHY/UNKNOWN`, and submits only confirmed incidents through an idempotent shared loopback contract. Deployment uses a dormant shadow stage followed by a separately reviewed cleanup stage; no production task may cross its explicit approval checkpoint.

**Tech Stack:** Python 3.12, Typer, FastAPI, SQLAlchemy/SQLite query-only reads, httpx, pytest, systemd, Bash installer tests.

---

## Mandatory Execution Rules

- Read `AGENTS.md` at the start of every execution turn and immediately before every production operation.
- Use @test-driven-development for every behavior change and @requesting-code-review at both local review gates.
- Work only in `/Users/steven/Documents/telegram获取消息-deployment-gate-recovery-plan` on `codex/deployment-gate-batch-recovery-plan` unless the operator explicitly changes the branch.
- Preserve unrelated worktree changes. Never reset, clean, or overwrite them.
- Do not deploy, stop a production unit, enable a timer, change a root-owned environment file, or run a live canary before the exact approval checkpoint in Task 10.
- Do not perform the Batch 119 stopped capture or apply in this plan. Resume `docs/plans/2026-08-14-deployment-gate-batch-recovery.md` only after this plan stops and the operator explicitly directs it.
- Do not edit the production database, replay historical Telegram messages, call a Deepcoin mutation method, or enable MiMo v2.
- A read-only refresher credential is a production prerequisite. If its exchange permissions cannot be proven read-only, stop; never substitute the trading credential.
- Keep the phase-one implementation dormant by default. The old timer remains the production path until the separately approved shadow/cutover procedure.
- Stage two must remove the legacy path. The project is not complete while both paths remain.

## Phase One: Add The New Path Dormant

### Task 1: Create The Shared Monitor Contract

**Files:**
- Create: `src/telegram_kol_research/production_monitor_contract.py`
- Create: `tests/test_production_monitor_contract.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_production_safety_monitor.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing contract tests**

Cover exact adapter names, exact reason-policy names, canonical ordering,
duplicate rejection, maximum sizes, deterministic submission IDs, and strict
unknown-field rejection. Include `composite`, `entry_preamble`, and `coverage`.

```python
def test_v2_contract_has_one_adapter_authority():
    assert MONITOR_ADAPTER_NAMES == frozenset({
        "service", "head", "settings", "journal", "events", "audit",
        "composite", "entry_preamble", "coverage", "readiness",
    })


def test_projection_submission_id_is_deterministic():
    first = build_monitor_projection(_projection_input())
    second = build_monitor_projection(_projection_input())
    assert first["submission_id"] == second["submission_id"]
    assert len(first["submission_id"]) == 64
```

**Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/pytest -q tests/test_production_monitor_contract.py
```

Expected: FAIL because the shared module does not exist.

**Step 3: Implement the minimal closed contract**

Create immutable schema constants and pure builders/parsers. The v2 body must
contain only:

```python
MONITOR_PROJECTION_V2_FIELDS = frozenset({
    "schema_version",
    "submission_id",
    "checked_at",
    "execution_status",
    "observed_health",
    "reason_codes",
    "adapter_failures",
    "fallback_reason",
})
```

Canonicalize before hashing and compare hashes in constant time. Keep the
existing v1 receiver only as an explicitly marked phase-one compatibility
path for the currently active legacy timer. Do not add new behavior to v1.

**Step 4: Replace duplicate phase-one allowlists**

Import the shared adapter and reason sets in the new producer path and the v2
FastAPI parser. Add a regression proving v2 `composite` and `coverage` cannot
receive HTTP 422. Leave the old v1 parser reachable only by a v1 payload.

**Step 5: Run focused tests**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_contract.py \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py -k 'monitor_capture or monitor_contract'
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/production_monitor_contract.py \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/web_app.py \
  tests/test_production_monitor_contract.py \
  tests/test_production_safety_monitor.py tests/test_web_app.py
git commit -m "feat: share production monitor contract"
```

### Task 2: Build The Sealed Three-Generation Snapshot Store

**Files:**
- Create: `src/telegram_kol_research/production_monitor_snapshot.py`
- Create: `tests/test_production_monitor_snapshot.py`
- Reference: `src/telegram_kol_research/live_position_snapshot.py`
- Reference: `src/telegram_kol_research/deepcoin_snapshot_authority.py`

**Step 1: Write failing store tests**

Test schema validation, generation ordering, atomic replacement, mode `0600`,
three-generation retention, failure envelopes, incomplete pagination, account
scope mismatch, duplicate position/order identities, future timestamps,
out-of-order generations, oversized data, and symlink refusal.

```python
def test_store_retains_only_three_distinct_complete_generations(tmp_path):
    store = ProductionMonitorSnapshotStore(tmp_path / "manifest.json")
    for ordinal in range(4):
        store.seal_success(_generation(ordinal))
    loaded = store.load()
    assert [item.generation for item in loaded.generations] == [1, 2, 3]


def test_failure_does_not_refresh_last_success(tmp_path):
    store = ProductionMonitorSnapshotStore(tmp_path / "manifest.json")
    store.seal_success(_generation(1))
    store.seal_failure(_failure(2, "exchange_timeout"))
    assert store.load().last_success.generation == 1
    assert store.load().latest_attempt.generation == 2
```

**Step 2: Verify failure**

```bash
.venv/bin/pytest -q tests/test_production_monitor_snapshot.py
```

Expected: FAIL because the store is absent.

**Step 3: Implement immutable envelopes and atomic persistence**

Use dataclasses for `SnapshotCollectionEvidence`, `SnapshotGeneration`, and
`SnapshotManifest`. Store sanitized positions, open orders, and pending trigger
orders plus per-collection completeness. Use descriptor-safe validation,
temporary-file fsync, `os.replace`, directory fsync, and restrictive modes.

Do not reuse the display-only `LivePositionSnapshotStore`; its one-generation
and UI-refresh semantics are intentionally different.

**Step 4: Run the focused tests**

```bash
.venv/bin/pytest -q tests/test_production_monitor_snapshot.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_monitor_snapshot.py \
  tests/test_production_monitor_snapshot.py
git commit -m "feat: seal production monitor snapshots"
```

### Task 3: Add The Read-Only Deepcoin Refresher And CLI

**Files:**
- Create: `src/telegram_kol_research/production_monitor_refresher.py`
- Create: `tests/test_production_monitor_refresher.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_cli_smoke.py`
- Reference: `src/telegram_kol_research/deepcoin_client.py`
- Reference: `src/telegram_kol_research/deepcoin_snapshot_authority.py`

**Step 1: Write failing authority tests**

Use a fake client that records method names. Prove the refresher calls only
raw collection readers such as `read_positions`, `read_open_orders`, and
`read_trigger_orders_pending`. Give the fake mutation methods that raise if
touched. Test complete empty collections separately from failed empty
collections.

```python
def test_refresher_has_no_exchange_mutation_surface(tmp_path):
    client = RecordingDeepcoinClient()
    outcome = refresh_production_monitor_snapshot(
        client=ReadOnlyDeepcoinMonitorClient(client),
        store=ProductionMonitorSnapshotStore(tmp_path / "snapshot.json"),
        now=NOW,
    )
    assert outcome.execution_status == "COMPLETED"
    assert client.calls == [
        "read_positions", "read_open_orders", "read_trigger_orders_pending"
    ]
```

Also add an architecture test that the refresher module contains none of the
mutation method names or mutation endpoint constants.

**Step 2: Verify failure**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_refresher.py \
  tests/test_cli_smoke.py -k 'production_monitor_snapshot'
```

Expected: FAIL because the refresher/command is absent.

**Step 3: Implement a narrow protocol and orchestration**

Expose only the required read methods through `DeepcoinMonitorReadProtocol`.
Validate `uid_scope_hash`, collection evidence, page completion, bounded rows,
and generation identity before sealing. Catch closed read failures and seal a
failure attempt without touching the last successful capture time.

**Step 4: Add the dormant CLI**

Add `refresh-production-monitor-snapshot` with exact options for the manifest
path, wall-clock timeout, and no hidden fallback to `data/research.db`. Load
credentials in environment-only mode. Emit one bounded JSON summary and return
nonzero only when configuration or result persistence prevents a completed
attempt.

**Step 5: Run focused tests**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_refresher.py \
  tests/test_production_monitor_snapshot.py \
  tests/test_deepcoin_client.py \
  tests/test_cli_smoke.py -k 'monitor or deepcoin'
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/production_monitor_refresher.py \
  src/telegram_kol_research/cli.py \
  tests/test_production_monitor_refresher.py tests/test_cli_smoke.py
git commit -m "feat: refresh monitor exchange evidence read only"
```

### Task 4: Implement The Reason Policy And Settling State Machine

**Files:**
- Create: `src/telegram_kol_research/production_monitor_policy.py`
- Create: `src/telegram_kol_research/production_monitor_state.py`
- Create: `tests/test_production_monitor_policy.py`
- Create: `tests/test_production_monitor_state.py`
- Reference: `src/telegram_kol_research/strategy_management_components.py`
- Reference: `src/telegram_kol_research/models.py`

**Step 1: Write failing policy-coverage tests**

Require every sentinel reason to appear exactly once. Define explicit policy
classes `IMMEDIATE`, `SETTLING`, and `EVIDENCE_UNKNOWN`; no default should
convert an unregistered reason to healthy or confirmed.

```python
def test_every_reason_has_exactly_one_policy():
    assert set(REASON_POLICIES) == set(SENTINEL_REASON_CODES)


def test_unknown_reason_fails_closed():
    result = classify_candidate(_candidate(reason="future_reason"), _context())
    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.deployment_blocking is True
```

**Step 2: Add fake-clock deadline tests**

Cover one second before, exactly at, and one second after
`execution_deadline_at`; snapshots before/after `last_progress_at`; repeated
generation rejection; two distinct bad generations; bad then good; confirmed
then one good; confirmed then two good; and future/out-of-order time.

**Step 3: Verify failures**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_policy.py \
  tests/test_production_monitor_state.py
```

Expected: FAIL because policy/state modules do not exist.

**Step 4: Implement the registry and pure transition function**

Use immutable inputs and return a new bounded candidate state. For settling
policies, require the row's durable deadline. A missing required deadline is
`UNKNOWN`. Cross-source mismatch confirmation requires two distinct complete
post-progress snapshot generations. Resolution requires a durable terminal
fact or the policy's configured distinct healthy generations.

**Step 5: Implement versioned atomic state persistence**

Persist only bounded candidate fingerprints, timestamps, generations,
incident acceptance, fallback state, latest completed result, and audit cursor.
Reject legacy/unknown shapes rather than silently normalizing them. Stage one
uses a new file, `/var/lib/telegram-kol-monitor/sentinel-v2.json`, so the old
timer cannot corrupt it.

**Step 6: Run tests and commit**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_policy.py \
  tests/test_production_monitor_state.py
git add src/telegram_kol_research/production_monitor_policy.py \
  src/telegram_kol_research/production_monitor_state.py \
  tests/test_production_monitor_policy.py tests/test_production_monitor_state.py
git commit -m "feat: classify monitor evidence after settling"
```

### Task 5: Build The Lightweight Sentinel And Exit Semantics

**Files:**
- Create: `src/telegram_kol_research/production_monitor_sentinel.py`
- Create: `tests/test_production_monitor_sentinel.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_cli_smoke.py`
- Reference: `src/telegram_kol_research/production_safety_monitor.py`

**Step 1: Write failing two-axis result tests**

```python
@pytest.mark.parametrize(
    ("health", "persisted", "exit_code"),
    [
        ("HEALTHY", True, 0),
        ("UNHEALTHY", True, 0),
        ("UNKNOWN", True, 0),
        ("UNKNOWN", False, 1),
    ],
)
def test_exit_code_describes_execution_not_business_health(
    health, persisted, exit_code
):
    assert run_sentinel_case(health, persisted).exit_code == exit_code
```

Test that `SETTLING`, `STARTING`, and `UNKNOWN` never submit an Agent incident
and remain deployment-blocking. Test that immediate and confirmed candidates
produce one v2 projection.

**Step 2: Verify failures**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_sentinel.py \
  tests/test_cli_smoke.py -k sentinel
```

Expected: FAIL because the sentinel is absent.

**Step 3: Implement a pure evaluation boundary and thin I/O runner**

Define `SentinelObservation`, `SentinelResult`, and `SentinelRunOutcome`.
Collection errors become sanitized adapter facts. Evaluation never receives
raw exceptions, credentials, messages, order payloads, or unbounded rows.

Initially reuse only deterministic read functions from
`production_safety_monitor.py`; record every reused symbol in the stage-two
deletion inventory. Do not reuse the old notification decision or exit-code
logic.

**Step 4: Add the dormant CLI**

Add `run-production-monitor-sentinel` with explicit state, snapshot, database,
checkout, and loopback endpoints. Emit the exact bounded top-level dimensions.
The command has no `--notify` option; notification routing is Task 7.

**Step 5: Run tests and commit**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_sentinel.py \
  tests/test_production_monitor_policy.py \
  tests/test_production_monitor_state.py \
  tests/test_cli_smoke.py -k 'sentinel or monitor'
git add src/telegram_kol_research/production_monitor_sentinel.py \
  src/telegram_kol_research/cli.py \
  tests/test_production_monitor_sentinel.py tests/test_cli_smoke.py
git commit -m "feat: add deterministic production sentinel"
```

### Task 6: Add Readiness Evidence And Temporally Coherent Fact Readers

**Files:**
- Create: `src/telegram_kol_research/production_monitor_facts.py`
- Create: `tests/test_production_monitor_facts.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/production_monitor_sentinel.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_strategy_management_components.py`

**Step 1: Write failing startup/readiness tests**

Test main-service startup with no reconciliation success, reconciliation
success but stale supervisor heartbeat, complete readiness, restarted worker,
and future heartbeat. Require `STARTING/UNKNOWN` until evidence exists; elapsed
wall time alone cannot turn readiness healthy.

**Step 2: Write failing coherent-read tests**

Use a temporary SQLite WAL database and a writer hook between queries. Prove
one query-only transaction sees one coherent local view. For every exchange
comparison, test that snapshot capture completion must be later than the
relevant `last_progress_at` and that the row's own `execution_deadline_at` is
used instead of the legacy global fifteen-minute threshold.

**Step 3: Add a strict loopback readiness projection**

Expose an authenticated bounded endpoint under the existing monitor-capture
token. Return only service start generation, first/last successful Deepcoin
reconciliation heartbeat, management-worker heartbeat, message-supervisor
heartbeat, and closed policy status. Do not expose raw errors, IDs, counts that
are not bounded, or provider configuration.

Update the relevant supervised loops to publish a heartbeat only after a
successful full cycle. A started task is not a successful heartbeat.

**Step 4: Implement query-only fact readers**

Move new sentinel readers into `production_monitor_facts.py`. Return typed,
bounded facts with source timestamps/deadlines. Generic journald ERROR lines
must not become immediate business incidents; only registered closed reason
markers may be immediate.

**Step 5: Run focused tests**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_facts.py \
  tests/test_production_monitor_sentinel.py \
  tests/test_web_app.py -k 'readiness or monitor' \
  tests/test_strategy_management_components.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/production_monitor_facts.py \
  src/telegram_kol_research/production_monitor_sentinel.py \
  src/telegram_kol_research/web_app.py \
  tests/test_production_monitor_facts.py \
  tests/test_production_monitor_sentinel.py tests/test_web_app.py \
  tests/test_strategy_management_components.py
git commit -m "feat: correlate monitor facts with durable deadlines"
```

### Task 7: Implement Idempotent Incident Routing And Narrow Fallback

**Files:**
- Create: `src/telegram_kol_research/production_monitor_notifications.py`
- Create: `tests/test_production_monitor_notifications.py`
- Modify: `src/telegram_kol_research/production_monitor_sentinel.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_runtime_incident_adapters.py`

**Step 1: Write failing lost-response/idempotency tests**

Simulate server commit followed by client timeout, then resubmit the same
`submission_id`. Assert one incident generation, one accepted response, and no
fallback. Test a 422 schema refusal, repeated transport unavailability,
notification-pipeline SLA, normal Agent queue time, Agent timeout, fallback
deduplication, and `fallback_pending` retry.

**Step 2: Verify failures**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_notifications.py \
  tests/test_web_app.py -k monitor_capture \
  tests/test_runtime_incident_adapters.py -k monitor
```

Expected: FAIL for missing v2 routing behavior.

**Step 3: Make v2 capture idempotent**

Bind the monitor source identity to `submission_id` and existing unique Runtime
Incident source identity. A repeated accepted submission returns the prior
semantic acceptance. Do not add an unrestricted generic incident creation API.

**Step 4: Add separate channel-health deadlines**

Track incident intake, deterministic notification, and Agent diagnosis
separately. Only intake or deterministic-notification failure can enable the
fixed fallback. Normal Agent diagnosis queueing never causes a duplicate
monitor alert.

**Step 5: Implement the fixed fallback formatter**

It accepts only closed reason/component labels and timestamps, is bounded and
redacted, and cannot format ordinary business alerts after incident acceptance.
Delivery failure persists `fallback_pending`; it does not change a completed
sentinel exit code.

**Step 6: Run tests and commit**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_notifications.py \
  tests/test_production_monitor_sentinel.py \
  tests/test_web_app.py -k 'monitor_capture or monitor_readiness' \
  tests/test_runtime_incident_adapters.py
git add src/telegram_kol_research/production_monitor_notifications.py \
  src/telegram_kol_research/production_monitor_sentinel.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/runtime_incident_adapters.py \
  tests/test_production_monitor_notifications.py \
  tests/test_production_monitor_sentinel.py tests/test_web_app.py \
  tests/test_runtime_incident_adapters.py
git commit -m "feat: route confirmed monitor incidents idempotently"
```

### Task 8: Add Dormant V2 systemd Units And Split The Heavy Audit

**Files:**
- Create: `deploy/systemd/telegram-kol-monitor-snapshot.service`
- Create: `deploy/systemd/telegram-kol-monitor-snapshot.timer`
- Create: `deploy/systemd/telegram-kol-sentinel.service`
- Create: `deploy/systemd/telegram-kol-sentinel.timer`
- Create: `deploy/systemd/telegram-kol-monitor-audit.service`
- Create: `deploy/systemd/telegram-kol-monitor-audit.timer`
- Create: `scripts/install_production_monitor_v2.sh`
- Create: `tests/test_production_monitor_v2_installation.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write failing static unit/installer tests**

Require:

- snapshot timer cadence of two minutes;
- sentinel cadence of five minutes;
- a separately scheduled low-frequency audit;
- distinct unprivileged snapshot and sentinel identities;
- no production database mount in the snapshot unit;
- query-only database mounts in sentinel/audit units;
- state/cache write paths only;
- no system bus/control socket access;
- empty capabilities and `NoNewPrivileges=true`;
- exact API host/address families and no checkout `.env` mount;
- a root-owned mode-0600 snapshot credential file containing only allowlisted
  read-only Deepcoin fields plus a proof marker;
- installer default behavior leaves all v2 timers disabled and inactive;
- refusal if any target unit is active during install;
- refusal to use the main trading credential file;
- no `systemctl restart telegram-kol.service`.

**Step 2: Verify failures**

```bash
.venv/bin/pytest -q tests/test_production_monitor_v2_installation.py
```

Expected: FAIL because the units/installer are absent.

**Step 3: Implement hardened units**

Use separate state directories under `/var/lib/telegram-kol-monitor-v2/` and a
separate sealed snapshot directory. The refresher unit can write only the
snapshot store. The sentinel can read that store and the production database
but can write only its own state. Keep every timer without `[Install]` linkage
from the main service.

**Step 4: Implement an install-only helper**

The helper validates the fixed production checkout, unit inactivity, exact
credential ownership/content, read-only proof marker, state path metadata, and
unit hardening before mutation. It copies units and reloads systemd but has no
enable option in the phase-one commit. Activation is an explicit Task 11
operator procedure.

**Step 5: Split heavy audit CLI behavior**

Add a dedicated audit command that reads the new state schema and updates only
the audit result/cursor. Remove `--force-full-audit` from the new sentinel CLI;
do not remove it from the legacy CLI until stage two.

**Step 6: Run tests and commit**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_v2_installation.py \
  tests/test_production_monitor_snapshot.py \
  tests/test_production_monitor_refresher.py \
  tests/test_production_monitor_sentinel.py \
  tests/test_cli_smoke.py -k 'monitor or sentinel'
git add deploy/systemd/telegram-kol-monitor-snapshot.service \
  deploy/systemd/telegram-kol-monitor-snapshot.timer \
  deploy/systemd/telegram-kol-sentinel.service \
  deploy/systemd/telegram-kol-sentinel.timer \
  deploy/systemd/telegram-kol-monitor-audit.service \
  deploy/systemd/telegram-kol-monitor-audit.timer \
  scripts/install_production_monitor_v2.sh \
  src/telegram_kol_research/cli.py \
  tests/test_production_monitor_v2_installation.py tests/test_cli_smoke.py
git commit -m "feat: install dormant production monitor v2"
```

### Task 9: Add The Phase-One Race Matrix And Operator Documentation

**Files:**
- Create: `tests/test_production_monitor_temporal_races.py`
- Create: `docs/production-monitor-v2-runbook.md`
- Create: `docs/production-monitor-v2-cleanup-inventory.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Write the temporal race matrix**

Use fake clocks and injected adapters for:

- submit/readback delay;
- cancellation still visible after accepted response;
- partial fill and staggered position/protection updates;
- snapshot captured before local progress;
- two identical reads of one generation;
- two distinct bad generations;
- first bad then good;
- incomplete pagination and authoritative empty lists;
- exchange timeout/rate limit/recovery;
- startup without first successful worker cycles;
- incident commit with lost HTTP response;
- normal Agent queueing versus expired SLA;
- timer overlap, killed process, state-write failure, clock rollback, and future
  snapshot;
- confirmed anomaly flapping and two-generation recovery;
- immediate durable `submit_unknown` and `recovery_required`.

Every case must assert deployment blocking independently from notification and
Agent eligibility.

**Step 2: Run the race tests**

```bash
.venv/bin/pytest -q tests/test_production_monitor_temporal_races.py
```

Expected: PASS after Tasks 1-8.

**Step 3: Write the runbook**

Document the plain-language meaning of systemd execution status versus
structured production health, the three settling states, exact freshness
budgets, how to inspect redacted state, no-notify shadow commands, activation
ordering, rollback, and the rule that `UNKNOWN` blocks deployment.

**Step 4: Write the deletion inventory**

List exact legacy symbols, CLI flags, files, unit directives, installer
branches, tests, fixtures, and documentation to remove in Task 12. Include at
least old direct ordinary notification, old exit coupling, old state schema,
duplicate allowlists, UI-cache dependency, and full-audit-in-sentinel behavior.

**Step 5: Run documentation/static checks and commit**

```bash
git diff --check
.venv/bin/pytest -q \
  tests/test_production_monitor_temporal_races.py \
  tests/test_production_monitor_v2_installation.py
git add tests/test_production_monitor_temporal_races.py \
  docs/production-monitor-v2-runbook.md \
  docs/production-monitor-v2-cleanup-inventory.md \
  docs/server-deployment.md docs/migration-handoff.md
git commit -m "test: cover production monitor timing races"
```

### Task 10: Complete Phase-One Local Verification And Stop For Deployment Approval

**Files:**
- Modify only if tests/review require: files changed in Tasks 1-9

**Step 1: Run focused suites**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_contract.py \
  tests/test_production_monitor_snapshot.py \
  tests/test_production_monitor_refresher.py \
  tests/test_production_monitor_policy.py \
  tests/test_production_monitor_state.py \
  tests/test_production_monitor_facts.py \
  tests/test_production_monitor_sentinel.py \
  tests/test_production_monitor_notifications.py \
  tests/test_production_monitor_temporal_races.py \
  tests/test_production_monitor_v2_installation.py \
  tests/test_web_app.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_deployment_preflight.py
```

Expected: PASS.

**Step 2: Run adjacent safety suites**

```bash
.venv/bin/pytest -q \
  tests/test_strategy_management_components.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_snapshot.py \
  tests/test_server_monitor_installation.py \
  tests/test_cli_smoke.py
```

Expected: PASS.

**Step 3: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS with only already documented skips/warnings.

**Step 4: Run static safety checks**

```bash
git diff --check
rg -n 'place_order|cancel_order|trigger_order|set_position_sltp|close_bound_position' \
  src/telegram_kol_research/production_monitor_refresher.py \
  deploy/systemd/telegram-kol-monitor-snapshot.service
rg -n 'ReadWritePaths=.*/data/research.db|DEEPCOIN_API_SECRET' \
  deploy/systemd/telegram-kol-sentinel.service \
  deploy/systemd/telegram-kol-monitor-snapshot.service
```

Expected: the first search has no matches; the second has no unsafe mount or
literal secret value. A credential variable name is allowed only in the
root-owned environment validation code and must never be printed.

**Step 5: Request independent code review**

Use @requesting-code-review against the phase-one range. Resolve every Critical
or Important finding with a failing regression first, rerun focused/full tests,
and commit the correction.

**Step 6: Push the reviewed branch only**

```bash
git status --short
git log -1 --format=%H
git push origin codex/deployment-gate-batch-recovery-plan
```

Expected: clean worktree and exact reviewed SHA on the independent branch.

**Step 7: STOP — ordinary production deployment approval boundary**

Report the reviewed SHA, test totals, review result, dormant defaults, read-only
credential prerequisite, and exact proposed server operations. Do not run any
server mutation. Require a new explicit approval for phase-one deployment;
prior Batch 119 approval does not authorize it.

## Phase-One Production Shadow And Cutover

### Task 11: Deploy Dormant, Canary Read-Only Evidence, Shadow, Then Cut Over

**Prerequisite:** The operator has explicitly approved phase-one ordinary code
deployment after Task 10. Re-read `AGENTS.md`; re-prove a safe window. If any
time-sensitive recognition, execution, management, protection, reconciliation,
Runtime Agent claim, or unknown exchange operation is active, stop.

**Files changed on server:**
- `/opt/telegram-kol-analyzer` through the reviewed Git deployment helper
- `/etc/systemd/system/telegram-kol-monitor-snapshot.*`
- `/etc/systemd/system/telegram-kol-sentinel.*`
- `/etc/systemd/system/telegram-kol-monitor-audit.*`
- `/etc/telegram-kol-monitor-snapshot.env`
- `/var/lib/telegram-kol-monitor-v2/`

**Step 1: Capture the pre-deployment redacted baseline**

Record production SHA, main/Agent/scanner/legacy-monitor unit states, timer
states, latest Telegram/checkpoint watermarks, zero in-flight facts, current
MiMo mode, and current monitor reason codes. Do not include credentials, raw
IDs, messages, or exchange payloads.

**Step 2: Deploy code with all v2 timers disabled**

Use the repository's reviewed deployment helper. Confirm the exact reviewed
SHA before and after editable install/restart. This is an ordinary code deploy;
it must use existing fail-closed deployment preflight and must not apply Batch
119.

**Step 3: Install units dormant**

Run `scripts/install_production_monitor_v2.sh` with no enable flag. Prove all
new services/timers are disabled/inactive and the legacy timer state is
unchanged.

**Step 4: Prove the credential boundary before any API canary**

Verify root ownership/mode and a separate exchange-side read-only permission.
If the same credential can trade or permission evidence is incomplete, stop
without running the refresher.

**Step 5: Run one manual refresher canary**

Run the exact snapshot service manually. Prove complete bounded generations,
correct account-scope fingerprint, no database access/write, no exchange write
request metrics, unchanged exchange write generation, and unchanged trading
state.

**Step 6: Run no-notify sentinel shadow comparisons**

Run the sentinel manually across at least one stable capture and every locally
simulated settling case available on the server. Compare bounded old/new facts.
No v2 incident, Telegram notification, or Agent claim is permitted during the
no-notify shadow.

**Step 7: Enable in order**

Enable the snapshot timer first. After at least three complete generations,
enable the sentinel timer. Enable the low-frequency audit timer last. Normal
notifications remain owned by Runtime Incident; enable only the narrow fallback
configuration after an authenticated idempotent no-op capture proves the
bridge.

**Step 8: Verify cutover evidence**

Prove fresh `COMPLETED + HEALTHY`, no settling/unknown candidates, accepted v2
schema, no duplicate incident/notification, main service and Telegram intake
health, zero unexpected restart/mutation, and MiMo v1. Keep the legacy timer
available only for the bounded shadow interval defined in the runbook; do not
delete it in phase one.

**Step 9: Stop and report**

Record the exact production SHA and redacted evidence. Stage-two local cleanup
may start only after the shadow acceptance criteria are met. If cutover fails,
disable v2 timers and restore the reviewed prior timer state; deployment stays
blocked rather than manufacturing health.

## Phase Two: Delete The Legacy Monitor

### Task 12: Move Remaining Shared Utilities And Delete Legacy Runtime Paths

**Files:**
- Create if still needed: `src/telegram_kol_research/bounded_subprocess.py`
- Modify: `src/telegram_kol_research/runtime_agent_production_audit.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Delete or reduce to no legacy runtime behavior: `src/telegram_kol_research/production_safety_monitor.py`
- Delete: `deploy/systemd/telegram-kol-monitor.service`
- Delete: `deploy/systemd/telegram-kol-monitor.timer`
- Delete: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Delete: `deploy/systemd/telegram-kol-monitor-test-notification.service`
- Delete: `scripts/install_server_monitor.sh`
- Delete/rewrite: `tests/test_production_safety_monitor.py`
- Delete/rewrite: `tests/test_server_monitor_installation.py`
- Modify: `tests/test_entry_assembly_fingerprint_repair.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `docs/production-monitor-v2-cleanup-inventory.md`

**Step 1: Convert the cleanup inventory into failing absence tests**

Write static/reachability assertions proving the repository contains no old
CLI command, old ordinary direct notification, old state schema fields, old
unit names, duplicate adapter allowlist, UI-cache monitor path, or legacy
`healthy -> exit 1` logic.

**Step 2: Move still-used generic utilities**

Move `_run_bounded_command` to `bounded_subprocess.py`. Move runtime-incident
source scanning to its owning Runtime Incident module. Move any still-current
pure fact evaluator to `production_monitor_facts.py`. Update consumers and
tests before deleting the old file.

**Step 3: Remove the old CLI and notification behavior**

Delete `monitor-production-safety`, `--notify`, `--test-notification`,
`--force-full-audit` on the old path, old formatter/decision/deduplication,
legacy recovery notifications, and v1 monitor-capture parsing.

**Step 4: Remove old state compatibility**

Delete legacy state readers/migration branches. The v2 state file remains the
only current schema. Preserve rollback by Git/unit version, not dead code.

**Step 5: Remove old units/installer/tests/docs**

Delete the inventoried files and rewrite any useful assertions against v2.
Do not delete the display-only `live_position_snapshot.py`; remove only the
monitor's dependency on the UI-driven cache.

**Step 6: Run focused tests**

```bash
.venv/bin/pytest -q \
  tests/test_production_monitor_contract.py \
  tests/test_production_monitor_snapshot.py \
  tests/test_production_monitor_refresher.py \
  tests/test_production_monitor_policy.py \
  tests/test_production_monitor_state.py \
  tests/test_production_monitor_facts.py \
  tests/test_production_monitor_sentinel.py \
  tests/test_production_monitor_notifications.py \
  tests/test_production_monitor_temporal_races.py \
  tests/test_production_monitor_v2_installation.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_cli_smoke.py
```

Expected: PASS and no skipped legacy-absence assertion.

**Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove legacy production monitor"
```

### Task 13: Bind Deployment Preflight To Structured Sentinel State

**Files:**
- Modify: `src/telegram_kol_research/deployment_preflight.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `scripts/server_git_update.ps1`
- Modify: `docs/server-deployment.md`
- Modify: `docs/production-monitor-v2-runbook.md`

**Step 1: Write failing preflight tests**

Add facts for timer activation, state schema, result freshness,
`execution_status`, `observed_health`, candidate states, incident acceptance,
and required evidence completeness. Test that all of these block:

- missing state;
- stale state;
- unsupported schema;
- `FAILED` execution;
- `UNHEALTHY` or `UNKNOWN` health;
- `SETTLING`, `STARTING`, or unsubmitted confirmed incident;
- inactive/disabled sentinel timer;
- incomplete snapshot/readiness evidence.

Also prove a systemd oneshot exit code of zero cannot override a blocked
structured result.

**Step 2: Verify failures**

```bash
.venv/bin/pytest -q tests/test_deployment_preflight.py -k monitor
```

Expected: FAIL because preflight does not consume v2 state.

**Step 3: Add bounded structured monitor facts**

Read the v2 state through its strict parser. Add only bounded booleans/enums and
the state fingerprint to the preflight artifact. Do not include reason details,
candidate identities, raw state, or exchange payloads. Every incomplete fact
adds a blocking reason.

**Step 4: Update the deployment helper**

Require the exact v2 state path and timer proof before the preliminary and
immediate pre-mutation preflight. Do not use `is-active` on the oneshot service
as health evidence. Keep all existing database/exchange/schema checks.

**Step 5: Run tests and commit**

```bash
.venv/bin/pytest -q \
  tests/test_deployment_preflight.py \
  tests/test_production_monitor_state.py \
  tests/test_production_monitor_v2_installation.py
git add src/telegram_kol_research/deployment_preflight.py \
  src/telegram_kol_research/cli.py tests/test_deployment_preflight.py \
  scripts/server_git_update.ps1 docs/server-deployment.md \
  docs/production-monitor-v2-runbook.md
git commit -m "fix: require structured sentinel deployment health"
```

### Task 14: Complete Stage-Two Verification And Stop For Cleanup Deployment Approval

**Files:**
- Modify only if review requires: Task 12-13 files

**Step 1: Prove legacy deletion**

```bash
rg -n 'monitor-production-safety|telegram-kol-monitor\.service|MonitorNotificationDecision|MONITOR_TEST_NOTIFICATION_TEXT|last_full_audit_date' \
  src tests deploy scripts docs --glob '!docs/plans/**'
```

Expected: no legacy runtime/config/test matches. Any documentation history that
must remain should be explicitly allowlisted in the absence test, not silently
ignored.

**Step 2: Run focused and adjacent tests**

Use the Task 10 commands plus `tests/test_deployment_preflight.py` and all
Runtime Incident/management/reconciliation suites affected by moved imports.

Expected: PASS.

**Step 3: Run the full suite**

```bash
.venv/bin/pytest -q
git diff --check
```

Expected: PASS with only documented skips/warnings and a clean diff check.

**Step 4: Request independent code review**

Use @requesting-code-review. Require zero Critical and zero Important findings
on deletion completeness, deployment fail-closed behavior, read-only authority,
temporal races, and notification ownership. Fix with TDD and rerun the full
suite.

**Step 5: Push the reviewed stage-two SHA**

```bash
git status --short
git log -1 --format=%H
git push origin codex/deployment-gate-batch-recovery-plan
```

Expected: clean worktree and exact reviewed SHA pushed.

**Step 6: STOP — second ordinary deployment approval boundary**

Report reviewed SHA, tests, review, phase-one production shadow evidence,
deletion proof, rollback SHA, and exact server operations. Do not remove or
replace production units without a fresh explicit approval.

### Task 15: Deploy Cleanup, Verify Production, And Record The SHA

**Prerequisite:** The operator explicitly approves the stage-two cleanup
deployment after Task 14. Re-read `AGENTS.md` and re-prove the safe window.

**Step 1: Capture redacted pre-deployment state**

Record exact SHA, v2 timers/results, legacy/v2 unit states, incident/channel
health, Telegram/checkpoint watermarks, zero in-flight facts, exchange snapshot
completeness, and MiMo v1.

**Step 2: Run fail-closed deployment preflight twice**

Use the reviewed helper and v2 structured monitor state. Both preliminary and
immediate pre-mutation checks must pass. Any `UNKNOWN`, stale result, candidate,
schema mismatch, or active exchange writer stops the deployment.

**Step 3: Deploy the exact reviewed cleanup SHA**

Use the project helper. Reinstall the editable package and perform only the
ordinary approved restart. Do not apply Batch 119 or enable MiMo v2.

**Step 4: Install current units and remove only inventoried legacy units**

Use the reviewed v2 installer/cleanup procedure. Confirm exact target paths
before removal. Reload systemd, preserve v2 timer enablement, and verify the
legacy units are absent. Do not touch `telegram-kol.service` outside the already
approved deployment restart.

**Step 5: Verify production**

Require main service and Telegram intake health, successful reconciliation and
worker heartbeats, three complete snapshot generations, fresh
`COMPLETED + HEALTHY`, no candidates, one notification owner, idempotent bridge,
zero new exchange/database mutation attributable to monitoring, passing
deployment preflight, and MiMo v1.

**Step 6: Roll back on any mismatch**

Disable v2 timers and restore the exact reviewed prior code/unit version. Leave
deployment blocked. Do not edit the database or exchange to make verification
pass.

**Step 7: Record the exact production SHA as the monitor rollout result**

Update the redacted handoff/status document locally with the exact deployed
SHA, verification evidence, rollback SHA, and the fact that Batch 119 remains
paused and MiMo v2 remains disabled. Commit the documentation without deploying
that documentation-only commit.

**Step 8: Return control**

Report completion of monitor cleanup and the exact production SHA. Do not
resume the stopped Batch 119 capture merely because monitor health is restored;
request explicit direction under the original executing-plans workflow. The
final MiMo v2 baseline is the final production SHA recorded after the original
deployment-gate recovery plan is subsequently completed.

## Final Acceptance Checklist

- The snapshot refresher has a proven read-only exchange credential and no
  database/mutation authority.
- Three bounded sealed generations are retained; partial/old evidence never
  becomes fresh.
- The five-minute sentinel and separate heavy audit do not overlap or block the
  trading path.
- `execution_status` and `observed_health` are independent.
- Every reason has one explicit settling/confirmation/resolution policy.
- Exchange/local mismatches respect durable deadlines and post-progress
  snapshot ordering.
- `SETTLING`, `STARTING`, and `UNKNOWN` block deployment without premature
  Telegram/Agent escalation.
- Confirmed incidents are idempotent and normal notifications have one owner.
- Fallback is channel-failure-only and deduplicated.
- Deployment preflight is fail-closed on structured v2 state.
- The legacy monitor code, units, state compatibility, configuration, tests,
  and ordinary direct notification path are deleted.
- Local/full/server verification and independent reviews have no Critical or
  Important findings.
- Production remains MiMo v1; no historical message was replayed; Batch 119 was
  not applied by this plan.
