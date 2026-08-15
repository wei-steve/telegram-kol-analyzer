# Bound Position Close Reservation Convergence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant, fingerprinted, read-only-first recovery command that marks only exchange-proven terminal bound close reservations as `confirmed`, allowing the existing Batch 119 and Phase One gates to proceed without weakening them.

**Architecture:** A new closed-scope module reads all nonterminal reservations from one coherent SQLite snapshot, captures bounded exact Deepcoin GET evidence, and classifies every row as `PROVEN_TERMINAL`, `ACTIVE`, or `UNKNOWN`. Dry-run serializes only redacted evidence and refuses unless every row is proven terminal; separately authorized apply recaptures, verifies the exact fingerprint under `BEGIN IMMEDIATE`, changes only reservation terminal state, and appends one aggregate audit event. The existing deployment preflight remains unchanged.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite WAL/query-only transactions, Typer, httpx, pytest.

---

## Global constraints

- Work only on `codex/bound-close-reservation-recovery`, based on Phase One SHA
  `c50887b991712340d7d5606fb6916cdbb033926e`.
- Use `@test-driven-development` for every behavior change.
- Do not modify `deployment_preflight.py` to ignore age or statuses.
- Do not add exchange POST/cancel/replace/close/TPSL reachability.
- Do not change MiMo, trading settings, Telegram ingestion, management batches,
  bindings, lifecycles, legs, mutations, or old Monitor behavior.
- Do not run a production dry-run, apply, deployment, service stop, or database
  bootstrap during local implementation.
- Stop after local review and request the read-only double-capture approval.

### Task 1: Define the closed recovery contract

**Files:**
- Create: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Create: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing contract tests**

Cover exact enums, frozen records, strict booleans/integers, aware UTC times,
64-hex fingerprints, maximum 64 items, and rejection of unknown fields or
reason codes. Start with these public shapes:

```python
class ReservationClassification(StrEnum):
    PROVEN_TERMINAL = "PROVEN_TERMINAL"
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class BoundCloseReservationObservation:
    reservation_ref: str
    classification: ReservationClassification
    reason_code: str
    source_fingerprint: str
    exchange_fingerprint: str

@dataclass(frozen=True, slots=True)
class BoundCloseReservationRecoveryPlan:
    schema_version: int
    status: str
    observations: tuple[BoundCloseReservationObservation, ...]
    source_fingerprint: str
    exchange_snapshot_fingerprint: str
    evidence_fingerprint: str
    confirmation_token: str
    action_count: int
```

Require `status == "ready"` only when the population is nonempty and every
observation is `PROVEN_TERMINAL`; otherwise require `status == "refused"` and
`action_count == 0`.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_recovery.py
```

Expected: collection/import failure because the module does not exist.

**Step 3: Implement the minimal contract**

Add closed constants and strict validators. The only item reason codes initially
allowed are:

```python
PROVEN_TERMINAL_REASONS = frozenset({
    "exact_close_and_position_terminal",
})
ACTIVE_REASONS = frozenset({
    "exact_position_currently_live",
    "exact_close_order_currently_pending",
    "exact_close_order_nonterminal",
})
UNKNOWN_REASONS = frozenset({
    "local_evidence_incomplete",
    "local_identity_conflict",
    "exchange_evidence_unavailable",
    "exchange_schema_invalid",
    "exchange_identity_conflict",
    "exchange_history_incomplete",
    "exchange_capture_timeout",
    "exchange_response_size_exceeded",
    "exchange_state_conflict",
})
```

Use a canonical JSON encoder and a private `_sha256_json()` helper. Do not add a
generic status or reason extension point.

**Step 4: Run the focused tests and verify GREEN**

Run the Task 1 test file. Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "feat: define close reservation recovery contract"
```

### Task 2: Load one coherent, bounded local source population

**Files:**
- Modify: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing source-loader tests**

Seed reservations and prove that one `mode=ro` connection with
`PRAGMA query_only=ON` and explicit `BEGIN` loads:

- every row in `reserved`, `submitted`, `submit_unknown`,
  `unknown_exchange_outcome`, or `recovery_required`;
- its exact binding;
- exactly one matching `close_bound_position_market` event with a nonempty order
  id and the same binding/position identity;
- exact close mutations and owned entry leg facts when present.

Add failures for missing binding, zero/duplicate events, mismatched identity,
malformed JSON/time, unknown source status, 65 rows, schema drift, and a writer
committing between SELECTs. The WAL test must show the second SELECT still sees
the original transaction snapshot. Assert that INSERT/UPDATE on the loader
connection fails.

