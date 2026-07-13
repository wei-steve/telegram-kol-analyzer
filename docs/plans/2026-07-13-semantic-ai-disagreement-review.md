# Semantic AI Disagreement Review Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move DeepSeek disagreement analysis out of the MiMo trading critical path and replace raw field comparison with a persisted, severity-aware semantic review that notifies only on material trading conflicts.

**Architecture:** MiMo remains synchronous and authoritative: persist its decision, apply it, run existing safety gates, and save the automation outcome before returning. A single database-backed background worker later asks DeepSeek for an independent structured interpretation, applies deterministic severity floors, persists `none`/`normal`/`critical`, and sends an idempotent notification only for `critical`.

**Tech Stack:** Python 3.12+, FastAPI lifespan tasks, SQLAlchemy/SQLite compatibility migrations, httpx, Jinja2, versioned prompt registry, pytest.

---

## Preconditions

- Work in an isolated worktree created with `@superpowers:using-git-worktrees`; do not implement directly in a dirty production branch checkout.
- Read `AGENTS.md`, `docs/migration-handoff.md`, `docs/runbook.md`, `docs/server-deployment.md`, and `docs/plans/2026-07-13-semantic-ai-disagreement-review-design.md` before editing.
- Record the baseline full-suite result. Known baseline failures are acceptable only when the implementation does not increase them and all new focused tests pass.
- Use `@superpowers:test-driven-development` for every task and `@superpowers:verification-before-completion` before claiming completion.

### Task 1: Add durable semantic-review state

**Files:**
- Modify: `src/telegram_kol_research/models.py:133-160`
- Modify: `src/telegram_kol_research/db.py:76-81`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `tests/test_db_bootstrap.py:41-70`
- Modify: `tests/test_recognition_decisions.py`

**Step 1: Write failing schema tests**

Extend `test_database_bootstrap_creates_recognition_decisions_table` to require:

```python
{
    "comparison_status",
    "disagreement_severity",
    "comparison_model",
    "comparison_payload_json",
    "comparison_error",
    "comparison_attempts",
    "comparison_next_attempt_at",
    "comparison_started_at",
    "compared_at",
    "notification_fingerprint",
}
```

Add a compatibility test that creates the pre-change `recognition_decisions` table, calls `create_session_factory`, and asserts the columns were added. Assert pre-existing rows get `comparison_status = 'completed'`, so historical records are not accidentally queued.

**Step 2: Run the schema tests and confirm failure**

Run:

```bash
pytest -q tests/test_db_bootstrap.py -k recognition_decisions
```

Expected: FAIL because semantic-review columns do not exist.

**Step 3: Add model and SQLite compatibility columns**

Add nullable audit fields plus safe defaults to `RecognitionDecision`. Use `completed` as the database default for legacy compatibility; new authoritative writes will explicitly set `pending`.

```python
comparison_status = mapped_column(String(32), nullable=False, default="completed")
disagreement_severity = mapped_column(String(32), nullable=True)
comparison_model = mapped_column(String(128), nullable=True)
comparison_payload_json = mapped_column(Text, nullable=True)
comparison_error = mapped_column(Text, nullable=True)
comparison_attempts = mapped_column(Integer, nullable=False, default=0)
comparison_next_attempt_at = mapped_column(DateTime, nullable=True)
comparison_started_at = mapped_column(DateTime, nullable=True)
compared_at = mapped_column(DateTime, nullable=True)
notification_fingerprint = mapped_column(String(64), nullable=True)
```

Add one `ALTER TABLE` entry per column under `SQLITE_COMPAT_COLUMNS["recognition_decisions"]`. Use `NOT NULL DEFAULT 'completed'` and `NOT NULL DEFAULT 0` only for the two non-null compatibility fields.

**Step 4: Add persistence operations and their failing tests**

Replace the current all-at-once auxiliary record assumption with explicit helpers:

