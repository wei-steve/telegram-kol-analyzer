# 保本价取策略价 + 保本类消息不再因附带数字被拒：实施规格

日期：2026-09-21
上位分析：`docs/plans/2026-09-21-break-even-stray-price-refusal-analysis.md`（第 8 节的规则 R1–R4，用户 2026-09-21 确认；两腿都成交时取**简单中点**）
验证等级：**L3**（真实交易所写入语义变化）。无 schema 变化、无数据修复。

## 1. 用户原则（不可偏离）

1. 保本价取**策略的价格**，不取我们自己的成交价：只有一条入场腿有仓位 → 该腿的策略价；两条腿都有仓位 → 区间中点。
2. 群组消息有离场 / 减风险意愿 → 跟着做，不要因为拿不准而拒绝或继续持有。
3. 贪婪入场是我们自己的选择；离场价位跟策略原价；小亏可以。
4. 任何情况下不得关闭 / 暂停真实自动交易开关；不产生开仓、加仓、放宽止损。

## 2. 代码现状（2026-09-21 核实）

- 入场腿：`execution_order_legs`（`purpose='entry'`、`leg_index` 1 / 2、`pos_id`）。每条成交的入场腿是一个独立仓位。
  区间入场的腿价由 `deepcoin_order_builder._range_entry_leg_prices` 给出：空单 腿 1 = `low − 偏移`、腿 2 = `high − 偏移`；多单 腿 1 = `high + 偏移`、腿 2 = `low + 偏移`；
  hybrid 模式下腿 1 实际用市价单（贪婪价）。**策略价 = 不含偏移、不含市价替换的区间端点。**
- 管理腿：`strategy_management_legs`（`execution_order_leg_id`、`leg_index`、`avg_entry_price`、`planned_tpsl_json`）。
- `leg.avg_entry_price` 目前身兼两职：
  (a) **身份 / 经济学核对**（与交易所 `avgPx` 比对，如 `break_even_convergence_executor.py:768`、`strategy_management_executor.py:2393`）——**绝不能改**；
  (b) **保本止损的目标价**：`strategy_management_executor.py:621`（`assess_break_even_market(entry_price=leg.avg_entry_price)`）、`:3663`（保本动作的计划止损）、
  `strategy_management_composite_executor.py:948`（`requested_stop = … else desired["avg_entry_price"]`）、`strategy_management_market_decisions.py:234 / 265`。
- 保本价挂不上（价格已越过）：`move_stop_to_break_even` → `break_even_by_market` → 该仓位**市价全平**；
  `partial_then_break_even`（复合）→ 减仓完成后 `requested_stop_market_side_invalid` → 组件 `operator_required`，**剩余仓位保持原止损、等人工**。
- 附带数字：`management_price_plausibility.sanitize_management_prices`（闸门前）只剔除与现价差 10 倍以上的；其余进入
  `management_stop_price_gate.stop_action_conflicts` → 整条拒绝。

## 3. 改动

### 3.1 新模块 `break_even_reference.py`（纯函数，无 I/O）

```python
@dataclass(frozen=True, slots=True)
class BreakEvenReference:
    price: str          # 十进制文本，未做 tick 归一（执行器沿用现有归一）
    source: str         # 见下
    evidence: dict      # 入参回显：side、区间、有仓位的腿序号

def resolve_break_even_reference(*, side, entry_range_low, entry_range_high,
                                 open_entry_leg_indexes, actual_avg_entry_price) -> BreakEvenReference
```

| 条件 | price | source |
|---|---|---|
| 区间有效（两端为正、low < high）且有仓位的腿 = {1} | 空单 `low`；多单 `high` | `strategy_first_leg` |
| 区间有效且有仓位的腿 = {2} | 空单 `high`；多单 `low` | `strategy_second_leg` |
| 区间有效且有仓位的腿 ⊇ {1, 2} | `(low + high) / 2` | `strategy_midpoint` |
| `low == high`（单一入场价） | 该价 | `strategy_single_price` |
| 无入场价（纯市价策略）、区间非法、腿序号不是 1 / 2、或任何入参无法解析 | `actual_avg_entry_price` | `actual_fill_no_strategy_price` |

"有仓位的腿"= 本批次管理腿所关联的 `execution_order_legs.leg_index` 集合（即计划时刻仍有在仓仓位的入场腿）。
区间取自目标 `strategy_lifecycles.entry_range_low / entry_range_high`。入参非法时**绝不抛错**，一律回落到最后一行（行为与今天相同）。

