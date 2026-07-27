# Contextual Strategy Thread Resolution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist text/image evidence once, connect Telegram reply and revision messages into strategy threads, and use a second structured AI decision plus deterministic exchange checks to resolve contextual actions automatically.

**Architecture:** MiMo becomes an evidence extractor whose versioned output separates text from image facts. A deterministic context builder creates bounded reply/message/strategy candidates, DeepSeek returns a closed strategy-thread decision, and existing lifecycle/management planners remain the only path to live writes. Unresolved decisions are durable and automatically retried on new evidence or exchange-state changes.

**Tech Stack:** Python 3.11+, SQLAlchemy, SQLite, Telethon, httpx, pytest, Jinja2, existing MiMo/DeepSeek OpenAI-compatible chat APIs.

---

Implementation must follow @test-driven-development and @systematic-debugging. Before
merging or deploying, use @requesting-code-review. Production verification must follow
`AGENTS.md`: push reviewed commits to `codex/deepcoin-auto-trading-v1`, update the server
from GitHub, reinstall the editable package, restart `telegram-kol.service`, and perform
real verification on the server.

### Task 1: Add versioned message evidence storage

**Files:**
- Modify: `src/telegram_kol_research/models.py:36-68`
- Modify: `src/telegram_kol_research/models.py:1384-1460`
- Modify: `src/telegram_kol_research/db.py:20-260`
- Create: `src/telegram_kol_research/message_evidence.py`
- Create: `tests/test_message_evidence.py`
- Modify: `tests/test_db_migrations.py`

**Step 1: Write failing model and migration tests**

Add tests that create an old-style SQLite database, run `create_session_factory`, and
assert:

```python
assert inspector.has_table("message_evidence_versions")
assert inspector.has_table("strategy_threads")
assert inspector.has_table("strategy_message_links")
assert inspector.has_table("context_resolution_attempts")
assert "strategy_thread_id" in {
    column["name"] for column in inspector.get_columns("strategy_lifecycles")
}
```

Add persistence tests for an immutable evidence version:

```python
row = save_message_evidence_version(
    session_factory,
    raw_message_id=message.id,
    input_fingerprint="sha256:one",
    model="mimo-v2.5",
    prompt_versions={"evidence": "v1"},
    extraction_status="completed",
    confidence=0.94,
    text_evidence={"symbol": {"value": "BTC", "source": "text"}},
    image_evidence={"image_type": "strategy_screenshot"},
    normalized_evidence={"symbol": "BTC", "side": "long"},
)
assert row.version == 1
```

**Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_message_evidence.py tests/test_db_migrations.py -v
```

Expected: FAIL because the new tables, lifecycle column, and persistence helper do not
exist.

**Step 3: Add SQLAlchemy models and indexes**

Add:

```python
class MessageEvidenceVersion(Base):
    __tablename__ = "message_evidence_versions"
    __table_args__ = (
        UniqueConstraint(
            "raw_message_id", "input_fingerprint",
            name="uq_message_evidence_input_fingerprint",
        ),
        UniqueConstraint(
            "raw_message_id", "version",
            name="uq_message_evidence_message_version",
        ),
    )
    id = mapped_column(Integer, primary_key=True)
    raw_message_id = mapped_column(ForeignKey("raw_messages.id"), index=True)
    version = mapped_column(Integer, nullable=False)
    input_fingerprint = mapped_column(String(80), nullable=False)
    model = mapped_column(String(128), nullable=False)
    prompt_versions_json = mapped_column(Text, nullable=False, default="{}")
    extraction_status = mapped_column(String(32), nullable=False)
    confidence = mapped_column(Float, nullable=False, default=0.0)
    text_evidence_json = mapped_column(Text, nullable=False, default="{}")
    image_evidence_json = mapped_column(Text, nullable=False, default="{}")
    normalized_evidence_json = mapped_column(Text, nullable=False, default="{}")
    superseded_at = mapped_column(DateTime, nullable=True)
    created_at = mapped_column(DateTime, default=utc_now, nullable=False)
