# Adjacent Entry Message Assembly Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Assemble explicit entry sizing, leg allocation, and supplemental-entry instructions from adjacent Telegram messages, then safely revise an exact submitted strategy without exceeding its effective stop-loss budget.

**Architecture:** Preserve authoritative recognition and contextual target resolution, but persist their non-executable entry fragments in a general append-oriented ledger. Add a source-order admission barrier and immutable multi-fragment assembly before entry execution; route any post-submission change through a durable exact-leg revision state machine with exchange read-back, continuous stop protection, and fail-closed recovery.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, pytest, Telethon message ingestion, existing Deepcoin REST client and exact strategy revision/management infrastructure.

---

### Task 1: Add dormant fragment, assembly-link, and revision settings contracts

**Files:**
- Modify: `src/telegram_kol_research/models.py:251`
- Modify: `src/telegram_kol_research/db.py:627`
- Modify: `src/telegram_kol_research/trading_settings.py:55`
- Modify: `src/telegram_kol_research/templates/index.html:365`
- Test: `tests/test_db_bootstrap.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing database and settings tests**

Require fresh and upgraded databases to contain `entry_strategy_fragments` and
`entry_assembly_fragments`. Require two independent settings to default to
`disabled` and accept only `disabled`, `shadow`, or `live`:

```python
assert settings.entry_message_assembly_v2_mode == "disabled"
assert settings.entry_revision_v2_mode == "disabled"

with pytest.raises(ValueError, match="entry_message_assembly_v2_mode"):
    trading_settings_from_payload({"entry_message_assembly_v2_mode": "unsafe"})
```

Test the fragment status and kind check constraints, unique fingerprint, and
assembly/fragment unique association. Assert the settings API and HTML render
both switches while retaining the existing `entry_preamble_mode` control.

**Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest -q \
  tests/test_db_bootstrap.py \
  tests/test_trading_settings.py \
  tests/test_web_app.py -k 'entry_message or entry_revision' \
  tests/test_web_page_render.py -k 'entry_message or entry_revision'
```

Expected: FAIL because the new tables, model classes, and settings do not exist.

**Step 3: Add minimal dormant models and migrations**

Add `EntryStrategyFragment` with bounded columns for source identity, normalized
symbol/side, fragment kind, payload JSON, evidence generation, optional target
identities, source relationship, status, reason, fingerprint, and transition
timestamps. Add `EntryAssemblyFragment` as the unique association between one
assembly and one fragment.

Use explicit constraints:

```python
CheckConstraint(
    "fragment_kind IN ('risk_multiplier','leg_allocation','supplemental_entry')",
    name="ck_entry_strategy_fragments_kind",
)
CheckConstraint(
    "status IN ('pending','assembled','consumed','invalidated','expired','blocked')",
    name="ck_entry_strategy_fragments_status",
)
CheckConstraint(
    "source_relationship IN ('before_strategy','after_strategy','unresolved')",
    name="ck_entry_strategy_fragments_relationship",
)
```

Add idempotent `ALTER TABLE`/`CREATE TABLE IF NOT EXISTS` bootstrap support using
the repository's existing migration style. Do not rename or rewrite
`entry_preambles` or `entry_strategy_assemblies`.

Add both settings to `TradingSettings`, serialization, validation, save/load,
API, and the trading settings form. Keep both disabled by default.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/templates/index.html \
  tests/test_db_bootstrap.py tests/test_trading_settings.py \
  tests/test_web_app.py tests/test_web_page_render.py
