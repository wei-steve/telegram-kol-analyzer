# Adjacent Entry Admission Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make adjacent-entry admission distinguish an inert MiMo placeholder from unresolved actionable evidence, surface a deferred entry truthfully, and provide a safe operator path for already-stuck historical rows without ever creating a late order automatically.

**Architecture:** Keep the existing authoritative recognition, contextual target resolution, and execution path. Treat a completed source message as actionable only when its normalized evidence has material content. Persist an adjacent-context defer on the exact entry instruction item, release only that exact item when its final blocker completes, and use execution-binding evidence—not a market-price lifecycle transition—as the UI's proof of an actual order or position.

**Tech Stack:** Python 3.12+, SQLAlchemy, existing Telegram/Deepcoin integration, Jinja templates, pytest via `uv run pytest`.

---

## Safety invariants

- A historical message must never be retried into a new exchange order merely because code was upgraded, a service restarted, or a repair command ran.
- The existing first-pass MiMo recognition and contextual multi-instruction target resolution remain authoritative; this work only classifies their durable output and resumes the exact already-created instruction item.
- `StrategyLifecycle.entered` alone is not proof of a Deepcoin order. A real execution requires the exact `execution_binding_id` and confirmed exchange-side evidence.
- Unknown exchange writes remain non-retryable. Every repair starts read-only/dry-run and requires an explicit operator confirmation before any local-state mutation.
- The immediate code fix does not backfill or replay historic messages. It intentionally leaves 陈哥的历史消息 `#9974` unchanged and creates no order for it.

## Applied safeguard in this change

- `src/telegram_kol_research/entry_assembly_admission.py` now treats an all-null fixed `strategy` object as a non-actionable placeholder rather than an unresolved action.
- `src/telegram_kol_research/message_instruction_items.py` keeps `adjacent_entry_context_pending` items pending instead of incorrectly finishing them as succeeded.
- A final adjacent-context wakeup releases the matching pending entry item immediately, while leaving unrelated/replaced/terminal items untouched.
- Regression coverage lives in `tests/test_entry_assembly_admission.py` and `tests/test_auto_trade_execution.py`.

## Task 1: Make the authoritative evidence contract explicit

**Files:**

- Modify: `src/telegram_kol_research/message_evidence.py`
- Modify: `src/telegram_kol_research/entry_assembly_admission.py`
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Test: `tests/test_message_evidence.py`
- Test: `tests/test_entry_assembly_admission.py`

**Step 1: Write failing contract tests.**

Cover all of these completed-evidence shapes:

- non-strategy plus a fixed all-null `strategy` schema → unrelated;
- a blank-string-only strategy schema → unrelated;
- a material strategy field, a non-`none` lifecycle event, or an entry fragment → waits for durable candidate/fragment projection;
- malformed normalized JSON → stays fail-closed as unresolved.

**Step 2: Implement one shared material-action classifier.**

Move the narrow placeholder test into a named, unit-tested helper near normalized-evidence handling. It must inspect only structured fields, not infer intent from raw text. Update the prompt contract to prefer `strategy: null` for non-strategy messages while retaining compatibility with old fixed-schema payloads.

**Step 3: Verify.**

```bash
uv run pytest -q tests/test_message_evidence.py tests/test_entry_assembly_admission.py
```

## Task 2: Expose a truthful entry execution state

**Files:**

- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_strategy_mid_panel.html`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`
- Test: `tests/test_web_queries.py`
- Test: `tests/test_web_app.py`

**Step 1: Add failing presentation tests.**

Create a lifecycle that is `entered` solely from price monitoring and has no execution binding. Assert that it is shown as “价格触发，未提交交易所订单”, not “持仓中”. Add a bound lifecycle with a confirmed Deepcoin binding and assert that it remains “持仓中”.

**Step 2: Build a binding-aware display projection.**

Have `web_queries.py` derive one display state from lifecycle status, instruction-item status/reason, binding presence, binding status, and exact exchange evidence. Include the fixed defer reason `adjacent_entry_context_pending` as “等待相邻消息确认”. Do not alter lifecycle persistence or allow UI data to drive execution.

