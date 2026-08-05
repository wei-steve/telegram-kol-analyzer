# Entry Preamble Strategy Assembly Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve an explicit sizing instruction posted before a complete entry strategy and apply it as a multiplier on the configured symbol loss budget.

**Architecture:** Add a durable, non-executable entry-preamble record produced by authoritative recognition, then use a deterministic assembler to consume exactly one compatible preamble when the later complete entry instruction executes. Persist the assembly evidence and pass only the resulting effective USDT risk budget into the existing Deepcoin order builder; retain disabled, shadow, and allowlisted-live modes for rollout and rollback.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, existing MiMo authoritative recognition, existing message-instruction queue, existing Deepcoin execution pipeline.

---

### Task 1: Add durable preamble and assembly models

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Test: `tests/test_db_bootstrap.py`
- Create: `tests/test_entry_preambles.py`

**Step 1: Write failing schema tests**

Add tests that initialize a fresh database and assert the following schema:

```python
def test_init_db_creates_entry_preamble_and_assembly_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        assert session.query(EntryPreamble).count() == 0
        assert session.query(EntryStrategyAssembly).count() == 0
```

Also initialize from a representative older SQLite schema and assert that
`init_db` adds both tables without rewriting existing rows.

**Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_bootstrap.py \
  tests/test_entry_preambles.py -q
```

Expected: FAIL because `EntryPreamble` and `EntryStrategyAssembly` do not exist.

**Step 3: Add the SQLAlchemy models**

In `models.py`, add:

```python
class EntryPreamble(Base):
    __tablename__ = "entry_preambles"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_message_id = mapped_column(
        ForeignKey("raw_messages.id"), nullable=False, unique=True, index=True
    )
    chat_id = mapped_column(Integer, nullable=False, index=True)
    message_id = mapped_column(Integer, nullable=False)
    symbol = mapped_column(String(64), nullable=False)
    side = mapped_column(String(16), nullable=False)
    risk_multiplier = mapped_column(String(32), nullable=False)
    evidence_version_id = mapped_column(
        ForeignKey("message_evidence_versions.id"), nullable=False
    )
    recognition_generation = mapped_column(String(64), nullable=False)
    fingerprint = mapped_column(String(64), nullable=False, unique=True)
    status = mapped_column(String(32), nullable=False, default="pending", index=True)
    reason = mapped_column(Text, nullable=False)
    consumed_at = mapped_column(DateTime)
    invalidated_at = mapped_column(DateTime)
    created_at = mapped_column(DateTime, nullable=False, default=utc_now)
    updated_at = mapped_column(DateTime, nullable=False, default=utc_now)


class EntryStrategyAssembly(Base):
    __tablename__ = "entry_strategy_assemblies"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_preamble_id = mapped_column(
        ForeignKey("entry_preambles.id"), nullable=False, unique=True
    )
    strategy_raw_message_id = mapped_column(
        ForeignKey("raw_messages.id"), nullable=False, index=True
    )
    signal_candidate_id = mapped_column(
        ForeignKey("signal_candidates.id"), nullable=False, unique=True
    )
    strategy_instance_id = mapped_column(String(255), nullable=False, unique=True)
    risk_multiplier = mapped_column(String(32), nullable=False)
    evidence_json = mapped_column(Text, nullable=False)
    fingerprint = mapped_column(String(64), nullable=False, unique=True)
    created_at = mapped_column(DateTime, nullable=False, default=utc_now)
```

Add indexes for `(chat_id, status, created_at)` and a database check that
`status` is one of `pending`, `consumed`, `expired`, or `invalidated`.

**Step 4: Add the additive SQLite bootstrap migration**

Follow the existing additive migration style in `db.py`. Create missing tables
and indexes only; do not rebuild existing tables. Keep the migration idempotent.

**Step 5: Run the tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  tests/test_db_bootstrap.py tests/test_entry_preambles.py
git commit -m "feat: add durable entry preamble records"
```

### Task 2: Add rollout settings

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing settings tests**

