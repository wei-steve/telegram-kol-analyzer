# Phase 6 Compatible Deployment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy the reviewed candidate while production remains on `global + 20 + queue`, and prove the new code is compatible before cutover.

**Architecture:** Push and deploy only a separately authorized exact 40-hex SHA. Restart the split runtime through the existing deployment workflow, retain current settings, and perform L2 post-deploy verification without taking over concurrency authority.

**Tech Stack:** GitHub, PowerShell deployment helper, SSH, systemd, SQLite, HTTP health endpoints, pytest.

---

## Task 1: Authorization and pre-deploy gate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

1. Require Phases 1-5 complete, a clean exclusive worktree, frozen candidate SHA, and status pointer naming this file.
2. Obtain explicit owner authorization naming that exact SHA for push and deployment. Cutover is not included.
3. Freshly verify local/upstream/remote topology, production deployed SHA, split ingest/worker/web ownership, no active write/management/claimed work, healthy SQLite/WAL/loop/session state, and current tuple exactly `global + 20 + queue`.
4. Any mismatch, time-sensitive strategy operation, incomplete query, or dirty/shared ownership stops the phase. Do not repair Git or production state.

## Task 2: Push and deploy the exact candidate

1. Push the reviewed exact SHA to `codex/deepcoin-auto-trading-v1` without force.
2. Use `scripts/server_git_update.ps1` as the existing deployment path.
3. Confirm production HEAD equals the authorized SHA and exactly one ingest, worker, and web authority is active. Confirm ingest alone owns the Telegram session.
4. Confirm settings remain exactly `global + 20 + queue`; a changed tuple triggers fail-closed investigation, not cutover.

## Task 3: Compatibility proof

1. Run focused server tests for event-loop offload, worker claims, lock admission, settings expected-state transition, and process roles.
2. Through the ingest-owned settings endpoint, submit an exact no-op expected-state request for `global + 20 + queue`; require success with no value change.
3. Submit one deliberately stale expected-state request; require conflict with no value change. Do not submit `per_chat` or cap `3`.
4. Observe the L2 window for 30 continuous minutes and at least five natural messages, attempting two chats. Stop at 30 minutes if traffic is insufficient and leave the phase `in_progress`.
5. Require backlog convergence, no duplicates, no SQLite locks, no new loop stalls, no session conflict, and complete worker-owned read-only exchange parity when message execution can affect exchange state.

Use a quiet server monitor and no more than pre-deploy, post-deploy, post-restart, and observation-end checkpoints. No manufactured message, replay, manual DB write, settings cutover, or observer exchange write.

## Task 4: Close

On success, record the authorized/deployed SHA, window, traffic, health metrics, evidence path/hash, unchanged tuple, and Phase 7 pointer. On failure, leave `global + 20`, record the exact failure, and stop. Commit the explicit status path; do not start cutover in this session.
