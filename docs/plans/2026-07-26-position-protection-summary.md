# Position Protection Summary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the compact current-position protection summary agree exactly with verified exchange TPSL orders.

**Architecture:** `web_app.py` already receives `direct_protection_rows` keyed by exact `pos_id`. Add a small pure summary projection that filters verified rows, classifies stop/take-profit prices, and sorts according to position side. The Jinja template renders that projection and removes the legacy backup-stop status from the summary path.

**Tech Stack:** Python 3, FastAPI/Jinja templates, pytest.

---

### Task 1: Specify verified exchange-summary behavior

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_position_tpsl_display.py` only if the projection is placed there

**Step 1: Write the failing test**

Create a long-position fixture with verified protection rows in non-display
order: stops `60878`, `61000`; take profits `70300`, `67100`, `68500`; and an
unverified/foreign row. Assert the rendered summary contains primary stop
`61000`, second stop `60878`, and `67100/68500/70300`, while excluding the
unverified row and the old `交易所未验证（旧通用条件单）` state.

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py -k protection_summary -q`

Expected: FAIL because the current page uses `protection` and legacy backup-stop data.

### Task 2: Build the exact-position summary projection

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: Implement the minimal projection**

Add a helper that accepts exact-position protection rows and side, keeps only
`verified` rows, maps `take_profit` and `stop_loss` kinds, parses valid numeric
trigger prices, deduplicates prices, and applies long/short sort direction.
Return primary stop, optional second stop, and all take-profit text values.

**Step 2: Wire it into position rows**

Use the helper for `stop_loss_text`, `backup_stop_text`, and
`take_profit_text`. Keep legacy backup-stop records available for audits but do
not use their status as a current-exchange summary fallback.

**Step 3: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_position_tpsl_display.py -q`

Expected: PASS.

### Task 3: Render the neutral absent state

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Test: `tests/test_web_page_render.py`

**Step 1: Update the template**

When there is one or zero verified stop, render `第二止损未设置`; do not render
legacy verification errors in the compact summary. Preserve the detailed list.

**Step 2: Run targeted tests**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py -k 'positions_panel and protection' -q`

Expected: PASS.

### Task 4: Full verification and deployment

**Files:**
- Modify: the files above only

**Step 1: Run regression tests**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_position_tpsl_display.py tests/test_current_protection_backfill.py -q`

Expected: PASS.

**Step 2: Commit and deploy**

Run:

```bash
git add docs/plans/2026-07-26-position-protection-summary-design.md docs/plans/2026-07-26-position-protection-summary.md src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/_exchange_positions_panel.html tests/test_web_page_render.py
git commit -m "fix: summarize verified position protection"
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

**Step 3: Verify production without mutation**

Fetch `/positions-panel` and confirm the `63894.1` long position renders
`止损 61000`, `第二止损 60878`, and `止盈 67200`, while the detailed list still
contains all three verified order IDs.
