# Exchange Position Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated dashboard tab that mirrors Deepcoin mobile's order/position area with `持仓`, `当前委托`, `历史委托`, and `历史仓位` sub-tabs.

**Architecture:** Reuse the existing top-level dashboard tab switcher for a new `exchange-positions` panel. Render the panel as a focused Jinja partial so `index.html` stays readable, and add a tiny client-side sub-tab switcher that only affects elements inside the new panel.

**Tech Stack:** FastAPI, Jinja2 templates, plain JavaScript, CSS, pytest/TestClient.

## Global Constraints

- Keep the new feature isolated from the existing KOL strategy middle panel.
- Do not change the existing `持仓 / 待入场 / 已离场` strategy filters.
- Use existing local query data for the first implementation; do not require new Deepcoin API calls.
- Render empty states when a tab has no reliable local data.
- Verify with focused render tests and relevant web page tests.

---

### Task 1: Lock the Render Contract

**Files:**
- Modify: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: `create_web_app(...)` and the existing `/` dashboard render.
- Produces: Tests that require the new top-level menu entry, panel marker, and four sub-tabs.

- [ ] **Step 1: Write the failing test**

Add these assertions to `test_index_page_shows_group_list_and_messages` after the existing dashboard panel assertions:

```python
    assert 'data-dashboard-tab="exchange-positions"' in response.text
    assert 'data-dashboard-panel="exchange-positions"' in response.text
    assert "交易持仓" in response.text
    assert response.text.index("持仓") < response.text.index("当前委托")
    assert response.text.index("当前委托") < response.text.index("历史委托")
    assert response.text.index("历史委托") < response.text.index("历史仓位")
    assert 'data-exchange-position-tabs' in response.text
    assert 'data-exchange-position-tab="positions"' in response.text
    assert 'data-exchange-position-tab="open-orders"' in response.text
    assert 'data-exchange-position-tab="order-history"' in response.text
    assert 'data-exchange-position-tab="position-history"' in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_page_render.py::test_index_page_shows_group_list_and_messages -q`

Expected: FAIL because the new dashboard tab and exchange position panel are not rendered yet.

- [ ] **Step 3: Commit**

Do not commit this task alone; it is a failing test. Carry it into Task 2.

### Task 2: Add the Isolated Exchange Positions Panel

**Files:**
- Create: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: Existing template variables `holding_positions`, `pending_entry_signals`, `exited_positions`, and `strategy_kpi`.
- Produces: New DOM hooks `data-dashboard-panel="exchange-positions"`, `data-exchange-position-tabs`, `data-exchange-position-tab`, and `data-exchange-position-panel`.

- [ ] **Step 1: Add the panel partial**

Create `src/telegram_kol_research/templates/_exchange_positions_panel.html` with a contained panel. It must render the four sub-tabs in this exact order:

