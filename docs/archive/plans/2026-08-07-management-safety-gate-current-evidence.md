# Management Safety-Gate Current-Evidence Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent immutable historical protection incidents from indefinitely blocking exact risk-reducing management actions when one fresh complete exchange snapshot proves the position is currently healthy, while preserving every hard identity, completeness, concurrency, and unknown-outcome gate.

**Architecture:** Introduce an append-only exact-position health observation, reuse the canonical protection matcher for a three-way current-health decision, bind that decision into remediation and batch fingerprints, allow supersession only for proven zero-submission preflight refusals, and complete partial closes as durable close-plus-protection-convergence sagas. Roll out observation and divergence detection in shadow before enabling one exact action.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite additive migrations, Deepcoin REST reconciliation snapshots, pytest, Typer, systemd.

---

## Execution constraints

- Use test-driven development for every code task.
- Do not delete or update historical `PositionProtectionIncident` rows.
- Do not infer order ownership from symbol, side, price, time, or proximity.
- Never retry a submitted or unknown-outcome mutation.
- Keep the Runtime Incident Agent read-only and its action allowlists empty.
- Introduce runtime behavior dormant or shadow-only first.
- Deploy/restart only after proving a quiet window with two identical complete
  exchange snapshots and no strategy work in flight.
- Treat Flyang as a historical regression fixture because the user manually
  closed it. Never regenerate, promote, or apply its old remediation action.

### Task 1: Persist append-only current protection-health observations

**Files:**

- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `tests/test_migration_assets.py`

**Step 1: Write the failing model and migration tests**

Require a `position_protection_health_observations` table with:

- exact venue, binding ID, execution-leg ID, and `pos_id`;
- classification constrained to `healthy_current_evidence`,
  `recovery_required`, or `evidence_insufficient`;
- evidence fingerprint, exchange snapshot fingerprint, bounded source incident
  ID JSON, bounded summary JSON, and `observed_at`;
- indexes on exact position/time and evidence fingerprint.

Prove rows are insert-only in application code and that migration upgrade is
additive on a production-shaped database.

**Step 2: Run the tests and verify RED**

```bash
uv run pytest tests/test_db_migrations.py tests/test_migration_assets.py -q
```

Expected: FAIL because the model and table do not exist.

**Step 3: Add the smallest model and migration**

Follow the repository's existing `Base.metadata.create_all()` plus
`SQLITE_COMPAT_COLUMNS` bootstrap pattern. A new table is created additively;
do not invent a second migration framework. Store no raw exchange payload,
Telegram text, or credential.

**Step 4: Run focused migration tests**

```bash
uv run pytest tests/test_db_migrations.py tests/test_migration_assets.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py tests/test_db_migrations.py \
  tests/test_migration_assets.py
git commit -m "feat: record current protection health evidence"
```

### Task 2: Build one reusable three-way current-health classifier

**Files:**

- Modify: `src/telegram_kol_research/protection_health.py`
- Modify: `src/telegram_kol_research/protection_incident_convergence.py`
- Create: `tests/test_protection_health.py`
- Modify: `tests/test_protection_incident_convergence.py`

**Step 1: Write production-shaped failing tests**

Create an exact live position with a historical `protection_missing` incident,
pending TPSL rows without `posId`, canonical ledger ownership, distinct visible
primary/backup stops, and one verified take profit. Require
`healthy_current_evidence` and one bounded observation.

Add parameterized cases for incomplete pagination, exchange errors, ownership
conflict, missing primary, missing backup, same primary/backup ID, missing TP,
wrong binding/leg, and an unowned affecting order. Require
`evidence_insufficient` for unavailable evidence and `recovery_required` for a
complete unhealthy snapshot.

**Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_protection_health.py \
  tests/test_protection_incident_convergence.py -q
```

Expected: FAIL because planning has no reusable three-way classifier.

**Step 3: Extract and reuse canonical matching**

Implement a pure classification result in `protection_health.py` using
`build_position_protection_audit()` and account-wide ownership. Make the
incident convergence audit call the same classifier so audit and planning
cannot drift. Persistence of the observation occurs only after classification;
the pure function never commits.

**Step 4: Prove immutability and redaction**

Compare incident, ledger, backup, TP, binding, and leg source rows before and
after classification. Only the new observation count may increase. Assert that
serialized output contains hashes and bounded reason codes, not raw payloads.

**Step 5: Run focused tests**

```bash
uv run pytest tests/test_protection_health.py \
  tests/test_protection_incident_convergence.py \
  tests/test_protection_snapshot.py tests/test_protection_ledger.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/protection_health.py \
  src/telegram_kol_research/protection_incident_convergence.py \
  tests/test_protection_health.py tests/test_protection_incident_convergence.py