git commit -m "feat: add dormant adjacent entry contracts"
```

### Task 2: Normalize explicit sizing, allocation, and supplemental-entry evidence

**Files:**
- Modify: `src/telegram_kol_research/message_evidence.py:29`
- Modify: `src/telegram_kol_research/prompt_defaults.py:66`
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Modify: `src/telegram_kol_research/prompt_composition.py`
- Test: `tests/test_message_evidence.py`
- Test: `tests/test_prompt_composition.py`
- Test: `tests/test_ai_recognition_config.py`

**Step 1: Write failing evidence tests**

Cover these exact normalized results:

```python
assert normalize_entry_fragments({
    "fragments": [{
        "kind": "risk_multiplier",
        "symbol": "btc",
        "side": "LONG",
        "risk_multiplier": "0.50",
        "confidence": 0.95,
        "reason": "明确50%仓位",
    }]
})[0].payload == {"risk_multiplier": "0.5"}
```

Also require:

- `半仓操作` and numeric `50%仓位` produce multiplier `0.5`;
- `全仓操作` and `正常仓位操作` produce multiplier `1`;
- `轻仓` alone produces no fragment;
- `轻仓入场，50%仓位` uses the explicit numeric percentage;
- `两个点位各半仓` produces multiplier `1` plus allocations `[0.5, 0.5]`;
- `补仓：63400附近` produces a supplemental-entry price, not a new budget;
- range width alone produces no risk fragment;
- boolean, zero, greater-than-one, NaN, missing reason, invalid side, and
  unparseable price are rejected without failing the whole recognition.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_message_evidence.py \
  tests/test_prompt_composition.py \
  tests/test_ai_recognition_config.py
```

Expected: FAIL because only the legacy single `entry_context` preamble is
normalized.

**Step 3: Implement the bounded fragment contract**

Add frozen dataclasses for the three fragment kinds and one parser returning a
tuple of validated fragments. Payloads must be canonical decimal strings and
bounded lists, not free-form model output.

Extend the prompt schema with an `entry_fragments` array. Preserve
`entry_context` during rollout for backward compatibility, but adapt it into a
single risk fragment when the new array is absent. State explicitly that
`全仓/正常仓位` means 100 percent of the configured maximum-loss budget, never
account balance or margin mode.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/message_evidence.py \
  src/telegram_kol_research/prompt_defaults.py \
  src/telegram_kol_research/ai_recognition_config.py \
  src/telegram_kol_research/prompt_composition.py \
  tests/test_message_evidence.py tests/test_prompt_composition.py \
  tests/test_ai_recognition_config.py
git commit -m "feat: normalize adjacent entry fragments"
```

### Task 3: Persist authoritative fragments without changing execution

**Files:**
- Create: `src/telegram_kol_research/entry_strategy_fragments.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:340`
- Test: `tests/test_entry_strategy_fragments.py`
- Test: `tests/test_authoritative_recognition.py`

**Step 1: Write failing persistence tests**

Test that shadow/live modes persist valid fragments, disabled mode persists
nothing, repeated evidence is idempotent, and a newer evidence version
invalidates only pending fragments from the older generation. Require append
semantics for consumed fragments.

Test a non-strategy authoritative result carrying two fragments:

```python
{
    "recognition_result": "非策略",
    "strategy": {},
    "lifecycle_event": {"event_type": "none"},
    "entry_fragments": [
        {"kind": "risk_multiplier", "risk_multiplier": "1", ...},
        {"kind": "leg_allocation", "allocations": ["0.5", "0.5"], ...},
    ],
}
```

Assert two durable rows share the source message and evidence version but have
different fingerprints.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_strategy_fragments.py \
  tests/test_authoritative_recognition.py -k 'entry_fragment or entry_context'
```

Expected: FAIL because the persistence module and authoritative hook do not
exist.

**Step 3: Implement persistence**

Mirror the transaction ownership and fingerprint discipline in
`entry_preambles.py`, but persist one row per normalized fragment. Do not assign
a strategy target in first-pass extraction. Record `source_relationship` as
`unresolved` until deterministic/contextual assembly resolves it.

Call persistence only after the current evidence version is durably saved. Do
not trigger `auto_trade_executor` from fragment persistence.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_strategy_fragments.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_entry_strategy_fragments.py \
  tests/test_authoritative_recognition.py
