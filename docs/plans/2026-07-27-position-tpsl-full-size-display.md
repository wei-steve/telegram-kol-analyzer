# Web 持仓止盈止损全仓数量展示 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 Deepcoin TPSL 的 `sz=0` 从错误的“0 contracts”改为“全部剩余仓位”，并在强归属和可信合约规格存在时附带最新 contracts 与基础币快照。

**Architecture:** 在 `position_tpsl_display` 领域展示层解释 Deepcoin 数量语义，生成结构化 `size_mode` 和模板可直接使用的 `size_display_text`。当前仓位只在订单已经通过 `PositionID` 或强账本证据精确归属后参与数量文案；合约换算复用 Web 进程已经加载的 `DeepcoinContractSpecProvider`，缺失时安全降级为 contracts。

**Tech Stack:** Python 3.11、FastAPI、Jinja2、SQLAlchemy、pytest、Decimal、现有 Deepcoin contract-spec provider

---

### Task 1: 在纯展示模型中表达全仓与部分仓位数量

**Files:**
- Modify: `src/telegram_kol_research/position_tpsl_display.py:1-176`
- Test: `tests/test_position_tpsl_display.py`

**Step 1: 写精确归属全仓数量的失败测试**

在 `tests/test_position_tpsl_display.py` 增加：

```python
from telegram_kol_research.deepcoin_contract_specs import (
    DeepcoinContractSpec,
    StaticDeepcoinContractSpecProvider,
)


def _btc_specs():
    return StaticDeepcoinContractSpecProvider(
        specs_by_instrument_id={
            "BTC-USDT-SWAP": DeepcoinContractSpec(
                instrument_id="BTC-USDT-SWAP",
                contract_value=0.001,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.1,
            )
        }
    )


def test_display_renders_zero_size_as_full_remaining_position_snapshot():
    result = build_position_tpsl_display(
        positions=[
            {
                "posId": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "10",
            }
        ],
        pending_orders=[
            {
                "ordId": "full-stop",
                "triggerOrderType": "TPSL",
                "PositionID": "pos-a",
                "instId": "BTC-USDT-SWAP",
                "sz": "0",
                "slTriggerPx": "63895.725",
            }
        ],
        exact_order_position_ids={},
        contract_spec_provider=_btc_specs(),
    )

    row = result.by_pos_id["pos-a"][0]
    assert row.size_mode == "full_position"
    assert row.raw_size_text == "0"
    assert row.current_position_size_text == "10"
    assert (
        row.size_display_text
        == "全部剩余仓位（当前 10 contracts / 0.01 BTC）"
    )
```

再增加下列失败用例：

```python
def test_display_renders_partial_size_in_contracts_and_base_asset():
    # pos=10、sz=2、BTC contract_value=0.001
    # assert size_mode == "partial"
    # assert size_display_text == "2 contracts / 0.002 BTC"


def test_display_full_position_without_contract_spec_keeps_contract_snapshot():
    # pos=10、sz=0、contract_spec_provider=None
    # assert size_display_text == "全部剩余仓位（当前 10 contracts）"


def test_display_unattributed_full_position_never_uses_a_position_snapshot():
    # 两个同币种仓位 + 一个无 PositionID/无 ledger 映射的 sz=0 TPSL
    # assert result.by_pos_id 两边都为空
    # assert result.unattributed[0].size_display_text
    #        == "全部仓位（具体仓位未归属）"
    # assert current_position_size_text is None


@pytest.mark.parametrize("raw_size", [None, "", "0", "0.0"])
def test_display_empty_or_numeric_zero_size_is_full_position(raw_size):
    # 精确归属当前仓位
    # assert size_mode == "full_position"


def test_display_invalid_size_does_not_break_snapshot():
    # sz="not-a-number"
    # assert build_position_tpsl_display 正常返回
    # assert size_display_text 不包含错误的基础币换算
```

保留现有 `test_display_uses_verified_local_order_mapping_and_leaves_unknown_zero_size_unattributed`，并补充断言未知全仓订单不能获得任何一个仓位的当前数量。

**Step 2: 运行测试确认失败**

Run:

```bash
uv run pytest -q tests/test_position_tpsl_display.py
```

Expected: FAIL，原因是 `build_position_tpsl_display()` 尚不接受
`contract_spec_provider`，且 `PositionTpslDisplayRow` 没有新的数量语义字段。

**Step 3: 扩展展示行数据结构**

