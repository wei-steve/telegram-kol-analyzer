# Exact Historical Position Evidence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add exact fully closed Deepcoin position-history evidence to historical attribution repair, resolve stale shared ownership safely, and make planned clear-then-terminalize action chains transactional.

**Architecture:** Extend the read-only Deepcoin client with an exact `posId` history lookup used only by `repair-position-attribution`. Load deduplicated candidate histories into the repair snapshot and fingerprint, select terminal proof per entry leg, and preserve fail-closed behavior for missing, partial, mismatched, or unavailable evidence. Keep clear and terminalize as separate audited actions, but validate their explicit same-plan dependency during apply.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy, httpx, pytest, SQLite.

---

### Task 1: Add the exact historical-position read API

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `tests/test_deepcoin_client.py`

**Step 1: Write the failing client test**

Add a test that uses the existing mock transport and calls:

```python
rows = client.list_position_history(
    inst_id="BTC-USDT-SWAP",
    pos_id="position-1",
)
```

Assert the request is a signed `GET` to
`/deepcoin/account/positions-history` with:

```python
{
    "instType": "SWAP",
    "instId": "BTC-USDT-SWAP",
    "mrgPosition": "split",
    "posId": "position-1",
    "limit": "100",
}
```

Also assert the returned `data` list is validated through
`_require_list_data`.

**Step 2: Run the test and verify RED**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_deepcoin_client.py::test_list_position_history_queries_exact_split_position -q
```

Expected: FAIL because `DeepcoinRestClient` has no
`list_position_history` method.

**Step 3: Implement the minimal read-only method**

Add:

```python
DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH = "/deepcoin/account/positions-history"
```

Extend the protocol and client:

```python
def list_position_history(
    self,
    *,
    inst_id: str,
    pos_id: str,
) -> list[dict[str, Any]]:
    payload = self._request(
        "GET",
        _path_with_query(
            DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
            {
                "instType": "SWAP",
                "instId": inst_id,
                "mrgPosition": "split",
                "posId": pos_id,
                "limit": 100,
            },
        ),
    )
    return _require_list_data(
        payload,
        endpoint=DEEPCOIN_ACCOUNT_POSITIONS_HISTORY_PATH,
    )
```

This method must not be added to any trading/mutation path.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_deepcoin_client.py -q
```

Expected: all Deepcoin client tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py tests/test_deepcoin_client.py
git commit -m "feat: read exact Deepcoin position history"
```

---

### Task 2: Load exact history into repair evidence and fingerprints

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/position_attribution_repair.py`
- Modify: `tests/test_position_attribution_repair.py`

**Step 1: Extend fake clients without changing production code**

Add `list_position_history` to the repair test clients. It should record
`(inst_id, pos_id)` calls and default to `[]`.

**Step 2: Write failing evidence-loading tests**

Add tests that assert:

1. candidate IDs are built only from nonterminal Deepcoin entry-leg `pos_id`
   and `order_id` values;
2. duplicate `(instrument, identifier)` requests are issued once;
3. rows are stored in `snapshot.position_history`;
4. one request failure adds a source error and produces no historical actions;
5. changing a position-history row changes
   `exchange_evidence_fingerprint` and the plan fingerprint.

Use candidates such as:

```python
{
    ("BTC-USDT-SWAP", "stale-pos"),
    ("BTC-USDT-SWAP", "actual-order-pos"),
}
```

**Step 3: Run the new tests and verify RED**

Run the exact new test node IDs with `pytest -q`.

Expected: FAIL because `_ReconcileSnapshot` has no `position_history` and the
repair builder does not call the exact history method.

**Step 4: Add snapshot storage and a repair-only loader**

Add to `_ReconcileSnapshot`:

```python
position_history: list[dict[str, Any]] = field(default_factory=list)
```

In `position_attribution_repair.py`, derive candidates after local legs load:

```python
def _historical_position_candidates(bindings_by_id, legs):
    ...
```

Only include legs where:

- `purpose == "entry"`;
- status is not terminal;
- venue is Deepcoin; and
- binding symbol can produce `<SYMBOL>-USDT-SWAP`.

Load each unique candidate through `list_position_history`. Store exact rows in
the snapshot. Prefix errors with a stable key such as
`position_history:BTC-USDT-SWAP:<identifier>`.

