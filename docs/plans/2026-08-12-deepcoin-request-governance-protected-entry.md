# Deepcoin Request Governance and Protected Multi-Leg Entry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add UID-aware Deepcoin request governance, bounded safe-read retry, durable request/snapshot evidence, and a protected multi-leg entry state machine that cannot resend unknown writes or increase exposure before protection is confirmed.

**Architecture:** Keep MiMo, contextual strategy resolution, order economics, and existing position ownership authoritative. Add a transport-level governor beneath all Deepcoin clients and a version-pinned execution-operation layer above the existing live entry writer; automation, reconciliation, and the Web read the same durable operation state. Roll out additively behind disabled-by-default gates and never migrate or replay historical incidents.

**Tech Stack:** Python 3.12+, SQLAlchemy 2, SQLite, `httpx`, FastAPI/Jinja, `fcntl` local-process coordination, pytest with fake clocks and fake Deepcoin clients.

---

## Mandatory execution boundaries

- Follow @test-driven-development for every task: obtain a real RED before
  production changes, then the smallest GREEN.
- Use @requesting-code-review at the review checkpoints and before push.
- Work only in the dedicated clean worktree.
- Do not mutate, replay, or migrate the known frozen two-leg production incident.
- Do not execute the batch 119 recovery as part of this plan.
- Do not send a synthetic Deepcoin order for verification.
- Do not push until all Critical and Important review findings are closed.
- This plan may produce reviewed, dormant code. Every production setting change,
  restart, stage activation, or live writer verification requires a later,
  separately approved turn and a proven quiet window.

## Commit map

Each task ends in one focused commit. Do not amend reviewed commits.

1. `feat: define deepcoin request policy`
2. `feat: govern deepcoin uid request budgets`
3. `feat: classify and retry deepcoin reads safely`
4. `feat: persist deepcoin execution evidence`
5. `feat: record immutable deepcoin operations`
6. `fix: require complete deepcoin snapshots`
7. `feat: define protected entry state machine`
8. `feat: gate protected entry rollout`
9. `feat: protect market entries before later legs`
10. `feat: defer stale later entry preflight`
11. `fix: preserve live entry lifecycle on partial failure`
12. `feat: reconcile protected entry operations read only`
13. `feat: show canonical entry execution state`
14. `fix: isolate background deepcoin reads`
15. `test: harden protected entry fault boundaries`
16. `docs: add deepcoin governor rollout runbook`

---

### Task 1: Define closed request, retry, and outcome contracts

**Files:**
- Create: `src/telegram_kol_research/deepcoin_request_policy.py`
- Create: `tests/test_deepcoin_request_policy.py`

**Step 1: Write failing profile and classification tests**

Cover normalized paths with query strings removed and the four approved budget
classes:

```python
def test_pending_tpsl_uses_safe_five_per_second_profile():
    profile = request_profile("GET", "/deepcoin/trade/trigger-orders-pending?limit=100")
    assert profile.per_second == 4
    assert profile.per_minute == 120


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_safe_get_status_is_retryable(status):
    fact = classify_http_failure(method="GET", status_code=status)
    assert fact.category in {"rate_limited", "http_retryable"}
    assert fact.retryable is True
    assert fact.outcome_certainty == "not_sent"


def test_post_transport_failure_is_unknown_and_never_retryable():
    fact = classify_transport_failure(method="POST", sent=True, code="read_timeout")
    assert fact.outcome_certainty == "unknown"
    assert fact.retryable is False
```

Also test:

- GET connect/read timeout is retryable;
- POST pre-send local refusal is `not_sent` and may be retried only inside the
  caller deadline;
- POST HTTP/transport/JSON ambiguity is `unknown`;
- 401/403 is `auth_failed` and non-retryable;
- ordinary 4xx and Deepcoin business errors are explicit rejections;
- the second malformed response becomes `schema_incompatible`; and
- unknown methods/paths fail closed to the most conservative profile.

**Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_deepcoin_request_policy.py
```

Expected: import failure because the policy module does not exist.

**Step 3: Implement the closed policy types**

Use `StrEnum` and frozen dataclasses. Keep these exact public dimensions:

```python
class RequestPriority(StrEnum):
    CRITICAL = "critical"
    NORMAL = "normal"
    BACKGROUND = "background"


class OutcomeCertainty(StrEnum):
    NOT_SENT = "not_sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"


class ErrorCategory(StrEnum):
    RATE_LIMITED = "rate_limited"
    TRANSPORT_TIMEOUT = "transport_timeout"
    HTTP_RETRYABLE = "http_retryable"
    AUTH_FAILED = "auth_failed"
    BUSINESS_REJECTED = "business_rejected"
    SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
    SCHEMA_INVALID = "schema_invalid"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    STATE_CONFLICT = "state_conflict"


@dataclass(frozen=True, slots=True)
class RequestProfile:
    per_second: int
    per_minute: int
    background_per_second: int
    background_per_minute: int


@dataclass(frozen=True, slots=True)
class FailureFact:
    category: ErrorCategory
    outcome_certainty: OutcomeCertainty
    retryable: bool
    safe_code: str
    http_status: int | None = None
