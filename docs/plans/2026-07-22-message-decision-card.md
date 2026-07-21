# Message Decision Card Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the overloaded “AI识别结果” detail with a decision-first message card that safely separates the authoritative MiMo analysis, DeepSeek review, message facts, inherited strategy context, and exchange execution state.

**Architecture:** Keep all execution and recognition behavior unchanged. Add a presentation-only `decision_card` view model in `web_queries.py`, derived from the already-loaded `RecognitionDecision`, `SignalCandidate`, `StrategyLifecycle`, and `StrategyManagementBatch` records. Render it in `_messages.html` ahead of the existing debugging/history block, then use focused CSS to establish the approved three-layer hierarchy.

**Tech Stack:** Python 3.14, SQLAlchemy, FastAPI/Jinja templates, vanilla JavaScript, CSS, pytest.

---

### Task 1: Define the presentation contract with failing query tests

**Files:**
- Modify: `tests/test_web_queries_messages.py`
- Modify: `src/telegram_kol_research/web_queries.py:450-820`

**Step 1: Write the failing tests**

Add tests that persist one `RawMessage`, an authoritative MiMo `RecognitionDecision`, a completed DeepSeek comparison payload, a `SignalCandidate`, and a uniquely linked `StrategyLifecycle`. Assert `load_group_messages()` returns a `decision_card` with this shape:

```python
assert row["decision_card"] == {
    "state": "manual_review",
    "state_label": "需人工确认",
    "recommended_action": "不执行",
    "blocker": "未提供新的止损价格",
    "strategy": {"lifecycle_id": lifecycle.id, "summary": "BTC 空单 #<id>"},
    "message_facts": [],
    "inherited_context": [
        {"label": "入场", "value": "65200–65500"},
        {"label": "当前止损", "value": "65350"},
    ],
    "primary_analysis": {
        "label": "主分析 · MiMo",
        "conclusion": "仓位管理",
        "reason": "识别到调整止损意图；未提取到新止损价。",
    },
    "secondary_review": {
        "label": "辅助复核 · DeepSeek",
        "conclusion": "需人工确认",
        "reason": "同意不可自动执行；建议补充价格后再处理。",
    },
    "agreement": {"label": "一致 · 不自动执行", "tone": "agreed"},
    "execution": {"label": "未发送交易所请求", "state": "not_executed", "detail": None},
}
```

Also add focused cases for media unavailable (`state == "fetch_failed"`), pure non-strategy content (`state == "record_only"`), and conflicting review text. The last case must prove that a completed review whose payload says no material difference is never serialised with `agreement["tone"] == "critical"`.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_queries_messages.py -q`

Expected: FAIL because `decision_card` is not present.

**Step 3: Implement the smallest serializer**

In `src/telegram_kol_research/web_queries.py`, add a pure `_build_message_decision_card(...)` helper. Pass it the data already loaded for each row; do not add N+1 queries.

Implement stable presentation enums:

```python
DECISION_CARD_STATES = {
    "auto_executable", "manual_review", "record_only", "blocked", "fetch_failed"
}
```

Rules:

- Obtain the primary analysis from `RecognitionDecision.authoritative_*`; label it MiMo only when the authoritative model is MiMo. Never treat old `RecognitionExperiment` rows as production evidence.
- Obtain the auxiliary review from `comparison_*`; label it DeepSeek only when a completed comparison exists.
- Parse `authoritative_payload_json` defensively. Emit direct message facts only when a value is explicit in that payload; inherited strategy values must come from `StrategyLifecycle` and be marked as inherited.
- Treat a missing target price for a management/update action as `manual_review` with recommended action `不执行`.
- Reuse `_serialize_execution_outcome()` and show a neutral `未发送交易所请求` record when no outcome exists.
- Build the agreement label from structured severity plus the comparison payload. If the payload says no material disagreement, normalise the display tone to `agreed`; do not alter persisted audit data.
- Return `None` only where no recognition/decision/strategy evidence exists, allowing old unrecognised rows to keep their compact display.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_queries_messages.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py tests/test_web_queries_messages.py
git commit -m "feat: serialize message decision cards"
```

