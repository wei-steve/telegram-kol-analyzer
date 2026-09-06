# 区间入场第一腿仓位优化 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将新生成的两腿区间入场从固定 50/50 风险分配改为动态等仓位分配，并将第一腿风险占比限制在 50%–65%。

**Architecture:** 在离线 Deepcoin 下单草稿构建器内增加一个纯函数，根据两个最终入场价与止损距离返回两腿分配比例。只在真实两价区间分支使用该比例；单价、已持久化草稿和已有持仓不受影响。

**Tech Stack:** Python 3.11+、pytest、现有 `deepcoin_order_builder` 纯计算与 Deepcoin 合约规范化。

---

### Task 1: 用测试定义动态两腿分配

**Files:**
- Modify: `tests/test_deepcoin_order_builder.py`
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:130-273`

**Step 1: Write the failing tests**

在 `tests/test_deepcoin_order_builder.py` 增加三个定向测试：

```python
def test_true_range_balances_quantity_by_stop_distance():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="110-100", stop_loss="80", risk_budget_usdt=20.0)
    )
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [60.0, 40.0]
    assert [leg["risk_budget_usdt"] for leg in draft["order_legs"]] == [12.0, 8.0]
    assert [leg["quantity"] for leg in draft["order_legs"]] == [0.4, 0.4]


def test_true_range_caps_first_leg_risk_at_sixty_five_percent():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="120-100", stop_loss="90", risk_budget_usdt=20.0)
    )
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [65.0, 35.0]
    assert sum(leg["risk_budget_usdt"] for leg in draft["order_legs"]) == 20.0


def test_true_range_without_stop_loss_keeps_equal_risk_allocation():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="110-100", stop_loss=None)
    )
    assert [leg["allocation_pct"] for leg in draft["order_legs"]] == [50.0, 50.0]
```

根据现有 `_range_entry_leg_prices` 对多空方向的排序调整 fixture，保证断言中的第一腿是实际首先触发的腿，不依赖输入区间文本顺序。

**Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_order_builder.py::test_true_range_balances_quantity_by_stop_distance \
  tests/test_deepcoin_order_builder.py::test_true_range_caps_first_leg_risk_at_sixty_five_percent \
  tests/test_deepcoin_order_builder.py::test_true_range_without_stop_loss_keeps_equal_risk_allocation
```

Expected: 前两个测试因现有固定 50/50 分配失败，缺失止损测试已经通过或与前两个一起运行时保持通过。

**Step 3: Implement the pure allocation helper**

在 `deepcoin_order_builder.py` 增加：

```python
def _range_entry_allocations(
    *,
    first_price: float,
    second_price: float,
    stop_loss: float | None,
) -> tuple[float, float]:
    if stop_loss is None:
        return 50.0, 50.0
    first_distance = abs(first_price - stop_loss)
    second_distance = abs(second_price - stop_loss)
    total_distance = first_distance + second_distance
    if first_distance <= 0 or second_distance <= 0 or total_distance <= 0:
        raise DeepcoinOrderDraftError("stop_loss must differ from entry price")
    first_allocation = min(65.0, max(50.0, first_distance / total_distance * 100))
    return first_allocation, 100.0 - first_allocation
```

在 hybrid market/limit 和普通两条 limit 分支中，先使用最终规范化价格调用该函数，再将返回的比例同时传入 `allocation_pct`、`_leg_risk_budget` 和 `_estimate_leg_quantity`。不修改 `_single_entry_leg`。

**Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_deepcoin_order_builder.py
```

Expected: PASS。现有明确断言真实区间为 `[50.0, 50.0]` 的测试应更新为新计算值；与本改动无关的断言不放宽。

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_order_builder.py tests/test_deepcoin_order_builder.py
git commit -m "feat: prioritize first range entry leg"
```

### Task 2: 验证草稿和提交边界不回算旧订单

**Files:**
- Modify: `tests/test_deepcoin_order_builder.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Add invariant assertions**

在上限测试中计算每腿理论止损亏损：

```python
estimated_loss = sum(
    leg["quantity"] * abs(leg["price"] - draft["stop_loss"])
    for leg in draft["order_legs"]
)
assert estimated_loss <= draft["risk_budget_usdt"]
```

保留现有单价测试的 `allocation_pct == 100.0` 断言。在现有 `recovery_live_submit` 已保存草稿 fixture 中，明确断言提交的数量与草稿内容一致，不重新调用构建器。

**Step 2: Run related tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_order_builder.py \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py
```

Expected: PASS。

**Step 3: Run static checks available in the project**

Run:

```bash
.venv/bin/python -m compileall -q src tests
```

Expected: exit code 0。

**Step 4: Commit any test-only boundary additions**

```bash
git add tests/test_deepcoin_order_builder.py tests/test_recovery_live_submit.py
git commit -m "test: protect range allocation boundaries"
```

如果没有额外文件变更，跳过此提交。

### Task 3: 推送并在安全窗口部署

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: `docs/plans/2026-07-31-range-entry-first-leg-risk-design.md`

**Step 1: Review the final diff and local status**

Run:

```bash
git diff --check
git status --short
git log -3 --oneline
```

Expected: 没有 whitespace 错误；本功能提交仅包含规划的 builder、测试和文档，用户现有未跟踪文件不纳入提交。

**Step 2: Push the reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds。

**Step 3: Prove a safe deployment window**

在服务器上使用项目现有只读状态检查，确认没有正在识别、创建、修订、取消或管理的时效性策略操作，并检查 `telegram-kol.service` 当前状态。

Expected: 安全窗口可被积极证明。如果不能证明，停止在已推送状态，不重启服务。

**Step 4: Deploy with the existing helper**

在 Windows 本地项目环境运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

该帮助脚本应在服务器从 GitHub 拉取目标分支、重新安装可编辑包并重启 `telegram-kol.service`。

Expected: pull/install/restart 全部成功。

**Step 5: Verify production without submitting trades**

在服务器上检查：

```bash
systemctl is-active telegram-kol.service
journalctl -u telegram-kol.service --since "10 minutes ago" --no-pager
```

Expected: service is `active`，启动日志无导入、配置、数据库或 Telegram 会话错误。不提交人工测试订单。