```

Map documented 5/150, 10/300, 15/450, and strict 1/60 endpoints to
4/120, 8/240, 12/360, and 1 per 1.25 seconds/48 per minute. Give background
traffic at most half of each safe profile.

Do not parse human exception messages to classify behavior. Classification
accepts typed transport facts, HTTP status, and Deepcoin business code only.

**Step 4: Run the tests and verify GREEN**

Run the Task 1 test command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_request_policy.py \
  tests/test_deepcoin_request_policy.py
git commit -m "feat: define deepcoin request policy"
```

---

### Task 2: Add shared UID and endpoint request governance

**Files:**
- Create: `src/telegram_kol_research/deepcoin_request_governor.py`
- Create: `tests/test_deepcoin_request_governor.py`
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `tests/test_deepcoin_client.py`

**Step 1: Write failing deterministic budget tests**

Use an injected monotonic clock, sleeper, and temporary state directory. Prove:

- first request does not sleep;
- the fifth request to a 4/second path waits to the next second;
- the 121st request waits to the minute boundary;
- 8/240 and 12/360 paths use their own budgets;
- strict position-history calls remain at least 1.25 seconds apart;
- query variants share one normalized endpoint budget;
- distinct UID hashes do not share a budget;
- two governor instances with the same UID and state directory share starts;
- background traffic stops at its sub-budget while critical traffic can use the
  remaining safe capacity; and
- filenames/state never contain the API key.

Add one small real `multiprocessing` test with a 1/request window to prove two
processes cannot reserve the same slot. Do not use a wall-clock sleep longer than
one second.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_request_governor.py \
  tests/test_deepcoin_client.py -k 'governor or uid_budget'
```

Expected: import/API failure.

**Step 3: Implement `DeepcoinRequestGovernor`**

Required constructor and result shape:

```python
class GovernorMode(StrEnum):
    DISABLED = "disabled"
    TELEMETRY = "telemetry"
    ENFORCE_READS = "enforce_reads"
    ENFORCE_ALL = "enforce_all"


@dataclass(frozen=True, slots=True)
class GovernorLease:
    uid_scope_hash: str
    normalized_path: str
    waited_ms: int
    observed_delay_ms: int


class DeepcoinRequestGovernor:
    def acquire(
        self,
        *,
        method: str,
        request_path: str,
        priority: RequestPriority,
        deadline_monotonic: float | None,
    ) -> GovernorLease: ...
```

Hash `base_url + NUL + api_key` with SHA-256. The state filename uses only the
hash of UID scope plus normalized method/path. Create the state directory with
mode `0700` and state/lock files with mode `0600`.

Use `fcntl.flock(LOCK_EX)` and canonical JSON containing only bounded monotonic
start times. Under the lock: validate, prune starts older than 60 seconds,
reserve immediately when capacity exists, otherwise calculate delay. Release the
lock before sleeping and loop. If current monotonic time is below persisted
starts after a reboot, discard the old window. Malformed/enormous state refuses
an enforced request instead of resetting to an unsafe empty budget.

`TELEMETRY` returns the delay that would have applied without sleeping or
reserving an enforcing slot. `ENFORCE_READS` enforces GET only. `ENFORCE_ALL`
enforces every request. A deadline exhaustion raises a typed local pre-send
exception and sends no HTTP request.

**Step 4: Replace the isolated TPSL limiter boundary**

Inject one governor into `DeepcoinRestClient`. Keep
`DeepcoinTpslWriteLimiter` temporarily as a compatibility adapter in this task,
but make new governed tests prove a position-TPSL writer is charged exactly once.
Do not delete old tests until the client integration task proves equivalence.

**Step 5: Run focused and compatibility tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_request_governor.py tests/test_deepcoin_client.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/deepcoin_request_governor.py \
  src/telegram_kol_research/deepcoin_client.py \
  tests/test_deepcoin_request_governor.py tests/test_deepcoin_client.py
git commit -m "feat: govern deepcoin uid request budgets"
```

**Review checkpoint:** request an independent review of normalization, budget
math, lock ordering, corrupt state, deadline behavior, and credential redaction.
Resolve every Critical/Important finding with RED-to-GREEN tests before Task 3.

---

### Task 3: Integrate typed failures, safe GET retry, and connection reuse

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `tests/test_deepcoin_client.py`
- Create: `tests/test_deepcoin_client_retry.py`

**Step 1: Write failing sequential-response tests**

Add an HTTP fake that returns or raises a sequence. Test:

- GET timeout then success sends exactly two GETs;
- GET 429 honors bounded `Retry-After`;
- GET 503 uses 0.5/1/2-second backoff plus injected zero jitter;
- the fourth failed critical GET stops inside ten seconds;
- background GET uses at most two attempts/five seconds;
- malformed JSON retries once, then raises `schema_incompatible`;
- 401/403 and business rejection make one call;
- POST timeout/HTTP error/JSON error makes one call and raises
  `DeepcoinRequestOutcomeUnknown` with structured facts;
- a client-close error after a parsed POST success does not replace the success;
- timestamp and signature are generated after governor wait on every attempt;
- `Retry-After` larger than the remaining deadline stops locally; and
- a client reuses one owned `httpx.Client` for all calls until explicit close.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_client_retry.py tests/test_deepcoin_client.py
```

Expected: retry and persistent-client assertions fail.

**Step 3: Add an explicit request scope**

Use a `ContextVar` so one shared thread-safe client can carry per-operation
priority without leaking context between concurrent tasks:

```python
@dataclass(frozen=True, slots=True)
class DeepcoinRequestScope:
    phase: str
    priority: RequestPriority
    deadline_monotonic: float | None
    correlation_id: str | None = None
    attempt_recorder: Callable[[RequestAttemptFact], None] | None = None