**Step 3: Verify.**

```bash
uv run pytest -q tests/test_web_queries.py tests/test_web_app.py
```

## Task 3: Add bounded operator diagnostics for stuck admission

**Files:**

- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Test: `tests/test_production_safety_monitor.py`
- Test: `tests/test_runtime_incident_scanner.py`

**Step 1: Write failing monitor tests.**

Require a high-signal incident only when all conditions are true: a pending adjacent-entry attempt exceeds its bounded wait, the matching entry item is still pending, and no execution binding exists. Prove that a completed unrelated all-null placeholder, a terminal safety refusal, and a bound entry do not raise this incident.

**Step 2: Implement read-only evidence collection.**

Return the attempt ID, raw-message IDs, candidate ID, item status/reason, binding ID/status, and age. The monitor may alert or render diagnostics, but it must not change an attempt, replay a message, call Deepcoin, or enqueue an order.

**Step 3: Verify.**

```bash
uv run pytest -q tests/test_production_safety_monitor.py tests/test_runtime_incident_scanner.py
```

## Task 4: Provide an explicit, no-order historical repair workflow

**Files:**

- Modify: `src/telegram_kol_research/historical_state_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_historical_state_repair.py`
- Modify: `docs/runbook.md`

**Step 1: Write dry-run tests.**

The planner must select only rows that have all of the following: a completed non-strategy fixed-null placeholder cited as the blocker, no trade signal, no execution binding, no exchange submission/unknown outcome, and a historical timestamp older than an explicit safety cutoff. Assert it excludes every currently active, claimed, submitted, bound, or ambiguous row.

**Step 2: Implement a report-first command.**

Add a command that emits a deterministic JSON plan containing the evidence and a local-only recommendation, defaulting to `dry_run`. The apply form may only annotate/terminalize an incident or remove a false wait from local display after a fresh precondition check; it must not call `auto_process_message_trade_signal`, create a `TradeSignal`, or submit to Deepcoin.

**Step 3: Document operator confirmation.**

Require a named row ID/fingerprint, a fresh read-only Deepcoin check, an explicit `--apply`, and an audit record. Document a separate manual review path if an operator wants to consider a new order; that decision must use the current market and a new strategy instruction, never the old message.

**Step 4: Verify.**

```bash
uv run pytest -q tests/test_historical_state_repair.py
uv run telegram-kol-research repair-historical-state-convergence --help
```

## Task 5: Roll out and prove the fix without affecting live trading

**Files:**

- Modify: `docs/runbook.md`
- Modify: `docs/runtime-incident-agent-status.md` only if this is executed as an approved runtime-incident phase

**Step 1: Preflight (read-only).**

On the server, verify the deployed revision, service health, zero in-flight time-sensitive entry operation, the current pending/claimed `entry_assembly_attempts`, matching `message_instruction_items`, and no unknown Deepcoin submission state. Do not restart during an active strategy window.

**Step 2: Deploy the immediate patch only in a safe window.**

Pull the reviewed commit, reinstall the editable package, and restart `telegram-kol.service`. Confirm the service is active, then run the focused local-state checks and monitor in no-notify/read-only mode. Do not replay old raw messages.

**Step 3: Validate future-only behavior.**

Use an approved synthetic or naturally arriving non-trading message sequence to verify: a null placeholder does not block; a real pending adjacent message keeps the entry item pending; its terminal evidence wakes the exact item; and no order status is claimed without an execution binding.

**Step 4: Roll back if an invariant fails.**

Disable the entry-assembly live mode or restore the prior reviewed commit, restart only in a safe window, and preserve all evidence rows for diagnosis. Never compensate with a historical order retry.

## Completion criteria

- Fixed-null non-strategy evidence cannot stall a future entry.
- A real adjacent defer remains visible as pending and resumes only through the exact terminal wakeup.
- The operator UI distinguishes price/lifecycle state from verified exchange execution.
- Stuck historical rows are diagnosable and repairable only through a dry-run-first, no-order workflow.
- Focused and full regression suites pass before rollout; server verification occurs only in a proven safe window.
