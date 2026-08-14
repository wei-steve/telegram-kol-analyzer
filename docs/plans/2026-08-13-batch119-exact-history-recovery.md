# Batch 119 Exact-History Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the allowlisted batch-119 dry-run prove an automatic stop from exact `posId` and verified protection-order evidence without accepting truncated instrument-wide history.

**Architecture:** Add optional exact-ID filters to the raw Deepcoin history readers, then replace only the batch-119 CLI snapshot with a dedicated same-generation exact-scope capture. Keep generic reconciliation unchanged. The planner may classify an absent position only when one verified `stop_loss` or `backup_stop` order uniquely and coherently proves the close; every ambiguous or unowned close remains refused.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy 2, SQLite, `httpx`, pytest, existing Deepcoin snapshot authority and composite recovery planner.

---

### Task 1: Add bounded exact-ID history reads

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py:190-240`
- Modify: `src/telegram_kol_research/deepcoin_client.py:526-650`
- Test: `tests/test_deepcoin_client.py`

**Step 1: Write the failing client-contract tests**

Add tests that call the three raw readers with an exact order ID and explicit
limit:

```python
def test_exact_history_readers_send_ord_id_and_bounded_limit():
    http = _CapturingHttpClient({"code": "0", "data": []})
    client = _client(http)

    client.read_order_history(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )
    client.read_trade_fills(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )
    client.read_trigger_order_history(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )

    paths = [request["request_path"] for request in http.requests]
    assert all("ordId=owned-stop" in path for path in paths)
    assert all("limit=100" in path for path in paths)
```

Also assert that invalid `limit` values and blank order IDs fail before HTTP,
and existing calls without exact filters retain their current URL behavior.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_client.py::test_exact_history_readers_send_ord_id_and_bounded_limit \
  tests/test_deepcoin_client.py::test_exact_history_readers_reject_invalid_identity_before_http
```

Expected: FAIL because the readers do not accept `order_id` or `limit`.

**Step 3: Implement the minimal optional filters**

Extend the protocol and concrete raw readers without changing existing list
reader defaults:

```python
def read_order_history(
    self,
    *,
    inst_id: str | None = None,
    order_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    return self._request(
        "GET",
        _path_with_query(
            DEEPCOIN_ORDERS_HISTORY_PATH,
            {
                "instType": "SWAP",
                "instId": inst_id,
                "ordId": _optional_exact_exchange_id(order_id),
                "limit": _optional_history_limit(limit),
            },
        ),
    )
```

Apply the same exact filter to fills and trigger history. Reuse the existing
safe exchange-ID validation; permit only `1 <= limit <= 100`. Do not add a
pagination loop or automatic retry.

**Step 4: Run focused and adjacent client tests**

Run:

```bash
.venv/bin/pytest -q tests/test_deepcoin_client.py tests/test_deepcoin_snapshot_authority.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py tests/test_deepcoin_client.py
git commit -m "feat: add exact deepcoin history filters"
```

### Task 2: Build a batch-specific exact-scope snapshot

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:90-340`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:2870-2920`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write the failing exact-scope snapshot tests**

Create a fake client whose instrument-wide history methods return 100 rows but
whose exact raw readers return one owned stop and one exact closed-position row.
Assert the batch loader never invokes an instrument-wide history call:

```python
def test_batch119_snapshot_uses_only_exact_history_scope(batch119_db):
    client = ExactHistoryClient(
        positions=[],
        position_history=[closed_position_row()],
        trigger_history={"owned-stop": [triggered_stop_row()]},
    )

    snapshot = load_composite_batch_recovery_snapshot_read_only(
        batch119_db.session_factory,
        client=client,
    )

    assert snapshot.errors == {}
    assert client.instrument_wide_history_calls == 0
    assert client.exact_position_ids == {batch119_db.pos_id}
    assert client.exact_order_ids == {batch119_db.primary_id, batch119_db.backup_id}
```