```

Add `StrategyThread`, `StrategyMessageLink`, and `ContextResolutionAttempt` with the
fields defined in the design document. Add nullable `strategy_thread_id` to
`StrategyLifecycle`.

Required unique constraints:

- one evidence version per message/input fingerprint;
- one strategy link per `(strategy_thread_id, raw_message_id, relation_kind)`;
- one active resolution fingerprint per current message/context fingerprint.

**Step 4: Add compatible SQLite migration**

Use `Base.metadata.create_all()` for new tables and add
`strategy_lifecycles.strategy_thread_id` to `SQLITE_COMPAT_COLUMNS`. Add indexes only
after verifying their required columns exist.

Do not backfill or merge old lifecycles in this task.

**Step 5: Implement evidence persistence**

In `message_evidence.py`, implement:

```python
def build_message_input_fingerprint(raw_message, media_assets) -> str: ...
def save_message_evidence_version(...) -> MessageEvidenceVersion: ...
def load_current_message_evidence(session, raw_message_id: int) -> MessageEvidenceVersion | None: ...
```

Canonicalize JSON with `sort_keys=True`, include `edit_date`, text, media kind,
Telegram file ID, and a local content hash when a readable media file exists. Repeated
identical input returns the existing row. A changed fingerprint increments `version`
and marks the previous current row `superseded_at`.

**Step 6: Run tests**

Run:

```bash
uv run pytest tests/test_message_evidence.py tests/test_db_migrations.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/message_evidence.py \
  tests/test_message_evidence.py tests/test_db_migrations.py
git commit -m "feat: persist versioned message evidence"
```

### Task 2: Separate MiMo text and image evidence

**Files:**
- Modify: `src/telegram_kol_research/prompt_defaults.py:1-115`
- Modify: `src/telegram_kol_research/prompt_composition.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py:242-337`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:160-225`
- Test: `tests/test_prompt_composition.py`
- Test: `tests/test_recognition_experiments.py`
- Test: `tests/test_authoritative_recognition.py`

**Step 1: Write failing contract tests**

Require MiMo output to contain:

```json
{
  "evidence": {
    "text": {"fields": {}, "observed_text": "..."},
    "images": [
      {
        "asset_id": 12,
        "image_type": "position_screenshot",
        "fields": {},
        "confidence": 0.92
      }
    ],
    "conflicts": []
  }
}
```

Test that a text `ETH short` plus a historical `BTC long` image produces separate
fields and a conflict instead of a fused BTC/ETH strategy.

Test that unreadable declared image media saves `extraction_status="image_unavailable"`
rather than falling back to text-only evidence.

**Step 2: Run focused tests**

```bash
uv run pytest tests/test_prompt_composition.py \
  tests/test_recognition_experiments.py \
  tests/test_authoritative_recognition.py -k "image or evidence or unavailable" -v
```

Expected: FAIL on the new evidence contract.

**Step 3: Update the MiMo prompt contract**

Require:

- one evidence block per source;
- no lifecycle target choice inside image evidence;
- a closed `image_type` vocabulary;
- per-field `value`, `source`, `confidence`, and `evidence`;
- explicit `conflicts`;
- no silent merge when text and image differ.

Keep the existing top-level recognition/lifecycle output temporarily for backward
compatibility.

**Step 4: Persist evidence before applying actions**

In `process_authoritative_message`:

1. compute the input fingerprint;
2. run MiMo;
3. validate and persist the evidence version;
4. only then apply the current authoritative action result.

If evidence persistence fails, do not execute an AI-derived action.

**Step 5: Run tests**

```bash
uv run pytest tests/test_prompt_composition.py \
  tests/test_recognition_experiments.py \
  tests/test_authoritative_recognition.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/prompt_defaults.py \
  src/telegram_kol_research/prompt_composition.py \
  src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_prompt_composition.py \
  tests/test_recognition_experiments.py \
  tests/test_authoritative_recognition.py
git commit -m "feat: separate text and image evidence"
```

### Task 3: Build bounded reply chains and dynamic message windows

