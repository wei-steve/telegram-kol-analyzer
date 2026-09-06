# Phase 7 Per-Chat Cutover and Acceptance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Atomically enable `per_chat + 3`, observe real traffic for two continuous hours, and finish only with acceptance or a completed rollback.

**Architecture:** Use the existing ingest-owned expected-state transition without restart. Preserve the durable queue as ordering authority and use the approved two-level atomic rollback.

**Tech Stack:** SSH, HTTP settings endpoint, SQLite read-only diagnostics, systemd, runtime health endpoints.

---

## Task 1: Authorization and cutover gate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

1. Require Phase 6 complete on the exact deployed SHA and obtain separate owner authorization for the settings transition and rollback authority.
2. Verify exactly one worker, ingest-only Telegram session ownership, no active write/management, no claimed or inflight message job, no claimed/executing worker command, healthy WAL and `quick_check`, clean loop/SQLite/session evidence, and complete worker-owned read-only exchange baseline.
3. Read the exact current tuple; require `global + 20 + queue`. Any mismatch or incomplete query stops without a write.

## Task 2: Atomic cutover

Submit one ingest-owned expected-state request:

```text
expected: global + 20 + queue
desired:  per_chat + 3 + queue
```

Require both fields to change together and do not restart. Conflict must leave the old tuple unchanged. A timeout is unknown: read the full tuple and retry once only when that read proves the write did not apply.

## Task 3: Continuous acceptance window

Start the quiet monitor immediately after confirming the new tuple. Keep this session active through one uninterrupted two-hour natural-traffic window. Do not stitch windows or deploy, restart, change settings, invoke worker commands, replay, manufacture traffic, or make observer-triggered exchange writes.

Require all of the following:

- at least five natural messages, attempting at least two chats;
- `peak_active_chat_lanes_since_limit_change` between two and three;
- exact same-chat order and non-overlap plus observed cross-chat progress;
- bounded backlog convergence and zero missing/orphan/stuck/duplicate job, decision, or execution identities;
- zero new SQLite locks, event-loop stalls, Telegram session conflicts, DeepSeek/402 errors, or authority drift;
- complete worker-owned exchange baseline/end evidence with explained parity.

An incomplete external query remains unknown after one reasoned retry and is a failure. No traffic waiver is allowed.

## Task 4: Rollback on any failure

- Lock, admission, or ingest anomaly: atomically set `global + 3 + queue` from the exact observed tuple.
- Scheduler, duplicate, SQLite, execution, or concurrency anomaly: atomically set `global + 1 + queue`.

Do not reset jobs or restart services. If rollback response is unknown, read the full tuple before one reasoned retry. Do not return control while `per_chat` remains enabled but unobserved or while rollback is unconfirmed.

## Task 5: Close the workstream

On acceptance, record exact SHA, tuple, two-hour window, traffic/chats, peak lanes, ordering/overlap, backlog/duplicate/loop/SQLite/session/exchange evidence, and evidence path/hash; mark the workstream complete. On rollback, record the confirmed rollback tuple and leave it incomplete with the failure reason. Commit only the status document with explicit staging.