在 `src/telegram_kol_research/position_tpsl_display.py`：

```python
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

from telegram_kol_research.deepcoin_contract_specs import (
    DeepcoinContractSpecProvider,
)


@dataclass(frozen=True, slots=True)
class PositionTpslDisplayRow:
    kind: str
    trigger_price_text: str
    size_text: str
    order_id: str
    ownership_state: str
    size_mode: Literal["partial", "full_position"]
    raw_size_text: str
    size_display_text: str
    current_position_size_text: str | None = None
    instrument_id: str | None = None
    side: str | None = None
```

`as_dict()` 必须输出：

```python
row = {
    "kind": self.kind,
    "trigger_price_text": self.trigger_price_text,
    "size_text": self.size_text,
    "raw_size_text": self.raw_size_text,
    "size_mode": self.size_mode,
    "size_display_text": self.size_display_text,
    "order_id": self.order_id,
    "ownership_state": self.ownership_state,
}
if self.current_position_size_text is not None:
    row["current_position_size_text"] = self.current_position_size_text
```

暂时保留 `size_text=raw_size_text` 作为兼容字段，但模板不得继续用它拼接单位。

**Step 4: 以精确仓位为边界生成数量文案**

把 `build_position_tpsl_display()` 改为接受可选规格提供器：

```python
def build_position_tpsl_display(
    *,
    positions: list[dict[str, Any]],
    pending_orders: list[dict[str, Any]],
    exact_order_position_ids: Mapping[str, object],
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> PositionTpslDisplayResult:
```

建立完整位置索引，而不是只有 ID 集合：

```python
positions_by_id = {
    position_id: position
    for position in positions
    if (position_id := _first_text(
        position, "PositionID", "posId", "pos_id", "id"
    ))
}
by_pos_id = {position_id: [] for position_id in positions_by_id}
```

解析出 `position_id` 后，只有 `position_id in positions_by_id` 才把
`positions_by_id[position_id]` 传给 `_split_order()`；否则传 `None` 并生成未归属
文案。不要通过 instrument、side、size 或时间选择仓位。

加入定点数量辅助函数：

```python
def _decimal_or_none(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _base_symbol(instrument_id: str) -> str | None:
    symbol = instrument_id.upper().split("-", 1)[0].strip()
    return symbol or None
```

加入统一换算函数：

```python
def _quantity_display(
    contracts_text: str,
    *,
    instrument_id: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> str:
    contracts = _decimal_or_none(contracts_text)
    if contracts is None:
        return f"{contracts_text} contracts"
    spec = (
        contract_spec_provider.get_contract_spec(instrument_id)
        if contract_spec_provider is not None and instrument_id
        else None
    )
    if spec is None:
        return f"{_decimal_text(contracts)} contracts"
    base_quantity = contracts * Decimal(str(spec.contract_value))
    symbol = _base_symbol(spec.instrument_id)
    if not symbol:
        return f"{_decimal_text(contracts)} contracts"
    return (
        f"{_decimal_text(contracts)} contracts / "
        f"{_decimal_text(base_quantity)} {symbol}"
    )
```

`get_contract_spec()` 若异常，不应使页面失败；把规格读取包在小范围
`try/except (KeyError, TypeError, ValueError)` 中并降级为 contracts。不要捕获整个
快照构建流程，也不要访问网络补规格。

数量语义构造规则：

```python
def _size_fields(
    order: dict[str, Any],
    *,
    position: dict[str, Any] | None,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> tuple[str, str, str, str | None]:
    raw_size = _first_text(order, "sz", "size", "Volume")
    raw_size_text = raw_size if raw_size is not None else "0"
    parsed_size = _decimal_or_none(raw_size_text)
    is_full_position = raw_size is None or parsed_size == 0
    instrument_id = str(
        order.get("instId")
        or order.get("InstrumentID")
        or (position or {}).get("instId")
        or (position or {}).get("InstrumentID")
        or ""
    ).upper()

    if not is_full_position:
        return (
            "partial",
            raw_size_text,
            _quantity_display(
                raw_size_text,
                instrument_id=instrument_id,
                contract_spec_provider=contract_spec_provider,
            ),
            None,
        )

    if position is None:
        return (
            "full_position",
            raw_size_text,
            "全部仓位（具体仓位未归属）",
            None,
        )

    current_size = _first_text(position, "pos", "size")
    if current_size is None or (_decimal_or_none(current_size) or Decimal("0")) <= 0:
        return ("full_position", raw_size_text, "全部剩余仓位", None)
    snapshot = _quantity_display(
        current_size,
        instrument_id=instrument_id,
        contract_spec_provider=contract_spec_provider,
    )
    return (
        "full_position",
        raw_size_text,
        f"全部剩余仓位（当前 {snapshot}）",
        current_size,
    )
```

