# Deterministic Trigger Position Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably map every filled DeepCoin trigger entry to its originating strategy and entry leg.

**Architecture:** Reconciliation derives an order-leg-specific position mapping from trigger history, fill size, side, and time. Binding-level status remains recoverable while any entry leg is still live. Entry-leg updates are isolated from sibling legs.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, DeepCoin REST adapter.

## Global Constraints

- Keep all matching read-only against DeepCoin.
- Do not infer a strategy from symbol and price alone.
- Preserve user-authored uncommitted files and deploy only the reviewed commit.
- Run production verification on the server after GitHub deployment.

---

### Task 1: Reproduce Multi-Leg Attribution Failures

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Interfaces:**
- Consumes `ExecutionBinding`, `ExecutionOrderLeg`, trigger history, and live positions.
- Produces one distinct recovered `pos_id` per entry leg.

- [ ] Add a failing test where two trigger orders from one binding fill at distinct positions and one fill price differs from its trigger price by more than 0.1%.
- [ ] Verify the test fails because the second position is not recovered.
- [ ] Change reconciliation to correlate positions to each entry leg using exact trigger order identity, side, quantity, and matching fill time; retain price only as non-blocking evidence.
- [ ] Change leg refresh so it updates only the matched order leg.
- [ ] Verify the focused test passes.

### Task 2: Keep Pending Sibling Legs Recoverable

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Interfaces:**
- Consumes a closed binding with an entry order leg that later fills.
- Produces an active binding, entered lifecycle, and exact recovered position ID.

- [ ] Add a failing test for a second trigger leg filling after its sibling position was closed.
- [ ] Verify the test fails because closed bindings are excluded from reconciliation.
- [ ] Include closed bindings with outstanding entry legs in reconciliation and revive them only when direct order evidence confirms a live position.
- [ ] Verify the focused test passes.

### Task 3: Regression Verification And Deployment

**Files:**
- Modify: `tests/test_execution_bindings.py`

- [ ] Run the focused execution-binding suite.
- [ ] Run the full local test suite.
- [ ] Commit only this repair's source, tests, design, and plan files.
- [ ] Push `codex/deepcoin-auto-trading-v1` to GitHub.
- [ ] Run `scripts/server_git_update.ps1` and verify the service plus the repaired live positions on the server.