git commit -m "feat: persist authoritative entry fragments"
```

### Task 4: Build the pure bidirectional source-order selector

**Files:**
- Create: `src/telegram_kol_research/adjacent_entry_assembly.py`
- Modify: `src/telegram_kol_research/entry_strategy_assembly.py:77`
- Test: `tests/test_adjacent_entry_assembly.py`
- Test: `tests/test_entry_strategy_assembly.py`

**Step 1: Write failing pure-selection tests**

Represent all facts as immutable values and test:

- 陈哥 `9901 -> 9902` selects a preceding `0.5` fragment;
- 陈哥 `9935 -> 9936` selects a following `1.0` fragment;
- 米娅 `558 -> 559` and `538 -> 539` select following `0.5` fragments;
- 飞扬 `4154 -> 4155` selects a following supplemental price;
- an unresolved following fact returns
  `adjacent_entry_context_pending`;
- a completed unrelated fact does not block;
- a new complete entry, cancellation, opposite-side entry, replacement, or
  expired adjacency boundary stops selection;
- conflicting explicit multipliers block with
  `entry_risk_multiplier_conflict`;
- a 300-point range with no fragment remains multiplier `1`;
- `两个点位各半仓` returns multiplier `1` and allocations `[0.5, 0.5]`.

The selector signature should accept an explicit cutoff and no database handle:

```python
decision = select_adjacent_entry_fragments(
    strategy=strategy_fact,
    facts=facts,
    cutoff=cutoff,
)
```

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_adjacent_entry_assembly.py \
  tests/test_entry_strategy_assembly.py
```

Expected: FAIL because the current selector discards every fact at or after the
strategy source key and supports only one preamble.

**Step 3: Implement the pure selector and compatibility adapter**

Use `(posted_at UTC, message_id, raw_message_id)` for ordering. Bound the segment
by hard message facts rather than worker completion time. Return a calculation
object containing fragment IDs, explicit multiplier, allocations,
supplemental prices, boundary evidence, status, and reason code.

Adapt legacy `PriorMessageFact`/`EntryPreamble` rows into risk fragments so
existing live preamble behavior remains unchanged while v2 is disabled.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/adjacent_entry_assembly.py \
  src/telegram_kol_research/entry_strategy_assembly.py \
  tests/test_adjacent_entry_assembly.py \
  tests/test_entry_strategy_assembly.py
git commit -m "feat: select bidirectional entry fragments"
```

### Task 5: Add the source-order admission barrier and durable wake-up

**Files:**
- Create: `src/telegram_kol_research/entry_assembly_admission.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:550`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:700`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Test: `tests/test_entry_assembly_admission.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_telegram_live_listener.py`

**Step 1: Write failing admission and wake-up tests**

Create a complete strategy plus a later same-chat raw message that already
exists but has an active evidence extraction claim. Assert:

```python
result = auto_process_message_trade_signal(...)
assert result == {
    "status": "deferred",
    "reason": "adjacent_entry_context_pending",
}
assert session.query(TradeSignal).count() == 0
```

Then finish the later message as irrelevant and assert the original entry is
woken exactly once. Repeat with a valid 50-percent fragment and assert the wake
uses that fragment. Cover recognition failure/expiry, duplicate completion,
concurrent workers, a different chat, and a later hard boundary.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_assembly_admission.py \
  tests/test_auto_trade_execution.py -k 'adjacent or preamble' \
  tests/test_telegram_live_listener.py -k 'adjacent or authoritative'
