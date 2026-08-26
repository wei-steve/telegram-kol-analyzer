# Phase 3 Final Candidate Review Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Assemble one reviewable local production candidate and freeze its exact SHA after the final required test suite.

**Architecture:** Review Phase 1 and Phase 2 together against the approved design. Repair any finding with a new RED test, then run one final full suite only after the last production-code edit.

**Tech Stack:** Git, Python, pytest.

---

## Task 1: Claim and inspect the candidate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`
- Review: all Phase 1 and Phase 2 source/test diffs

1. Require both prior phases complete, a clean exclusive worktree, exact ancestry, and the status pointer naming this file; commit the claim.
2. Review for event-loop blocking, cancellation races, SQL lane overtaking, stale-lease recovery, unbounded result sets, session leaks, and accidental semantic/config/schema changes.
3. Confirm the diff adds no service, executor, queue, actor, index, migration, exchange authority, or new status system.

## Task 2: Resolve findings with TDD

For every actionable defect, first add a failing focused test, then make the smallest fix and rerun the affected slice. Commit each coherent repair with exact paths. If a repair changes trading semantics or requires schema/data mutation, stop and return to design rather than proceeding.

## Task 3: Freeze the final candidate

1. Run the consolidated focused suite for reconcile, lifecycle, Bot, worker executor, blocking census, keyed locks, message queue, settings transitions, role ownership, and restart/recovery.
2. Run `git diff --check` and `python -m compileall -q src/telegram_kol_research tests`.
3. After the last production-code edit, run the repository's complete pytest suite exactly once. Any later production-code edit creates a new candidate and requires affected focused tests plus one new final full suite.
4. Perform an independent read-only review in this same sequential session; do not use a subagent or parallel checkout.
5. Record exact candidate SHA, test commands/results, diff boundary, and Phase 4 pointer in the canonical status; commit only documentation if code is unchanged after the suite.

No push, deployment, restart, production read/write, settings change, Telegram traffic, or exchange call is authorized.