git commit -m "feat: classify protection gates from current evidence"
```

### Task 3: Replace the historical-row planner freeze without weakening hard gates

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `tests/test_strategy_management_batches.py`
- Modify: `tests/test_strategy_management_worker.py`

**Step 1: Reproduce the Flyang planning refusal**

Add a fixture with exact current size six, fraction `0.5`, requested close size
three, stop 64100, independent backup, TP 67000, and a historical
`protection_missing` row. Require the same complete snapshot to produce a
plan-only batch instead of `protection_recovery_required`.

**Step 2: Add hard-gate regression cases**

For the same fixture independently introduce incomplete TPSL pagination,
identity conflict, order-ownership conflict, live-size drift, duplicate active
batch, or a prior submitted/unknown mutation. Require the existing fail-closed
reason and zero exchange mutation.

**Step 3: Run tests and verify RED**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_batches.py \
  tests/test_strategy_management_worker.py \
  -k "protection_recovery or current_evidence or flyang" -q
```

Expected: the healthy historical-incident case fails under
`_protection_incident_requires_recovery()`.

**Step 4: Introduce the three-way gate**

Replace the incident-existence boolean with a function that consumes the
already loaded coherent reconciliation snapshot and exact target scope. A
healthy result neutralizes only the historical protection freeze for the
existing risk-reducing intents. `recovery_required` and
`evidence_insufficient` retain distinct blocked reasons. Do not add another
exchange snapshot call.

Persist the classification, observation ID/fingerprint, owned protection role
IDs, and exchange snapshot fingerprint into the batch target snapshot and
`management_target_fingerprint()` input.

**Step 5: Run focused and adjacent tests**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_batches.py \
  tests/test_strategy_management_worker.py \
  tests/test_auto_trade_execution.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_batches.py \
  tests/test_strategy_management_worker.py tests/test_auto_trade_execution.py
git commit -m "fix: gate management on current protection health"
```

### Task 4: Safely supersede zero-submission remediation refusals

**Files:**

- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify only if needed: `src/telegram_kol_research/db.py`
- Modify: `tests/test_position_management_remediation.py`
- Modify: `tests/test_strategy_management_planner.py`

**Step 1: Write failing supersession tests**

Prove that a `blocked/protection_recovery_required` predecessor with zero legs,
zero mutation intents, and zero exchange attempts can be superseded after the
new current-health proof. Require the replacement to reference its predecessor
and retain the same message/lifecycle/action identity while using a new
evidence fingerprint.

Parameterize forbidden predecessors: any management leg, mutation intent,
`executing`, `reconciling`, `recovery_required`, `submit_unknown`, or ambiguous
request marker. Require no new batch and no exchange write.

**Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py \
  -k "supersed or zero_submission or protection_recovery" -q
```

Expected: the eligible predecessor remains permanently idempotent-blocked.

**Step 3: Add explicit supersession metadata and predicate**

Add a nullable predecessor batch reference if the existing snapshot metadata
cannot express and query it safely. Centralize a predicate that positively
proves zero submission. Replace only within one database transaction after
rechecking current status and absence of legs/intents.

**Step 4: Freeze combined remediation evidence**

Include the current-health observation, exchange snapshot, exact position
economics, protection roles, source message, fraction, close size, and intended
post-close protection in action and chain fingerprints. Rebuild immediately
before promotion and refuse any mismatch.

**Step 5: Run the remediation suite**

```bash
uv run pytest tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_batches.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_management_remediation.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/models.py tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py src/telegram_kol_research/db.py
git commit -m "fix: supersede safe remediation preflight refusals"
```

### Task 5: Complete partial take profit as a protection-converging saga

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify only if needed: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_strategy_management_reconciliation.py`
- Modify: `tests/test_position_management_remediation.py`

**Step 1: Write the six-to-three Flyang saga test**

Require an exact partial-take-profit action to:

- freeze size six and close size three;
- cancel/replace only exact owned protection that cannot remain valid;
- submit one reduce-only close for three;
- wait for confirmed remaining size three;
- preserve stop 64100 and the independent backup;
- resize/rebuild TP 67000 to size three;
- succeed only after a complete final read-back.

Assert that no break-even move is introduced.

**Step 2: Add restart and unknown-outcome tests**

Restart after protection cancellation, close submission, close confirmation,
and TP replacement. Each phase must resume from durable intent and current
read-back. A close or cancel unknown outcome must submit nothing further until
reconciled. Repeated reconciliation must be idempotent.

**Step 3: Run tests and verify RED**

```bash
uv run pytest tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_management_remediation.py \
  -k "partial_take_profit and protection or flyang" -q