**Step 2: Run the new tests and verify RED**

Expected: `load_bound_close_reservation_source()` is missing.

**Step 3: Implement the source loader**

Use this interface:

```python
def load_bound_close_reservation_source(
    database_path: str | Path,
    *,
    between_selects_hook: Callable[[], None] | None = None,
) -> BoundCloseReservationSource:
    ...
```

Use `LIMIT 65`, reject overflow, and hash raw identifiers immediately into typed
redacted references for all serialized structures. Keep raw ids only in a
private in-process apply capability. Return source-level `UNKNOWN` facts instead
of silently dropping malformed rows.

**Step 4: Run the focused tests and verify GREEN**

Expected: loader, WAL coherence, overflow, and query-only tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "feat: load bounded close reservation evidence"
```

### Task 3: Add a bounded capability-only Deepcoin reader

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Modify: `tests/test_deepcoin_client.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing transport and capability tests**

Test a recovery-specific read-only builder with `trust_env=False`. Under proxy,
custom CA, and proxy-auth environment variables, assert the owned httpx client
does not inherit them. Assert every POST/cancel/replace method remains rejected.

Extend `DeepcoinRequestScope` response limits to the exact closed phase
`bound_close_reservation_recovery`; keep every other non-monitor/non-recovery
phase rejected. Test `Content-Length` overflow, streamed overflow before JSON
decode, slow-drip deadline, malformed JSON, business error, and request-scope
cleanup.

**Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_deepcoin_client.py \
  tests/test_bound_close_reservation_recovery.py
```

Expected: missing recovery scope/builder/capture failures.

**Step 3: Implement the minimal transport extension**

Add a named builder that always constructs `DeepcoinRestClient` with
`read_only=True` and `trust_env=False`. Permit `max_response_bytes` only for the
existing monitor phase and the exact recovery phase. Keep the 1 MiB per-response
limit and one absolute total capture deadline.

Wrap the transport with a class exposing only:

```python
read_positions()
read_open_orders()
read_order_history(inst_id=..., order_id=..., limit=100)
read_trade_fills(inst_id=..., order_id=..., limit=100)
read_position_history(inst_id=..., pos_id=...)
request_scope(...)
```

Do not expose the transport or any writer method.

**Step 4: Run focused tests and verify GREEN**

Expected: Deepcoin client and recovery transport tests pass; normal trading and
Batch119 builder semantics remain unchanged.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py \
  src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_deepcoin_client.py tests/test_bound_close_reservation_recovery.py
git commit -m "feat: bound close reservation exchange reads"
```

### Task 4: Classify terminal, active, and unknown exchange outcomes

**Files:**
- Modify: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write the classification matrix first**

Add table-driven tests for:

- exact successful terminal close order + exact full-close position history + no
  current position/order => `PROVEN_TERMINAL`;
- an active parent binding with an independently proven closed sibling =>
  `PROVEN_TERMINAL` without changing the binding;
- current exact position, pending exact order, or exchange nonterminal order =>
  `ACTIVE`;
- rejected/cancelled/partial order, missing or duplicate order/fill/history,
  mismatched instrument/side/position/order, position absence without terminal
  history, page-limit ambiguity, callback delay, timestamp inversion, and
  conflicting mutation => `UNKNOWN`.

Test exact decimal equality for filled/closed quantities and aware UTC ordering:
reservation/event <= order terminal <= position close <= capture completion.

**Step 2: Run the matrix and verify RED**

Expected: classifier missing or incomplete.

**Step 3: Implement one pure classifier**

Use:

```python
def classify_bound_close_reservation(
    local: LocalReservationEvidence,
    exchange: ExchangeReservationEvidence,
    *,
    capture_completed_at: datetime,
) -> BoundCloseReservationObservation:
    ...
```

Order checks from most conservative to least: malformed/incomplete => UNKNOWN,
identity conflict => UNKNOWN, current/pending => ACTIVE, contradictory terminal
facts => UNKNOWN, complete terminal chain => PROVEN_TERMINAL, otherwise UNKNOWN.
Do not use an age cutoff or inferred callback deadline.

**Step 4: Run focused tests and verify GREEN**