**Files:**
- Create: `src/telegram_kol_research/contextual_message_window.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py:500-543`
- Test: `tests/test_contextual_message_window.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write failing reply/window tests**

Cover:

- direct reply;
- five-level reply chain;
- cycle detection;
- missing reply target;
- 72-hour cutoff;
- 50-message cutoff;
- inclusion of the oldest related active strategy;
- preservation of reply ancestors outside the ordinary time window;
- timestamps and evidence version IDs in every context item.

Example:

```python
window = build_contextual_message_window(
    session,
    raw_message_id=current.id,
    max_age_hours=72,
    max_messages=50,
    max_reply_depth=5,
)
assert window.reply_chain[0].message_id == 1462
assert window.reply_chain[1].message_id == 1460
assert window.messages[-1].posted_at is not None
```

**Step 2: Verify failures**

```bash
uv run pytest tests/test_contextual_message_window.py \
  tests/test_message_recognition.py -k "reply or context_window" -v
```

Expected: FAIL because no bounded context builder exists.

**Step 3: Implement the pure database context builder**

Return typed dataclasses, not an already-rendered prompt. Include:

- current standardized evidence;
- recent standardized evidence;
- reply chain with resolution status;
- relevant active/pending/expired lifecycles;
- existing strategy-thread links.

Never fetch Telegram or call an AI inside this pure builder.

**Step 4: Add a separate missing-reply fetch hook**

Expose:

```python
async def fetch_missing_reply_target(
    telegram_client,
    *,
    chat_id: int,
    message_id: int,
) -> bool: ...
```

Invoke it once from the live listener orchestration before final context construction.
Persist through the existing raw-ingest path. Record `reply_target_unavailable` if the
message remains absent.

**Step 5: Replace the legacy 20-message context**

Make `build_authoritative_context_for_message` render the typed bounded context,
including ISO timestamps. Replace the legacy context directly; do not add a shadow
or background comparison path.

**Step 6: Run tests**

```bash
uv run pytest tests/test_contextual_message_window.py \
  tests/test_message_recognition.py tests/test_raw_ingest.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/contextual_message_window.py \
  src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/telegram_live_listener.py \
  tests/test_contextual_message_window.py \
  tests/test_message_recognition.py tests/test_raw_ingest.py
git commit -m "feat: build bounded reply-aware message context"
```

### Task 4: Generate deterministic strategy-thread candidates

**Files:**
- Create: `src/telegram_kol_research/strategy_thread_candidates.py`
- Create: `src/telegram_kol_research/strategy_threads.py`
- Test: `tests/test_strategy_thread_candidates.py`
- Modify: `tests/conftest.py`

**Step 1: Write failing candidate tests**

Build fixtures for:

- `1460` root entry;
- `1462` wording “更新” with matching chat/symbol/side and overlapping prices;
- `1465` wording “策略先取消”;
- two unrelated BTC-long strategies in the same group;
- a direct reply to one exact strategy;
- an already exited strategy.

Assert candidate scores and reasons, without yet calling an AI:

```python
assert candidates[0].thread_id == thread_1460.id
assert candidates[0].reasons == (
    "same_chat", "same_symbol", "same_side",
    "revision_language", "recent_active_thread",
)
```

Direct reply must rank ahead of temporal similarity. Contradictory symbol/side must be
excluded, not merely down-scored.

**Step 2: Run tests**

```bash
uv run pytest tests/test_strategy_thread_candidates.py -v
```

Expected: FAIL.

**Step 3: Implement thread/link repositories**

Implement idempotent helpers:

```python
def create_strategy_thread_for_lifecycle(...): ...
def link_message_to_strategy_thread(...): ...
def list_relevant_strategy_threads(...): ...
```

Do not automatically merge old lifecycles.

**Step 4: Implement candidate generation**

Use closed reason codes and deterministic ordering:

1. direct reply link;
2. reply ancestor link;
3. existing message-thread link;
4. same chat/symbol/side and state;
5. overlapping entry/SL/TP evidence;
6. time proximity.

Return at most 20 candidates and include exact lifecycle/binding/verified-leg summaries.

**Step 5: Run tests**

```bash
uv run pytest tests/test_strategy_thread_candidates.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_thread_candidates.py \
  src/telegram_kol_research/strategy_threads.py \
  tests/test_strategy_thread_candidates.py tests/conftest.py
