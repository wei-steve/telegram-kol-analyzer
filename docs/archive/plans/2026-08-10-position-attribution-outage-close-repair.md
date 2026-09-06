# Verified Attribution Outage/Close Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent a previously verified position from becoming a false attribution conflict after an API outage and normal close, then use audited read-only evidence to restore and terminalize production convergence ID 40.

**Architecture:** Reuse the immutable policy-v2 `ownership_verified` audit as the authority source when a healthy snapshot no longer contains a formerly verified position and no real competing identity exists. Extend the existing dry-run-first historical state repair flow with a bounded exact-position-history loader and one atomic `take_profit_attribution_repair` action that restores terminal ownership and terminalizes its stale TP ledger without any exchange writes.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, Typer, pytest, Deepcoin read-only REST client, systemd.

---

### Task 1: Reproduce and fix the runtime false-conflict transition

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:700-890`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write the failing outage/close regression test**

Add a test that seeds one active entry leg with:

- `pos_id="pos-verified"`
- `attribution_status="verified"`
- a policy-v2 `PositionAttributionAudit(event_type="ownership_verified", new_state="verified")`

Run reconciliation once with a client whose position snapshot fails and assert the leg becomes `evidence_unavailable`. Run it again with a complete empty position snapshot and no candidate conflicts. Assert the leg is restored to `verified`, retains the same `pos_id`, and is not `attribution_conflict`.

```python
def test_reconcile_restores_prior_authority_after_outage_and_position_close(tmp_path):
    # seed binding, leg, and immutable policy-v2 ownership audit
    reconcile_deepcoin_execution_bindings(session_factory, client=FailingClient())
    reconcile_deepcoin_execution_bindings(session_factory, client=CompleteEmptyClient())

    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        assert leg.pos_id == "pos-verified"
        assert leg.attribution_status == "verified"
        assert not session.query(PositionAttributionAudit).filter_by(
            event_type="attribution_conflict"
        ).count()
```

**Step 2: Run the regression and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py::test_reconcile_restores_prior_authority_after_outage_and_position_close
```

Expected: FAIL because the second complete snapshot changes the leg to `attribution_conflict`.

**Step 3: Implement the minimal authority restoration**

In the unmatched-leg loop, treat a same-venue/same-leg/same-pos policy-v2 audit as prior authority only when the current matcher produced no conflict for the leg:

```python
prior_authority_missing = bool(
    leg.pos_id
    and (
        str(leg.attribution_status or "") == "verified"
        or leg_id in prior_authoritative_leg_ids
    )
    and str(leg.pos_id) not in live_position_ids
    and leg_id not in conflict_leg_ids
)
if prior_authority_missing:
    if str(leg.attribution_status or "") != "verified":
        _transition_leg_attribution(
            session,
            leg=leg,
            event_type="ownership_restored",
            new_state="verified",
            evidence={
                "evidence_type": "prior_authoritative_position_audit",
                "policy_version": ATTRIBUTION_POLICY_VERSION,
                "pos_id": str(leg.pos_id),
            },
            recovered_at=recovered_at,
        )
    continue
```

Do not change `leg.status` to `active`; ownership and liveness remain separate.

**Step 4: Add the true-conflict negative test**

Seed the same prior audit but return a matcher conflict involving another leg or a different position. Assert the leg remains `attribution_conflict`; prior authority must never override current contradictory evidence.

**Step 5: Run focused execution-binding tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_execution_bindings.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py tests/test_execution_bindings.py
git commit -m "fix: preserve verified ownership across snapshot outages"
```

### Task 2: Add a bounded read-only exact position-history snapshot

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:3460-3675`
- Modify: `src/telegram_kol_research/historical_state_repair.py`
- Modify: `src/telegram_kol_research/cli.py:4070-4160`
- Test: `tests/test_historical_state_repair_cli.py`

**Step 1: Expose one shared full-close predicate**

Extract or rename the existing `_position_history_row_proves_full_close` logic so both execution reconciliation and historical repair use the same exact rules: matching instrument, side, `pos_id`, positive opened size, and `opened == closed` within tolerance. Do not duplicate the numeric/alias logic.

**Step 2: Write the failing CLI snapshot test**

Build a fake client and an ID-40-shaped database candidate. Assert dry-run calls:

```python
client.list_position_history(
    inst_id="BTC-USDT-SWAP",
    pos_id="pos-terminal",
)
```

exactly once, and assert no fake write method is called.

**Step 3: Add the historical repair snapshot loader**

Create a bounded helper in `historical_state_repair.py`:

```python
def load_historical_state_repair_snapshot_read_only(
    session_factory, *, client
):
    snapshot = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory, client=client
    )
    for instrument_id, pos_id in _restorable_history_candidates(session_factory):
        try:
            rows = client.list_position_history(
                inst_id=instrument_id,
                pos_id=pos_id,
            )
        except Exception as exc:
            snapshot.errors[f"position_history:{instrument_id}:{pos_id}"] = str(exc)
            continue
        # Require a list of dict rows and exact returned position identities.
        snapshot.position_history.extend(rows)
    return snapshot
```

