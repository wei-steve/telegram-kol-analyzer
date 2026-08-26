# Phase 5 Trigger Intents Read-Only Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prove that trigger-protection intents `138`, `141`, and `147` remain terminal and no longer block deployment.

**Architecture:** Query only the three exact production rows and their persisted execution-leg identities. No exchange query or historical re-attribution is needed to verify non-regression of already-terminal state.

**Tech Stack:** SSH, SQLite, Git.

---

## Task 1: Claim and verify identity

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

Require Phase 4 complete, the exact frozen candidate unchanged, a clean exclusive worktree, and the status pointer naming this file; commit the claim. Reconfirm the production database path read-only.

## Task 2: Run the bounded production checks

1. Enable `PRAGMA query_only=ON`; require `PRAGMA quick_check=ok`.
2. Query exactly intents `138`, `141`, and `147`; require exactly three rows, each with `recovery_state=resolved`, `recovery_disposition=terminal`, and `last_reason_code=entry_leg_terminal_after_snapshot_wait`.
3. Read each intent's persisted execution-leg reference and require it still resolves to the same terminal leg identity. Do not infer ownership from symbol, side, time, tag, or `clOrdId`.
4. Re-read the three rows and `quick_check` at the end. Save detailed output in a server evidence directory and put only its path/hash in status.

Do not call Deepcoin, rebuild attribution, create a backup/CAS plan, write the database, deploy, restart, replay, or send an operator/system Bot message.

## Task 3: Decide and close

- Exact complete results advance the status pointer to Phase 6.
- A missing row, changed terminal state, broken leg reference, or incomplete query stops the workstream. Record the discrepancy and require a separate L3 design/approval; do not repair it here.
- Commit only the explicit status path. No production mutation occurred.
