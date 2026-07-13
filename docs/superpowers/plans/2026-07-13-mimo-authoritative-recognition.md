# MiMo Authoritative Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MiMo authoritative for all text and image strategy recognition, use DeepSeek only as non-blocking text validation, notify on disagreement without delaying execution, and keep live-bound lifecycle state active until Deepcoin reconciliation confirms closure.

**Architecture:** Add a pure two-model assessment layer that calls MiMo for the authoritative unified entry/lifecycle result and optionally calls DeepSeek for text-only comparison. Persist the assessment separately from provider experiments, apply only the MiMo result to candidates/lifecycles, execute before dispatching disagreement notifications, and route both live delivery and recovery reconciliation through the same coordinator. Preserve the existing exact-`pos_id`, allowlist, confidence, binding, and duplicate-close gates.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, SQLite, httpx, Telethon, pytest.

## Global Constraints

- MiMo is authoritative for text-only, image-only, and combined text/image messages.
- DeepSeek is auxiliary validation only for text-only messages and never overrides MiMo.
- MiMo/DeepSeek disagreement sends a notification but never creates a human-review execution gate.
- Notification latency or failure must not block a valid MiMo-authorized action.
- MiMo failure may show DeepSeek analysis for text, but DeepSeek alone cannot authorize a live mutation.
- MiMo failure for an image message cannot fall back to DeepSeek.
- A live-bound lifecycle remains active until exchange reconciliation confirms the bound position/order is gone.
- Local development and review come first; push only reviewed commits to `codex/deepcoin-auto-trading-v1`; production pulls from GitHub, reinstalls editable code, and restarts `telegram-kol.service`.
- Never use an unapproved live order as a verification shortcut.

---

### Task 1: Persist authoritative recognition decisions

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `tests/test_db_bootstrap.py`
- Create: `tests/test_recognition_decisions.py`

**Interfaces:**
- Produces: `RecognitionDecision` SQLAlchemy model, one row per `raw_message_id`.
- Produces: `save_recognition_decision(session_factory, record: RecognitionDecisionRecord) -> RecognitionDecision`.
- Produces: `update_recognition_execution_outcome(session_factory, *, raw_message_id: int, automation_status: str, automation_reason: str | None, notification_status: str | None = None, notification_error: str | None = None) -> None`.

- [ ] **Step 1: Write the failing database bootstrap test**

Add an assertion that a new database contains `recognition_decisions` with these fields:

```python
assert {
    "raw_message_id",
    "input_kind",
    "authoritative_model",
    "authoritative_status",
    "authoritative_payload_json",
    "auxiliary_model",
    "auxiliary_status",
    "auxiliary_payload_json",
    "agreement_status",
    "differences_json",
    "automation_status",
    "automation_reason",
    "notification_status",
    "notification_error",
    "created_at",
    "updated_at",
}.issubset(columns)
```

- [ ] **Step 2: Run the bootstrap test and verify RED**

Run: `./.venv/bin/python -m pytest tests/test_db_bootstrap.py::test_database_bootstrap_creates_recognition_decisions_table -q`

Expected: FAIL because `recognition_decisions` does not exist.

- [ ] **Step 3: Add the model and persistence helper**

Implement a unique `raw_message_id` model and a frozen input record:

```python
@dataclass(frozen=True)
class RecognitionDecisionRecord:
    raw_message_id: int
    input_kind: str
    authoritative_model: str
    authoritative_status: str
    authoritative_payload: dict[str, Any]
    auxiliary_model: str | None
    auxiliary_status: str | None
    auxiliary_payload: dict[str, Any] | None
    agreement_status: str
    differences: list[str]
```

Serialize JSON with `ensure_ascii=False, sort_keys=True`. Upsert by `raw_message_id` so retries update the audit row rather than duplicate it. `Base.metadata.create_all()` creates the table on production startup; no destructive migration is required.

- [ ] **Step 4: Verify persistence GREEN**

Run: `./.venv/bin/python -m pytest tests/test_db_bootstrap.py tests/test_recognition_decisions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the audit slice**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py src/telegram_kol_research/recognition_decisions.py tests/test_db_bootstrap.py tests/test_recognition_decisions.py
git commit -m "feat: audit authoritative recognition decisions"
```

### Task 2: Build the unified MiMo prompt and parse its result

**Files:**
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `tests/test_ai_recognition_config.py`
- Modify: `tests/test_recognition_experiments.py`

