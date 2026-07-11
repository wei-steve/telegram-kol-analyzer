# Bound Position Close Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a confirmed, exact-position market close action to bound exchange-position cards.

**Architecture:** Add a server endpoint that accepts only `pos_id`, reloads/validates the active binding and live position, then uses the existing Deepcoin market-close payload semantics with an exact `closePosId`. Render the action only for bound cards and reuse the existing confirmation pattern with position-specific context.

**Tech Stack:** FastAPI, SQLAlchemy, Deepcoin REST client, Jinja, vanilla JavaScript, pytest.

---

### Task 1: Service closes only an exact bound live position

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_app.py`

**Step 1: Add failing tests**

Test that a POST endpoint accepting only `pos_id`:

- rejects an unbound or ambiguous position before placing an order;
- reloads the live position and submits one market order with its exact `closePosId` and complete live size;
- records an execution event but does not mark the lifecycle closed merely on submission.

**Step 2: Verify RED**

Run: `./.venv/bin/python -m pytest tests/test_web_app.py -k bound_position_close -v`

Expected: FAIL because the endpoint does not exist.

**Step 3: Minimal implementation**

Add a dedicated helper and endpoint. Do not accept symbol, direction, size, or trading-mode data from the browser. Query the active binding by exact position ID, fetch/reconcile the exchange position, derive the close payload, submit one `closePosId` market order, and record the event. Return a structured response suitable for card feedback.

**Step 4: Verify GREEN**

Run: `./.venv/bin/python -m pytest tests/test_web_app.py -k bound_position_close -v`

Expected: PASS.

### Task 2: Expose and confirm the card action

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Add failing tests**

Require the bound position card to render a `data-close-bound-position` button only for `attribution.state == "bound"`, with `pos_id`, symbol, side, and size context. Require JavaScript confirmation/binding and an endpoint fetch reference.

**Step 2: Verify RED**

Run: `./.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -k bound_position_close -v`

Expected: FAIL because card actions do not exist.

**Step 3: Minimal implementation**

Render one `市价平仓` button for bound cards. Reuse the existing native confirmation dialog, ensuring its text includes full-close context. On confirmation POST only `{pos_id}` to the endpoint; disable the card button while in flight, report success/failure accessibly, then refresh the exchange position view.

**Step 4: Verify GREEN**

Run: `./.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -k bound_position_close -v`

Expected: PASS.

### Task 3: Integrated verification and controlled deployment

**Step 1:** Run focused unit, render, and asset tests.

```bash
./.venv/bin/python -m pytest tests/test_web_app.py -k bound_position_close -v
./.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -k bound_position_close -v
```

**Step 2:** Run the full local suite and document any existing failures.

**Step 3:** Commit, push, run `./scripts/server_git_update.sh`, and confirm `telegram-kol.service` is active.

**Step 4:** Perform no live close automatically. The operator manually confirms the first production close from the card and checks its execution event and refreshed position state.
