# Phase 4 Batch 150 Read-Only Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prove that production management batch `150` remains terminal and no longer blocks deployment.

**Architecture:** Run bounded read-only SQLite checks against the live production database. Treat any mismatch or incomplete result as a stop; this phase has no repair path.

**Tech Stack:** SSH, SQLite, Git, systemd read-only inspection.

---

## Task 1: Claim and verify identity

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

1. Require Phase 3 complete at the frozen candidate SHA, a clean exclusive worktree, and the status pointer naming this file; commit the claim.
2. Read production service/deployed SHA and database path without changing them. A missing or ambiguous database identity stops the phase.

## Task 2: Run the bounded production checks

Store raw output in one server evidence directory and record only its path and hashes in status.

1. Run `PRAGMA query_only=ON` and `PRAGMA quick_check`; require `ok`.
2. Read batch `150`; require `status=resolved` and `reason_code=historical_position_fully_closed`.
3. Read only its directly related management components, execution legs, and bindings. Require terminal/closed consistency with no row still claiming active management authority.
4. Run the established bounded active-management count; require zero.
5. Re-read batch `150` and `quick_check` at the end to prove the gate did not observe an unstable transition.

Do not create a backup or CAS plan, begin a write transaction, invoke a repair tool, call Deepcoin, deploy, restart, replay, or send an operator/system Bot message.

## Task 3: Decide and close

- If every result is complete and exact, record the evidence path/hash and advance the status pointer to Phase 5.
- If any field regressed or any query is incomplete, leave the workstream `in_progress`, record the mismatch, and stop. A new L3 design and owner approval are required before any repair.
- Commit only the status update with explicit staging. No production mutation occurred.