```

Expected: FAIL because execution does not examine later persisted messages or
schedule the original strategy after their completion.

**Step 3: Implement admission and idempotent scheduling**

Capture one assembly cutoff at the execution attempt. In shadow mode, record
the proposed defer without stopping the legacy path. In live mode, return the
fixed deferred reason before recovery evaluation, trade-signal creation, or any
Deepcoin call.

Persist a bounded retry marker keyed by strategy raw-message ID, candidate
generation, and cutoff. When a blocking later message reaches terminal
recognition, enqueue/reinvoke the original executor once. Reuse an existing
durable retry/claim table if it provides exact generation identity; otherwise
add a small `entry_assembly_attempts` table in Task 1's migration style rather
than relying on an in-memory asyncio task.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS with zero exchange-client calls in deferred cases.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_assembly_admission.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/telegram_live_listener.py \
  tests/test_entry_assembly_admission.py \
  tests/test_auto_trade_execution.py tests/test_telegram_live_listener.py
git commit -m "feat: defer entries for adjacent evidence"
```

### Task 6: Persist immutable multi-fragment assemblies and size every entry leg

**Files:**
- Modify: `src/telegram_kol_research/entry_strategy_assembly.py:378`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:576`
- Modify: `src/telegram_kol_research/deepcoin_executor.py`
- Modify: `src/telegram_kol_research/recovery_order_drafts.py`
- Test: `tests/test_entry_strategy_assembly.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_deepcoin_executor.py`
- Test: `tests/test_recovery_order_drafts.py`

**Step 1: Write failing atomicity and sizing tests**

Require one assembly to consume multiple fragments atomically. Repeating the
same generation returns the same assembly and does not consume another row.
Concurrent assembly attempts produce one current assembly.

Test calculations:

```python
assert evidence["configured_risk_budget_usdt"] == 20.0
assert evidence["strategy_risk_multiplier"] == "0.5"
assert evidence["effective_risk_budget_usdt"] == 10.0
assert sum(Decimal(str(leg["estimated_stop_loss_usdt"])) for leg in legs) <= Decimal("10")
```

Cover full/normal 20U, half-range sharing 10U, two-points-each-half sharing 20U,
contract rounding, and a supplemental third price that remains within the cap.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_strategy_assembly.py \
  tests/test_auto_trade_execution.py -k 'fragment or allocation or supplemental' \
  tests/test_deepcoin_executor.py -k 'risk or leg' \
  tests/test_recovery_order_drafts.py -k 'risk or leg'
```

Expected: FAIL because the current assembly is one-to-one and current draft
builders do not accept supplemental prices or explicit leg allocations.

**Step 3: Implement atomic assembly and one-pass risk sizing**

Create the assembly and association rows, transition every selected pending
fragment to `consumed`, and commit in one transaction. Save canonical bounded
evidence and a fingerprint covering all source/evidence generations.

Apply the total risk multiplier exactly once before allocating risk to legs.
Build quantities from per-leg allocated loss budget and the shared stop. Round
down. Reject any draft whose recomputed aggregate risk exceeds the effective
budget.

Shadow mode must record proposed evidence but apply multiplier `1` and preserve
the legacy order path.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_strategy_assembly.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/deepcoin_executor.py \
  src/telegram_kol_research/recovery_order_drafts.py \
  tests/test_entry_strategy_assembly.py tests/test_auto_trade_execution.py \
  tests/test_deepcoin_executor.py tests/test_recovery_order_drafts.py
git commit -m "feat: size immutable adjacent entry assemblies"
```

### Task 7: Plan a durable sizing/supplemental revision for submitted strategies

**Files:**
- Modify: `src/telegram_kol_research/models.py:492`
- Modify: `src/telegram_kol_research/db.py:627`
- Create: `src/telegram_kol_research/entry_revision_planner.py`
- Modify: `src/telegram_kol_research/strategy_revision_planner.py:72`
- Test: `tests/test_entry_revision_planner.py`
- Test: `tests/test_strategy_revision_planner.py`

**Step 1: Write failing planner tests**

Test exact strategies with:

- two verified pending legs;
- one filled `posId` plus one verified pending leg;
- an unknown or missing order identity;
- an order/position state conflict;
- an assembly fingerprint already planned;
- a fragment targeting a different chat/thread/strategy generation;
- revision mode disabled, shadow, and live.

Assert the planner writes no Deepcoin request. Require immutable snapshots of
every entry leg and current protection identity. Repeated planning returns the
same batch.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_revision_planner.py \
  tests/test_strategy_revision_planner.py
```

