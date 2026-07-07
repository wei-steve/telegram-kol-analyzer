# Exchange Strategy Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add switchable real-list and grouped-by-group views to the exchange positions page, with strategy attribution on each real Deepcoin position and order.

**Architecture:** Build attribution in `web_app.py` after the Deepcoin snapshot is loaded, using existing strategy rows and binding fields as inputs. Render one reusable Jinja card macro in `_exchange_positions_panel.html`, then use JavaScript to switch both the existing item tabs and the new real/group view mode.

**Tech Stack:** FastAPI, SQLAlchemy-backed strategy rows already serialized by `web_app.py`, Jinja2 templates, vanilla JavaScript, CSS, pytest with FastAPI `TestClient`.

## Global Constraints

- First version is display-only: do not add manual bind or correction actions.
- Preserve existing exchange tabs: positions, open orders, order history, position history.
- Keep unmatched real positions and orders visible under an unassigned group.
- Prefer conservative attribution: bound data wins, inferred candidates must be clear, ambiguous rows stay unassigned.
- Live Deepcoin verification must run on the server because credentials and IP allowlist only work there.

---

### Task 1: Build Exchange Attribution Data

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: `_load_exchange_position_snapshot(...)`, `holding_positions`, `pending_entry_signals`, `exited_positions`, `group_label_by_chat_id`.
- Produces: `_annotate_exchange_snapshot_attribution(snapshot, holding_positions, pending_entry_signals, exited_positions, group_label_by_chat_id) -> dict[str, Any]`.
- Produces per item: `item["attribution"]` with `state`, `label`, `chat_id`, `group_name`, `strategy_summary`, `source_excerpt`, `score`, `reasons`, `order_role`.
- Produces snapshot grouping: `snapshot["grouped"][tab_key]` where `tab_key` is `positions`, `open_orders`, `order_history`, or `position_history`.

- [ ] **Step 1: Write the failing bound-position test**

Add a test that creates a live Deepcoin position with a matching `ExecutionBinding` and `StrategyLifecycle`, renders `/`, and asserts the exchange position card includes the group attribution and grouped-view markup:

```python
def test_exchange_position_attribution_shows_bound_group_and_grouped_view(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=56,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-56",
            pos_id="pos-live-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=56,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                entry_price_actual=62400,
                stop_loss=60800,
                take_profit="63600",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-live-1",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "62400",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Alpha Group",
                        chat_id=100,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert 'data-exchange-view-mode="list"' in response.text
    assert 'data-exchange-view-mode="grouped"' in response.text
    assert "已绑定" in response.text
    assert "Alpha Group" in response.text
    assert "BTC long" in response.text
    assert 'data-exchange-group-section' in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_page_render.py::test_exchange_position_attribution_shows_bound_group_and_grouped_view -q`

Expected: FAIL because the new view mode and attribution markup do not exist yet.

- [ ] **Step 3: Implement attribution helpers**

Add helper functions near `_load_exchange_position_snapshot(...)`:

```python
def _annotate_exchange_snapshot_attribution(
    snapshot: dict[str, Any],
    *,
    holding_positions: list[dict[str, Any]],
    pending_entry_signals: list[dict[str, Any]],
    exited_positions: list[dict[str, Any]],
    group_label_by_chat_id: dict[int, str],
) -> dict[str, Any]:
    strategy_rows = {
        "holding": holding_positions,
        "pending": pending_entry_signals,
        "exited": exited_positions,
    }
    for item in snapshot.get("positions", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=[*strategy_rows["holding"], *strategy_rows["pending"]],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=None,
        )
    for item in snapshot.get("open_orders", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=[*strategy_rows["pending"], *strategy_rows["holding"]],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=_infer_exchange_order_role(item),
        )
    for item in snapshot.get("order_history", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=[*strategy_rows["exited"], *strategy_rows["holding"], *strategy_rows["pending"]],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=_infer_exchange_order_role(item),
        )
    for item in snapshot.get("position_history", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=strategy_rows["exited"],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=None,
        )
    snapshot["grouped"] = {
        "positions": _group_exchange_items(snapshot.get("positions", [])),
        "open_orders": _group_exchange_items(snapshot.get("open_orders", [])),
        "order_history": _group_exchange_items(snapshot.get("order_history", [])),
        "position_history": _group_exchange_items(snapshot.get("position_history", [])),
    }
    return snapshot
```