让 `_split_order()` 接受 `position` 和 `contract_spec_provider`，一次生成
`size_mode`、`raw_size_text`、`size_display_text` 和
`current_position_size_text`，并将相同数量语义用于同一 combined TPSL 拆出的 TP
与 SL 两行。

对于不可解析的非空 `sz`，保持 `partial` 并只显示原始
`<value> contracts`，不得伪造基础币换算，也不得抛异常。

**Step 5: 运行纯模型测试**

Run:

```bash
uv run pytest -q tests/test_position_tpsl_display.py
```

Expected: PASS。

**Step 6: 提交纯展示模型**

```bash
git add \
  src/telegram_kol_research/position_tpsl_display.py \
  tests/test_position_tpsl_display.py
git commit -m "fix: interpret full-position TPSL quantities"
```

---

### Task 2: 将合约规格接入持仓快照并更新模板

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:843-1145`
- Modify: `src/telegram_kol_research/web_app.py:1169-1220`
- Modify: `src/telegram_kol_research/web_app.py:2330-2350`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:65-76`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:248-263`
- Test: `tests/test_web_page_render.py`

**Step 1: 写页面级失败测试**

在 `tests/test_web_page_render.py` 增加一个当前仓位：

```python
{
    "instId": "BTC-USDT-SWAP",
    "posId": "pos-full-1",
    "posSide": "long",
    "pos": "10",
    "avgPx": "63895.725",
}
```

以及两条已经精确归属的 TPSL：

```python
{
    "ordId": "tp-full-1",
    "triggerOrderType": "TPSL",
    "posId": "pos-full-1",
    "instId": "BTC-USDT-SWAP",
    "sz": "0",
    "tpTriggerPx": "66330",
},
{
    "ordId": "tp-partial-1",
    "triggerOrderType": "TPSL",
    "posId": "pos-full-1",
    "instId": "BTC-USDT-SWAP",
    "sz": "2",
    "tpTriggerPx": "67000",
},
```

用 Task 1 的 BTC 静态规格提供器创建 Web app，并断言：

```python
assert "数量 全部剩余仓位（当前 10 contracts / 0.01 BTC）" in response.text
assert "数量 2 contracts / 0.002 BTC" in response.text
assert "数量 0 contracts" not in response.text
```

再增加未归属用例：

```python
assert "全部仓位（具体仓位未归属）" in response.text
```

并确保未归属文案附近不出现任一当前仓位数量。

**Step 2: 运行页面测试确认失败**

Run:

```bash
uv run pytest -q \
  tests/test_web_page_render.py \
  -k "full_position_tpsl_quantity or unattributed_full_position_tpsl"
```

Expected: FAIL，页面仍输出 `size_text` 并固定追加 `contracts`。

**Step 3: 把已有 contract-spec provider 传入展示模型**

为 `_load_deepcoin_live_position_rows()` 增加：

```python
contract_spec_provider: DeepcoinContractSpecProvider | None = None,
```

为 `_split_exchange_protection_display_rows()` 增加同名可选参数，并传给：

```python
display = build_position_tpsl_display(
    positions=positions,
    pending_orders=pending_orders,
    exact_order_position_ids=exact_order_position_ids or {},
    contract_spec_provider=contract_spec_provider,
)
```

在 `_load_exchange_position_snapshot()` 调用 `_load_deepcoin_live_position_rows()`
时传入它已经收到的 `contract_spec_provider`。保留默认 `None`，避免破坏现有单元
测试和直接调用方。

不要新增第二套规格加载逻辑；CLI 已经通过
`config/deepcoin_contract_specs.yaml` 构造并注入 provider。

**Step 4: 模板只渲染完整数量文案**

把持仓卡中的：

```jinja2
<span>数量 {{ order.size_text }} contracts</span>
```

替换为：

```jinja2
<span>数量 {{ order.size_display_text }}</span>
```

把未归属保护单中的：

```jinja2
<span>{{ order.size_text }} contracts</span>
```

替换为：

```jinja2
<span>{{ order.size_display_text }}</span>
```

模板不得再判断 `size_mode` 或 `size_text == "0"`。

**Step 5: 运行页面级测试**

Run:

```bash
uv run pytest -q \
  tests/test_web_page_render.py \
  -k "positions_panel or tpsl or protection"