Expected: FAIL because the current revision planner handles replacement
strategies, not sizing/supplemental assembly generations.

**Step 3: Implement the new revision kind**

Extend the revision batch schema with a bounded revision kind or add a dedicated
batch table if changing current semantics would weaken existing replacement
invariants. Store target assembly fingerprint, configured/effective budget,
exact binding/thread/lifecycle, leg snapshots, protection snapshot, and desired
replacement legs.

Use only exact owned IDs. A submitted signal with unknown exchange-write state
returns `revision_submission_state_unknown` and creates no replacement plan.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS and zero exchange-client calls.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/entry_revision_planner.py \
  src/telegram_kol_research/strategy_revision_planner.py \
  tests/test_entry_revision_planner.py \
  tests/test_strategy_revision_planner.py
git commit -m "feat: plan durable entry sizing revisions"
```

### Task 8: Execute exact unfilled-leg cancellation and rebuild

**Files:**
- Create: `src/telegram_kol_research/entry_revision_executor.py`
- Modify: `src/telegram_kol_research/context_resolution_replay.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_entry_revision_executor.py`
- Test: `tests/test_context_resolution_replay.py`

**Step 1: Write failing executor tests**

Use a fake Deepcoin client and require this order:

1. read exact pending regular/trigger orders;
2. cancel each old exact entry leg;
3. read back every old leg as terminal;
4. submit replacement legs from the immutable assembly;
5. read back replacements;
6. reconcile the batch.

Cover already-cancelled idempotency, cancellation `not found` followed by proven
terminal state, timeout/unknown response, disappeared order without proof,
wrong order economics, service restart, and duplicate worker claims.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_revision_executor.py \
  tests/test_context_resolution_replay.py -k revision
```

Expected: FAIL because no sizing-revision executor exists.

**Step 3: Implement serialized, resumable execution**

Reuse exact cancellation and exchange-write serialization primitives. Allow one
bounded revalidation for a definite `not found`; treat timeouts and ambiguous
responses as `recovery_required`. Never submit replacements until all old
pending legs are proven terminal.

In shadow mode, persist the planned sequence and read-only snapshot with zero
write calls. In disabled mode, do not create or advance new batches.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_revision_executor.py \
  src/telegram_kol_research/context_resolution_replay.py \
  src/telegram_kol_research/recovery_live_submit.py \
  tests/test_entry_revision_executor.py \
  tests/test_context_resolution_replay.py
git commit -m "feat: rebuild verified unfilled entry legs"
```

### Task 9: Reconcile partial fills, remaining risk headroom, and risk reduction

**Files:**
- Create: `src/telegram_kol_research/entry_revision_risk.py`
- Modify: `src/telegram_kol_research/entry_revision_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_sizing.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Test: `tests/test_entry_revision_risk.py`
- Test: `tests/test_entry_revision_executor.py`
- Test: `tests/test_strategy_management_sizing.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write failing pure-risk and integration tests**

Build a pure calculator using verified quantity, average entry, stop, contract
value, and side. Cover filled risk below, equal to, and above the target.

Require decisions:

```python
assert assess_revision_risk(...).action == "retain_and_use_headroom"
assert assess_revision_risk(...).remaining_risk_usdt == Decimal("4.2")

assert assess_revision_risk(over_target).action == "reduce_to_target"
```

Integration tests must prove pending legs are cancelled before any partial
close, no new leg is submitted before the reduced position and stop are read
back, a tighter existing stop is never weakened, and missing/unverified
protection blocks rebuilding.

Cover cancellation racing with a fill and recomputing the plan from the newly
verified exact position.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_entry_revision_risk.py \
  tests/test_entry_revision_executor.py \
  tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_executor.py -k 'entry_revision or risk_headroom'
```

