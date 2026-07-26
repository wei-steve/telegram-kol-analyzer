# Compact Position Summary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove duplicated TPSL values from the compact current-position summary.

**Architecture:** Keep the verified TPSL summary calculation for the detailed
list and future API consumers, but remove the stop, backup-stop, and take-profit
template fields from the card grid. The detailed `止盈止损(n)` section remains the
single visual representation of those orders.

**Tech Stack:** Jinja2, pytest.

---

### Task 1: Lock the compact layout with a failing render test

**Files:**
- Modify: `tests/test_web_page_render.py`

1. Add a test fixture with verified stop and take-profit orders.
2. Assert the detailed `止盈止损` section includes them, but the summary grid does
   not contain `第二止损` or `止盈` labels.
3. Run the focused test and confirm it fails.

### Task 2: Remove duplicated template fields

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`

1. Remove the stop-loss, second-stop, and take-profit `<dt>/<dd>` entries from
   the compact grid.
2. Run the focused test, then the position-render regression suite.
3. Commit, push, deploy with `./scripts/server_git_update.sh`, and verify
   `/positions-panel` shows only average price and quantity in the grid.