```

Expected: PASS。

**Step 6: 提交 Web 接线与模板**

```bash
git add \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  tests/test_web_page_render.py
git commit -m "fix: display full-position TPSL quantity semantics"
```

---

### Task 3: 完整回归与安全审查

**Files:**
- Verify: `src/telegram_kol_research/position_tpsl_display.py`
- Verify: `src/telegram_kol_research/web_app.py`
- Verify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Verify: `tests/test_position_tpsl_display.py`
- Verify: `tests/test_web_page_render.py`
- Verify: `tests/test_web_app.py`

**Step 1: 运行 focused tests**

```bash
uv run pytest -q \
  tests/test_position_tpsl_display.py \
  tests/test_web_page_render.py \
  tests/test_web_app.py
```

Expected: PASS。

**Step 2: 运行完整测试**

```bash
uv run pytest -q
```

Expected: PASS。若服务器专属测试因本地缺少身份或密钥而跳过，记录跳过原因，不
使用真实订单补测试。

**Step 3: 检查格式和改动范围**

```bash
git diff --check
git status --short
git diff --stat HEAD~2..HEAD
```

Expected:

- `git diff --check` 无输出；
- 只有计划内实现、测试和已提交计划文档属于本次改动；
- 不暂存或提交现有 `uv.lock`、`.audit-evidence/`、`.inspect/`、`artifacts/`
  等用户改动和审计产物。

**Step 4: 复核安全不变量**

人工检查：

- `position_tpsl_display` 只生成展示字段；
- `size_display_text` 不进入订单请求、撤单或归属判定；
- 无 `PositionID`/账本映射的订单不能取得当前仓位数量；
- provider 缺失或规格异常只降级展示；
- 没有新增 Deepcoin 写请求。

如审查发现问题，先补失败测试，再做最小修复并提交：

```bash
git add <exact-reviewed-files>
git commit -m "test: cover TPSL quantity display edge cases"
```

---

### Task 4: 推送、部署和只读生产验证

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: `docs/server-deployment.md`
- Verify: deployed `/positions-panel`

**Step 1: 确认分支和提交范围**

```bash
git branch --show-current
git log --oneline --decorate -5
git status --short
```

Expected:

- 分支为 `codex/deepcoin-auto-trading-v1`；
- 实现提交只包含本方案文件；
- 用户原有未提交文件保持原样。

**Step 2: 推送 GitHub**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push 成功。

**Step 3: 使用标准 helper 更新服务器**

macOS 当前工作站运行：

```bash
./scripts/server_git_update.sh
```

若在 Windows 工作站，则运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: 服务器从 GitHub 拉取相同 SHA、重新安装 editable package，并成功重启
`telegram-kol.service`。

**Step 4: 验证部署状态**

只读执行：

```bash
ssh -i "$KEY_PATH" root@43.167.220.225 \
  'cd /opt/telegram-kol-analyzer &&
   git rev-parse HEAD &&
   systemctl is-active telegram-kol.service &&
   curl -fsS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/positions-panel'
```

Expected:

- SHA 等于已推送实现 SHA；
- service 为 `active`；
- `/positions-panel` 返回 `200`。

**Step 5: 用现有真实订单进行只读页面核对**

不得创建、修改或撤销订单。使用部署时仍存在的当前 TPSL：

- 找一条公开 pending API 返回 `sz=0` 且已经精确归属当前仓位的保护单；
- 页面应显示 `全部剩余仓位（当前 … contracts / … BTC|ETH）`；
- 找一条 `sz>0` 的分段止盈，页面应显示部分 contracts 和基础币换算；
- 页面不应出现 `数量 0 contracts`；
- 未归属全仓保护单应显示 `全部仓位（具体仓位未归属）`，且不带猜测的当前仓位
  数量。

若当前实盘快照中恰好没有某一类订单，只报告“无可用生产样本”，以服务器 focused
测试和只读页面结果为准；不得为验证而创建真实订单或仓位。

**Step 6: 最终状态检查**

```bash
git status --short
```

Expected: 本次方案文件均已提交，用户原有未提交内容保持不变。