Expected: FAIL because current revision logic retains filled legs without
calculating the new target budget or reducing excess exposure.

**Step 3: Implement pure risk assessment and exact reduction orchestration**

Calculate stop-loss risk from current exchange evidence and contract spec, not
from original requested quantities. Quantize all new sizes down. Reuse the
existing exact risk-reducing close path and protection reconciliation rather
than issuing a direct close from the entry revision module.

Persist the market snapshot used for the decision. If state changes before the
write boundary, refresh once and either create the exact revised component set
or enter recovery; do not silently enlarge risk.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS with continuous verified stop protection in all success cases.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_revision_risk.py \
  src/telegram_kol_research/entry_revision_executor.py \
  src/telegram_kol_research/strategy_management_sizing.py \
  src/telegram_kol_research/strategy_management_executor.py \
  tests/test_entry_revision_risk.py tests/test_entry_revision_executor.py \
  tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_executor.py
git commit -m "feat: enforce risk headroom during entry revision"
```

### Task 10: Expose truthful assembly and revision status

**Files:**
- Modify: `src/telegram_kol_research/reporting.py:15`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/strategy_records.py:1950`
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/strategy_alerts.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Test: `tests/test_reporting.py`
- Test: `tests/test_web_queries_messages.py`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_strategy_alerts.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write failing presentation tests**

Require bounded output for:

- `等待相邻仓位/补仓消息识别`;
- `配置20U × 50% = 实际风险预算10U`;
- `整单100%；两档各50%`;
- supplemental price and remaining headroom;
- revision stage, successful read-back, and operator-required recovery.

Assert no notification says orders were changed while a batch is only planned,
cancelling, or awaiting read-back. Assert payloads do not expose credentials,
raw API responses, or unbounded model reasons.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_reporting.py tests/test_web_queries_messages.py \
  tests/test_web_page_render.py tests/test_strategy_alerts.py \
  tests/test_system_operator_bot.py -k 'entry_assembly or entry_revision or supplemental'
```

Expected: FAIL because only the legacy preamble summary is projected.

**Step 3: Implement bounded truthful projections**

Add one shared formatter for immutable assembly evidence and one for revision
status. Render source message IDs, total sizing, leg allocation, supplemental
entries, and fixed reason code. Keep historical preamble rendering compatible.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/reporting.py \
  src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/strategy_alerts.py \
  src/telegram_kol_research/system_operator_bot.py \
  tests/test_reporting.py tests/test_web_queries_messages.py \
  tests/test_web_page_render.py tests/test_strategy_alerts.py \
  tests/test_system_operator_bot.py
git commit -m "feat: report adjacent entry assembly state"
```

### Task 11: Add invariant monitoring and historical replay

**Files:**
- Create: `scripts/replay_adjacent_entry_assembly.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:609`
- Test: `tests/test_replay_adjacent_entry_assembly.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1: Write failing replay and monitor tests**

Build sanitized fixture rows for:

- 飞扬 `4154/4155`;
- 陈哥 `9901/9902` and `9935/9936`;
- 米娅 `558/559` and `538/539`.

Require replay output to show expected risk and supplemental prices while making
zero writes. Add monitor reason codes for stale pending admission, consumed
fragment without assembly association, live assembly without binding evidence,
aggregate estimated risk above effective budget, revision replacement submitted
before old legs are terminal, and live revision without verified protection.

**Step 2: Run tests and verify failure**

```bash
uv run pytest -q \
  tests/test_replay_adjacent_entry_assembly.py \
  tests/test_production_safety_monitor.py -k 'adjacent_entry or entry_revision'