```

Expected: the plain partial-close path does not guarantee final TP/protection
convergence.

**Step 4: Reuse the composite phase machinery**

Generalize the existing partial-close/protection phase coordinator so
`partial_take_profit` can maintain the existing requested protection without
changing strategy intent. Use durable mutation intents and exact order IDs at
every write boundary.

**Step 5: Run focused and adjacent suites**

```bash
uv run pytest tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_management_remediation.py \
  tests/test_protection_health.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_management_remediation.py
git commit -m "fix: converge protection after partial take profit"
```

### Task 6: Detect safety-gate divergence in shadow

**Files:**

- Modify: `src/telegram_kol_research/runtime_incident_rules.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `tests/test_runtime_incident_rules.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `docs/runtime-incident-agent-runbook.md`

**Step 1: Write the failing shadow-rule tests**

Given a historical-only refusal and a healthy observation for the same exact
scope/fingerprint generation, require one bounded
`management_safety_gate_divergence` observation. Require no candidate when
current evidence is incomplete/unhealthy, identities differ, or the refusal
has any other hard reason.

**Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_adapters.py \
  -k safety_gate_divergence -q
```

Expected: FAIL because the rule does not exist.

**Step 3: Add the dormant/shadow rule**

Add an explicit disabled-by-default rule version. Its first deployment writes
only runtime observations, sends no notification, and grants no action
authority. Bound evidence to exact IDs, reason codes, timestamps, and hashes.

**Step 4: Document enable, disable, and rollback**

Add commands and expected no-notify behavior to the runbook. State that the
rule diagnoses divergence but cannot clear a gate.

**Step 5: Run focused tests**

```bash
uv run pytest tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_adapters.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/runtime_incident_rules.py \
  src/telegram_kol_research/runtime_incident_scanner.py \
  src/telegram_kol_research/runtime_incident_adapters.py \
  tests/test_runtime_incident_rules.py tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_adapters.py docs/runtime-incident-agent-runbook.md
git commit -m "feat: observe management safety gate divergence"
```

### Task 7: Review and stage rollout without replaying the closed Flyang action

**Files:**

- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Run full local verification and code review**

```bash
uv run pytest -q
git diff --check
```

Reject any loose ownership match, second exchange snapshot during planning,
blind retry, incident mutation, unbounded payload, implicit break-even move, or
path that can promote an in-flight predecessor.

**Step 2: Push reviewed commits**

Push only to `codex/deepcoin-auto-trading-v1` after the focused and full suites
pass.

**Step 3: Deploy observation-only behavior in a proven quiet window**

Require terminal recognition state, zero management/protection work in flight,
two identical complete exchange snapshots, and healthy services. Use the
standard server Git update helper, reinstall editable package, migrate, and
restart `telegram-kol.service`. Keep planner recovery disabled.

**Step 4: Verify shadow behavior**

Run the read-only audit and remediation planner without notifications. Require
the historical incident to classify healthy for the exact Flyang scope, all
hard gates to remain closed in negative diagnostics, and exchange/database
business state to remain unchanged except append-only observations.

**Step 5: Prove Flyang is terminal before enabling the narrow switch**

Run a fresh complete read-only reconciliation and require no live Flyang
position, no executable Flyang remediation action, and no surviving owned
TPSL order that could affect a live position. Require every previously printed
Flyang action/fingerprint to fail closed as stale or target absent.

In a second proven quiet window, enable only exact risk-reducing recovery from
healthy current evidence. Do not replay the historical message or create a
synthetic replacement action.

**Step 6: Canary with read-only planning**

Use naturally arriving management work or a production-shaped read-only
fixture to compare the old and new gate decisions. No live write is required
to validate the safety-gate correction. A future real action follows the normal
fresh plan, fingerprint review, and execution path; it receives no special
Flyang bypass.

**Step 7: Verify final invariants**

Require Flyang to remain absent, with no new Flyang batch, mutation intent,
close, or protection order. Require the gate's shadow and enabled decisions to
agree for healthy evidence, and no unintended change to Chen, Shuqin, or any
other live position.

**Step 8: Record and push the checkpoint**

Record deployed SHA, feature states, test totals, redacted fingerprints,
before/after position and protection evidence, service health, and rollback
commands in `docs/runtime-incident-agent-status.md`. Commit and push the
documentation-only checkpoint without another restart.
