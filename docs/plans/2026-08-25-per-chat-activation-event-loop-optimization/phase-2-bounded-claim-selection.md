# Phase 2 Bounded Claim Selection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop loading every pending/claimed message job inside `BEGIN IMMEDIATE` while preserving exact lane and recovery semantics.

**Architecture:** Replace only the unbounded candidate read with one SQLite CTE that returns at most the available-lane limit. Retain the current transaction, conditional row updates, claim tokens, and single-worker scheduling.

**Tech Stack:** Python, SQLAlchemy text SQL, SQLite, pytest.

---

## Task 1: Claim and characterize

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`
- Modify: `tests/test_message_processing_worker.py`

1. Require Phase 1 complete at its exact committed SHA, a clean exclusive worktree, and the status pointer naming this file; commit the Phase 2 claim.
2. Add RED tests proving Python receives no more than `limit` candidates with a large backlog and that the old unbounded `.all()` path is no longer acceptable.
3. Add or tighten semantic cases: large same-chat backlog, no overtaking when the oldest retry is not due, live-claim blocking, stale reclaim, progress in another chat, two-worker competition, limit enforcement, and restart recovery.

## Task 2: Implement the bounded selection

**Files:**

- Modify: `src/telegram_kol_research/message_processing_worker.py`

1. Keep `BEGIN IMMEDIATE` and `claim_limit == 0` behavior.
2. Use a CTE/window selection to identify the lowest `raw_message_id` nonterminal, non-shadow row for each chat.
3. Filter only that owner row to due `pending` or stale `claimed`, order deterministically, and apply `LIMIT :claim_limit` in SQL.
4. Feed the bounded rows into the existing conditional update and claim construction. Preserve stale reason, tokens, attempt count, timestamps, and commit boundary.
5. Do not add an index, migration, model, fallback query, or new abstraction layer.

## Task 3: Verify and close

1. Run the full message-processing worker test file plus the focused shadow-enqueue, settings-cap, process-role, and event-loop census slices affected by the change.
2. Run `git diff --check` and compile the touched source/test modules.
3. Confirm `EXPLAIN QUERY PLAN` on a representative temporary test database completes and the selected row count never exceeds the limit; this is diagnostic evidence, not an index requirement.
4. Update the canonical status with RED/GREEN results, exact commit, and Phase 3 pointer.
5. Stage exact paths and commit locally. No full suite, push, deployment, restart, production query, or setting change.