Cover these defaults and round trips:

```python
assert settings.entry_preamble_mode == "disabled"
assert settings.entry_preamble_live_chat_ids == []
```

Accept only `disabled`, `shadow`, or `live`. Normalize chat IDs to unique
integers. Reject or fall back safely for malformed values.

**Step 2: Run the focused tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  tests/test_web_page_render.py -q
```

Expected: FAIL because the settings and controls are missing.

**Step 3: Implement settings and Web controls**

Add to `TradingSettings`:

```python
entry_preamble_mode: str = "disabled"
entry_preamble_live_chat_ids: list[int] = field(default_factory=list)
```

Persist them through the existing settings JSON. Add a select control for the
mode and a comma-separated allowlist control. Do not automatically enable live
mode during migration or deployment.

**Step 4: Run the focused tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  tests/test_trading_settings.py tests/test_web_app.py tests/test_web_page_render.py
git commit -m "feat: add entry preamble rollout settings"
```

### Task 3: Extend authoritative recognition with a non-executable sizing fragment

**Files:**
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Modify: `src/telegram_kol_research/message_evidence.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_message_evidence.py`
- Test: `tests/test_prompt_registry.py`

**Step 1: Write failing contract tests**

Add fixtures for:

```text
BTC换手入场做空，半仓操作做个短线空单。
ETH做多，30%仓位。
BTC轻仓做空。
```

Assert that the first two normalize to an `entry_context` object with decimal
multipliers `0.5` and `0.3`. Assert that vague `轻仓`, `满仓`, leverage, and
`加仓` do not produce an executable preamble.

Validate all of these constraints:

```python
Decimal("0") < risk_multiplier <= Decimal("1")
symbol is non-empty and uppercase
side in {"long", "short"}
kind == "entry_preamble"
```

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_authoritative_recognition.py \
  tests/test_message_evidence.py \
  tests/test_prompt_registry.py -q
```

Expected: FAIL because the authoritative payload has no `entry_context`.

**Step 3: Update the prompt schema**

Add an optional top-level object:

```json
"entry_context": {
  "kind": "entry_preamble",
  "symbol": "BTC",
  "side": "short",
  "risk_multiplier": "0.5",
  "confidence": 0.95,
  "reason": "..."
}
```

State explicitly that this object is non-executable and may accompany
`recognition_result = 非策略`. Preserve the existing complete-entry and lifecycle
rules.

**Step 4: Normalize and validate the fragment**

Add a small immutable value object, for example:

```python
@dataclass(frozen=True, slots=True)
class EntryPreambleEvidence:
    symbol: str
    side: str
    risk_multiplier: Decimal
    confidence: float
    reason: str
```

Malformed fragments are omitted with a fixed audit reason; they must not fail
otherwise valid message recognition.

**Step 5: Run tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/prompt_defaults.py \
  src/telegram_kol_research/message_evidence.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_authoritative_recognition.py tests/test_message_evidence.py \
  tests/test_prompt_registry.py
git commit -m "feat: recognize entry sizing preambles"
```

### Task 4: Persist and invalidate preambles

**Files:**
- Create: `src/telegram_kol_research/entry_preambles.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/raw_ingest.py`
- Modify: `src/telegram_kol_research/source_message_deletion.py`
- Test: `tests/test_entry_preambles.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_source_message_deletion.py`

**Step 1: Write failing persistence tests**

Cover:

- idempotent persistence by raw message and evidence fingerprint;
- no row for malformed or unsupported sizing;
- an edited message invalidates the old pending row before saving replacement
  evidence;
- deletion invalidates only pending rows;
- a consumed row is never rewritten by edit/delete handling;
- disabled mode retains recognition evidence but writes no preamble;
- shadow and live modes persist the same preamble evidence.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_preambles.py \
  tests/test_authoritative_recognition.py \
  tests/test_source_message_deletion.py -q
