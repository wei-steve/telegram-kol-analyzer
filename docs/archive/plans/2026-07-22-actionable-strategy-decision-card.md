# Actionable Strategy Decision Card Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show complete new strategies and safely linked lifecycle messages as positive states instead of generic manual review.

**Architecture:** Extend `_build_message_decision_card` with read-only classification predicates based on the authoritative payload and the semantic-review result. Keep missing stop prices and disagreement/ambiguity safeguards ahead of the positive-state branches.

**Tech Stack:** Python, FastAPI view model, pytest.

---

### Task 1: Add failing classification tests

**Files:**
- Modify: `tests/test_web_queries_messages.py`
- Modify: `src/telegram_kol_research/web_queries.py:701-812`

**Step 1:** Test a complete strategy payload maps to `strategy_identified` / `策略已识别`.

**Step 2:** Test an agreed lifecycle event with one `target_lifecycle_id` maps to `strategy_linked` / `已关联策略`.

**Step 3:** Run `uv run pytest tests/test_web_queries_messages.py -k 'strategy_identified or strategy_linked' -q` and verify failure.

**Step 4:** Add the minimal classification predicates, retaining existing manual-review guards.

**Step 5:** Run the focused and scoped regression suite, then deploy.