git commit -m "feat: generate strategy thread candidates"
```

### Task 5: Add the structured DeepSeek context resolver

**Files:**
- Create: `src/telegram_kol_research/context_resolution.py`
- Create: `src/telegram_kol_research/context_resolution_prompt.py`
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Modify: `config/ai_recognition.example.yaml`
- Test: `tests/test_context_resolution.py`
- Test: `tests/test_ai_recognition_config.py`

**Step 1: Write failing schema tests**

Define a typed result:

```python
ContextResolutionDecision(
    decision="revise_thread",
    target_thread_ids=(12,),
    management_action=None,
    confidence=0.93,
    supporting_message_ids=(1460, 1462),
    opposing_message_ids=(),
    conflict_types=(),
    risk_reducing_fanout_allowed=False,
    reanalysis_triggers=(),
)
```

Reject:

- invented thread IDs;
- unknown decision/action/conflict values;
- an empty target for revise/manage/cancel/exit;
- multiple targets for a risk-increasing action;
- confidence outside `[0, 1]`;
- supporting messages outside the provided context.

**Step 2: Verify failure**

```bash
uv run pytest tests/test_context_resolution.py \
  tests/test_ai_recognition_config.py -v
```

Expected: FAIL.

**Step 3: Implement the prompt and parser**

DeepSeek receives only:

- raw/current text;
- saved text/image evidence;
- reply graph;
- bounded message events;
- candidate strategy threads;
- redacted exchange state;
- MiMo first-pass result.

It never receives image bytes or local paths.

Use `temperature=0`, a closed JSON contract, one retry for malformed JSON, and no retry
for network outcome ambiguity beyond the existing read-only AI-call policy.

**Step 4: Persist every attempt**

Store context fingerprint, model, prompt versions, request summary, decision JSON,
status, error class, reanalysis triggers, and timestamps in
`context_resolution_attempts`. Do not store credentials or image bytes.

**Step 5: Run tests**

```bash
uv run pytest tests/test_context_resolution.py \
  tests/test_ai_recognition_config.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/context_resolution.py \
  src/telegram_kol_research/context_resolution_prompt.py \
  src/telegram_kol_research/ai_recognition_config.py \
  config/ai_recognition.example.yaml \
  tests/test_context_resolution.py tests/test_ai_recognition_config.py
git commit -m "feat: resolve contextual strategy threads with DeepSeek"
```

### Task 6: Route ambiguous messages through the second resolver

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py:160-225`
- Modify: `src/telegram_kol_research/message_recognition.py:1037-1260`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write failing orchestration tests**

Require second resolution when:

- text contains revision/cancel/entered-holder language;
- first pass has a management action without an exact target;
- multiple same-source candidates exist;
- reply target and first-pass target disagree;
- text/image evidence conflicts;
- an apparent entry may be a revision.

Require no second call for an unambiguous, independent new strategy.

**Step 2: Verify failures**

```bash
uv run pytest tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py -k "context_resolution or revision" -v
```

Expected: FAIL.

**Step 3: Add deterministic trigger logic**

Implement:

```python
def requires_context_resolution(
    *,
    first_pass_payload,
    evidence,
    context_window,
    candidates,
) -> tuple[bool, tuple[str, ...]]: ...
```

Closed trigger reasons must be auditable.

**Step 4: Apply resolution before creating candidates**

For `new_thread`, continue the existing entry path and create/link the thread.
For revise/manage/cancel/exit/hold, translate the selected thread into exact current
lifecycle targets, then call existing candidate upsert functions.

An unresolved result creates no executable `MessageInstructionItem`.

**Step 5: Preserve idempotency**

Use `(raw_message_id, evidence_version_id, context_fingerprint)` in the resolution
fingerprint. Retire stale unexecuted instruction items if a new evidence version changes
the decision. Never retire or replay a submitted/unknown/succeeded item.

**Step 6: Run tests**

```bash
uv run pytest tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py tests/test_message_instruction_items.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/recognition_decisions.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_message_instruction_items.py
git commit -m "feat: route contextual messages through second resolution"
```

### Task 7: Implement safe “update” semantics

**Files:**
- Create: `src/telegram_kol_research/strategy_revision_planner.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Test: `tests/test_strategy_revision_planner.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write the `1460 → 1462` replay test**

Create two pending split entry legs for `1460`, then resolve `1462` as a revision.
Assert:

- old pending legs are canceled first;
- no replacement submits until both cancellation outcomes are confirmed;
- confirmed filled legs become entered positions and are not canceled as orders;
- new parameters create replacement legs only for the remaining intended exposure;
- the same strategy thread is retained.

**Step 2: Add failure tests**

Cover:

- cancellation outcome unknown;
- one leg filled during cancellation;
- existing entered positions plus pending range leg;
- explicit “另开一单” creating a new thread;
- revision that would widen risk without a unique target.

**Step 3: Run tests**

```bash
uv run pytest tests/test_strategy_revision_planner.py \
  tests/test_deepcoin_execution_actions.py -k "revision or replace_entry" -v
```

Expected: FAIL.

**Step 4: Implement a durable revision state machine**

Use states:

```text
planned -> cancelling_old_entries -> old_entries_terminal
        -> submitting_replacements -> reconciling -> succeeded
```

Unknown exchange outcomes transition to `submit_unknown`/`recovery_required`; they do
not automatically retry.

**Step 5: Reuse existing order writers**

Do not add a new Deepcoin endpoint. Reuse exact cancel-trigger/cancel-order and entry
submission helpers. Every request must retain binding, leg, client-order, and thread
identity.

**Step 6: Run tests**

```bash
uv run pytest tests/test_strategy_revision_planner.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_recovery_live_submit_gate.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/strategy_revision_planner.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/strategy_management_planner.py \
  tests/test_strategy_revision_planner.py \
  tests/test_deepcoin_execution_actions.py
git commit -m "feat: safely revise pending strategy entries"
```

### Task 8: Implement precise cancel and “有入场的” fan-out semantics

**Files:**
- Modify: `src/telegram_kol_research/management_directives.py`
- Modify: `src/telegram_kol_research/management_scope.py:33-170`
- Modify: `src/telegram_kol_research/strategy_management_planner.py:181-840`
- Test: `tests/test_management_directives.py`
- Test: `tests/test_management_scope.py`
- Test: `tests/test_strategy_management_planner.py`

**Step 1: Write failing cancel tests**

Assert:

- reply to pending thread + “取消” cancels all pending legs;
- entered position + “取消挂单” cancels deferred entries but keeps live positions;
- “全部离场” creates full exit;
- canceled thread with a late verified fill requires the existing exact-position recovery
  path and never binds by symbol/time proximity.

**Step 2: Write failing risk-reducing fan-out tests**

For “有入场的止盈一半带保护”:

- include every same-thread/same-scope verified live position;
- exclude pending-only, exited, other-symbol, other-side, and unverified bindings;
- allow partial close and stop tightening;
- reject add, stop widening, protection removal, and new entries for multiple targets.

**Step 3: Run tests**

```bash
uv run pytest tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py -k "cancel or fanout or entered" -v
```

Expected: FAIL.

**Step 4: Implement thread-aware scope resolution**

Extend `ManagementScopeTarget` with `strategy_thread_id` and `scope_source`. Keep exact
position authority in execution binding/leg checks. A thread ID is not a position ID.

**Step 5: Run tests**

```bash
uv run pytest tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py \
  tests/test_position_management_remediation.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/management_directives.py \
  src/telegram_kol_research/management_scope.py \
  src/telegram_kol_research/strategy_management_planner.py \
  tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py
git commit -m "feat: apply thread-aware risk reducing management"
```

### Task 9: Make break-even deterministic from live average price

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/management_directives.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write the `4103` regression test**

Given live position:

```python
{"posId": "1001124367311625", "avgPx": "64478.5", "pos": "3"}
```

and action `move_stop_to_break_even`, assert planned stop is `64478.5`, not lifecycle
support `63600`.

For split positions, assert each leg uses its own `avgPx`.

**Step 2: Add explicit-price tests**

An explicit stop price may override average price only when:

- the resolver marks it as explicit current-message text;
- it tightens risk for that side;
- it passes tick-size normalization.

Image-only or historical-context price must not override break-even semantics.