Also add `_exchange_item_attribution`, `_bound_exchange_attribution`, `_candidate_exchange_attribution`, `_strategy_summary`, `_strategy_excerpt`, `_score_exchange_candidate`, `_exchange_item_price`, `_group_exchange_items`, and `_infer_exchange_order_role`.

- [ ] **Step 4: Wire attribution into the index route**

After `exchange_snapshot["position_history"] = exited_positions`, call:

```python
exchange_snapshot = _annotate_exchange_snapshot_attribution(
    exchange_snapshot,
    holding_positions=holding_positions,
    pending_entry_signals=pending_entry_signals,
    exited_positions=exited_positions,
    group_label_by_chat_id=group_label_by_chat_id,
)
```

- [ ] **Step 5: Run the failing test again**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_page_render.py::test_exchange_position_attribution_shows_bound_group_and_grouped_view -q`

Expected: still FAIL until Task 2 adds template markup.

### Task 2: Render View Switch, Attribution Cards, And Grouped Sections

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: `item.attribution` and `exchange_snapshot.grouped`.
- Produces: view mode buttons with `data-exchange-view-mode`.
- Produces: list containers with `data-exchange-view-panel`.
- Produces: grouped sections with `data-exchange-group-section`.

- [ ] **Step 1: Add reusable card macros**

At the top of `_exchange_positions_panel.html`, add Jinja macros for attribution and for exchange cards so the list view and group view render the same card body:

```jinja2
{% macro exchange_attribution(item) -%}
  {% set attr = item.attribution if item.attribution is defined else none %}
  {% if attr %}
    <div class="exchange-attribution exchange-attribution--{{ attr.state }}">
      <span class="exchange-attribution-chip">{{ attr.label }}</span>
      {% if attr.group_name %}<span>{{ attr.group_name }}</span>{% endif %}
      {% if attr.strategy_summary %}<span>{{ attr.strategy_summary }}</span>{% endif %}
      {% if attr.order_role %}<code>{{ attr.order_role }}</code>{% endif %}
      {% if attr.reasons %}<small>{{ attr.reasons | join('，') }}</small>{% endif %}
    </div>
  {% endif %}
{%- endmacro %}
```

Use the macro inside all position and order cards.

- [ ] **Step 2: Add view-mode buttons**

Add a compact switch below the tab strip:

```jinja2
<div class="exchange-view-switch" role="tablist" aria-label="交易持仓视图">
  <button type="button" class="exchange-view-button is-active" data-exchange-view-mode="list" aria-selected="true">真实列表</button>
  <button type="button" class="exchange-view-button" data-exchange-view-mode="grouped" aria-selected="false">按群组</button>
</div>
```

- [ ] **Step 3: Add grouped panels**

For each existing tab panel, render two body containers:

```jinja2
<div class="exchange-view-panel is-active" data-exchange-view-panel="list">
  ... existing card list ...
</div>
<div class="exchange-view-panel" data-exchange-view-panel="grouped">
  {% for group in exchange_snapshot.grouped.positions %}
    <section class="exchange-group-section" data-exchange-group-section>
      <header class="exchange-group-header">
        <strong>{{ group.group_name }}</strong>
        <span>{{ group.items|length }}</span>
      </header>
      <div class="exchange-card-list">
        {% for item in group.items %}
          ... same card macro ...
        {% endfor %}
      </div>
    </section>
  {% else %}
    <p class="exchange-empty">暂无持仓</p>
  {% endfor %}