Do not change normal `reconcile_deepcoin_execution_bindings` behavior.

**Step 5: Fingerprint exact history**

Add stable sorted `snapshot.position_history` rows to
`_exchange_evidence_fingerprint`.

**Step 6: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_position_attribution_repair.py -q
```

Expected: all position-attribution repair tests pass.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/position_attribution_repair.py \
  tests/test_position_attribution_repair.py
git commit -m "feat: fingerprint exact historical position evidence"
```

---

### Task 3: Require exact fully closed evidence per leg

**Files:**
- Modify: `src/telegram_kol_research/historical_attribution_cleanup.py`
- Modify: `tests/test_historical_attribution_cleanup.py`
- Modify: `tests/test_position_attribution_repair.py`

**Step 1: Write failing classification tests**

Add a helper that creates a position-history row and tests these cases
individually:

- exact split row with `pos="4"` and `closePos="4.0"` is terminal;
- `closePos="3"` is not terminal;
- zero or malformed `pos` is invalid;
- mismatched `posId`, instrument, side, or `mrgPosition` is invalid;
- duplicate conflicting exact rows are invalid;
- current live or pending identity still blocks cleanup even with closed history.

Expected terminal evidence shape:

```python
{
    "source": "exchange_position_history",
    "pos_id": "position-1",
    "pos": "4",
    "close_pos": "4.0",
    "avg_px": "62500",
    "close_avg_px": "62790.1",
    "pnl": "1.1604",
    "created_at": "...",
    "updated_at": "...",
}
```

**Step 2: Verify RED**

Run the new classification tests.

Expected: FAIL because historical position rows are ignored.

**Step 3: Implement strict decimal classification**

Use `Decimal`, not float equality:

```python
def _fully_closed_position_history_evidence(...):
    original = Decimal(str(row.get("pos")))
    closed = Decimal(str(row.get("closePos")))
    if original <= 0 or closed != original:
        return None
    ...
```

Require exact candidate ID, split mode, expected instrument, and expected side.
If more than one non-identical exact row exists, return an unresolved conflict
instead of choosing one.

**Step 4: Write the stale shared-owner RED test**

Model the production pattern:

```text
binding 9 / leg 10: order_id=shared, pos_id=shared
binding 11 / leg 13: order_id=actual-1, pos_id=shared
binding 11 / leg 14: order_id=actual-2, pos_id=shared
```

Provide fully closed exact rows for `shared`, `actual-1`, and `actual-2`.

Assert:

- leg 10 retains `shared` and is terminalized;
- legs 13 and 14 each receive a clear action for `shared`;
- their terminalize evidence references `actual-1` and `actual-2` respectively;
- the real historical identifier is present in evidence;
- no new persisted assignment action is produced; and
- no unresolved conflict remains for the component.

**Step 5: Verify RED**

Run the new production-pattern test.

Expected: FAIL because terminal evidence is currently calculated by shared
binding/component `pos_id`, not per leg.

**Step 6: Refactor terminal evidence selection per leg**

Introduce a deterministic helper such as:

```python
def terminal_evidence_for_leg(
    leg,
    binding,
    *,
    persisted_position_is_redundant: bool,
    ...,
) -> dict[str, object] | None:
    candidates = [leg.order_id] if persisted_position_is_redundant else [leg.pos_id, leg.order_id]
    ...
```

Retain the existing local lifecycle/event/reservation precedence when it is
exact. Use order-derived position history for a redundant competitor only when
the exact row is fully closed and matches the binding.

For exchange position history, set terminal leg state to the existing neutral
terminal state `closed`, record `historical_exchange_position_closed` as the
leg terminal reason, and use `exchange_closed` when an execution-backed
`entered` lifecycle must transition to `exited`. Do not label it stop-loss,
take-profit, or manual without proof. Extend the lifecycle reason documentation
or model comment so `exchange_closed` is an explicit supported neutral value.

**Step 7: Run planner and integration tests**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py -q
```

Expected: all tests pass.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/historical_attribution_cleanup.py \
  tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py
git commit -m "feat: prove historical closure by exact position"
```

---

### Task 4: Make clear-then-terminalize action chains atomic