Add a second test where one exact response has 100 rows with no completion
metadata and assert `snapshot_page_limit_ambiguous` remains in `errors`.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'exact_history_scope or exact_history_page_limit'
```

Expected: FAIL because the loader still delegates to the generic
instrument-wide reconciliation snapshot.

**Step 3: Add a dedicated snapshot value and immutable scope builder**

Add a module-local snapshot dataclass with the existing required collections,
account authority, capture times, and one `scope_fingerprint`. Load the fixed
batch, leg, verified entry leg, and protection ledger read-only. Require exactly
the allowlisted target identity and only verified `stop_loss`/`backup_stop`
rows owned by the same binding, leg, position, instrument, and side.

The scope payload must contain hashed references only:

```python
scope_payload = {
    "schema_version": 1,
    "batch_id": 119,
    "position_ref": _redacted_ref("recovery_position", leg.pos_id),
    "protection_refs": sorted(
        _redacted_ref("protection_order", row.order_id) for row in ledger
    ),
}
```

Reject missing, duplicate, non-verified, wrong-purpose, wrong-owner, or more
than two protection rows before any exchange read.

**Step 4: Capture exact collections inside one generation fence**

Use `capture_account_snapshot` with one composite reader. Inside it:

- use `read_positions` and `read_open_orders` with complete raw evidence;
- use `read_trigger_orders_pending` for `BTC-USDT-SWAP` and
  `observe_pending_tpsl` for its observation;
- use `read_position_history(inst_id=..., pos_id=...)`;
- call `read_trigger_order_history(..., order_id=..., limit=100)` once for each
  verified protection order;
- call exact regular-order/fill readers only when a durable exact regular-order
  reference exists; otherwise record a complete empty exact scope; and
- deduplicate only after every response has independently passed
  `build_exchange_collection_evidence`.

Any reader error, invalid schema, 100-row ambiguity, or generation drift adds a
closed safe reason to `snapshot.errors`. Never copy exception text.

**Step 5: Bind completeness and scope to planner evidence**

Extend `_snapshot_is_complete` to require complete account authority and a
valid SHA-256 `scope_fingerprint`. Include that fingerprint and capture
authority facts in `_exchange_evidence_payload` so a different exact query
scope invalidates the dry-run fingerprint.

**Step 6: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'snapshot or history or pagination or fingerprint or redacted'
```

Expected: PASS.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "feat: capture exact batch119 history evidence"
```

### Task 3: Prove an owned natural stop before `position_absent`

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:1560-1735`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:2000-2160`
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py:4480-4575`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write a failing natural-stop positive test**

Use real ORM rows for batch 119, a verified stop ledger, no durable management
close evidence, no current target position, exact closed-position history, and
one matching successful trigger row:

```python
def test_position_absent_accepts_one_exact_verified_natural_stop(batch119_db):
    plan = _plan(
        batch119_db,
        positions=[],
        position_history=[closed_position_row()],
        trigger_history=[triggered_stop_row(order_id=batch119_db.primary_id)],
    )

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"
    assert plan.production_writes == 0
    assert plan.exchange_calls == 0
    assert plan.evidence["natural_stop"]["purpose"] == "stop_loss"
    assert batch119_db.primary_id not in repr(plan.evidence)
```

**Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_composite_management_batch_recovery.py::test_position_absent_accepts_one_exact_verified_natural_stop
```

Expected: FAIL with `exchange_close_submission_evidence_present`.

**Step 3: Add failing adversarial tests before production logic**

Add separate tests for:

- a manual or unowned triggered order;
- both original stops claiming successful trigger;
- wrong position, instrument, side, or ledger purpose;
- missing, malformed, future, or reversed trigger/close timestamps;
- a durable management request, response, ID, mutation intent, or execution
  event appearing after snapshot capture; and
- a current position still present.

Each must return a stable refusal and zero writer calls.

**Step 4: Run the adversarial group and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'natural_stop'
```

Expected: the positive test fails; adversarial tests either fail because the
new refusal code is absent or demonstrate the old over-broad classification.

**Step 5: Implement one closed natural-stop proof**

Add a pure validator that returns a redacted proof or a safe refusal code:

```python
def _validated_natural_stop_proof(
    *, snapshot, ledger, pos_id: str, profile
) -> Mapping[str, Any]:
    # exactly one verified owned stop must prove the terminal trigger;
    # position history must prove the same position closed afterward;
    # every other close candidate is a conflict.
```

Only call it when `classify_recovery_position` returns `position_absent`.
Continue using the existing `_has_exchange_close_submission` refusal for every
non-absent disposition. For an absent position, consume only the close rows
accounted for by the validated proof; any residual close evidence refuses.