**Step 3: Run tests**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py -k "break_even or breakeven" -v
```

Expected: FAIL for the `63600` regression fixture.

**Step 4: Implement minimal fix**

Represent semantic intent and explicit price separately:

```python
ManagementDirective(
    intent="move_stop_to_break_even",
    stop_loss=None,
    stop_price_source=None,
)
```

Resolve the final price only after the exact live position snapshot is validated.

**Step 5: Run tests**

```bash
uv run pytest tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_management_directives.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/management_directives.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_management_directives.py
git commit -m "fix: derive break even from exact live position"
```

### Task 10: Add automatic unresolved reanalysis

**Files:**
- Create: `src/telegram_kol_research/context_resolution_worker.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_context_resolution_worker.py`

**Step 1: Write failing worker tests**

Persist unresolved attempts and verify reanalysis is scheduled on:

- next same-chat message;
- missing reply target becoming available;
- entry leg status change;
- position/protection snapshot change;
- edited message/evidence version change.

Verify concurrent workers claim one generation exactly once.

**Step 2: Add non-retry safety tests**

No reanalysis may replay a `submitted`, `submit_unknown`, `succeeded`, or reconciled
instruction. The worker may only create a new decision for a new context fingerprint.

**Step 3: Run tests**

```bash
uv run pytest tests/test_context_resolution_worker.py -v
```

Expected: FAIL.

**Step 4: Implement durable claiming**

Use conditional updates and claim tokens, following the existing semantic-review worker
pattern. Add bounded attempts, next-at timestamps, and one final escalation notification.

**Step 5: Wire the worker**

Run one bounded pass from the listener loop and expose:

```bash
telegram-kol-research resolve-context-once --database-path data/research.db
```

The command must not execute exchange writes unless the ordinary global/group gates and
the selected resolution action allow them.

**Step 6: Run tests**

```bash
uv run pytest tests/test_context_resolution_worker.py \
  tests/test_lifecycle_monitor.py tests/test_authoritative_recognition.py -v
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/context_resolution_worker.py \
  src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/lifecycle_monitor.py \
  src/telegram_kol_research/cli.py \
  tests/test_context_resolution_worker.py
git commit -m "feat: automatically reanalyze unresolved context"
```

### Task 11: Add direct enablement settings and observability

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py:21-60`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Modify: `src/telegram_kol_research/templates/index.html:300-350`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_strategy_records.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing settings tests**

Add:

```python
context_resolution_enabled: bool = False
context_resolution_live_chat_ids: list[int] = []
```

Assert malformed values fail closed and absent legacy settings default to disabled.

**Step 2: Write failing UI/query tests**

The strategy detail must show:

- strategy thread root;
- linked revision/management messages and timestamps;
- reply chain;
- evidence version and image type;
- second-decision confidence and evidence IDs;
- unresolved reason and next reanalysis trigger;
- whether contextual automation was enabled for the message's group.

Do not render raw model payloads, image base64, credentials, or full exchange response
JSON.

**Step 3: Run tests**

```bash
uv run pytest tests/test_trading_settings.py \
  tests/test_web_strategy_records.py tests/test_web_page_render.py -v
```

Expected: FAIL.

**Step 4: Implement settings and projections**

Gate contextual action by the new boolean, the existing auto-trade/management gates,
and the chat allowlist. When disabled, the production listener must not call the
context resolver or persist background comparison results.

**Step 5: Run tests**

```bash
uv run pytest tests/test_trading_settings.py \
  tests/test_web_strategy_records.py tests/test_web_page_render.py \
  tests/test_web_app.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html \
  src/telegram_kol_research/templates/_strategy_detail.html \
  src/telegram_kol_research/templates/index.html \
  tests/test_trading_settings.py \
  tests/test_web_strategy_records.py tests/test_web_page_render.py
git commit -m "feat: gate contextual strategy resolution"
```

### Task 12: Add historical replay and complete local verification

**Files:**
- Create: `tests/fixtures/context_resolution/dpl_1460_1465.json`
- Create: `tests/test_context_resolution_replay.py`
- Modify: `docs/runbook.md`
- Modify: `README.md`

**Step 1: Add a redacted replay fixture**

Include:

- `1460` initial strategy;
- `1462` update;
- `1465` cancel;
- timestamps and reply metadata;
- standardized image evidence placeholders;
- pending/fill transitions;
- expected single strategy thread and terminal actions.