```

Expose `client.request_scope(scope)` as a context manager. Existing callers get
a conservative normal scope and no durable recorder; background and protected
entry callers will set explicit scopes later.

**Step 4: Refactor `_request` into a bounded attempt loop**

For each attempt:

1. acquire governor capacity;
2. generate timestamp/signature;
3. send through the persistent client;
4. classify only typed transport/HTTP/business/schema facts;
5. invoke the recorder with sanitized bounded facts; and
6. retry only when method, category, attempt budget, and deadline permit.

Preserve `DeepcoinDefiniteRejection` and `DeepcoinRequestOutcomeUnknown` as
public compatibility subclasses carrying a `.fact`. GET terminal failures use a
typed `DeepcoinReadUnavailable` subclass. Do not include request body, response
body, credentials, or raw headers in exception strings.

Make persistent reuse the default. `__exit__`/explicit `close()` releases the
owned client; an unscoped request no longer opens a fresh client. Remove the
finally-close path that can overwrite a successful response.

**Step 5: Run focused and affected gateway tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_client.py tests/test_deepcoin_client_retry.py \
  tests/test_position_mutation_gateway.py
```

Expected: PASS and no real sleeping.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py \
  tests/test_deepcoin_client.py tests/test_deepcoin_client_retry.py
git commit -m "feat: classify and retry deepcoin reads safely"
```

---

### Task 4: Add additive durable operation, attempt, snapshot, and write-generation schema

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `tests/test_db_bootstrap.py`
- Modify: `tests/test_db_migrations.py`

**Step 1: Write failing schema tests**

Require these new tables and exact unique/index/constraint boundaries:

```text
deepcoin_execution_operations
deepcoin_request_attempts
deepcoin_snapshot_evidence
deepcoin_account_write_generations
```

The tests must inspect a new database and a representative legacy database.
Assert `create_session_factory` is idempotent and additive.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_db_bootstrap.py tests/test_db_migrations.py \
  -k deepcoin_execution
```

Expected: missing table failures.

**Step 3: Add the models**

Implement `DeepcoinExecutionOperation` with:

- unique `operation_key`;
- required `trade_signal_id` and optional parent/binding/order-leg links;
- `contract_version`, `phase`, `state`, `outcome_certainty`;
- optional `error_category` and bounded `reason_code`;
- exact `request_fingerprint`;
- deadline, writer-attempt, completion timestamps;
- attempt count and monotonic `state_version`;
- bounded canonical `evidence_json`; and
- created/updated timestamps.

Implement `DeepcoinRequestAttempt` as append-only with unique
`(deepcoin_execution_operation_id, ordinal)`, normalized method/path, priority,
phase, outcome/error facts, status/business code, wait/retry/latency numbers,
hashed identity/fingerprint references, and timestamps. It must have no request
or response body column.

Implement `DeepcoinSnapshotEvidence` with operation, snapshot kind,
availability/schema/completeness booleans, row/page count, collection
fingerprint, local write generations, capture interval, bounded evidence JSON,
and error category/code.

Implement `DeepcoinAccountWriteGeneration` with unique UID scope hash,
nonnegative generation, and update timestamp. Store no API key.

Add closed check constraints for phase/state/certainty/category and bounded JSON
lengths. New tables are created by `Base.metadata.create_all`; add only necessary
legacy-column compatibility entries if a table is introduced in an intermediate
test fixture.

**Step 4: Run schema tests and full model tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_db_bootstrap.py tests/test_db_migrations.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  tests/test_db_bootstrap.py tests/test_db_migrations.py
git commit -m "feat: persist deepcoin execution evidence"
```

---

### Task 5: Implement immutable operation and evidence repositories

**Files:**
- Create: `src/telegram_kol_research/deepcoin_execution_operations.py`
- Create: `tests/test_deepcoin_execution_operations.py`

**Step 1: Write failing reservation, CAS, and redaction tests**

Test that:

- `reserve_operation` uses `BEGIN IMMEDIATE` and is idempotent for identical
  immutable identity;
- the same operation key with changed trade signal, economics, fingerprint, or
  contract version conflicts;
- transitions require expected state and expected `state_version`;
- attempt ordinals are atomic and append-only;
- snapshot evidence is immutable;
- write generation increments before and after a writer boundary;
- JSON is canonical, bounded, finite, and depth-limited; and
- hostile secrets in safe messages/evidence are rejected or redacted before
  persistence.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_execution_operations.py
```

Expected: module import failure.

**Step 3: Implement the repository API**

Use these public functions:

```python
reserve_execution_operation(...)
transition_execution_operation(...)
record_request_attempt(...)
record_snapshot_evidence(...)
advance_account_write_generation(...)
load_operation_bundle(...)
```

All mutating functions obtain `BEGIN IMMEDIATE` before their first source read,
compare immutable identity and state version, and commit once. Return detached
immutable records rather than live ORM objects.