```

Expected: FAIL because the v2 replay and invariant readers do not exist.

**Step 3: Implement read-only replay and bounded invariants**

The replay script accepts a database path and optional chat/message filters,
opens SQLite read-only, and emits canonical JSON. It must never import an
exchange-write adapter.

Extend monitor expectations with both new rollout modes. Keep legacy preamble
invariants active until migration retirement is separately approved.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/replay_adjacent_entry_assembly.py \
  src/telegram_kol_research/production_safety_monitor.py \
  tests/test_replay_adjacent_entry_assembly.py \
  tests/test_production_safety_monitor.py
git commit -m "feat: monitor adjacent entry invariants"
```

### Task 12: Complete regression, runbook, review, and staged production verification

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/plans/2026-08-08-adjacent-entry-message-assembly-design.md`
- Test: all affected tests

**Step 1: Document exact rollout and rollback commands**

Document this order:

1. deploy with both v2 modes disabled;
2. run server-focused tests and read-only replay;
3. enable assembly shadow only;
4. inspect natural-message proposals and invariants;
5. enable assembly live while revision stays disabled;
6. enable revision shadow;
7. enable unfilled-only live revision after review;
8. enable partial-fill and supplemental actions only after natural shadow
   evidence proves exact ownership and protection;
9. rollback by disabling new admissions while continuing read-only
   reconciliation of existing batches.

Include checks proving no active time-sensitive strategy operation exists before
any restart or mode transition.

**Step 2: Run the focused suite**

```bash
uv run pytest -q \
  tests/test_message_evidence.py \
  tests/test_authoritative_recognition.py \
  tests/test_entry_strategy_fragments.py \
  tests/test_adjacent_entry_assembly.py \
  tests/test_entry_assembly_admission.py \
  tests/test_entry_strategy_assembly.py \
  tests/test_auto_trade_execution.py \
  tests/test_entry_revision_planner.py \
  tests/test_entry_revision_executor.py \
  tests/test_entry_revision_risk.py \
  tests/test_production_safety_monitor.py \
  tests/test_replay_adjacent_entry_assembly.py
```

Expected: PASS.

**Step 3: Run full local validation**

```bash
uv run pytest -q
uv run ruff check src tests scripts
git diff --check
```

Expected: PASS with no formatting errors. If the repository's full suite has a
known unrelated failure, record the exact test and confirm it predates this
branch; do not suppress it.

**Step 4: Request code review**

Use `@requesting-code-review` against the design and this plan. Resolve all
correctness, regression, exchange-write, ownership, protection, restart, and
missing-test findings before deployment.

**Step 5: Commit documentation**

```bash
git add docs/runbook.md docs/server-deployment.md \
  docs/plans/2026-08-08-adjacent-entry-message-assembly-design.md
git commit -m "docs: add adjacent entry rollout runbook"
```

**Step 6: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub accepts the reviewed commits.

**Step 7: Prove a safe deployment window and deploy disabled**

Run the project's documented read-only checks for in-flight recognition,
entries, revisions, cancellations, and management operations. Only after they
prove a safe window, run:

```bash
./scripts/server_git_update.sh
```

Expected: the server pulls the reviewed SHA, reinstalls the editable package,
restarts `telegram-kol.service`, and reports it active. Both new modes remain
`disabled`.

**Step 8: Run server verification without synthetic trades**

On the server, run the focused tests and the read-only replay for the approved
historical message pairs. Verify:

- deployed SHA matches the pushed commit;
- `telegram-kol.service` is active;
- listener freshness and authoritative recognition are healthy;
- current entry preamble and production paths remain unchanged while v2 is
  disabled;
- replay proposes 10U for 陈哥 half and 米娅 50-percent samples, 20U for 陈哥
  normal sizing, and a capped supplemental leg for 飞扬;
- no exchange write is produced by replay or shadow verification.

Do not enable a later rollout phase in the same implementation turn unless its
own reviewed evidence and safe deployment window are complete.