Do not include credentials, Telegram session data, or private media bytes.

**Step 2: Write end-to-end replay assertions**

Assert:

- `1462` revises the `1460` thread;
- `1465` cancels all still-pending legs in that thread;
- no old entry may submit or reconcile as active after confirmed cancellation;
- a late fill takes the exact recovery path;
- repeated replay is idempotent;
- disabled mode produces no AI or Deepcoin call.

**Step 3: Run focused tests**

```bash
uv run pytest tests/test_context_resolution_replay.py -v
```

Expected: PASS.

**Step 4: Run the full local suite**

```bash
uv run pytest tests -q
```

Expected: PASS. Any test requiring real credentials must remain skipped locally, not
mocked as a successful live verification.

**Step 5: Run static checks**

```bash
uv run python -m compileall -q src tests
git diff --check
```

Expected: both commands exit 0.

**Step 6: Update documentation**

Document:

- evidence extraction lifecycle;
- strategy-thread decisions;
- disabled/enabled gates and the group allowlist;
- unresolved automatic retry;
- read-only server audit commands;
- rollback procedure.

**Step 7: Commit**

```bash
git add tests/fixtures/context_resolution/dpl_1460_1465.json \
  tests/test_context_resolution_replay.py docs/runbook.md README.md
git commit -m "test: replay contextual strategy resolution"
```

### Task 13: Review, push, deploy disabled, and verify production

**Files:**
- Review all files changed by Tasks 1-12

**Step 1: Request code review**

Use @requesting-code-review. Resolve every correctness, ownership, idempotency, and
missing-test finding before pushing.

**Step 2: Re-run focused safety suites**

```bash
uv run pytest \
  tests/test_context_resolution_replay.py \
  tests/test_authoritative_recognition.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py -q
```

Expected: PASS.

**Step 3: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to the reviewed local SHA.

**Step 4: Deploy through the existing helper**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls the reviewed SHA, reinstalls the editable package, and restarts
`telegram-kol.service`.

**Step 5: Verify server health**

On the server:

```bash
cd /opt/telegram-kol-analyzer
systemctl is-active telegram-kol.service
git rev-parse HEAD
.venv/bin/python -m pytest \
  tests/test_context_resolution_replay.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py -q
```

Expected: service `active`, SHA matches the pushed commit, tests PASS.

**Step 6: Keep contextual resolution disabled**

Set:

```json
{
  "context_resolution_enabled": false,
  "context_resolution_live_chat_ids": []
}
```

The production listener must not call either AI resolver through the new path while
this switch is false.

**Step 7: Run a read-only production audit**

Verify for new and recent “大漂亮社区 11分组” messages:

- evidence versions are present;
- image facts are separated from text;
- reply chains resolve;
- `1460/1462/1465` replay resolves to one thread;
- the one-shot command returns the proposed thread/action without writing the source
  database;
- zero contextual instruction, AI background call, or Deepcoin write was created by
  the disabled production path;
- existing position/order attribution remains unchanged.

**Step 8: Run bounded offline fixtures and one-shot read-only audits**

Run redacted historical fixtures and explicit one-shot read-only audits covering:

- one update;
- one cancel or exit;
- one “有入场的” management action;
- one text+image message;
- one reply message.

The audit must operate on a coherent private database snapshot and make no source DB,
listener, instruction, notification, or exchange write. Review mismatches and fix them
through the normal local commit/push/deploy workflow.

**Step 9: Enable risk-reducing live actions for one group**

Only after offline and read-only evidence is reviewed, set
`context_resolution_enabled=true` and allow live contextual resolution for
`-1002805019371`, initially limited to:

- cancel confirmed pending entries;
- partial close;
- full exit;
- tighten stop / exact break-even;
- hold/no-op.

Keep new entry, add, stop widening, and protection removal gated to a unique
high-confidence target.

**Step 10: Verify the first live contextual action**

On the server, compare:

- source message and reply chain;
- evidence version;
- resolution decision;
- strategy thread;
- lifecycle/binding/entry legs;
- exact live `posId`;
- management batch and exchange order/TPSL readback.

Stop rollout on any cross-thread order mutation, unknown exchange result, missing
protection, or mismatch between confirmed strategy ownership and the live position.