The attempt recorder adapter passed to `DeepcoinRequestScope` must never make a
failed evidence write look like a safe exchange result. If evidence persistence
fails before a writer, stop locally. If it fails after a writer, the operation
becomes unknown/recovery-required without another POST.

**Step 4: Run GREEN and concurrency tests**

Run the Task 5 command, then repeat the operation-reservation concurrency test
ten times. Expected: PASS with one row/ordinal owner.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_execution_operations.py \
  tests/test_deepcoin_execution_operations.py
git commit -m "feat: record immutable deepcoin operations"
```

---

### Task 6: Make exchange snapshot authority explicit and complete

**Files:**
- Create: `src/telegram_kol_research/deepcoin_snapshot_authority.py`
- Create: `tests/test_deepcoin_snapshot_authority.py`
- Modify: `src/telegram_kol_research/protection_snapshot.py`
- Modify: `src/telegram_kol_research/deepcoin_readonly.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `tests/test_deepcoin_readonly.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write failing completeness tests**

Cover:

- trigger-read exception returns unavailable evidence, never `[]`;
- invalid row/schema is unavailable/schema-invalid;
- 99 valid rows with no pagination metadata may be complete;
- exactly 100 rows with no affirmative pagination completion is incomplete;
- unsupported pagination metadata remains incomplete;
- expected order visibility cannot be true for an incomplete snapshot;
- local write generation change between capture start/end invalidates the whole
  snapshot;
- a writer that starts and ends during capture is detected by generation drift;
- collection fingerprints are order-independent and content-sensitive; and
- the loader retains per-endpoint errors instead of continuing with false
  absence.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_snapshot_authority.py tests/test_deepcoin_readonly.py \
  tests/test_execution_bindings.py -k 'snapshot or trigger_read or completeness'
```

Expected: current exception-to-empty and 100-row completeness tests fail.

**Step 3: Implement one canonical snapshot result**

Use an immutable shape:

```python
@dataclass(frozen=True, slots=True)
class ExchangeCollectionEvidence:
    endpoint: str
    available: bool
    schema_valid: bool
    complete: bool
    rows: tuple[Mapping[str, Any], ...]
    row_count: int
    page_count: int
    fingerprint: str | None
    reason_code: str | None
```

`capture_account_snapshot` records start generation, performs the governed reads,
records end generation, and rejects the composite if generations differ. Keep
raw rows in memory only; persist redacted fingerprints/counts and only the
bounded owned facts required by the operation.

Change `_load_all_open_orders` to propagate a typed unavailable result. Update
existing callers to stop or surface an incomplete snapshot; do not silently
skip the trigger collection.

Update `observe_pending_tpsl` so `response_count >= 100` without an affirmative
completion proof is incomplete.

**Step 4: Run focused and reconciliation suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_snapshot_authority.py tests/test_protection_snapshot.py \
  tests/test_deepcoin_readonly.py tests/test_execution_bindings.py \
  tests/test_strategy_management_reconciliation.py
```

Expected: PASS.

**Step 5: Commit and review**

```bash
git add src/telegram_kol_research/deepcoin_snapshot_authority.py \
  src/telegram_kol_research/protection_snapshot.py \
  src/telegram_kol_research/deepcoin_readonly.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_deepcoin_snapshot_authority.py tests/test_deepcoin_readonly.py \
  tests/test_execution_bindings.py tests/test_protection_snapshot.py
git commit -m "fix: require complete deepcoin snapshots"
```

**Review checkpoint:** independently attack false-empty conversion, row-100
truncation, generation races, malformed/deep JSON, and sensitive snapshot
serialization. Close all Critical/Important findings before Task 7.

---

### Task 7: Define the pure protected-entry transition machine

**Files:**
- Create: `src/telegram_kol_research/protected_entry_execution.py`
- Create: `tests/test_protected_entry_execution.py`

**Step 1: Write a transition-table RED suite**

Model these exact aggregate states:

```text
planned
entry_prepared
entry_submitting
entry_pending_readback
entry_unknown
entry_rejected
entry_confirmed
protection_prepared
protection_pending_readback
protection_unknown
protected
next_leg_preflight
pre_submit_deferred
completed
recovery_required
submission_failed_no_exposure
```

Prove illegal transitions fail closed, especially:

- `entry_unknown -> entry_submitting`;
- incomplete protection -> `next_leg_preflight`;
- `pre_submit_deferred -> entry_submitting`;
- any state with live exposure -> `submission_failed_no_exposure`; and
- aggregate protected when only one of two required protections is confirmed.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_protected_entry_execution.py
```

Expected: module import failure.

**Step 3: Implement pure state facts and transitions**

The pure function takes current durable state plus closed facts and returns a
transition; it performs no database or exchange I/O:

```python
@dataclass(frozen=True, slots=True)
class ProtectedEntryFacts:
    live_exposure: bool
    writer_attempted: bool
    required_protection_count: int
    confirmed_protection_count: int
    snapshot_complete: bool
    operation_deadline_expired: bool


def decide_protected_entry_transition(
    *, current_state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition: ...
```

Return a bounded reason code and allowed next action
(`submit`, `readback_only`, `defer`, `supervision_only`, or `none`). Never accept
or inspect human error text.

**Step 4: Run the state-machine suite**

