# Phase 7 Low-Perturbation Observer Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task by task.

**Goal:** Decouple frequent durable-queue sampling from role HTTP sampling so
Phase 7 can prove real cross-chat progress without the observer materially
perturbing the event loops it evaluates.

**Architecture:** Acceptance mode will keep the existing read-only SQLite and
HTTP collectors but schedule them independently. Complete database snapshots
update ordering, identity, traffic, backlog, and simultaneous claimed-chat
evidence every second. Complete runtime snapshots update authority, cap, lane
peak, and cumulative health evidence every 30 seconds. The tracker combines the
two independent lower bounds while every due read retains one-retry-then-fail-
closed semantics. Convergence and rollback keep their existing bounded combined
sampling.

**Tech Stack:** Python standard library, SQLite read-only URI, HTTP GET, pytest.

---

### Task 1: Establish RED cadence and attribution contracts

**Files:**
- Modify: `tests/test_per_chat_phase7_observer.py`

1. Add a test whose injected clock advances through frequent database ticks and
   proves runtime HTTP is called only at start and its independent due times.
2. Add a test proving two simultaneously claimed chats in a complete database
   snapshot plus a complete same-window worker peak of two establishes cross-
   chat progress without simultaneous active-lane HTTP evidence.
3. Add tests proving a due runtime read is retried once, a second incomplete
   result fails closed, and cached runtime data cannot mask it.
4. Add a test proving a cumulative role-attributed stall discovered on the next
   sparse runtime sample fails closed with the exact role-specific reason.
5. Run only the new tests and record the expected failures before production
   code changes.

### Task 2: Implement the smallest split-cadence collector

**Files:**
- Modify: `scripts/per_chat_phase7_observer.py`
- Test: `tests/test_per_chat_phase7_observer.py`

1. Add explicit acceptance CLI intervals for database and runtime sampling with
   safe defaults of one and 30 seconds; keep `--poll-interval` for convergence
   and rollback compatibility.
2. Split the current combined retry helper into database-only and runtime-only
   bounded collectors. Each due collector gets exactly two attempts.
3. Add an acceptance scheduler that always samples SQLite on the database
   cadence and calls role HTTP only when the runtime cadence is due, including
   the first sample and final window boundary.
4. Preserve the last complete runtime snapshot only for output and combination;
   never substitute it when a due runtime collection is incomplete.
5. Run the new tests until GREEN.

### Task 3: Make acceptance combine independent proof signals

**Files:**
- Modify: `scripts/per_chat_phase7_observer.py`
- Modify: `tests/test_per_chat_phase7_observer.py`

1. Track the maximum distinct claimed-chat count observed in any complete
   SQLite snapshot.
2. Require both that durable lower bound to be at least two and a complete
   same-window worker cumulative peak at least as large; preserve the peak cap
   of three.
3. Keep same-chat and oldest-nonterminal evaluation scoped to each database
   snapshot and preserve all existing role-attribution and rollback mappings.
4. Run the complete observer test module and the directly related durable
   ordering regression slice.

### Task 4: Verify and freeze the local candidate

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

1. Run `compileall` for the observer and its test module.
2. Run the focused Phase 7 observer/runtime-loop-health tests.
3. Run one final complete local pytest suite after the last code edit.
4. Record RED, GREEN, focused, full-suite, source-boundary, and candidate
   evidence in canonical status.
5. Stage only the observer, its test, this plan, the design, and canonical
   status paths as applicable; inspect the cached path list; commit and push
   non-force to `codex/deepcoin-auto-trading-v1`.

### Task 5: Install read-only candidate and run fresh Phase 7 window

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

1. Re-run the production preflight: exact production runtime SHA
   `0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f`, tracked-clean checkout,
   `global + 1 + queue`, unique authorities/session owner, no active write,
   management, non-shadow job, worker command, revision claim, time-sensitive
   strategy, or incomplete exchange snapshot.
2. Copy the exact standalone observer candidate only into a new server evidence
   directory; do not modify the production checkout or restart a service.
3. Perform the authorized expected-state cutover to `per_chat + 3 + queue` and
   prove three-sample convergence with the existing short combined observer.
4. Run one fresh continuous two-hour natural-message acceptance window using
   the split cadence. Do not manufacture traffic or extend the deadline.
5. On any real failed gate or twice-incomplete query, atomically restore the
   mapped `global` target and independently prove rollback convergence. On full
   acceptance, leave the accepted tuple only if every Phase 7 minimum passes.
6. Record the window, natural traffic, ordering, cross-chat proof, stall roles,
   exchange parity, evidence path and digest, final tuple, and released claim in
   canonical status; commit and push the exact status path.