**Interfaces:**
- Produces: `build_authoritative_mimo_prompt(config: AiRecognitionConfig) -> str`.
- Produces: `MimoAuthoritativeResult` with `payload`, `input_kind`, `model`, `status`, `error_message`, and `is_actionable` properties.
- Produces: `run_mimo_authoritative_for_message(...) -> MimoAuthoritativeResult`.

- [ ] **Step 1: Write failing prompt-composition tests**

Assert that a custom DeepSeek entry rule and lifecycle rule automatically appear in the effective MiMo prompt together with image-only grounding rules:

```python
prompt = build_authoritative_mimo_prompt(
    AiRecognitionConfig(
        recognition_prompt="CUSTOM ENTRY EXPERIENCE",
        lifecycle_event_prompt="CUSTOM EXIT EXPERIENCE",
        mimo_direct_prompt="CUSTOM IMAGE EXPERIENCE",
    )
)
assert "CUSTOM ENTRY EXPERIENCE" in prompt
assert "CUSTOM EXIT EXPERIENCE" in prompt
assert "CUSTOM IMAGE EXPERIENCE" in prompt
assert "input_reading" in prompt
assert "lifecycle_event" in prompt
assert "不要补全图片" in prompt
```

Also assert that the output schema permits `recognition_result = 非策略` and `lifecycle_event.event_type = exit_position` in the same payload.

- [ ] **Step 2: Run prompt tests and verify RED**

Run: `./.venv/bin/python -m pytest tests/test_ai_recognition_config.py tests/test_recognition_experiments.py -q`

Expected: FAIL because the authoritative prompt/result interfaces do not exist and the current prompt does not embed the complete lifecycle schema.

- [ ] **Step 3: Implement runtime prompt composition**

Compose, in order:

```python
sections = [
    config.recognition_prompt,
    NORMALIZED_STRATEGY_OUTPUT_INSTRUCTIONS,
    config.lifecycle_event_prompt,
    PRICE_SHORTHAND_NORMALIZATION_INSTRUCTION,
    config.mimo_direct_prompt,
    MIMO_AUTHORITATIVE_OUTPUT_INSTRUCTIONS,
]
return "\n\n".join(section.strip() for section in sections if section.strip())
```

`MIMO_AUTHORITATIVE_OUTPUT_INSTRUCTIONS` must require one JSON object containing `recognition_result`, `strategy`, `lifecycle_event`, `input_reading`, `confidence`, and `reason`. It must explicitly say that new-entry classification and lifecycle classification are independent dimensions.

- [ ] **Step 4: Implement authoritative MiMo result parsing**

Before the provider call, load the same recent same-group messages and safely serialized active lifecycle rows used by lifecycle recognition, then append them to the user content under distinct `Recent context` and `Active strategies` headings. Include lifecycle IDs, symbol, side, status, entry/stop/take-profit fields, and source message IDs; exclude execution credentials and provider secrets.

Keep the raw provider payload in `recognition_experiments`, but return a typed result instead of requiring callers to reverse-parse the ORM row. Invalid JSON, missing schema sections, unreadable image, transport failure, and timeout produce `status="识别失败"` with `error_message` and never raise past the coordinator.

- [ ] **Step 5: Verify prompt and parser GREEN**

Run: `./.venv/bin/python -m pytest tests/test_ai_recognition_config.py tests/test_recognition_experiments.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the MiMo prompt slice**

```bash
git add src/telegram_kol_research/ai_recognition_config.py src/telegram_kol_research/recognition_experiments.py tests/test_ai_recognition_config.py tests/test_recognition_experiments.py
git commit -m "feat: unify MiMo strategy and lifecycle prompt"
```

### Task 3: Add pure DeepSeek auxiliary assessment and MiMo-only application

**Files:**
- Create: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Create: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_message_recognition.py`

**Interfaces:**
- Produces: `assess_message_authoritatively(session_factory, *, raw_message_id: int, ai_recognition_config: AiRecognitionConfig, media_root: str | Path) -> AuthoritativeAssessment`.
- Produces: `apply_authoritative_assessment(session_factory, assessment: AuthoritativeAssessment) -> MessageRecognitionResult`.
- Produces: `compare_assessments(mimo_payload: dict[str, Any], deepseek_payload: dict[str, Any] | None) -> tuple[str, list[str]]`.

- [ ] **Step 1: Write the Fengge regression test and verify RED**