```python
def save_pending_authoritative_decision(
    session_factory,
    record: RecognitionDecisionRecord,
) -> RecognitionDecision: ...

def claim_next_semantic_review(
    session_factory,
    *,
    now: datetime,
    stale_before: datetime,
) -> int | None: ...

def complete_semantic_review(
    session_factory,
    *,
    raw_message_id: int,
    model: str,
    auxiliary_payload: dict[str, Any],
    comparison_payload: dict[str, Any],
    agreement_status: str,
    severity: str,
    differences: list[str],
    prompt_versions: dict[str, int],
    compared_at: datetime,
) -> None: ...

def fail_semantic_review(
    session_factory,
    *,
    raw_message_id: int,
    error: str,
    next_attempt_at: datetime | None,
) -> None: ...

def claim_critical_notification(
    session_factory,
    *,
    raw_message_id: int,
    fingerprint: str,
) -> bool: ...
```

Tests must prove:

- a new MiMo decision is `pending` with no auxiliary payload;
- automation outcome survives later comparison completion;
- one worker can claim a pending row and a second claim returns none;
- stale `running` work is reclaimable;
- failure increments attempts and returns to `pending` only when a retry time exists;
- completed critical notification can be claimed only once for the same fingerprint;
- re-saving the same authoritative payload does not clear a sent fingerprint;
- re-saving a genuinely changed authoritative payload resets comparison state but does not make old candidates executable.

**Step 5: Implement the helpers minimally**

Keep each state transition in one short transaction. Never clear `automation_status` or `automation_reason` when comparison state changes. Preserve the current unique `raw_message_id` boundary.

**Step 6: Run focused tests**

Run:

```bash
pytest -q tests/test_db_bootstrap.py tests/test_recognition_decisions.py
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/recognition_decisions.py \
  tests/test_db_bootstrap.py tests/test_recognition_decisions.py
git commit -m "feat: persist semantic disagreement review state"
```

### Task 2: Register and validate the DeepSeek review prompt

**Files:**
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Modify: `src/telegram_kol_research/prompt_composition.py:93-156`
- Modify: `tests/test_ai_recognition_config.py`
- Modify: `tests/test_prompt_composition.py`
- Modify: `tests/test_ai_prompt_inventory.py`

**Step 1: Write failing prompt inventory tests**

Require a new prompt key:

```python
SEMANTIC_DISAGREEMENT_REVIEW_PROMPT = "trading.disagreement.semantic_review"
```

Assert its seed has:

```python
category == "notification"
consumers == ("deepseek_disagreement_review",)
required_variables == ()
validation_profile == "semantic_disagreement_review"
```

Assert the default contains every required JSON marker and explicit rules that it must independently interpret the current message, cite evidence, not modify trading, and not claim image-pixel access.

**Step 2: Run and confirm failure**

```bash
pytest -q tests/test_ai_recognition_config.py tests/test_prompt_composition.py \
  tests/test_ai_prompt_inventory.py -k 'prompt or disagreement'
```

Expected: FAIL because the prompt is not registered.

**Step 3: Add the prompt seed and validation profile**

Add a default prompt whose output contract includes:

```json
{
  "independent_action": {
    "action_type": "none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update",
    "target_lifecycle_id": null,
    "symbol": null,
    "side": null,
    "stop_loss": null,
    "take_profit": null,
    "management_action": null
  },
  "evidence": [],
  "conflict_types": [],
  "material_disagreement": false,
  "suggested_severity": "none | normal | critical",
  "confidence": 0.0,
  "reason": ""
}
```

Use a closed conflict vocabulary:

```text
actionability, action_family, full_vs_partial_exit, symbol, side,
target_lifecycle, stop_intent, urgent_exit_missed, execution_unresolved,
non_material_price_detail, wording_only
```

Extend `validate_prompt_content` so publication fails when schema markers, action enums, severity enums, or the image limitation are removed.

**Step 4: Run focused tests**

```bash
pytest -q tests/test_ai_recognition_config.py tests/test_prompt_composition.py \
  tests/test_ai_prompt_inventory.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/prompt_defaults.py \
  src/telegram_kol_research/prompt_composition.py \
  tests/test_ai_recognition_config.py tests/test_prompt_composition.py \
  tests/test_ai_prompt_inventory.py
git commit -m "feat: register semantic disagreement review prompt"
```