Candidate discovery must be read-only and limited to Deepcoin `submitted` TP convergences whose terminal leg is currently `attribution_conflict` or `evidence_unavailable`, has a nonblank exact `pos_id`, and has a matching policy-v2 authoritative audit. Deduplicate instrument/position pairs.

**Step 4: Wire both dry-run and apply reload to the new loader**

Replace both CLI snapshot calls with `load_historical_state_repair_snapshot_read_only`. The apply lambda must reload the same bounded exact history rather than reuse the dry-run object.

**Step 5: Add fail-closed snapshot tests**

Cover:

- exact history reader raises;
- response is not a list of dicts;
- response contains another nonblank `pos_id`;
- blank/missing returned position identity;
- duplicate exact rows.

Every malformed/error case must add a snapshot error and make the plan non-applicable. Duplicate exact rows must be deterministically deduplicated.

**Step 6: Run CLI tests and commit**

```bash
.venv/bin/python -m pytest -q tests/test_historical_state_repair_cli.py
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/historical_state_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_historical_state_repair_cli.py
git commit -m "feat: load exact history for attribution repair"
```

### Task 3: Plan a strictly proven atomic TP attribution repair

**Files:**
- Modify: `src/telegram_kol_research/historical_state_repair.py:420-640`
- Test: `tests/test_historical_state_repair.py`

**Step 1: Add the smallest production-shaped fixture**

Create a helper that seeds:

- terminal lifecycle and binding;
- terminal entry leg with `attribution_conflict` and exact `pos_id`;
- policy-v2 authority audits from `trade_fill` and `regular_order`;
- a later empty-candidate conflict audit;
- confirmed exact close mutation and close reservation;
- submitted convergence with three active TP ledger rows.

Use synthetic IDs, not production identifiers.

**Step 2: Write the failing positive plan test**

Provide a complete snapshot with the exact fully-closed position-history row and no current position/pending TP IDs. Assert exactly one action:

```python
assert [(row.kind, row.target_id, row.reason_code) for row in plan.actions] == [
    (
        "take_profit_attribution_repair",
        convergence_id,
        "convergence_position_terminal_prior_authority_restored",
    )
]
assert not plan.conflicts
```

**Step 3: Implement a pure evidence predicate**

Add a helper that returns canonical evidence only when all of these are exact and consistent:

- convergence/binding/leg/orders share Deepcoin venue, binding ID, leg ID, strategy identity, and normalized `pos_id`;
- lifecycle, binding, and leg are terminal;
- at least one matching policy-v2 `ownership_verified` audit exists;
- every later `attribution_conflict` audit has empty candidate leg and position identities;
- exact `close_position` mutation and close reservation are both `confirmed`;
- no other leg owns the same venue/`pos_id`;
- exactly one normalized position-history row proves a full close;
- exact position and all ledger order IDs are absent from complete current snapshots.

Include audit IDs, hashes, mutation/reservation identity and status, normalized position-history evidence, and competing-owner query result in `evidence_json` and the database/exchange fingerprints.

**Step 4: Add fail-closed negative tests**

Parameterize at least these mutations, each of which must produce no action and a specific exclusion/conflict:

- authority audit missing, wrong policy version, wrong leg, wrong venue, or wrong `pos_id`;
- later conflict audit contains a candidate leg or candidate position;
- close mutation/reservation missing, non-confirmed, wrong binding/leg/strategy/position;
- another leg owns the same position;
- history absent, partial close, wrong side/instrument/position, multiple exact rows;
- current position live or one TP order still pending;
- lifecycle/binding/leg nonterminal;
- convergence/order identity mismatch.

**Step 5: Verify focused planner tests and commit**

```bash
.venv/bin/python -m pytest -q tests/test_historical_state_repair.py
git add src/telegram_kol_research/historical_state_repair.py \
  tests/test_historical_state_repair.py
git commit -m "feat: plan proven terminal attribution repair"
```

### Task 4: Apply the repair atomically with complete local CAS

**Files:**
- Modify: `src/telegram_kol_research/historical_state_repair.py:650-1060`
- Test: `tests/test_historical_state_repair.py`
- Test: `tests/test_historical_state_repair_cli.py`

**Step 1: Write the failing atomic-apply test**

Apply a one-action plan and assert in one committed transaction:

- the leg is `verified` but remains `manually_closed`;
- the leg attribution evidence names `historical_authority_restored` and the plan fingerprint;
- a deduplicated `PositionAttributionAudit` records the restoration;
- the convergence is completed with the new reason code;
- all three TP rows are expired with terminalization evidence;
- the global repair audit is `notification_status="not_needed"`;
- no business row is deleted.

**Step 2: Add the new action dispatch**

Handle `take_profit_attribution_repair` separately from ordinary TP terminalization. Before writes, reconstruct and compare every local evidence field captured by the plan, including authority/conflict audits, close mutation/reservation, leg/binding/lifecycle, convergence and TP rows.