Expected: every valid edge passes and every other matrix edge refuses.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protected_entry_execution.py \
  tests/test_protected_entry_execution.py
git commit -m "feat: define protected entry state machine"
```

---

### Task 8: Add future-only feature gates and contract pinning

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `tests/test_trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/deepcoin_request_governor.py`

**Step 1: Write failing setting tests**

Add:

```python
protected_entry_execution_mode: Literal["disabled", "live"] = "disabled"
protected_entry_execution_after_trade_signal_id: int = 0
```

Prove defaults are disabled; there is no shadow value; live activation requires
a watermark at least equal to the latest current TradeSignal ID; invalid,
negative, bool, and decreasing watermarks fail closed; and save/load/API/form
round-trip exactly.

Test environment-only governor modes:

```text
DEEPCOIN_REQUEST_GOVERNOR_MODE=
  disabled | telemetry | enforce_reads | enforce_all
DEEPCOIN_GOVERNOR_STATE_DIR=<absolute protected directory>
```

Missing/invalid values default to `disabled`, not enforcement with guessed
paths. The Web setting does not store credentials or override the environment
transport gate.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_trading_settings.py tests/test_web_app.py \
  -k 'protected_entry or request_governor_mode'
```

**Step 3: Implement settings and version selection**

`protected_entry_mode_for_signal(settings, signal_id)` returns `live` only when
the mode is live and `signal_id > watermark`. When creating an operation, pin
`contract_version=1` permanently.

If the feature is disabled before a pinned operation attempts a writer, stop the
operation. If any writer was attempted, disabling the gate blocks new operations
but leaves existing operations available only to their version-1 readback
reconciler.

**Step 4: Verify GREEN and compatibility**

Run the Task 8 command plus all trading-settings tests. Expected: PASS and old
serialized settings continue to load with disabled defaults.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/deepcoin_request_governor.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  tests/test_trading_settings.py tests/test_web_app.py
git commit -m "feat: gate protected entry rollout"
```

---

### Task 9: Integrate first-leg intent and the hard protection gate

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_position_mutation_gateway.py`
- Modify: `tests/test_auto_trade_execution.py`

**Step 1: Write failing end-to-end fake-client tests**

Under the future-only live gate, prove:

- the first-leg `entry_prepared` operation commits before `place_order`;
- a request fingerprint/client-order identity conflict sends zero POSTs;
- accepted market order moves to pending-readback until exact order/position
  proof exists;
- position discovery uses bounded governed reads and never passes `None` into a
  protection writer;
- every required protection has a durable child operation before its POST;
- two required stops must both read back by exact order ID/economics;
- first protection confirmed plus second protection unavailable sends zero
  later-leg POSTs;
- first protection writer unknown sends no repeated writer and no later leg;
- explicit protection rejection freezes later exposure; and
- a crash after POST but before local success persistence resumes readback only.

Assert the legacy path remains byte-for-byte behavior-compatible while the gate
is disabled.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_recovery_live_submit.py tests/test_position_mutation_gateway.py \
  tests/test_auto_trade_execution.py \
  -k 'protected_entry or protection_gate or market_entry_operation'
```

Expected: current broad protection exception continues to the next leg or lacks
durable pre-POST operation evidence.

**Step 3: Add the gated version-1 path**

Before the first market POST:

1. reserve the operation and exact request fingerprint;
2. enter a critical request scope with the operation recorder;
3. advance account write generation immediately before/after the writer;
4. transition from typed writer outcome; and
5. poll only safe reads for exact order and position identity.

Before each protection POST, reserve a child operation tied to the execution leg
and existing `PositionMutationIntent`. Extend `submit_exact_position_sltp` to use
the shared critical readback helper instead of one immediate GET. Poll at
approximately immediate, 0.5, 1, 2, and 3 seconds while respecting the shared
ten-second parent deadline.

Remove catch-and-continue only from the gated version-1 path. Any protection
failure/unknown/incomplete evidence transitions the aggregate to
`recovery_required` and exits before subsequent legs. Do not add automatic close.

**Step 4: Verify GREEN and broad entry compatibility**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_recovery_live_submit.py tests/test_position_mutation_gateway.py \
  tests/test_auto_trade_execution.py tests/test_execution_bindings.py
```

Expected: PASS; fake writer counts prove no duplicate or later-leg write.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  tests/test_recovery_live_submit.py tests/test_position_mutation_gateway.py \
  tests/test_auto_trade_execution.py
git commit -m "feat: protect market entries before later legs"
```

---

### Task 10: Persist and bound later-leg TPSL preflight

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/protected_entry_execution.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_protected_entry_execution.py`

**Step 1: Write failing second-leg tests**

Prove:

- `next_leg_preflight` is durable before the first baseline GET;
- the latest complete post-protection snapshot is reused when its capture ends
  after all protection confirmations and its generation is unchanged;
- no redundant third TPSL GET occurs in that case;
- a transient baseline GET succeeds on a bounded later attempt and sends one
  second-leg POST;
- all attempts remain inside ten seconds;
- exhaustion produces `pre_submit_deferred`, zero second-leg POST, and a
  nonterminal protected parent;
- a timer/reconcile cycle cannot move deferred back to submitting;
- a second-leg POST timeout produces `entry_unknown`, one POST total, then GET
  reconciliation by stable client-order ID; and