### Task 3: Implement deterministic normalization and severity floors

**Files:**
- Create: `src/telegram_kol_research/semantic_disagreement_review.py`
- Create: `tests/test_semantic_disagreement_review.py`

**Step 1: Write failing pure-function tests**

Cover at least:

```python
def test_equivalent_numeric_formats_are_none(): ...
def test_same_action_with_different_reason_is_none(): ...
def test_noncritical_take_profit_detail_is_normal(): ...
def test_exit_versus_none_is_critical(): ...
def test_full_exit_versus_partial_exit_is_critical(): ...
def test_actionable_symbol_side_or_target_mismatch_is_critical(): ...
def test_deepseek_cannot_downgrade_code_critical_floor(): ...
def test_unsupported_deepseek_critical_escalation_is_normal(): ...
def test_supported_evidenced_high_confidence_escalation_is_critical(): ...
def test_image_review_without_text_evidence_cannot_be_critical(): ...
```

Use fixtures shaped like real MiMo and semantic-review JSON, including the Fengge exit example.

**Step 2: Run and confirm failure**

```bash
pytest -q tests/test_semantic_disagreement_review.py
```

Expected: FAIL because the module does not exist.

**Step 3: Implement the pure domain layer**

Create immutable result types and pure helpers:

```python
@dataclass(frozen=True)
class SemanticReviewDecision:
    agreement_status: str
    severity: str
    conflict_types: tuple[str, ...]
    differences: tuple[str, ...]
    reason: str

def normalize_price(value: Any) -> Decimal | tuple[Decimal, ...] | str | None: ...
def normalize_mimo_action(payload: dict[str, Any]) -> dict[str, Any]: ...
def validate_review_payload(payload: dict[str, Any]) -> None: ...
def decide_semantic_severity(
    *,
    mimo_payload: dict[str, Any],
    review_payload: dict[str, Any],
    automation: dict[str, Any],
    input_kind: str,
    critical_confidence: float = 0.80,
) -> SemanticReviewDecision: ...
```

Normalize case, whitespace, numeric strings, scalar/list price representations, and management-action token order before comparing. Map MiMo lifecycle events to the review action vocabulary. Deterministic critical rules are floors; model-only escalation requires an allowed material type, non-empty direct evidence, sufficient confidence, and text evidence for image-bearing messages.

**Step 4: Run focused tests**

```bash
pytest -q tests/test_semantic_disagreement_review.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/semantic_disagreement_review.py \
  tests/test_semantic_disagreement_review.py
git commit -m "feat: classify material AI disagreements"
```

### Task 4: Build the DeepSeek semantic reviewer and retryable worker

**Files:**
- Modify: `src/telegram_kol_research/semantic_disagreement_review.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `tests/test_semantic_disagreement_review.py`
- Create: `tests/test_semantic_disagreement_worker.py`

**Step 1: Write failing request and audit tests**

Inject the HTTP requester and clock. Assert one review request contains:

- current raw-message text and identity;
- safely serialized active-strategy context;
- MiMo authoritative payload and input reading;
- persisted automation status/reason;
- no API keys or provider authorization payload;
- the active semantic-review prompt version.

Assert the response is strict JSON and records a prompt invocation with feature `semantic_disagreement_review`.

**Step 2: Write failing worker-state tests**

Test one-shot worker behavior through an injected reviewer/notifier:

```python
async def test_worker_completes_pending_review(): ...
async def test_worker_retries_timeout_without_touching_automation(): ...
async def test_worker_marks_invalid_json_failed_after_three_attempts(): ...
async def test_worker_recovers_stale_running_claim(): ...
async def test_worker_notifies_only_critical(): ...
async def test_worker_does_not_duplicate_claimed_notification(): ...
```

**Step 3: Run and confirm failure**

```bash
pytest -q tests/test_semantic_disagreement_review.py \
  tests/test_semantic_disagreement_worker.py