```html
<section class="panel exchange-positions-panel" data-exchange-position-tabs>
  <div class="panel-heading">
    <div>
      <h2>交易持仓</h2>
      <p class="ai-helper-text">按 Deepcoin 手机端样式查看本地已同步的仓位、当前委托和历史记录。</p>
    </div>
    <button type="button" class="secondary-button" data-dashboard-tab="main">返回主界面</button>
  </div>

  <div class="exchange-tab-strip" role="tablist" aria-label="交易持仓分页">
    <button type="button" class="exchange-tab is-active" data-exchange-position-tab="positions" aria-selected="true">持仓{% if strategy_kpi.holding_count is defined %}({{ strategy_kpi.holding_count }}){% endif %}</button>
    <button type="button" class="exchange-tab" data-exchange-position-tab="open-orders" aria-selected="false">当前委托{% if strategy_kpi.pending_count is defined %}({{ strategy_kpi.pending_count }}){% endif %}</button>
    <button type="button" class="exchange-tab" data-exchange-position-tab="order-history" aria-selected="false">历史委托</button>
    <button type="button" class="exchange-tab" data-exchange-position-tab="position-history" aria-selected="false">历史仓位{% if strategy_kpi.exited_count is defined %}({{ strategy_kpi.exited_count }}){% endif %}</button>
  </div>

  <div class="exchange-tab-panels">
    <section class="exchange-tab-panel is-active" data-exchange-position-panel="positions">
      <div class="exchange-card-list">
        {% set live_positions = holding_positions | list %}
        {% for item in live_positions %}
          <article class="exchange-position-card">
            <div class="exchange-card-head">
              <div>
                <strong>{{ item.symbol }}</strong>
                <span class="side-badge{% if item.side == 'long' %} side-long{% elif item.side == 'short' %} side-short{% endif %}">{{ '多' if item.side == 'long' else '空' if item.side == 'short' else item.side }}</span>
              </div>
              <code>持仓</code>
            </div>
            <dl class="exchange-metric-grid">
              {% if item.entry_text %}<div><dt>开仓均价</dt><dd>{{ item.entry_text }}</dd></div>{% endif %}
              {% if item.position_size_text %}<div><dt>数量</dt><dd>{{ item.position_size_text }}</dd></div>{% endif %}
              {% if item.stop_loss_text %}<div><dt>止损</dt><dd>{{ item.stop_loss_text }}</dd></div>{% endif %}
              {% if item.take_profit_text %}<div><dt>止盈</dt><dd>{{ item.take_profit_text }}</dd></div>{% endif %}
              {% if item.last_checked_at_display %}<div><dt>更新时间</dt><dd>{{ item.last_checked_at_display }}</dd></div>{% endif %}
            </dl>
          </article>
        {% else %}
          <p class="exchange-empty">暂无持仓</p>
        {% endfor %}
      </div>
    </section>
    <section class="exchange-tab-panel" data-exchange-position-panel="open-orders">
      <div class="exchange-card-list">
        {% set pending_orders = pending_entry_signals | list %}
        {% for item in pending_orders %}
          <article class="exchange-position-card exchange-position-card--pending">
            <div class="exchange-card-head">
              <div>
                <strong>{{ item.symbol }}</strong>
                <span class="side-badge{% if item.side == 'long' %} side-long{% elif item.side == 'short' %} side-short{% endif %}">{{ '多' if item.side == 'long' else '空' if item.side == 'short' else item.side }}</span>
              </div>
              <code>当前委托</code>
            </div>
            <dl class="exchange-metric-grid">
              {% if item.entry_range_text %}<div><dt>委托价格</dt><dd>{{ item.entry_range_text }}</dd></div>{% endif %}
              {% if item.position_size_text %}<div><dt>数量</dt><dd>{{ item.position_size_text }}</dd></div>{% endif %}
              {% if item.stop_loss_text %}<div><dt>止损</dt><dd>{{ item.stop_loss_text }}</dd></div>{% endif %}
              {% if item.take_profit_text %}<div><dt>止盈</dt><dd>{{ item.take_profit_text }}</dd></div>{% endif %}
            </dl>
          </article>
        {% else %}
          <p class="exchange-empty">暂无当前委托</p>
        {% endfor %}
      </div>
    </section>
    <section class="exchange-tab-panel" data-exchange-position-panel="order-history">
      <p class="exchange-empty">历史委托等待接入 Deepcoin 委托历史接口。</p>
    </section>
    <section class="exchange-tab-panel" data-exchange-position-panel="position-history">
      <div class="exchange-card-list">
        {% set history_positions = exited_positions | list %}
        {% for item in history_positions %}
          <article class="exchange-position-card exchange-position-card--history">
            <div class="exchange-card-head">
              <div>
                <strong>{{ item.symbol }}</strong>
                <span class="side-badge{% if item.side == 'long' %} side-long{% elif item.side == 'short' %} side-short{% endif %}">{{ '多' if item.side == 'long' else '空' if item.side == 'short' else item.side }}</span>
              </div>
              <code>{{ item.exit_reason or item.lifecycle_status or '历史仓位' }}</code>
            </div>
            <dl class="exchange-metric-grid">
              {% if item.entry_text %}<div><dt>开仓均价</dt><dd>{{ item.entry_text }}</dd></div>{% endif %}
              {% if item.exit_price_actual %}<div><dt>平仓均价</dt><dd>{{ "%g"|format(item.exit_price_actual) }}</dd></div>{% endif %}
              {% if item.position_size_text %}<div><dt>数量</dt><dd>{{ item.position_size_text }}</dd></div>{% endif %}
              {% if item.entered_at_display %}<div><dt>开仓时间</dt><dd>{{ item.entered_at_display }}</dd></div>{% endif %}
              {% if item.exited_at_display %}<div><dt>最后平仓时间</dt><dd>{{ item.exited_at_display }}</dd></div>{% endif %}
            </dl>
          </article>
        {% else %}
          <p class="exchange-empty">暂无历史仓位</p>
        {% endfor %}
      </div>
    </section>
  </div>
</section>
```

- [ ] **Step 2: Wire the top-level dashboard panel**

In `src/telegram_kol_research/templates/index.html`, add this menu item inside `.settings-menu`:

```html
          <button type="button" class="settings-menu-item" data-dashboard-tab="exchange-positions">交易持仓</button>
```

Then add this top-level panel before the existing `data-dashboard-panel="main"` section:

```html
  <section class="dashboard-tab-panel" data-dashboard-panel="exchange-positions">
    {% with
      strategy_kpi=strategy_kpi,
      holding_positions=holding_positions,
      pending_entry_signals=pending_entry_signals,
      exited_positions=exited_positions
    %}
      {% include "_exchange_positions_panel.html" %}
    {% endwith %}
  </section>
```

- [ ] **Step 3: Add dashboard panel CSS and exchange card styles**

In `src/telegram_kol_research/static/app.css`, include `exchange-positions` in the dashboard panel overflow selectors and add `.exchange-*` styles for tab strip, panels, cards, metric grid, and empty states.

- [ ] **Step 4: Add sub-tab switching JavaScript**

In `src/telegram_kol_research/static/app.js`, add a `bindExchangePositionTabs()` function that scopes to `[data-exchange-position-tabs]`, toggles `.is-active` on buttons and panels, and updates `aria-selected`.

Call `bindExchangePositionTabs()` from `init()` with the other binders.

- [ ] **Step 5: Run render test**

Run: `pytest tests/test_web_page_render.py::test_index_page_shows_group_list_and_messages -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_web_page_render.py src/telegram_kol_research/templates/index.html src/telegram_kol_research/templates/_exchange_positions_panel.html src/telegram_kol_research/static/app.css src/telegram_kol_research/static/app.js
git commit -m "Add exchange position dashboard tab"
```

### Task 3: Verify the Web Surface

**Files:**
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Interfaces:**
- Consumes: Rendered dashboard HTML and static assets.
- Produces: Confidence that existing page rendering and static asset loading still pass.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -q
```

Expected: PASS.

- [ ] **Step 2: Run git status**

Run: `git status --short`

Expected: no unexpected unstaged changes beyond intended implementation files.

- [ ] **Step 3: If tests pass, stop**

Do not push or deploy in this task. Deployment remains a separate explicit action using the project workflow.
