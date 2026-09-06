# 受监督当前持仓保护单回填 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为当前仍在交易所挂起、但缺少原始 `OrderSysID -> PositionID` 字段的保护单生成受监督、指纹化的只读回填清单。

**Architecture:** 新的纯计划模块只接受操作者明确提供的订单 ID 与仓位 ID 对。它读取交易所当前仓位和 TPSL 委托，验证每一端仍存在、币种/方向一致且订单尚未拥有 ledger；输出 `review`、`exact` 或 `skip` 结果及稳定指纹。该阶段不写数据库、不请求任何交易所写接口。

**Tech Stack:** Python 3.11、SQLAlchemy、Typer、pytest。

---

### Task 1: 定义纯粹的受监督计划模型

**Files:**
- Create: `src/telegram_kol_research/current_protection_backfill.py`
- Test: `tests/test_current_protection_backfill.py`

**Step 1: Write the failing test**

```python
def test_review_plan_accepts_only_explicit_order_to_position_mapping():
    plan = build_current_protection_backfill_plan(...)
    assert plan.actions[0].order_id == "order-1"
    assert plan.actions[0].pos_id == "pos-1"
    assert plan.actions[0].classification == "review"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_current_protection_backfill.py::test_review_plan_accepts_only_explicit_order_to_position_mapping -q`

**Step 3: Write minimal implementation**

Create dataclasses for submitted mappings, actions, refusals and a stable JSON/SHA-256 fingerprint. Never infer a relationship from price, amount, side or time.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_current_protection_backfill.py -q`

### Task 2: 验证当前交易所与本地账本状态

**Files:**
- Modify: `src/telegram_kol_research/current_protection_backfill.py`
- Modify: `tests/test_current_protection_backfill.py`

**Step 1: Write failing tests**

Cover missing order, inactive position, symbol/side mismatch and existing verified ledger. Each must produce a refusal rather than an action.

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_current_protection_backfill.py -q`

**Step 3: Write minimal implementation**

Validate only explicit mappings against supplied current snapshots. Include a sanitized evidence hash, but never persist browser sessions, headers, cookies or keys.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_current_protection_backfill.py -q`

### Task 3: 暴露只读 CLI

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_current_protection_backfill.py`

**Step 1: Write failing CLI test**

Assert the command requires an explicit JSON mapping file and prints a dry-run plan; it must not call any ledger upsert function.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_current_protection_backfill.py -q`

**Step 3: Write minimal implementation**

Add `plan-current-protection-backfill` command. It reads snapshots through existing DeepCoin read methods and emits JSON only.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_current_protection_backfill.py -q`

### Task 4: 验证与交付

**Files:**
- Test: `tests/test_current_protection_backfill.py`

**Step 1:** Run targeted tests and existing entry-protection repair tests.

Run: `pytest tests/test_current_protection_backfill.py tests/test_entry_protection_ledger_repair.py -q`

**Step 2:** On the production server, run only the new dry-run command with an explicit mapping file; inspect the emitted fingerprint and action/refusal counts. Do not apply it.

**Step 3:** Commit source, tests and plan, then push `codex/deepcoin-auto-trading-v1`.