Expected: the entire matrix passes.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "feat: classify close reservation outcomes"
```

### Task 5: Seal plans and compare stopped-service double captures

**Files:**
- Modify: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Create: `scripts/compare_bound_close_reservation_dry_runs.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`
- Create: `tests/test_bound_close_reservation_dry_run_comparison.py`

**Step 1: Write failing plan and comparison tests**

Require exact serialized keys, redaction, bounded bytes/depth/items, sorted unique
references, count conservation, canonical fingerprints, and a derived
confirmation token. Assert raw DB ids, position ids, order ids, sizes, prices,
provider payloads, source text, and credentials cannot appear.

The comparator accepts exactly two 0600 files, rejects symlinks, duplicate JSON
keys, unknown fields, non-ready plans, nonzero exchange writes/history replays,
invalid counts/fingerprints, repeated capture identity, or any semantic drift.
It prints only:

```json
{"status":"stable"}
```

or one closed refusal code without echoing either input.

**Step 2: Run the tests and verify RED**

Expected: serializer/comparator missing.

**Step 3: Implement serialization and comparison**

Set fixed counters:

```python
"exchange_writes": 0,
"history_replays": 0,
"database_writes": 0,
```

Require all items `PROVEN_TERMINAL` for `ready`; otherwise output `refused` with
counts and zero actions. Capture timestamps are validated but excluded from the
semantic fingerprint.

**Step 4: Run both focused test files and verify GREEN**

Expected: all plan, redaction, and comparator tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/bound_close_reservation_recovery.py \
  scripts/compare_bound_close_reservation_dry_runs.py \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "feat: seal close reservation recovery evidence"
```

### Task 6: Implement the separately authorized CAS apply

**Files:**
- Modify: `src/telegram_kol_research/bound_close_reservation_recovery.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing apply tests**

Require these exact authorizations:

```text
I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE
I_AUTHORIZE_BOUND_CLOSE_RESERVATIONS_PROVEN_TERMINAL_ONLY
```

Test refusal before opening a writable session for a missing/wrong token,
fingerprint, action count, stopped-service authorization, fresh capture
capability, `mimo_contract_mode != v1`, source drift, exchange drift, new row,
already/mixed changed row, or unexpected audit event.

Test one transaction changes only:

- each planned reservation `status` to `confirmed`;
- its `last_error` to null;
- its `updated_at`;
- one aggregate `ExecutionEvent` with action
  `bound_close_reservation_history_converged`, status `succeeded`, null raw
  position/order fields, the overall fingerprint as its unique notification
  fingerprint, and bounded redacted before/after JSON.

Assert all other tables and exchange call counts are unchanged. Test rollback on
every statement boundary and idempotent repeat with no second event.

**Step 2: Run apply tests and verify RED**

Expected: apply function missing.

**Step 3: Implement minimal apply**

Use an opaque in-process capture capability and:

```python
def apply_bound_close_reservation_recovery(
    database_path: str | Path,
    *,
    plan: BoundCloseReservationRecoveryPlan,
    capture: SealedRecoveryCapture,
    expected_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
    authorization: str,
    applied_at: datetime,
) -> BoundCloseReservationRecoveryResult:
    ...
```

Open the writable connection lazily only after all non-database gates pass. Use
`BEGIN IMMEDIATE`, rebuild source/CAS facts under lock, update exact old statuses,
insert the aggregate event, and commit once.

**Step 4: Run focused tests and verify GREEN**

Expected: apply, rollback, idempotence, and mutation-boundary tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "feat: apply proven close reservation convergence"
```

### Task 7: Wire a dormant CLI without notification or writer reachability

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing CLI tests**

Test default dry-run, strict paths/options, exact JSON schema, exit 0 only for a
ready plan, exit 2 for refused evidence, and nonzero configuration failures.
Assert no Telegram notification, Runtime Agent route, database bootstrap,
generic Deepcoin writer builder, or executor is importable/reachable from this
command.

Apply must require all fingerprint/count/token/authorization arguments and the
same resolved path for local source/generation authority. It must not expose a
row selector, `--force`, ignore list, age, or notification option.

**Step 2: Run CLI tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_cli_smoke.py \
  tests/test_bound_close_reservation_recovery.py
```

Expected: command missing.

**Step 3: Implement CLI wiring**

Build only the capability-limited reader. Dry-run never creates a writable
factory. Apply recaptures in the same process and passes the opaque capability
to the module. Emit only canonical JSON.

**Step 4: Run focused and adjacent tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  tests/test_deepcoin_client.py \
  tests/test_deployment_preflight.py \
  tests/test_execution_bindings.py
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_smoke.py \
  src/telegram_kol_research/bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "feat: add dormant close reservation recovery cli"
```

### Task 8: Prove the existing deployment gate remains authoritative

**Files:**
- Modify: `tests/test_deployment_preflight.py`
- Modify: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Add regression tests without changing gate code**

Prove:

- any fresh `reserved/submitted/submit_unknown/unknown_exchange_outcome/
  recovery_required` reservation blocks ordinary code deployment;
- historical residues remain visible under the existing warning semantics;
- `confirmed` is terminal and is not counted as active;
- a recovery plan containing one `ACTIVE` or `UNKNOWN` row is refused and cannot
  make preflight deployable;
- terminalizing reservations does not clear the independent Batch 119 block;
- only independent reservation convergence plus independent Batch 119
  convergence can remove both facts.

**Step 2: Run tests and verify current behavior**

Run the two test files. Expected: new integration fixtures initially fail until
their helpers are complete; no production gate relaxation is permitted.

**Step 3: Complete only test fixtures/adapters**

Do not change `deployment_preflight.py`. If a test reveals that confirmed rows
are treated as active, stop and return to design review rather than adding an
exception.

**Step 4: Run tests and verify GREEN**

Expected: all reservation/preflight boundary tests pass.

**Step 5: Commit**

```bash
git add tests/test_deployment_preflight.py \
  tests/test_bound_close_reservation_recovery.py
git commit -m "test: preserve deployment gate after reservation recovery"
```

### Task 9: Document the two production approval windows

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/plans/2026-08-14-deployment-gate-batch-recovery.md`
- Create: `docs/runtime-bound-close-reservation-recovery-status.md`
- Test: `tests/test_cli_smoke.py`
- Test: `tests/test_bound_close_reservation_dry_run_comparison.py`

**Step 1: Add documentation assertions where practical**

Require the runbook to contain the exact branch/SHA checks, unit inventory,
trap-based restoration, unknown-process refusal, zero durable writer checks that
exclude only the exact target reservation population, two private 0600 capture
files, stable comparator, separate apply token, verified backup, query-only
postchecks, and explicit stop before Batch119.

The dry-run approval token is:

```text
I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_READ_ONLY_DOUBLE_CAPTURE
```

The apply window requires both fixed strings from Task 6.

**Step 2: Write the runbook section and status template**

Document only reviewed commands. Output must be limited to classification counts,
fingerprints, zero-write counters, service restoration, production SHA, and
backup path after apply. Never print raw ids, provider rows, or credentials.

Record the Phase One return point:

```text
reservation recovery -> Batch119 apply -> stable snapshot -> ordinary preflight
-> deploy exact c50887b -> Phase One canary/cutover
```

**Step 3: Run docs/CLI/comparator tests**

Expected: all pass; `git diff --check` is clean.

**Step 4: Commit**

```bash
git add docs/runbook.md \
  docs/plans/2026-08-14-deployment-gate-batch-recovery.md \
  docs/runtime-bound-close-reservation-recovery-status.md \
  tests/test_cli_smoke.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "docs: operate close reservation convergence"
```

### Task 10: Complete local verification and stop for production dry-run approval

**Files:**
- Review all files changed since design commit `c8a6c37`

**Step 1: Run focused suites**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_deepcoin_client.py \
  tests/test_cli_smoke.py \
  tests/test_deployment_preflight.py \
  tests/test_execution_bindings.py \
  tests/test_historical_state_repair.py
```

Expected: all pass.

**Step 2: Run the full suite and static checks**

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check c8a6c37^..HEAD
git status --short
```

Expected: full suite passes, compilation succeeds, no whitespace errors, and the
worktree is clean after commits.

**Step 3: Perform a Critical/Important review**

Use `@requesting-code-review`. Review especially:

- no exchange writer or notification reachability;
- exact identity and terminal evidence;
- callback-delay behavior remaining UNKNOWN;
- response/deadline/population bounds;
- redaction and fingerprint integrity;
- apply CAS, rollback, idempotence, and mutation scope;
- no deployment gate relaxation;
- no MiMo v2, history replay, service, timer, or installer change.

Fix every Critical or Important finding with RED/GREEN tests and rerun the
affected and full suites.

**Step 4: Push only the dedicated recovery branch**

```bash
git push -u origin codex/bound-close-reservation-recovery
```

Do not move or overwrite
`origin/codex/deployment-gate-batch-recovery-plan`; it must continue to identify
the approved Phase One SHA `c50887b991712340d7d5606fb6916cdbb033926e`.

**Step 5: Stop at the read-only production boundary**

Report the reviewed recovery SHA and request exactly:

```text
I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_READ_ONLY_DOUBLE_CAPTURE
```

Do not stop services, access Deepcoin production credentials, run the production
dry-run, apply, recover Batch119, or deploy Phase One in this implementation
turn.