Create an entered BTC-short lifecycle with an active execution binding, feed `现价62800附近出局，空仓等待。`, and return:

```python
mimo_payload = {
    "recognition_result": "非策略",
    "strategy": {},
    "lifecycle_event": {
        "event_type": "exit_position",
        "target_lifecycle_id": lifecycle_id,
        "symbol": "BTC",
        "side": "short",
        "exit_price": 62800,
        "confidence": 0.95,
        "reason": "现价出局",
    },
    "input_reading": {"observed_text": "现价62800附近出局，空仓等待。", "image_quality": "none"},
    "confidence": 0.95,
}
deepseek_payload = {"recognition_result": "非策略", "lifecycle_event": {"event_type": "none"}}
```

Assert disagreement is recorded, MiMo creates/updates the `close_signal` candidate, and DeepSeek does not mutate lifecycle state.

Run: `./.venv/bin/python -m pytest tests/test_authoritative_recognition.py::test_fengge_exit_uses_mimo_when_deepseek_disagrees -q`

Expected: FAIL because the coordinator does not exist.

- [ ] **Step 2: Extract DeepSeek inference from state mutation**

Add a pure helper that returns both entry and lifecycle payloads without calling `_apply_lifecycle_event_decision`, `_persist_ai_result`, or committing. Use it only for text-only auxiliary validation. Preserve existing `recognize_message_now` compatibility by making that legacy wrapper explicitly call inference and then application.

- [ ] **Step 3: Compare normalized meanings, not display labels**

Return `agreement_status="disagreed"` when models differ on entry classification, symbol, side, entry mode, risk prices, lifecycle event, target, or full/partial exit. Do not report an internal MiMo conflict merely because its new-entry status is `非策略` while its lifecycle event is `exit_position`.

- [ ] **Step 4: Apply only MiMo**

Map authoritative payloads as follows:

```python
if lifecycle_event["event_type"] != "none":
    persist_lifecycle_candidate(..., parse_source="mimo_authoritative")
    apply_lifecycle_event_decision(..., authoritative=True)
elif recognition_result == "是策略":
    persist_entry_candidate(..., parse_source="mimo_authoritative")
else:
    persist_non_strategy_recognition(..., engine=mimo_model)
```

Persist the MiMo model in `message_recognitions.engine` and the complete assessment through Task 1's helper.

- [ ] **Step 5: Add failure-degradation tests**

Cover text-only MiMo failure with visible DeepSeek analysis but no actionable candidate, and image MiMo failure without calling DeepSeek. Assert `agreement_status` is `authoritative_failed` and no live-authorizing candidate is produced.

- [ ] **Step 6: Verify assessment/application GREEN**

Run: `./.venv/bin/python -m pytest tests/test_authoritative_recognition.py tests/test_message_recognition.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the authority slice**

```bash
git add src/telegram_kol_research/authoritative_recognition.py src/telegram_kol_research/message_recognition.py tests/test_authoritative_recognition.py tests/test_message_recognition.py
git commit -m "feat: make MiMo recognition authoritative"
```

### Task 4: Execute before sending non-blocking disagreement notifications

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_system_operator_bot.py`

**Interfaces:**
- Produces: `process_authoritative_message(...) -> AuthoritativeProcessingResult` containing assessment, recognition, and automation outcome.
- Produces: `dispatch_recognition_anomaly_notification(...) -> asyncio.Task[None] | None`.

- [ ] **Step 1: Replace the old blocking-conflict expectation with a failing execution-first test**

For text-only disagreement, record callback order and assert:

```python
assert calls == ["apply_mimo", "auto_trade", "schedule_notification"]
assert result.automation["status"] == "submitted"
assert result.assessment.agreement_status == "disagreed"
```

Add a notification sender that waits forever until cancelled and prove `auto_trade` has already completed before notification delivery starts.

Run: `./.venv/bin/python -m pytest tests/test_telegram_live_listener.py::test_mimo_disagreement_executes_before_nonblocking_notification -q`

Expected: FAIL because current code returns before the auto-trade executor.

- [ ] **Step 2: Route live messages through the authoritative coordinator**

Remove the `conflict_payload -> return` branch. The live path must assess, apply MiMo, invoke `auto_trade_executor`, persist the automation outcome, then schedule notification delivery. Keep strategy alerts consistent with the authoritative MiMo result.

