# Per-Chat Activation and Event-Loop Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the proven synchronous database work from asyncio loops, bound durable queue claims, and safely cut production over to `per_chat + 3`.

**Architecture:** Keep the existing ingest/worker/web split, SQLite database, durable message queue, default executor, and single-thread management executor. Execute one phase per fresh session and preserve all recognition, strategy, position, execution, and exchange-write semantics.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy, SQLite, pytest, systemd.

---

## Canonical artifacts

- Design: `docs/plans/2026-08-25-per-chat-activation-event-loop-optimization-design.md`
- Status: `docs/per-chat-durable-lanes-status.md`
- Phase files: `docs/plans/2026-08-25-per-chat-activation-event-loop-optimization/`

The status file is the only mutable handoff ledger. Phase 1 first replaces its stale header pointers with this plan and records an exclusive claim. Every later session reads only `AGENTS.md`, that status file, and the phase file named by `current_phase_file`.

## Sequential phases

| Session | Phase file | Completion boundary |
| --- | --- | --- |
| 1 | `phase-1-event-loop-db-offload.md` | Focused RED/GREEN code and tests committed locally |
| 2 | `phase-2-bounded-claim-selection.md` | Bounded claim SQL and concurrency tests committed locally |
| 3 | `phase-3-final-candidate-review.md` | Review complete, final full suite passed once, exact SHA frozen |
| 4 | `phase-4-batch150-read-only-gate.md` | Existing terminal batch state verified read-only |
| 5 | `phase-5-trigger-intents-read-only-gate.md` | Existing terminal intent state verified read-only |
| 6 | `phase-6-compatible-deployment.md` | Separately authorized exact SHA deployed, still `global + 20` |
| 7 | `phase-7-cutover-acceptance.md` | Separately authorized cutover accepted or rolled back |

## Fixed protocol

1. Work sequentially in one dedicated exclusive worktree. Do not use subagents or parallel owners.
2. Pass the phase's clean-tree, ownership, current-pointer, and exact-SHA gates before editing.
3. Production-code changes use RED-to-GREEN TDD. Stage exact paths only; never use `git add -A`.
4. End each phase with one concise status entry containing changed paths, commit/evidence, verification, next phase, and remaining authorization.
5. Stop on mismatch or incomplete evidence. Do not pull, reset, clean, stash, repair production data, or broaden authority.

Design and plan approval does not authorize implementation, push, deployment, restart, production settings/database mutation, Telegram traffic, replay, or exchange writes.