```

Expected: FAIL because persistence and invalidation do not exist.

**Step 3: Implement the persistence API**

In `entry_preambles.py`, add functions with explicit transaction ownership:

```python
def persist_entry_preamble_in_session(
    session,
    *,
    raw_message,
    evidence_version_id: int,
    recognition_generation: str,
    evidence: EntryPreambleEvidence,
    now: datetime,
) -> EntryPreamble: ...


def invalidate_pending_entry_preamble_in_session(
    session,
    *,
    raw_message_id: int,
    now: datetime,
) -> int: ...
```

Use a canonical JSON fingerprint. Do not infer a preamble from raw text in this
module; accept only validated authoritative evidence.

**Step 4: Wire authoritative finalization and source mutations**

Persist the preamble only after current evidence finalization succeeds. On edit
or deletion, invalidate the pending row in the same transaction as the source
state change.

**Step 5: Run tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/entry_preambles.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/raw_ingest.py \
  src/telegram_kol_research/source_message_deletion.py \
  tests/test_entry_preambles.py tests/test_authoritative_recognition.py \
  tests/test_source_message_deletion.py
git commit -m "feat: persist authoritative entry preambles"
```

### Task 5: Build deterministic strategy assembly

**Files:**
- Create: `src/telegram_kol_research/entry_strategy_assembly.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Test: `tests/test_entry_strategy_assembly.py`
- Test: `tests/test_message_instruction_items.py`

**Step 1: Write failing pure matching tests**

Create table-driven tests for:

- matching same chat, symbol, and side;
- no match for symbol or side mismatch;
- latest relevant entry-intent wins only when it leaves exactly one candidate;
- unrelated advertisements between messages do not break adjacency;
- a complete entry, cancellation, opposite-side entry, or explicit replacement
  between messages is a hard boundary;
- two eligible preambles produce `entry_preamble_ambiguous`;
- no eligible preamble leaves the risk multiplier at `1`;
- a preceding message with nonterminal recognition produces
  `preceding_entry_context_unresolved`.

Use source ordering `(posted_at, message_id, raw_message_id)`, never worker
completion time.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_strategy_assembly.py \
  tests/test_message_instruction_items.py -q
```

Expected: FAIL because the assembler does not exist.

**Step 3: Implement the pure selector**

Define a closed result:

```python
@dataclass(frozen=True, slots=True)
class EntryAssemblyDecision:
    status: str  # none | ready | unresolved | blocked
    reason_code: str | None
    preamble_id: int | None
    risk_multiplier: Decimal
```

Keep candidate selection pure and pass in normalized prior-message facts.

**Step 4: Implement atomic consumption**

Add:

```python
def assemble_entry_strategy(
    session_factory,
    *,
    strategy_raw_message_id: int,
    signal_candidate_id: int,
    strategy_instance_id: str,
    mode: str,
    live_chat_ids: set[int],
    assembled_at: datetime,
) -> EntryAssemblyResult: ...
```

In one transaction:

1. Re-read the selected pending preamble.
2. Insert the unique `EntryStrategyAssembly`.
3. Conditionally update the preamble from `pending` to `consumed`.
4. Roll back if the conditional update affects anything other than one row.

In shadow mode, return the proposed match without consuming it. In live mode,
consume only for allowlisted chats. Disabled mode returns multiplier `1`.

**Step 5: Generalize instruction deferral**

Extend `execute_message_instruction_items` so
`preceding_entry_context_unresolved` uses the existing visibility retry fields
and deadline machinery instead of becoming terminally skipped.

**Step 6: Run tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/entry_strategy_assembly.py \
  src/telegram_kol_research/message_instruction_items.py \
  tests/test_entry_strategy_assembly.py tests/test_message_instruction_items.py
git commit -m "feat: assemble entry strategies from preambles"
```

### Task 6: Apply the risk multiplier before Deepcoin sizing

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_scan.py`
- Modify: `src/telegram_kol_research/recovery_execution_queue.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_recovery_scan.py`
- Test: `tests/test_deepcoin_order_builder.py`

**Step 1: Write failing execution tests**