Do not trust exchange rows re-read inside the transaction; they are bound by the freshly rebuilt plan and exchange fingerprint immediately before the transaction. If any local CAS differs, raise `HistoricalStateRepairRefused` and roll back everything.

**Step 3: Restore authority and terminalize in one transaction**

Use a deterministic audit fingerprint derived from venue, binding ID, leg ID, `pos_id`, action reason and repair-plan fingerprint. Preserve terminal leg status and original audit rows. Append terminalization evidence to each TP row rather than replacing its original evidence object.

**Step 4: Add concurrency/CAS tests**

After plan construction but before apply, independently change each category and assert refusal with no partial write:

- leg attribution/status/strategy/position;
- authority or conflict audit evidence;
- close mutation/reservation status or identity;
- binding/lifecycle state;
- convergence state;
- one TP row identity/status/evidence.

**Step 5: Add token/idempotency/write-boundary tests**

Assert stale fingerprint, wrong action count, wrong/reused token and zero-action apply all refuse. Use a fake client whose order/cancel/close methods raise and assert no write method is ever called during dry-run or apply.

**Step 6: Run focused tests and commit**

```bash
.venv/bin/python -m pytest -q \
  tests/test_historical_state_repair.py \
  tests/test_historical_state_repair_cli.py
git add src/telegram_kol_research/historical_state_repair.py \
  tests/test_historical_state_repair.py \
  tests/test_historical_state_repair_cli.py
git commit -m "feat: apply atomic terminal attribution repair"
```

### Task 5: Run complete review and regression gates

**Files:**
- Modify only if a test or review finds a defect.

**Step 1: Run static diff checks**

```bash
git diff --check
```

Expected: no output.

**Step 2: Run the focused safety suites**

```bash
.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py \
  tests/test_position_take_profit_orders.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_historical_state_repair.py \
  tests/test_historical_state_repair_cli.py
```

Expected: PASS.

**Step 3: Run the full suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass, with only already-known warnings/skips.

**Step 4: Review the complete change**

Review against `docs/plans/2026-08-10-position-attribution-outage-close-repair-design.md`, prioritizing:

- no exchange write path;
- no authority restoration over genuine conflicts;
- exact position/order alias normalization;
- snapshot completeness;
- local TOCTOU/CAS;
- cross-venue/strategy isolation;
- idempotency and audit preservation.

Fix every P1/P2 finding with a failing regression test and repeat Steps 1–3.

**Step 5: Push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

### Task 6: Deploy the prevention fix and clean production ID 40

**Files:**
- Update after success: `docs/plans/2026-08-10-position-attribution-outage-close-repair-design.md`

**Step 1: Capture a fresh read-only pre-deployment snapshot**

Record production SHA, service state, current positions, open orders, pending trigger orders, and exact ID-40 evidence. Prove no time-sensitive local mutation/management operation is active. Do not infer safety from stale earlier snapshots.

**Step 2: Stop service and create a verified SQLite backup**

Use the SQLite backup API, not an unlocked file copy. Run `PRAGMA integrity_check` on source and backup and require `ok` for both.

**Step 3: Pull, reinstall and run server-focused tests**

```bash
cd /opt/telegram-kol-analyzer
git pull --ff-only origin codex/deepcoin-auto-trading-v1
.venv/bin/python -m pip install -e .
.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  tests/test_historical_state_repair.py \
  tests/test_historical_state_repair_cli.py
```

Keep `telegram-kol.service` stopped.

**Step 4: Dry-run and inspect the exact plan**

Run `repair-historical-state-convergence` without `--apply`. Require:

- exactly one action;
- kind `take_profit_attribution_repair`;
- target ID 40;
- no conflicts;
- current live convergence IDs remain exclusions;
- complete exact position history and pending-order observations;
- no current live position/order ID appears in action evidence.

**Step 5: Apply once with all gates**

Use the exact dry-run fingerprint, action count `1`, and confirmation token. The apply-side reload must reproduce the plan exactly or refuse.

**Step 6: Verify database invariants**

Require:

- leg 378 is `verified/manually_closed`;
- convergence 40 is completed with the restoration reason;
- TP ledger 11–13 is expired;
- one restoration audit and one global non-notifying summary audit exist;
- row counts have not decreased;
- `PRAGMA integrity_check` is `ok`;
- immediate dry-run has zero actions and zero conflicts.

**Step 7: Verify exchange invariants and restart**

Reload the account read-only snapshot and compare exact current position/open-order/trigger-order identities and sizes with the pre-deployment snapshot. Start `telegram-kol.service`, require `active/monitoring`, HTTP 200 for the positions page and open-orders partial, and no new trading/reconciliation errors.

**Step 8: Record evidence and push the docs-only commit**

Append deployed SHA, backup path, plan fingerprint, before/after database counts, audit IDs, exchange snapshot comparison and service health to the design document. Commit and push the evidence; no restart is required for this docs-only pull.

**Rollback:** If any verification fails before apply, leave the database unchanged and restart only after resolving or reverting code. If verification fails after apply but before restart, preserve an incident copy and restore the verified backup. If code health also fails, return to the previous production SHA before starting the service.