- process restart never resets the original deadline or writer-attempt fact.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_recovery_live_submit.py tests/test_protected_entry_execution.py \
  -k 'next_leg or pre_submit_deferred or baseline'
```

Expected: current code reads baseline before durable intent and fails
immediately.

**Step 3: Reverse the persistence/read order**

Refactor `_submit_trigger_with_protection_intent`:

1. reserve execution leg and `next_leg_preflight` operation;
2. create stable client-order/request identity;
3. load/reuse or capture a complete TPSL baseline under a critical GET scope;
4. persist snapshot evidence;
5. verify parent protection still confirmed and generation unchanged;
6. transition to submitting; and
7. perform at most one trigger POST.

Do not create a delayed submit job. Persist deferred evidence with the original
decision deadline, attempt count, last complete snapshot reference, and safe
reason code.

**Step 4: Run focused and complete entry suites**

Run the Task 10 command, then all recovery/auto-trade tests. Expected: PASS.

**Step 5: Commit and review**

```bash
git add src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/protected_entry_execution.py \
  tests/test_recovery_live_submit.py tests/test_protected_entry_execution.py
git commit -m "feat: defer stale later entry preflight"
```

**Review checkpoint:** independently reproduce the original failure sequence,
post-protection snapshot reuse, ten-second exhaustion, restart, and unknown
second-leg writer. Require exact POST counts. Close all Critical/Important
findings before Task 11.

---

### Task 11: Correct parent TradeSignal and lifecycle finalization

**Files:**
- Modify: `src/telegram_kol_research/trade_signals.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Create: `src/telegram_kol_research/protected_entry_projection.py`
- Modify: `tests/test_trade_signals.py`
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_auto_trade_execution.py`

**Step 1: Write failing lifecycle tests**

Create a TradeSignal with first leg/position evidence and assert:

- protection pending/unknown does not invalidate lifecycle or close TradeIdea;
- protected second-leg deferred does not invalidate lifecycle;
- unknown entry writer does not terminalize no-exposure failure;
- explicit first-leg rejection with complete zero-exposure proof may become
  `submission_failed_no_exposure`; and
- the compatibility `TradeSignal.status` is projected from canonical operation
  state, not parsed from error strings.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_trade_signals.py tests/test_recovery_live_submit.py \
  tests/test_auto_trade_execution.py \
  -k 'live_exposure or active_protected_deferred or no_exposure'
```

Expected: current `_mark_lifecycle_auto_trade_failed` invalidates the lifecycle.

**Step 3: Add evidence-aware finalization**

Implement a projection returning one of:

```text
active_protection_pending
active_protected_deferred
recovery_required
submission_failed_no_exposure
submitted
```

Add `finalize_trade_signal_from_execution_operation(...)`. It loads the locked
canonical operation bundle and changes lifecycle/TradeIdea only for complete
`submission_failed_no_exposure` evidence. Keep `mark_trade_signal_failed` for
legacy/non-entry uses, but the new entry path must not call it blindly.

Update claim/reuse predicates so compatibility statuses do not become retry
queues. Unknown/recovery/deferred states remain frozen unless the versioned
reconciler or a separately authorized supervised operation owns them.

**Step 4: Verify GREEN and existing lifecycle tests**

Run all three Task 11 files without `-k`. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trade_signals.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/protected_entry_projection.py \
  tests/test_trade_signals.py tests/test_recovery_live_submit.py \
  tests/test_auto_trade_execution.py
git commit -m "fix: preserve live entry lifecycle on partial failure"
```

---

### Task 12: Add GET-only restart and unknown-outcome reconciliation

**Files:**
- Create: `src/telegram_kol_research/protected_entry_reconciliation.py`
- Create: `tests/test_protected_entry_reconciliation.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write failing read-only reconciliation tests**

For each `entry_pending_readback`, `entry_unknown`,
`protection_pending_readback`, and `protection_unknown` state, assert:

- the reconciler calls only list/read methods;
- exact pending/history/fill evidence may confirm and advance;
- incomplete/failed snapshot leaves state unchanged with appended evidence;
- absence from one current list never authorizes a resend or terminal absence;
- client-order/order/position identity conflict moves to `recovery_required`;
- `pre_submit_deferred` is never submitted; and
- repeated reconcile after confirmation is idempotent.

Use fake clients whose write methods raise immediately if called.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_protected_entry_reconciliation.py
```

Expected: module import failure.

**Step 3: Implement bounded reconciliation**

Load only nonterminal version-1 operations. Capture complete governed snapshots
at background priority, bind exact exchange evidence to the immutable request
fingerprint/client identity, and perform CAS transitions. Never reconstruct or
call a writer payload.

Call this reconciler from the existing execution-reconciliation cycle after the
shared account snapshot is available. Reuse that capture rather than repeating
the same endpoint set per operation.

**Step 4: Run focused and reconciliation suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_protected_entry_reconciliation.py tests/test_execution_bindings.py \
  tests/test_strategy_management_reconciliation.py
```

Expected: PASS and write-call counters remain zero.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protected_entry_reconciliation.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_protected_entry_reconciliation.py tests/test_execution_bindings.py
git commit -m "feat: reconcile protected entry operations read only"
```

---

### Task 13: Project the canonical execution state into group messages

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_group_messages_route.py`