</div>
```

- [ ] **Step 4: Extend JavaScript binding**

In `bindExchangePositionTabs()`, add view-mode support scoped to each exchange root:

```javascript
const viewButtons = root.querySelectorAll('[data-exchange-view-mode]');
const viewPanels = root.querySelectorAll('[data-exchange-view-panel]');
viewButtons.forEach((button) => {
  button.addEventListener('click', () => {
    const mode = button.dataset.exchangeViewMode || 'list';
    viewButtons.forEach((item) => {
      const isActive = item === button;
      item.classList.toggle('is-active', isActive);
      item.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    viewPanels.forEach((panel) => {
      panel.classList.toggle('is-active', panel.dataset.exchangeViewPanel === mode);
    });
  });
});
```

- [ ] **Step 5: Add CSS for attribution and grouped view**

Add styles for `.exchange-view-switch`, `.exchange-view-button`, `.exchange-view-panel`, `.exchange-attribution`, `.exchange-attribution-chip`, `.exchange-group-section`, and `.exchange-group-header`.

- [ ] **Step 6: Run template test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_page_render.py::test_exchange_position_attribution_shows_bound_group_and_grouped_view tests/test_web_page_render.py::test_exchange_position_tab_uses_deepcoin_account_snapshot -q`

Expected: PASS.

### Task 3: Add Candidate And Unassigned Coverage

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify as needed: `src/telegram_kol_research/web_app.py`

**Interfaces:**
- Consumes: attribution scoring helpers from Task 1.
- Produces: stable candidate and unassigned behavior.

- [ ] **Step 1: Add candidate order test**

Create a pending strategy and a matching open order with the same symbol, side, and nearby price. Assert `可能归属`, group name, and `data-exchange-group-section`.

- [ ] **Step 2: Add unassigned order test**

Create an open order for a symbol/side with no strategy candidate. Assert `未归属`, order id, and a grouped section for unassigned items.

- [ ] **Step 3: Tune scoring if needed**

Keep scoring conservative:

```python
if score >= 70 and not tied:
    return candidate_attribution
return unassigned_attribution
```

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_page_render.py::test_exchange_position_attribution_shows_bound_group_and_grouped_view tests/test_web_page_render.py::test_exchange_current_order_candidate_attribution tests/test_web_page_render.py::test_exchange_unmatched_order_stays_unassigned -q`

Expected: PASS.

### Task 4: Verify, Commit, Push, And Deploy

**Files:**
- Modify: no new implementation files unless tests reveal fixes.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: committed, pushed, deployed feature.

- [ ] **Step 1: Run focused web tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_page_render.py::test_exchange_position_tab_uses_deepcoin_account_snapshot tests/test_web_page_render.py::test_exchange_position_attribution_shows_bound_group_and_grouped_view tests/test_web_page_render.py::test_exchange_current_order_candidate_attribution tests/test_web_page_render.py::test_exchange_unmatched_order_stays_unassigned tests/test_web_assets_smoke.py -q`

Expected: all selected tests pass.

- [ ] **Step 2: Check git diff**

Run: `git diff --stat`

Expected: only the plan, `web_app.py`, `_exchange_positions_panel.html`, `app.js`, `app.css`, and tests changed.

- [ ] **Step 3: Commit**

Run:

```powershell
git add docs\superpowers\plans\2026-07-07-exchange-strategy-attribution.md src\telegram_kol_research\web_app.py src\telegram_kol_research\templates\_exchange_positions_panel.html src\telegram_kol_research\static\app.js src\telegram_kol_research\static\app.css tests\test_web_page_render.py
git commit -m "Add exchange strategy attribution view"
```

- [ ] **Step 4: Push to GitHub**

Run: `git push origin HEAD:codex/deepcoin-auto-trading-v1`

- [ ] **Step 5: Deploy to server**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1`

Expected: server reports `telegram-kol.service` active and running.

- [ ] **Step 6: Verify server HTML**

Run a server-local request over SSH and assert the page contains `data-exchange-view-mode="grouped"`, attribution labels, and real exchange counts.