- [ ] **Step 3: Make notification text explicit**

Change the old message from “已暂停自动交易” to content equivalent to:

```text
【AI识别分歧告警】
权威结果: MiMo
处理: 已按 MiMo 结果继续
执行结果: submitted | skipped | failed
```

Include both normalized model summaries, source message, and automation reason. Catch delivery errors, log them, and update `notification_status="failed"` without changing the automation outcome.

- [ ] **Step 4: Update the manual recognition API**

The API must return `authoritative_model`, `agreement_status`, `differences`, `automation`, and `notification_scheduled`. It must never expose provider API keys or authorization headers.

- [ ] **Step 5: Verify notification/execution GREEN**

Run: `./.venv/bin/python -m pytest tests/test_telegram_live_listener.py tests/test_web_app.py tests/test_system_operator_bot.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the orchestration slice**

```bash
git add src/telegram_kol_research/telegram_live_listener.py src/telegram_kol_research/web_app.py src/telegram_kol_research/system_operator_bot.py tests/test_telegram_live_listener.py tests/test_web_app.py tests/test_system_operator_bot.py
git commit -m "fix: keep MiMo disagreement execution nonblocking"
```

### Task 5: Keep live-bound exits active until exchange reconciliation

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_lifecycle_monitor.py`
- Modify: `tests/test_execution_bindings.py`

**Interfaces:**
- Produces: `record_lifecycle_exit_intent(...) -> StrategyLifecycle` which records `exit_signal_message_id`, `management_signal_message_id`, `management_action="exit_requested"`, and the reason while preserving `lifecycle_status="entered"` for an active live binding.
- Consumes: existing Deepcoin reconciliation as the only component allowed to finalize the live-bound exit.

- [ ] **Step 1: Write the failing live-bound exit-state test**

Create an entered lifecycle with active binding, apply a MiMo `exit_position`, and assert:

```python
assert lifecycle.lifecycle_status == "entered"
assert lifecycle.exit_reason is None
assert lifecycle.exited_at is None
assert lifecycle.exit_signal_message_id == exit_message_id
assert lifecycle.management_action == "exit_requested"
```

Run: `./.venv/bin/python -m pytest tests/test_lifecycle_monitor.py::test_kol_exit_keeps_live_bound_lifecycle_entered_until_reconcile -q`

Expected: FAIL because `on_new_exit_signal` currently sets `exited` immediately.

- [ ] **Step 2: Record exit intent without finalizing live state**

Use the same helper from both `_apply_lifecycle_event_decision` and `LifecycleMonitor.on_new_exit_signal`. For simulated/unbound strategies, preserve existing immediate lifecycle transitions. For active live bindings, set intent metadata only.

- [ ] **Step 3: Prove failed or skipped execution remains unresolved**

Add tests where close submission returns failed/skipped and assert lifecycle stays entered with visible exit intent. Do not clear the intent during ordinary monitor cycles.

- [ ] **Step 4: Prove reconciliation finalizes exactly once**

Feed an exchange snapshot where the exact bound `pos_id` is absent and no live entry order remains. Assert binding becomes closed and lifecycle becomes `exited`, with a single transition and the original KOL exit message retained.

- [ ] **Step 5: Verify lifecycle GREEN**

Run: `./.venv/bin/python -m pytest tests/test_message_recognition.py tests/test_lifecycle_monitor.py tests/test_execution_bindings.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the lifecycle-safety slice**

```bash
git add src/telegram_kol_research/message_recognition.py src/telegram_kol_research/lifecycle_monitor.py src/telegram_kol_research/execution_bindings.py tests/test_message_recognition.py tests/test_lifecycle_monitor.py tests/test_execution_bindings.py
git commit -m "fix: reconcile live-bound lifecycle exits"
```

### Task 6: Apply the same authority flow to missed-message recovery

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_web_cli.py`

**Interfaces:**
- Extends: `run_reconcile_once(..., authoritative_processor: Callable[[int], Awaitable[Any]] | None = None)`.
- Extends: `run_periodic_reconcile(...)` with the same processor.

- [ ] **Step 1: Write the failing recovery regression test**

Simulate a missed Fengge exit message inserted by `run_reconcile_once`. Assert the authoritative processor receives the new raw message ID exactly once and that a replayed overlap does not execute it again.

Run: `./.venv/bin/python -m pytest tests/test_telegram_live_listener.py::test_reconcile_processes_new_messages_through_authoritative_execution_once -q`