```

Expected: FAIL because reviewer and worker functions are missing.

**Step 4: Implement the request function**

Add:

```python
def run_deepseek_semantic_review(
    session_factory,
    *,
    raw_message_id: int,
    config: AiRecognitionConfig,
    requester: Callable[..., dict[str, Any]] | None = None,
) -> SemanticReviewRun: ...
```

Resolve `trading.disagreement.semantic_review` through the prompt registry, build dynamic user context separately from the system prompt, use the configured DeepSeek provider, validate the exact schema, and record prompt invocation success/failure. Reuse the existing OpenAI-compatible URL/content/JSON parsing utilities rather than creating a second incompatible protocol implementation.

**Step 5: Implement one-shot and loop workers**

Add:

```python
async def run_semantic_review_once(..., now: datetime) -> bool: ...

async def run_semantic_review_loop(
    *,
    session_factory,
    config_path: Path,
    notifier,
    poll_interval_seconds: float = 1.0,
    max_attempts: int = 3,
) -> None: ...
```

The loop claims one row at a time, reloads configuration per attempt, uses `asyncio.to_thread` for blocking HTTP/SQLite work, and applies increasing retry delays without sleeping inside a database transaction. Catch per-item exceptions so one bad message cannot terminate the loop.

**Step 6: Run focused tests**

```bash
pytest -q tests/test_semantic_disagreement_review.py \
  tests/test_semantic_disagreement_worker.py tests/test_prompt_registry.py
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/semantic_disagreement_review.py \
  src/telegram_kol_research/recognition_decisions.py \
  tests/test_semantic_disagreement_review.py \
  tests/test_semantic_disagreement_worker.py
git commit -m "feat: process semantic disagreement reviews asynchronously"
```

### Task 5: Remove DeepSeek from the MiMo execution critical path

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py:111-180`
- Modify: `src/telegram_kol_research/cli.py:153-229`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_cli_authoritative_recognition.py`

**Step 1: Write a failing latency/order regression**

Patch `infer_deepseek_auxiliary` to raise if called by `process_authoritative_message`. Use an actionable MiMo exit and assert:

```python
events == ["mimo", "persist_pending", "apply_mimo", "auto_trade"]
result.automation == {"status": "executed", "reason": "close_submitted"}
result.assessment.deepseek_payload is None
```

Add a separate async test with a semantic reviewer blocked on an event and assert the live message handler finishes its authoritative path without waiting for that event.

**Step 2: Run and confirm failure**

```bash
pytest -q tests/test_authoritative_recognition.py \
  tests/test_telegram_live_listener.py -k 'authoritative or semantic or delay'
```

Expected: FAIL because `assess_message_authoritatively` still calls DeepSeek synchronously.

**Step 3: Refactor authoritative assessment**

Make assessment run MiMo only. Persist the decision as pending before applying it. Keep `authoritative_failed` fail-closed behavior, but do not create a semantic review job for a MiMo transport/schema failure; preserve the independent MiMo-failure alert.

The only synchronous branches after MiMo must be:

```text
persist authoritative decision
apply MiMo
evaluate existing action candidate
call existing auto-trade executor
persist automation outcome
return
```

Delete production use of `compare_assessments` and `infer_deepseek_auxiliary` from this path. Retain a compatibility wrapper only if prompt draft testing still imports comparison logic; otherwise move semantic comparison entirely into the new module.

**Step 4: Remove inline disagreement notification scheduling**

The live listener and manual recognition endpoint must no longer build a DeepSeek conflict payload immediately after authoritative processing. They should only return the authoritative result and pending review status. MiMo-failure notification remains non-blocking and separate.

The CLI parse command may leave semantic review pending because it has no live worker; document that the Web service worker processes it later.

**Step 5: Run focused tests**

```bash
pytest -q tests/test_authoritative_recognition.py \
  tests/test_telegram_live_listener.py tests/test_cli_authoritative_recognition.py