### 3.2 计划器

- 在形成管理腿时为每条腿算出参考价，写进该腿 `planned_tpsl_json`：`break_even_reference_price`、`break_even_reference_source`；
  同一份 evidence 写进批次 `target_snapshot`。**同一批次的所有腿用同一个参考价**（R1 的"两腿都成交 → 中点"是策略级的）。
- `avg_entry_price` 的写入与含义**不变**。

### 3.3 执行器：目标价与身份价分离

新增唯一取价函数（放在 `break_even_reference.py`）：

```python
def break_even_target_price(leg) -> str:
    """planned_tpsl 里有参考价就用它；没有（旧批次）就用 leg.avg_entry_price。"""
```

把第 2 节 (b) 列出的四处"保本目标价"改为调用它；第 2 节 (a) 的身份 / 经济学核对**一处都不动**。
`break_even_convergence_executor`（TP1 成交后系统自动保本）**不在本次范围**，保持用 `avg_entry_price`——见第 7 节待决问题。
部署前已存在的批次没有参考价 → 自动回落，行为不变（无需数据迁移）。

### 3.4 闸门前的数字处置（R2）

扩展 `sanitize_management_prices`（仍只对 `IMPLICIT_STOP_ACTIONS` 生效，`adjust_stop_loss` 完全不经过）：新增入参 `side`、`break_even_reference`（3.1 的结果）。
对合约止损价与 `stop_loss_text` 各自按顺序判定，得到 `disposition`：

| 顺序 | 条件 | 处置 | disposition |
|---|---|---|---|
| 1 | 与现价相差 > 10 倍（现有） | 剔除 | `implausible_magnitude` |
| 2 | 在现价的无效一侧（多单 P ≥ 现价；空单 P ≤ 现价） | 剔除 | `not_a_possible_stop` |
| 3 | 可挂，且比参考价**更紧**（空单 P < 参考价；多单 P > 参考价） | **采用 P 作为保本目标价**：参考价改为 P，`source = "message_explicit_tighter"`；P 仍须通过 `validate_management_stop` 的显式价检查（来源、偏离上限、方向）——不过则按第 4 行处理 | `explicit_tighter_adopted` |
| 4 | 其余（含比参考价更松，如 17813 的 81200 对 80500） | 剔除 | `superseded_by_strategy_price` |
| — | 报价不可用 | 第 1–3 行无法判断 → 直接按第 4 行剔除（用户原则 2：不因拿不准而拒绝） | `ignored_quote_unavailable` |

"剔除"沿用现有中和方式：内存候选视图 `stop_loss_text=None`，合约 `stop_mode → actual_entry_price`、`stop_price=None`，重算合约指纹；
第 3 行则把 P 写进 3.2 的参考价字段，合约同样中和（目标价只有一个出处：参考价字段）。
结果：保本类动作不再到达 `stop_action_conflicts` 的拒绝分支。**闸门代码与该函数保留不删**，作为纵深防御；
`validate_batch_stops` 对已中和的合约不会报冲突（用测试钉住）。

事故台账：第 1 行保持现有 `management_price_implausible`（高）；第 2–4 行与"报价不可用"记为新类型 `management_price_disposed`（**低**，不进任何 Telegram 类型表），
摘要里带 `disposition`。

### 3.5 保本价挂不上时（R3）

- `move_stop_to_break_even`：逻辑不变，只是参考价换成 3.3 的目标价 → 越过则该仓位市价全平（现有行为）。
- `partial_then_break_even`（复合）：**本规格不改**——减仓后止损挂不上仍是 `operator_required` + 告警。是否改为"剩余仓位也市价全平"见第 7 节，待用户决定后另行实施。

> **2026-09-21 追加（已实施，另行提交）**：用户已拍板"剩余仓位也市价全平"。
> 设计见 `docs/plans/2026-09-21-composite-remainder-market-close-design.md`（方案 A），
> 实施状态见 `docs/break-even-strategy-price-status.md` 第 12 节。
> 触发条件收得很紧：只有 `requested_stop_market_side_invalid`、
> 只有 `actual_entry_price` 合约、本组件从未开始挂新止损、闸门为真、
> 且一次**不进缓存的新鲜 ticker** 二次确认同样越过，才会全平。
> 兜底路径上不撤、不改任何保护单；原止损一直武装到仓位变平为止。

## 4. 测试