Expected: FAIL because reconcile currently persists rule candidates only and never invokes the live auto-trade bridge.

- [ ] **Step 2: Process only newly inserted message keys**

After persistence, resolve `inserted_message_keys` to raw IDs and invoke the same coordinator used by the live listener. Do not call the legacy `recognize_records_with_ai_config` path for those records. Existing overlap replays with zero inserts must perform no assessment or execution.

- [ ] **Step 3: Wire web startup and one-shot refresh**

Pass the authoritative processor through periodic reconcile startup and the operator-triggered one-shot reconcile endpoint. Keep the existing Telegram operation lock so live and recovery ingestion do not race the same message.

- [ ] **Step 4: Verify recovery GREEN**

Run: `./.venv/bin/python -m pytest tests/test_telegram_live_listener.py tests/test_web_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the recovery slice**

```bash
git add src/telegram_kol_research/telegram_live_listener.py src/telegram_kol_research/web_app.py tests/test_telegram_live_listener.py tests/test_web_cli.py
git commit -m "fix: execute recovered Telegram signals once"
```

### Task 7: Full verification, documentation, GitHub push, and server deployment

**Files:**
- Modify: `docs/context/telegram-deepcoin-auto-trading-context.md`
- Modify: `docs/migration-handoff.md`
- Verify: all files changed by Tasks 1-6

**Interfaces:**
- Produces: reviewed GitHub branch and server deployment at the same commit.

- [ ] **Step 1: Update durable project memory**

Document MiMo authority, DeepSeek auxiliary-only text validation, non-blocking anomaly notifications, MiMo failure degradation, recovery-path processing, and reconcile-confirmed lifecycle exits. Do not include provider keys, bot tokens, chat IDs, or Deepcoin secrets.

- [ ] **Step 2: Run focused local verification**

```bash
./.venv/bin/python -m pytest \
  tests/test_ai_recognition_config.py \
  tests/test_recognition_experiments.py \
  tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_telegram_live_listener.py \
  tests/test_system_operator_bot.py \
  tests/test_lifecycle_monitor.py \
  tests/test_execution_bindings.py \
  tests/test_web_app.py \
  tests/test_web_cli.py \
  tests/test_db_bootstrap.py -q
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 3: Run the full local suite**

Run: `./.venv/bin/python -m pytest tests -q`

Expected: PASS with zero failures.

- [ ] **Step 4: Review the final diff and commit documentation**

```bash
git diff --check
git status --short
git diff --stat origin/codex/deepcoin-auto-trading-v1...HEAD
git add docs/context/telegram-deepcoin-auto-trading-context.md docs/migration-handoff.md
git commit -m "docs: record MiMo recognition authority"
```

- [ ] **Step 5: Push the reviewed local commits to GitHub**

```bash
git push origin codex/deepcoin-auto-trading-v1
git ls-remote origin refs/heads/codex/deepcoin-auto-trading-v1
```

Expected: the remote branch SHA equals local `HEAD`.

- [ ] **Step 6: Update production from GitHub**

Run from the Mac:

```bash
./scripts/server_git_update.sh
```

This causes the server to pull the branch, reinstall the editable package, and restart `telegram-kol.service`.

- [ ] **Step 7: Run server-side verification**

Use read-only/status commands plus the server test environment:

```bash
ssh -i "$HOME/.ssh/tecent.pem" root@43.167.220.225 '
  cd /opt/telegram-kol-analyzer &&
  git rev-parse HEAD &&
  systemctl is-active telegram-kol.service &&
  .venv/bin/python -m pytest tests/test_authoritative_recognition.py tests/test_telegram_live_listener.py tests/test_lifecycle_monitor.py tests/test_execution_bindings.py -q &&
  journalctl -u telegram-kol.service -n 200 --no-pager
'
```

Expected: server SHA equals GitHub/local SHA, service is `active`, focused tests pass, and recent logs contain no startup traceback. Verify the effective MiMo prompt through a local builder call that prints only rule markers, not credentials. Inspect the next naturally arriving recognition decision for authoritative model, agreement status, automation outcome, notification outcome, and reconciliation state; do not create an unapproved live order for testing.

- [ ] **Step 8: Report deployment evidence**

Report local test count, full-suite result, pushed commit SHA, server commit SHA, service state, server focused-test result, and any remaining verification that depends on the next natural Telegram message.