The proof serialized into evidence may contain only purpose, closed/triggered
state, bounded timestamps or time relation, counts, and hashed order/position
references.

**Step 6: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'position_absent or natural_stop or close_evidence'
```

Expected: PASS.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "fix: recognize exact owned batch119 stop close"
```

### Task 4: Close CLI, CAS, and zero-writer boundaries

**Files:**
- Modify: `src/telegram_kol_research/cli.py:4613-4785`
- Modify: `tests/test_composite_management_batch_recovery.py:5400-5750`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write failing end-to-end CLI tests**

Add a CLI test whose broad history would be ambiguous but whose exact natural
stop proof is complete. Assert the serialized plan is ready and contains no raw
identity. Add apply tests that assert:

- stale scope, source, or evidence fingerprint refuses before a transaction;
- a new durable close row between dry-run and apply refuses;
- `position_absent` apply creates no Deepcoin client writer and zero executor
  calls;
- repeated apply is strict and idempotent; and
- a non-v1 MiMo setting refuses with zero database changes.

**Step 2: Run the CLI group and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_composite_management_batch_recovery.py \
  -k 'recovery_cli and (natural_stop or position_absent or stale)'
```

Expected: FAIL until the CLI uses the exact loader and evidence fingerprint.

**Step 3: Route only the allowlisted command to the exact loader**

Keep the allowlist check before building a client. Use the new batch-specific
loader only after `batch_id == 119`; do not add a generic CLI option or expose
the helper to Web routes. Preserve current apply authorization and MiMo-v1
checks.

**Step 4: Run the CLI group and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_composite_management_batch_recovery.py
git commit -m "fix: bind batch119 cli to exact stop evidence"
```

### Task 5: Prove generic reconciliation is unchanged

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_protected_entry_reconciliation.py`
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Add regression tests for isolation and call bounds**

Assert:

- generic reconciliation still refuses instrument-wide 100-row history;
- protected-entry reconciliation still uses the generic complete snapshot;
- the batch-119 loader performs only current-state reads, one exact position
  history read, and at most two exact protection-history reads when no durable
  regular close reference exists; and
- no `POST`, cancel, close, or TPSL method is reachable from dry-run.

**Step 2: Run the tests and verify the new call-bound test RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_execution_bindings.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_composite_management_batch_recovery.py \
  -k 'page_limit or exact_history_call_bound or no_exchange_write'
```

Expected: the new exact call-bound test FAILS until all broad calls are removed.

**Step 3: Make only minimal isolation corrections**

Remove any remaining generic-history call from the dedicated loader. Do not
change the generic loader, page-limit authority, or protected-entry behavior.

**Step 4: Run the three complete files**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_execution_bindings.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_composite_management_batch_recovery.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  tests/test_execution_bindings.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_composite_management_batch_recovery.py \
  src/telegram_kol_research/composite_management_batch_recovery.py
git commit -m "test: isolate exact batch119 recovery reads"
```

### Task 6: Update the recovery runbook and perform final review

**Files:**
- Modify: `docs/runbook.md:1489-1575`
- Modify: `docs/deepcoin-request-governance-runbook.md:20-35`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Update the operator procedure**

Document that the batch-119 dry-run uses exact `posId` and verified protection
`ordId` reads, natural-stop proof is accepted only under the approved ownership
rule, a 100-row exact response still refuses, and apply remains separately
approved and zero-writer for `position_absent`.

The deployed service does not maintain the candidate's write-generation
authority, so an empty generation table cannot fence a running-service
capture. Replace that procedure with a separately approved stopped-service
window: record the original SHA/service state, prove the service inactive and
no durable/local writer remains, create two fresh private copies only after the
stop, bootstrap only each copy, and pass the same copy to both database CLI
arguments. A trap must restore the unchanged original service on success,
refusal, interruption, or cleanup failure.

Keep the explicit rule that batch 119 and Stage-1 deployment cannot share a
quiet window.

**Step 2: Run focused and broader regressions**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_client.py \
  tests/test_deepcoin_snapshot_authority.py \
  tests/test_execution_bindings.py \
  tests/test_protected_entry_reconciliation.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_executor.py
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: all tests PASS; compile and diff checks exit 0.

**Step 3: Commit documentation**

```bash
git add docs/runbook.md docs/deepcoin-request-governance-runbook.md
git commit -m "docs: require exact batch119 natural stop proof"
```

**Step 4: Request independent code review**

Use `superpowers:requesting-code-review` with the pre-implementation base SHA
and current HEAD. Require the reviewer to attack:

- unowned/manual close adoption;
- duplicate or mismatched stop evidence;
- exact response at the 100-row boundary;
- generation and fingerprint drift;
- hostile IDs/provider text and raw-identity leakage;
- accidental generic reconciliation behavior changes;
- any writer reachability; and
- position-absent apply/repeated-apply atomicity.

Fix every Critical or Important finding under a new RED/GREEN cycle and commit
the follow-up separately. Re-request review until READY.

**Step 5: Run the complete local gate**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
git status --short
```