### Task 2: Render the three-layer decision card and preserve current fallbacks

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html:50-295`
- Modify: `tests/test_web_group_messages_route.py`

**Step 1: Write the failing route-render tests**

Create a route fixture containing the Task 1 manual-review case. Assert that the response contains, in this order:

```python
assert "需人工确认" in response.text
assert "建议动作：不执行" in response.text
assert "未提供新的止损价格" in response.text
assert "本消息新增：无" in response.text
assert "已有策略（继承）" in response.text
assert "主分析 · MiMo" in response.text
assert "辅助复核 · DeepSeek" in response.text
assert "结论一致 · 不自动执行" in response.text
assert "未发送交易所请求" in response.text
assert response.text.index("需人工确认") < response.text.index("主分析 · MiMo")
```

Assert the response does not put `mimo-v2.5`, `deepseek-v4-flash`, `n/a`, or raw `strategy_json` in the decision card. Keep a separate assertion that the existing historical experiment section remains available below the card and is labelled as diagnostic history.

**Step 2: Run the route tests to verify they fail**

Run: `uv run pytest tests/test_web_group_messages_route.py -q`

Expected: FAIL because the decision card markup is absent.

**Step 3: Render the approved hierarchy**

In `_messages.html`:

1. Set `decision_card = message.decision_card` near the existing per-message variables.
2. For non-`None` cards, render a `section.message-decision-card` before `details.message-ai-insights`.
3. Render the always-visible final-decision region with state, recommended action, blocker, unique strategy link, and confidence only when supplied.
4. Render a fact/context region with exact labels `本消息新增` and `已有策略（继承）`. Never place inherited values in the message-fact list.
5. Render execution status as a third factual row, using the serialised state only.
6. Render a default-closed native `details.message-model-analysis` containing MiMo, DeepSeek, and one agreement row. Use `summary` text `模型分析（可展开）`; do not reveal internal model versions or JSON.
7. Keep the old `recognition-comparison` section only as default-closed `历史实验（调试）`, and do not use it to decide default expansion or the top-line status.
8. Preserve existing `查看策略记录` and `立即识别` controls. Do not add submit, edit, or manual-confirm action handlers in this change.

**Step 4: Run the route tests to verify they pass**

Run: `uv run pytest tests/test_web_group_messages_route.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/templates/_messages.html tests/test_web_group_messages_route.py
git commit -m "feat: render decision-first message cards"
```

### Task 3: Apply the dark control-room visual hierarchy and responsive behavior

**Files:**
- Modify: `src/telegram_kol_research/static/app.css:2740-3230`
- Modify: `tests/test_web_group_messages_route.py`

**Step 1: Add static-structure assertions**

Extend route assertions to require the decision card state modifier and model-analysis disclosure classes:

```python
assert 'class="message-decision-card is-manual-review"' in response.text
assert 'class="message-model-analysis"' in response.text
assert 'class="message-decision-card-facts"' in response.text
```

**Step 2: Run the route tests to verify they fail**

Run: `uv run pytest tests/test_web_group_messages_route.py -q`

Expected: FAIL because the new class names are absent.

**Step 3: Add only scoped CSS**

Add styles adjacent to the existing message-card rules:

- `.message-decision-card` uses a subtle surface, one left state border, readable 14–16px content, and spacing rather than nested cards.
- State modifiers use green only for confirmed/safe, amber for `manual_review`, neutral slate for `record_only`/`fetch_failed`, and red only for explicitly blocked execution errors.
- `.message-decision-card-facts` draws source-labelled rows with inheritance visually muted, never hidden.
- `.message-model-analysis` is a compact `details` area and has a visible focus outline on its summary.
- Use `@media (max-width: 760px)` to stack decision fields and action links without truncating their content.
- Remove the old production-facing red/green overload by keeping `.semantic-review` and `.recognition-comparison` visually subordinate to the new card.

Do not change global button, sidebar, group-list, or execution panel styles.

**Step 4: Run focused tests**

Run: `uv run pytest tests/test_web_group_messages_route.py tests/test_web_queries_messages.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/static/app.css tests/test_web_group_messages_route.py
git commit -m "style: prioritize message decision state"
```

### Task 4: Verify the no-side-effect UI integration locally and on the server

**Files:**
- Modify: `docs/plans/2026-07-22-message-decision-card.md` only if verification exposes a design decision that must be documented.

**Step 1: Run the complete relevant local test set**

Run:

```bash
uv run pytest \
  tests/test_web_queries_messages.py \
  tests/test_web_group_messages_route.py \
  tests/test_semantic_disagreement_review.py \
  tests/test_authoritative_recognition.py -q
```

Expected: PASS. No network identity, Telegram session, or production key is needed.

**Step 2: Inspect the local rendered partial**

Start the existing local web app using the project’s documented development command, seed the test fixture if necessary, and check that:

- the final-decision layer appears before model evidence;
- inherited values visibly say `已有策略（继承）`;
- MiMo and DeepSeek are present but collapsed by default;
- history remains out of the primary decision path;
- no control can send or amend an order.

**Step 3: Commit any verification-only documentation change**

```bash
git status --short
git add docs/plans/2026-07-22-message-decision-card.md
git commit -m "docs: record decision card verification"
```

Only commit if Task 4 changed the plan; otherwise do not create an empty commit.

**Step 4: Push and perform required server verification**

After review, push the reviewed commits to `codex/deepcoin-auto-trading-v1`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

On the server, verify the live page with a known manual-review message. Confirm the UI is presentation-only and that `telegram-kol.service` is healthy after restart.