**Step 1: Write failing Web projection tests**

Create synthetic operation bundles for protected, deferred, readback-pending,
unknown, explicit rejection, and no-exposure failure. Assert one canonical panel
shows:

- phase/state label;
- outcome certainty;
- retry attempt count and deadline;
- safe reason code/category;
- last complete snapshot capture time;
- limiter wait/readback latency summary; and
- allowed next action (`自动读取核实`, `已延期，禁止自动追单`, or `需要人工核实`).

Assert raw API key, passphrase, request body, response body, order ID, position
ID, and hostile error text do not render.

**Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_web_group_messages_route.py -k deepcoin_execution_operation
```

Expected: missing projection/panel.

**Step 3: Bulk-load and serialize operation evidence**

In `_serialize_raw_messages`, bulk-load TradeSignals for message keys, then
operations, attempts, and latest snapshot evidence by signal IDs. Do not add an
N+1 query. Return `deepcoin_execution` from one serializer with closed Chinese
labels; never derive state from `TradeSignal.last_error`.

Render a compact summary beneath the existing automatic-trading result with an
expandable attempt list. Keep the existing MiMo analysis and image evidence
unchanged.

**Step 4: Run Web tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_web_group_messages_route.py tests/test_web_app.py
```

Expected: PASS and query-count guard does not regress.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/static/app.css \
  tests/test_web_group_messages_route.py
git commit -m "feat: show canonical entry execution state"
```

---

### Task 14: Isolate background network work and expose bounded health metrics

**Files:**
- Create: `src/telegram_kol_research/deepcoin_request_metrics.py`
- Create: `tests/test_deepcoin_request_metrics.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing async-isolation tests**

Use an injected blocking reconcile function and an async heartbeat. Assert the
heartbeat advances while:

- `/api/execution/sync-deepcoin` executes reconciliation; and
- `run_deepcoin_execution_reconcile_loop` performs its cycle.

Assert both use a background request scope and defer cleanly when the background
governor wait exceeds two seconds.

**Step 2: Write failing metric projection tests**

From synthetic attempts/operations, calculate a bounded window containing:

- request count by normalized endpoint/phase;
- limiter wait p50/p95/max;
- retry, 429, unavailable, and schema counts;
- readback latency;
- open-circuit count/duration;
- unknown writer count;
- `pre_submit_deferred` count; and
- duration/count of live exposure without confirmed protection.

Reject a truncated scan rather than report false-complete metrics. Add a
read-only `/api/execution/deepcoin-request-health` response with no raw IDs.

**Step 3: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_request_metrics.py tests/test_web_app.py \
  -k 'deepcoin_request_health or reconcile_does_not_block_event_loop'
```

**Step 4: Implement thread isolation and metrics**

Use `await asyncio.to_thread(...)` for the synchronous network-heavy reconcile
call in the async route and background loop. Keep Telegram notification awaits
on the event loop after the worker returns.

Metrics are read-only database projections. Log a bounded structured warning for
any unknown writer, auth failure, schema incompatibility, or live unprotected
operation; do not send a new Telegram notification path in this task.

**Step 5: Run GREEN and background-loop tests**

Run Task 14 tests plus existing Web lifespan/reconcile-loop selections.

**Step 6: Commit and review**

```bash
git add src/telegram_kol_research/deepcoin_request_metrics.py \
  src/telegram_kol_research/web_app.py \
  tests/test_deepcoin_request_metrics.py tests/test_web_app.py
git commit -m "fix: isolate background deepcoin reads"
```

**Review checkpoint:** independently inspect event-loop isolation, SQLite session
thread ownership, bounded metrics, endpoint priority, and whether a background
cycle can starve critical readback.

---

### Task 15: Add the complete crash and fault-injection safety matrix

**Files:**
- Create: `tests/test_protected_entry_fault_injection.py`
- Create: `tests/fixtures/protected_entry/frozen_partial_entry.json`
- Modify: relevant source files only for defects proved by new RED tests

**Step 1: Add a sanitized synthetic incident fixture**

Model the durable shape only: one confirmed market leg, one live position, two
confirmed stop ledgers, no second leg, and a pre-submit baseline failure. Use no
production IDs, text, order IDs, response payloads, or credentials.

**Step 2: Parameterize every crash boundary**

Inject failure:

- before operation commit;
- after operation commit/before POST;
- immediately before HTTP send;
- after exchange acceptance/before response;
- after response/before local writer evidence;
- after writer evidence/before operation transition;
- during each readback attempt;
- between protection one and protection two;
- after all protections/before later-leg baseline;
- after baseline/before later-leg POST; and
- after later-leg POST/before result persistence.

For every case assert exact POST counts, durable state, allowed next action,
lifecycle state, and restart behavior.

**Step 3: Add invariant/property-style matrices**

Enumerate combinations of live exposure, writer attempted, required/confirmed
protection counts, snapshot completeness, and deadline status. Assert:

```text
protection incomplete => later-leg POST count == 0
writer unknown => logical writer POST count <= 1
read unavailable => absence proof == false
live exposure => lifecycle terminal failure == false
deadline expired before POST => state == pre_submit_deferred
```

Add concurrent same-operation tests and cross-process governor contention.

**Step 4: Prove frozen-history immutability**

Load the synthetic historical fixture, run schema bootstrap, governor telemetry,
read reconciliation, Web projection, and protected-entry worker selection.
Assert the TradeSignal, lifecycle, binding, leg, protection, and event rows are
byte-for-byte unchanged.

**Step 5: Run focused and broad suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_deepcoin_request_policy.py \
  tests/test_deepcoin_request_governor.py \
  tests/test_deepcoin_client.py tests/test_deepcoin_client_retry.py \
  tests/test_deepcoin_execution_operations.py \
  tests/test_deepcoin_snapshot_authority.py \
  tests/test_protected_entry_execution.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_protected_entry_fault_injection.py \
  tests/test_recovery_live_submit.py \
  tests/test_position_mutation_gateway.py \
  tests/test_auto_trade_execution.py tests/test_trade_signals.py \
  tests/test_execution_bindings.py \
  tests/test_web_group_messages_route.py tests/test_web_app.py
```