```

Expected: PASS, including Fengge exit and fail-closed MiMo tests.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/cli.py \
  tests/test_authoritative_recognition.py tests/test_telegram_live_listener.py \
  tests/test_cli_authoritative_recognition.py
git commit -m "fix: keep DeepSeek outside authoritative execution path"
```

### Task 6: Start and stop the durable worker with the Web service

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:1640-1790`
- Modify: `src/telegram_kol_research/web_app.py:1792-1870`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_cli.py`

**Step 1: Write failing lifespan tests**

Inject a fake `semantic_review_runner` into `create_web_app`. Assert lifespan:

- starts exactly one worker task;
- passes the session factory, AI config path, and system bot notifier;
- cancels and awaits the task during shutdown;
- logs task failure rather than silently losing the worker;
- does not start a duplicate worker when Telegram targets are changed.

**Step 2: Run and confirm failure**

```bash
pytest -q tests/test_web_app.py tests/test_web_cli.py -k semantic_review
```

Expected: FAIL because no worker is wired.

**Step 3: Wire the worker**

Add injectable app state:

```python
app.state.semantic_review_runner = semantic_review_runner or run_semantic_review_loop
app.state.semantic_review_task = None
```

Start it once during lifespan after database and prompt seeding are ready. The worker should run even when the Telegram client is temporarily disconnected because pending database rows still require review. Cancel it before closing shared runtime resources.

**Step 4: Run focused tests**

```bash
pytest -q tests/test_web_app.py tests/test_web_cli.py -k 'semantic_review or lifespan'
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_app.py tests/test_web_cli.py
git commit -m "feat: run semantic review worker with web service"
```

### Task 7: Send critical-only, evidence-backed notifications

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py:82-110`
- Modify: `src/telegram_kol_research/semantic_disagreement_review.py`
- Modify: `tests/test_system_operator_bot.py`
- Modify: `tests/test_semantic_disagreement_worker.py`

**Step 1: Write failing notification-format tests**

Require a critical notification to show:

```text
【AI语义严重分歧】
权威结果: MiMo
自动化结果: submitted / close_position
复核结果: DeepSeek ...
冲突类型: full_vs_partial_exit
处理状态: 已按MiMo结果继续，未等待人工复核
```

Assert the direct evidence and original message are truncated safely. Assert no inline keyboard is produced.

**Step 2: Write failing idempotency tests**

Simulate:

- worker recovery after a sent notification;
- worker recovery after `notification_status = scheduled`;
- a second processing attempt with the same fingerprint;
- a normal review.

Only the first critical claim may invoke the sender. Normal reviews must never invoke it.

**Step 3: Implement the formatter and sender call**

Rename the operator-facing formatter to semantic conflict terminology. Build its payload from persisted decision state after severity is final, not from a transient live-listener result.

Compute the fingerprint from stable canonical JSON of:

```text
raw_message_id + authoritative_payload + comparison_payload + severity
```

Claim it transactionally before calling Telegram. Update `sent` or `failed` afterward without changing automation state.

**Step 4: Run focused tests**

```bash
pytest -q tests/test_system_operator_bot.py tests/test_semantic_disagreement_worker.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/semantic_disagreement_review.py \
  tests/test_system_operator_bot.py tests/test_semantic_disagreement_worker.py
git commit -m "feat: notify only material AI disagreements"
```

### Task 8: Expose authoritative review state in the message detail

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py:350-447`
- Modify: `src/telegram_kol_research/templates/_messages.html:178-240`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/web_app.py:2827-2872`
- Modify: `tests/test_web_queries_messages.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing query serialization tests**

Bulk-load `RecognitionDecision` beside recognitions and experiments. Require each serialized message to include:

```python
"semantic_review": {
    "status": "completed",
    "severity": "normal",
    "label": "普通差异",
    "reason": "...",
    "conflict_types": ["non_material_price_detail"],
    "model": "deepseek-v4-flash",
}
```

Assert the query remains bulk-loaded rather than issuing one decision query per message.

**Step 2: Write failing rendering tests**

Assert the five states render:

```text
AI复核：一致
AI复核：普通差异
AI复核：严重分歧
AI复核：等待中
AI复核：失败
```

Critical gets a prominent class; normal details are inside a closed `<details>` element by default. Remove or clearly label the legacy experiment panel so it cannot be mistaken for the new authoritative review.

**Step 3: Run and confirm failure**

```bash
pytest -q tests/test_web_queries_messages.py tests/test_web_page_render.py \
  tests/test_web_app.py -k 'semantic_review or message_recognition'
```

Expected: FAIL because decisions are not serialized or rendered.

**Step 4: Implement query, template, and API state**

Add one bulk query keyed by `raw_message_id`, a defensive JSON serializer, minimal accessible status markup, and CSS using existing status colors. The manual recognition response should return `semantic_review_status = "pending"` rather than claiming immediate agreement/disagreement.

**Step 5: Run focused tests**

```bash
pytest -q tests/test_web_queries_messages.py tests/test_web_page_render.py \
  tests/test_web_app.py -k 'semantic_review or message_recognition'
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/static/app.css src/telegram_kol_research/web_app.py \
  tests/test_web_queries_messages.py tests/test_web_page_render.py tests/test_web_app.py
git commit -m "feat: show semantic AI review status"
```

### Task 9: Regression, documentation, and server verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/context/ai-prompt-registry.md`
- Modify: `docs/runbook.md`
- Modify: `docs/plans/2026-07-13-semantic-ai-disagreement-review.md` only if implementation paths changed

**Step 1: Run focused safety regressions**

```bash
pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_semantic_disagreement_review.py \
  tests/test_semantic_disagreement_worker.py \
  tests/test_recognition_decisions.py \
  tests/test_telegram_live_listener.py \
  tests/test_auto_trade_execution.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_system_operator_bot.py \
  tests/test_web_queries_messages.py \
  tests/test_web_page_render.py
```

Expected: PASS.

**Step 2: Run the full local suite**

```bash
pytest -q
```

Expected: no new failures compared with the recorded baseline.

**Step 3: Update durable documentation**

Document:

- MiMo execution precedes DeepSeek review;
- only critical semantic disagreements notify;
- normal differences are Web-visible and database-only;
- review status/retry fields and operator interpretation;
- DeepSeek failure never changes a trade;
- the new prompt registry key and publication validation;
- production queries for pending/failed/critical counts without exposing message or credential data.

**Step 4: Review the diff**

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors and only planned files changed.

Use `@superpowers:requesting-code-review` before the final implementation commit.

**Step 5: Commit documentation and final fixes**

```bash
git add docs/migration-handoff.md docs/context/ai-prompt-registry.md docs/runbook.md \
  docs/plans/2026-07-13-semantic-ai-disagreement-review.md
git commit -m "docs: document semantic disagreement operations"
```

**Step 6: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub accepts the reviewed commits on the required branch.

**Step 7: Deploy through the existing helper**

From an approved workstation:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected server actions: pull the GitHub commit, reinstall the editable package, restart `telegram-kol.service`.

**Step 8: Verify production state without placing real trades**

On the server, verify:

```bash
cd /opt/telegram-kol-analyzer
git rev-parse HEAD
systemctl is-active telegram-kol.service
journalctl -u telegram-kol.service -n 200 --no-pager
```

Run a read-only database audit for:

```text
comparison_status counts
disagreement_severity counts
pending age
failed attempt/error counts
critical notification status counts
```

Use controlled recognition fixtures or an approved non-trading group to prove:

- authoritative automation outcome is persisted before semantic comparison completes;
- an intentionally slow DeepSeek response does not delay MiMo processing;
- normal differences do not send a bot message;
- one critical fixture sends exactly one message;
- restart recovery completes a pending fixture;
- no Deepcoin order is created by semantic review itself.

Do not trigger a real order merely to test disagreement notification.

**Step 9: Final completion verification**

Use `@superpowers:verification-before-completion` and report exact focused/full test counts, deployed commit SHA, service state, database review counts, and the controlled latency result.