Reproduce messages 9901/9902 with BTC configured at 20 USDT and assert:

```python
assert signal.max_loss_usdt == 10.0
assert draft["risk_budget_usdt"] == 10.0
assert sum(leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]) <= 10.0
```

For the historical range-entry geometry, assert both contract quantities are
computed from the 10 USDT budget rather than taking the already-rounded 20 USDT
quantities and dividing them by two.

Also cover:

- no assembly -> existing configured risk unchanged;
- shadow assembly -> report 0.5 but execute configured risk unchanged;
- live allowlisted assembly -> execute effective risk;
- malformed, zero, negative, or greater-than-one multiplier -> block before
  trade-signal persistence;
- repeated execution of the same instruction reuses the same assembly and does
  not consume another preamble.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_scan.py \
  tests/test_deepcoin_order_builder.py -q
```

Expected: FAIL because `_resolve_signal_max_loss_usdt` is used without an
assembly multiplier.

**Step 3: Compute the effective risk at the execution boundary**

In `_auto_process_single_message_trade_signal`, after exact candidate loading
and before constructing `RecoverySignal`, load or create the assembly and use:

```python
configured_risk = _resolve_signal_max_loss_usdt(runtime_config, symbol=symbol)
effective_risk = configured_risk * float(assembly.risk_multiplier)
```

Canonicalize with `Decimal` before conversion to float. Pass `effective_risk`
as `RecoverySignal.max_loss_usdt`. Do not modify `deepcoin_order_builder.py` to
interpret a multiplier; it must continue receiving a final USDT budget.

**Step 4: Persist audit evidence**

Add this bounded evidence to the execution result and binding payload:

```json
{
  "configured_risk_budget_usdt": 20.0,
  "risk_multiplier": "0.5",
  "effective_risk_budget_usdt": 10.0,
  "preamble_message_id": 9901,
  "strategy_message_id": 9902,
  "assembly_fingerprint": "..."
}
```

Do not store prompt bodies or unrelated recent messages.

**Step 5: Run tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_scan.py \
  src/telegram_kol_research/recovery_execution_queue.py \
  tests/test_auto_trade_execution.py tests/test_recovery_scan.py \
  tests/test_deepcoin_order_builder.py
git commit -m "feat: apply preamble risk multiplier to entries"
```

### Task 7: Expose assembly evidence and operational diagnostics

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Modify: `src/telegram_kol_research/reporting.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Test: `tests/test_strategy_records.py`
- Test: `tests/test_web_strategy_records.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1: Write failing visibility tests**

Assert that the strategy detail and execution notification show:

```text
基础风险预算 20 USDT × 仓位倍率 50% = 实际风险预算 10 USDT
前置消息 9901 / 策略消息 9902
```

Add monitor coverage for stale unresolved preambles, ambiguous assemblies, and
live executions whose binding payload lacks the expected assembly evidence.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_production_safety_monitor.py -q
```

Expected: FAIL because assembly evidence is not rendered or monitored.

**Step 3: Implement bounded reporting**

Expose IDs, mode, multiplier, and budgets only. Keep all raw prompt text and
credentials out of notifications and monitor fingerprints.

**Step 4: Run tests and verify success**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/templates/_strategy_detail.html \
  src/telegram_kol_research/reporting.py \
  src/telegram_kol_research/production_safety_monitor.py \
  tests/test_strategy_records.py tests/test_web_strategy_records.py \
  tests/test_production_safety_monitor.py
git commit -m "feat: report entry preamble assemblies"
```

### Task 8: Add historical shadow replay and rollout verification

**Files:**
- Create: `scripts/replay_entry_preamble_shadow.py`
- Modify: `docs/runbook.md`
- Create: `docs/entry-preamble-live-verification.md`
- Test: `tests/test_entry_preamble_shadow_replay.py`

**Step 1: Write failing replay tests**

The replay must:

- open the source database read-only;
- accept exact raw-message IDs;
- never build a Deepcoin client or call an exchange write;
- output only message IDs, normalized symbol/side, multiplier, configured
  budget, proposed effective budget, decision, and fixed reason codes;
