# Mobile Page Scroll Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore reliable document scrolling on phone browsers while keeping the fixed mobile navigation accessible.

**Architecture:** Use the document as the only mobile scroll container. The existing fixed bottom nav remains in place; the dashboard receives bottom safe-area padding but no mobile viewport lock or vertical overflow rule.

**Tech Stack:** CSS media query, pytest static asset test.

---

### Task 1: Remove nested mobile scrolling

**Files:**
- Modify: `tests/test_web_assets_smoke.py:test_app_css_includes_mobile_work_mode_navigation`
- Modify: `src/telegram_kol_research/static/app.css:@media (max-width: 760px)`

**Step 1: Write the failing test**

Within the mobile media-query slice, assert that `.trader-layout` has safe-area bottom padding and does not contain `height: 100dvh`, `min-height: 100dvh`, or `overflow-y: auto`.

**Step 2: Verify RED**

Run: `./.venv/bin/python -m pytest tests/test_web_assets_smoke.py -k mobile_work_mode -v`

Expected: FAIL because the mobile layout currently uses all three nested-scroll declarations.

**Step 3: Minimal implementation**

Remove only the fixed viewport height and root vertical-scroll declarations from the mobile `.trader-layout` rule. Preserve horizontal overflow protection and bottom safe-area padding.

**Step 4: Verify GREEN**

Run: `./.venv/bin/python -m pytest tests/test_web_assets_smoke.py -k mobile_work_mode -v && git diff --check`

Expected: PASS.

**Step 5: Commit and deploy**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.css
git commit -m "fix: restore mobile dashboard page scrolling"
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```