1. `resolve_break_even_reference`：上表每一行 × 多 / 空；端点、`low == high`、`low > high`、None、负数、非数字、腿序号 0 / 3 / 空集；永不抛错。
2. 三条生产样本回归（按分析文档第 3 节与第 8 节的数字构造）：
   - raw 17813：空单，区间 80500–81600，仅腿 1 有仓位（`avg_entry_price=80436`），消息数字 81200，现价 80450 → 不再 `blocked`；参考价 `80500 / strategy_first_leg`；
     disposition `superseded_by_strategy_price`；复合批次的止损目标 80500。再加一条现价 80600 的变体 → 减仓完成、止损组件 `operator_required / requested_stop_market_side_invalid`（现状，见 3.5）。
   - raw 15475：空单 `move_stop_to_break_even`，数字 2450、现价 2455 → `not_a_possible_stop`，按参考价保本。
   - raw 15402：QQ 号 → 现有 `implausible_magnitude`，行为不变。
3. 两腿都有仓位 → 两条管理腿都用 81050；仅腿 2 有仓位 → 81600（空单）。
4. `explicit_tighter_adopted`：空单、参考价 80500、现价 79000、消息"止损移到 80000" → 目标 80000；同一数字但来源校验不过 → 回落为剔除、目标 80500。
5. 报价不可用 → `ignored_quote_unavailable`，照常保本，不拒绝。
6. 身份核对不受影响：构造 `avg_entry_price` 与参考价不同的腿，断言第 2 节 (a) 的比对仍用 `avg_entry_price`（现有相关测试全部保持通过）。
7. 旧批次（`planned_tpsl_json` 无参考价）→ `break_even_target_price` 返回 `avg_entry_price`，执行结果与改动前逐字一致。
8. `adjust_stop_loss` 带任意数字 → 完全不经过新处置，现有闸门测试全部保持通过。
9. `validate_batch_stops` 对中和后的持久化合约不报冲突；合约指纹与中和后的内容一致。
10. 既有的 3b、闸门、复合执行器、`break_even_by_market`、保本收敛测试全部保持通过。最终候选跑**一次**全量 `uv run python -m pytest -q`。

## 5. 行为变化清单（上线即生效）

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 保本类消息附带任何数字 | 整条拒绝，零操作 | 照常执行；数字按 3.4 处置 |
| 保本止损挂在哪 | 我们的实际开仓均价 | 策略价（3.1）；仅当消息给了更紧且合法的价位时用消息的价位 |
| 纯市价策略 / 旧批次 | 我们的均价 | 不变 |
| `adjust_stop_loss`、入场、止盈、全平、TP1 自动保本 | —— | 不变 |

## 6. 部署与回滚

- `tg-deploy <sha>`（重启 worker / web / ingest，用户已确认可以），选零在途窗口；**自动交易开关全程不动**。
- 回滚：`tg-deploy <上一个生产 sha>`。回滚后：新批次恢复旧行为；已按新规则**完成**的批次不受影响；
  处于执行中的批次里多出的 `planned_tpsl_json` 字段会被旧代码忽略（旧代码只读 `avg_entry_price`）——部署与回滚都要求零在途，避免同一批次跨版本执行。
- 首笔实盘样本：第一条命中新规则的真实保本消息，逐项核对交易所上的减仓数量、新止损价（应等于参考价按 tick 归一后的值）、旧止损已撤净。

## 7. 待用户决定（不阻塞本规格的实施）

1. ~~**复合指令（减仓后保本）里止损挂不上时**：现状是减仓完成、剩余仓位保持原止损并等人工（值守会告警）。按原则 2，是否改为"剩余仓位也市价全平"？~~
   **2026-09-21 用户已决定：改为剩余仓位市价全平。** 设计
   `docs/plans/2026-09-21-composite-remainder-market-close-design.md`，
   状态 `docs/break-even-strategy-price-status.md` 第 12 节；本地已实施，
   未推送未部署。原文保留作为记录。
2. **TP1 成交后的系统自动保本**（`break_even_convergence`）目前用我们的实际均价。是否也改用策略价？

## 8. 提交与汇报

- worktree 分支上提交；**禁止 `git add -A`**；不 push、不部署、不连服务器。
- 汇报：提交 SHA 与文件清单；四处目标价替换的确切位置与确认未触碰的身份核对位置；"有仓位的腿"在计划器里的确切取法；
  全量测试结果；偏离规格之处及理由；规格里你认为有问题或遗漏的地方（尤其是参考价与现有"保留更紧的已有止损"逻辑的相互作用）。