Expected: complete suite PASS, compile/diff checks exit 0, worktree clean.

### Task 7: Generate a new production dry-run without applying

**Files:**
- No source changes
- Read: `docs/runbook.md:1489-1575`

**Step 1: Push the reviewed commits**

Push the exact reviewed HEAD to `codex/deepcoin-auto-trading-v1`. Do not deploy,
restart, change settings, or run apply.

**Step 2: Enter a separately approved stopped-service diagnostic window**

Record the original production SHA and active service state. Require a new,
explicit operator approval before stopping `telegram-kol.service`; prove it is
inactive, reject any other local Telegram/Deepcoin worker, and fail closed if
any durable active/unknown writer operation exists. Install nothing and do not
change the production checkout, settings, database, or service definition.

The same approval and cleanup boundary must record, quiesce, verify inactive,
and restore the Runtime Agent, runtime scanner, monitor timer, and any active
monitor worker units because their `telegram-kol-research` processes touch the
same production database. Recording/stopping only the main unit is invalid.

Create a mode-0700 temporary directory only for this diagnostic. After the
stop, use SQLite `.backup` to make a fresh mode-0600 copy and run candidate
additive schema bootstrap only on that copy. Never bootstrap the production
database. A bounded cleanup trap must restore the original active service and
verify the production SHA remains unchanged on every exit path.

**Step 3: Run the candidate dry-run against the private copy**

Run the allowlisted command with candidate `PYTHONPATH`, production read-only
Deepcoin credentials, and the temporary database path. Use that same private
copy for both `--database-path` and `--generation-database-path`; never use an
empty production generation table as a running-service fence. Capture only the
bounded JSON plan and keep the service inactive until both captures finish.

Expected:

- `status=ready`;
- `position.disposition=position_absent`;
- one hashed, verified natural-stop proof;
- `production_writes=0` and `exchange_calls=0`;
- no raw position/order IDs or provider text; and
- no service, database, setting, or exchange mutation.

If any condition fails, stop and report. Do not apply or deploy.

**Step 4: Repeat one fresh dry-run for stability**

While the service remains inactive, create a second new copy and repeat with a
new capture window. Require identical source population, logical role evidence,
exact-scope and collection digests, natural-stop ownership, and stable
source/evidence fingerprints. Capture timestamps must be freshly valid but are
excluded from the semantic comparison. Any refusal or semantic drift stops the
operation and invokes the service-restoring cleanup path.

Before human review, run the candidate SHA's bounded executable comparator:

```bash
PYTHONPATH="$CANDIDATE_ROOT/src" "$RUNTIME_PYTHON" \
  "$CANDIDATE_ROOT/scripts/compare_batch119_dry_runs.py" \
  "$RECOVERY_TMP/dry-run-1.json" \
  "$RECOVERY_TMP/dry-run-2.json"
```

It must enforce the exact outer/plan/position key allowlists, `status=ready`, a
valid closed disposition, zero production writes and exchange calls, valid
SHA-256 semantic fingerprints, bounded strict JSON, and complete plan equality.
It prints only a fixed stable/refused result and never echoes input evidence.

**Step 5: Present the redacted plan for separate approval**

Restore the unchanged original service, then return control to the user. Do not
run `--apply` in the same turn. A later apply requires separate approval, a new
stopped-service final capture, and the production database as both database
arguments. It must not reuse the diagnostic copy, reviewed fingerprint, or
stopped-service permit.