Expected: PASS.

**Step 6: Commit only accepted fixes and tests**

```bash
git add tests/test_protected_entry_fault_injection.py \
  tests/fixtures/protected_entry/frozen_partial_entry.json \
  <only-source-files-changed-by-proved-failures>
git commit -m "test: harden protected entry fault boundaries"
```

---

### Task 16: Document rollout, run the full gate, review, and push without deployment

**Files:**
- Create: `docs/deepcoin-request-governance-runbook.md`
- Modify: `docs/deepcoin-order-management.md`
- Modify: `docs/migration-handoff.md`
- Review: every file changed after design commit `b455af9`

**Step 1: Write the dormant rollout runbook**

Document these separately approved stages:

1. `DEEPCOIN_REQUEST_GOVERNOR_MODE=disabled` foundation deploy;
2. `telemetry` observation with zero delay and zero simulated orders;
3. `enforce_reads` for background/Web, then critical reads;
4. future-only protected-entry watermark activation;
5. `enforce_all` for new-version writers; and
6. legacy retirement only after every old operation is terminal.

For every stage include:

- preflight database backup and exact Git SHA;
- service stop and active/unknown writer query;
- complete exchange snapshot/protection proof;
- known frozen-incident exact fingerprint exception with no recovery authority;
- setting/config change;
- post-restart PID/HTTP/log/database/exchange verification;
- rollback before writer attempt; and
- no legacy handoff after a versioned writer attempt.

State that batch 119 remains a separate runbook and cannot be executed in the
same deployment operation.

**Step 2: Run syntax and focused safety gates**

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
git diff --check
```

Run the Task 15 focused suite. Expected: PASS.

**Step 3: Run the full local suite**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: all tests pass, with only previously documented skips.

**Step 4: Request independent final review**

Use @requesting-code-review on `b455af9..HEAD`. Require explicit review of:

- official rate-profile mapping and cross-process UID sharing;
- `Retry-After`, deadline, and circuit behavior;
- timestamp/signing order;
- unknown POST non-retry invariant;
- snapshot false-empty and row-100 truncation;
- protection hard gate and later-leg ten-second boundary;
- lifecycle preservation with live exposure;
- restart/read-only reconciliation;
- operation version pinning and rollback;
- Web/metrics redaction and query bounds;
- frozen-incident and batch-119 isolation; and
- default-disabled production behavior.

For every accepted Critical/Important finding: first add a focused test that is
RED on the current reviewed commit, then implement the minimum GREEN and create
a separate follow-up commit. Repeat review until READY.

**Step 5: Commit documentation**

```bash
git add docs/deepcoin-request-governance-runbook.md \
  docs/deepcoin-order-management.md docs/migration-handoff.md
git commit -m "docs: add deepcoin governor rollout runbook"
```

If Step 4 produced follow-up commits after the documentation commit, rerun the
full suite and final review on the new exact range.

**Step 6: Push the reviewed candidate only**

Verify worktree cleanliness and remote ancestry, then push without force:

```bash
git status --short
git fetch origin codex/deepcoin-auto-trading-v1
git merge-base --is-ancestor origin/codex/deepcoin-auto-trading-v1 HEAD
git push origin HEAD:codex/deepcoin-auto-trading-v1
git rev-parse HEAD
git rev-parse origin/codex/deepcoin-auto-trading-v1
```

Expected: both SHAs match. Stop here. Do not pull on the server, restart the
service, alter settings, or run any production recovery in this implementation
session.

---

## Final acceptance gate

Do not call the implementation complete until all are true:

- every Deepcoin request reaches the governor in enabled modes;
- safe-read retry is typed, bounded, and recorded;
- any unknown writer is submitted at most once;
- later-leg writes are impossible before all required protection is confirmed;
- unavailable/incomplete exchange reads cannot prove absence;
- the later-leg pre-submit window never exceeds ten seconds and never schedules
  a stale automatic submit;
- live exposure cannot invalidate the lifecycle as no-exposure failure;
- restart reconciliation is GET-only;
- Web and automation read the same canonical operation state;
- default gates preserve current production behavior;
- the frozen two-leg incident and batch 119 are unchanged;
- the full local suite, compileall, and diff check pass; and
- independent review is READY with no Critical or Important findings.