**Files:**
- Modify: `src/telegram_kol_research/position_attribution_repair.py`
- Modify: `tests/test_position_attribution_repair.py`

**Step 1: Write the regression test and verify RED**

Build a plan where one leg has, in order:

```python
HistoricalCleanupAction(action="clear_redundant_historical_position", ...)
HistoricalCleanupAction(action="terminalize_historical_entry_leg", ...)
```

Apply it to a real temporary SQLite database and assert both changes commit and
both immutable audit rows exist.

Run the exact node ID.

Expected: FAIL with `stale repair plan: historical leg changed`, proving the
current defect.

**Step 2: Write the negative stale-state test**

Mutate the leg's `pos_id` before apply without a matching planned clear action.
Assert apply still raises `PositionAttributionRepairError` and no action/audit
commits.

This test may pass before the implementation and must remain green.

**Step 3: Implement explicit action dependencies**

Before applying actions, construct:

```python
planned_clears_by_leg = {
    action.leg_id: action
    for action in plan.historical_actions
    if action.action == "clear_redundant_historical_position"
}
```

Pass this map to `_apply_historical_cleanup_action`. For terminalize only,
accept the current `leg.pos_id == clear.new_pos_id` when the exact same plan has
a preceding clear whose old value equals the terminalize action's recorded
prior position. Keep status checks unchanged. Do not add generic `None`
tolerance.

**Step 4: Verify GREEN and rollback behavior**

Run the regression, stale-state, index rollback, and idempotency tests.

Expected: the planned chain commits; unplanned drift and forced index failure
roll back.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/position_attribution_repair.py \
  tests/test_position_attribution_repair.py
git commit -m "fix: apply dependent historical cleanup actions"
```

---

### Task 5: Document, verify, deploy, and stop at production dry-run

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Test: `tests/test_cli_smoke.py`

**Step 1: Add documentation and CLI-output assertions**

Document that exact fully closed position-history evidence may resolve a
historical conflict, while partial/missing/mismatched history remains blocking.
Document that order-derived historical IDs on stale competitors are audit
evidence, not new live ownership.

Keep the CLI dry-run JSON field names stable. Add smoke assertions only if the
new evidence summary changes serialized output.

**Step 2: Run focused verification**

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_deepcoin_client.py \
  tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py \
  tests/test_cli_smoke.py -q
```

Expected: all focused tests pass.

**Step 3: Run full verification**

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest -q
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m compileall -q src tests
git diff --check
git status --short
```

Expected: full suite passes, compile and diff checks exit zero, and only
intended changes remain before the final commit.

**Step 4: Commit documentation**

```bash
git add docs/runbook.md docs/server-deployment.md tests/test_cli_smoke.py
git commit -m "docs: verify exact historical close evidence"
```

**Step 5: Integrate and push**

Confirm both worktrees are clean. Fast-forward
`codex/deepcoin-auto-trading-v1` to this worktree HEAD and push the target
branch.

**Step 6: Verify production safety before deployment**

Read back:

- `auto_trade_enabled` is still `False`;
- the existing backup
  `data/research.db.20260715-123243.historical-cleanup.bak` still has size
  `22528000` and SHA-256
  `c6dfa9eba14628a0ac8d1de3453b09f0d3d34913a02cead640c316a5e56c3c6f`;
- no cleanup apply has occurred.

Stop if any precondition differs.

**Step 7: Deploy through the approved helper**

```bash
./scripts/server_git_update.sh
```

Verify server HEAD, active service PID, startup log, and automatic trading false.

**Step 8: Run production dry-run only**

```bash
ssh -i "$HOME/.ssh/tecent.pem" root@43.167.220.225 '
  cd /opt/telegram-kol-analyzer &&
  .venv/bin/telegram-kol-research repair-position-attribution \
    --database-path data/research.db
'
```

Do not pass `--apply`.

**Step 9: Review and stop**

Report:

- current live IDs and `actions` count;
- historical action counts and exact targets;
- remaining unresolved conflicts;
- absence of live IDs from historical actions;
- presence/absence of the unique-index action;
- new database, exchange, and plan fingerprints;
- automatic trading state, server HEAD, service PID, and backup hash.

Even if conflicts become zero, stop for separate operator approval before any
production apply.