- reproduce 9901/9902 as multiplier 0.5 and proposed BTC risk 10 USDT.

**Step 2: Run tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_entry_preamble_shadow_replay.py -q
```

Expected: FAIL because the replay tool does not exist.

**Step 3: Implement the read-only replay**

Reuse the pure selector and risk calculation. Refuse mutable SQLite connections,
missing evidence, ambiguous matches, or incomplete output.

**Step 4: Document rollout and rollback**

Document this sequence:

1. Deploy with `entry_preamble_mode=disabled`.
2. Run focused server tests.
3. Set `shadow` and replay curated historical pairs including 9901/9902.
4. Observe natural shadow traffic and audit ambiguous/blocked results.
5. Add one reviewed chat to `entry_preamble_live_chat_ids`.
6. Verify the next natural complete strategy records both budgets before any
   exchange write.
7. Expand the allowlist only after clean evidence.
8. Roll back immediately by setting mode to `disabled`; the original configured
   risk path remains unchanged.

Do not create a test Telegram strategy or a test live position.

**Step 5: Run focused and full local tests**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_preambles.py \
  tests/test_entry_strategy_assembly.py \
  tests/test_entry_preamble_shadow_replay.py \
  tests/test_authoritative_recognition.py \
  tests/test_auto_trade_execution.py -q

.venv/bin/python -m pytest -q
```

Expected: all tests PASS.

**Step 6: Commit**

```bash
git add scripts/replay_entry_preamble_shadow.py docs/runbook.md \
  docs/entry-preamble-live-verification.md \
  tests/test_entry_preamble_shadow_replay.py
git commit -m "docs: add entry preamble rollout runbook"
```

### Task 9: Review, push, deploy dormant, and verify on the server

**Files:**
- Review all files changed by Tasks 1-8
- Update: `docs/entry-preamble-live-verification.md`

**Step 1: Run a focused review**

Use `@requesting-code-review` and check specifically for:

- accidental exchange writes in disabled or shadow mode;
- a preamble being consumable twice;
- matching by worker completion instead of Telegram source order;
- a multiplier being applied to rounded contract quantity instead of USDT risk;
- ambiguous context falling back to configured risk rather than blocking;
- edit/delete paths leaving stale pending preambles;
- missing rollback controls or unbounded output.

**Step 2: Run final local checks**

```bash
.venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: tests PASS, `git diff --check` is clean, and only intentional files
are changed.

**Step 3: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Deploy only the dormant implementation**

Prove no time-sensitive management or position mutation is in flight. Then run:

```bash
./scripts/server_git_update.sh
```

The deployed setting must remain `entry_preamble_mode=disabled`. Do not enable
shadow or live in the deployment command.

**Step 5: Run server verification**

On `/opt/telegram-kol-analyzer`:

```bash
systemctl is-active telegram-kol.service
git rev-parse HEAD
.venv/bin/python -m pytest \
  tests/test_entry_preambles.py \
  tests/test_entry_strategy_assembly.py \
  tests/test_entry_preamble_shadow_replay.py \
  tests/test_auto_trade_execution.py -q
.venv/bin/python scripts/replay_entry_preamble_shadow.py \
  --database-path data/research.db \
  --preamble-raw-message-id 9334 \
  --strategy-raw-message-id 9335 \
  --configured-risk-usdt 20
```

Expected replay result: BTC short, multiplier `0.5`, proposed effective risk
`10`, no exchange client construction, and no database or exchange mutation.

**Step 6: Record evidence and commit**

Update `docs/entry-preamble-live-verification.md` with the deployed SHA, focused
test count, replay fingerprint, service state, and confirmation that the mode
remained disabled. Do not include credentials or raw exchange payloads.

```bash
git add docs/entry-preamble-live-verification.md
git commit -m "docs: record dormant entry preamble verification"
git push origin codex/deepcoin-auto-trading-v1
```

