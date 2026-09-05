# 管理止损合理性门禁：本地候选交付

## 范围与边界

- 分支：`codex/management-stop-price-gate`，独立 worktree：`/Users/steven/Documents/management-stop-price-gate`。
- 基线：用户指定生产版本 `af8676dca5ce83acfc060a8b856ccf3884f25150`。不包含共享 checkout 的其他提交或未提交文件。
- 仅本地代码、隔离 SQLite 测试库及假交易所客户端；无生产连接、schema 变更、业务数据修复、交易所写操作、合入或部署。
- 未读取/修改生产 uncertain 6/25/77/191/199；未重放 raw 15013、未修改 candidate 2203；未操作 ETH 实验仓位/保护单。
- 用户提供的事件作为缺陷背景；回归使用新建合成记录，不能视为对原交易所 outcome_unknown 的重新定性。

## 实现与参考价

`management_stop_price_gate.py` 提供共享纯校验、报价检查和拒绝记录。planner 在批次/组件形成前校验；legacy executor 在入口及取消保护单之前重新校验；composite executor 在第一个组件之前检查语义冲突。旧批次也受执行边界保护。

| 场景 | 参考价与行为 | 证据 |
|---|---|---|
| 已有持仓的显式止损调整 | 当前同一合约 ticker 的 last/lastPx；多单 stop < last，空单 stop > last。相等也拒绝 | 市价、字段、报价时间、实际检查时间、方向、开仓均价列表、偏离及阈值 |
| 锁利润止损 | 允许多单止损高于开仓均价、空单低于开仓均价，只要在当前市价正确一侧且满足现有风险收紧检查 | 同上；不错误地以开仓价作为方向边界 |
| 隐含保本 action | 保留现有实际开仓均价目标与市场安全处理；显式价与其同时出现则整体拒绝 | action、stop_mode、stop_price；reference_price_source=not_used_semantic_conflict |
| 市价缺失、错合约、非 last 字段、过期、未来、非有限或非正 | 拒绝；不回退到开仓价 | management_stop_reference_unavailable |

偏离公式为 `abs(stop - last) / last * 100`，超过阈值拒绝，等于阈值通过（仍需方向正确）。先判幅度，再判方向，保证 QQ 形态的离谱值得到稳定独立原因。默认 `max_management_stop_deviation_pct=10.0` 表示 10%，使用现有 TradingSettings / JSON 设置存储和正数配置风格；另有 `management_stop_quote_max_age_seconds=30.0`。默认值是可调整的保守拒绝界限，没有盈利最优性声明。本次不修改任何生产配置。

报价新鲜度使用独立的当前 UTC 检查时钟。业务 processed_at 可在 planner/executor 之间复用，不能用于判断新报价是否来自未来；已覆盖这一路径的回归。

provenance 原有必要条件保留；注释明确正文数字也可能是 QQ、电话、时间戳、点数或百分比，来源证明不等于金融语义证明。本次没有将价格异常降级为按保本价执行。合理范围内的签名数字仍可能通过数值门禁，这不是完整的文本语义识别器。

## 原因、Incident 与停止语义

新增专用原因：

- `management_stop_action_conflict`
- `management_stop_deviation_exceeded`
- `management_stop_direction_invalid`
- `management_stop_provenance_invalid`
- `management_stop_price_invalid`
- `management_stop_reference_unavailable`
- `management_stop_configuration_invalid`
- `management_stop_tick_invalid`

新门禁拒绝会记录 `RuntimeIncident.incident_type=management_stop_rejected`，以 batch + reason 形成确定性 fingerprint，记录结构化诊断和 raw/batch 引用。记录不依赖可选 AI capture 开关，也不启用新 AI playbook。Incident 写入失败会传播并停止执行，不降级放行。

规划拒绝记录 blocked 批次、无管理腿/组件；执行阶段 ready/protection_ready 拒绝转 blocked，已 executing 的批次转 recovery_required，保留可能已经发生副作用的不确定性。既有其他保护归属/收紧门禁仍保留。

## 验证

- RED：首次规划运行 5 failed，其中 4 个是预期新 reason 的断言失败，1 个是复合夹具 close_fraction 错误（后来修正，不计作有效 RED）。2 个旧批次执行回归均为未按门禁拒绝的断言失败。另有 19 个纯校验用例在模块未实现时失败，不将缺模块错误当作行为复现。有效行为 RED 共 6 项。
- GREEN：最终管理相关 focused 336 passed；随后纯校验模块格式整理后的 23 passed。早一轮含 settings/contracts 的 focused 为 569 passed。
- 覆盖：QQ 价、多空错误方向及等于市价、幅度边界与可配置值、盈利锁定的合理价格、action/mode 冲突、无来源、NaN、tick、错合约/过期/未来报价、当前时钟、配置持久化、非有限配置、Incident、零写入、首组件前拒绝、写边界市价变化。
- 独立 review 发现并修复：报价检查误用旧业务时间；旧 provenance/tick 检查截断 Incident 路径。复核 P1/P2 已闭环。
- `ruff check`（新模块及其测试）、`git diff --check` 通过。
- 首轮完整 pytest：7374 passed、15 failed、4 skipped、32 warnings（473.18 秒）。15 项均为新 worktree 缺 `.venv/bin/python` 导致部署脚本测试失败；补齐被 Git 忽略的本地解释器目录后，相关两文件 46 passed、1 skipped，原 15 项全部通过。未因此改动生产代码。
- 最终完整 pytest：**7389 passed、4 skipped、32 warnings，454.95 秒（7 分 34 秒），exit 0**。命令：`.venv/bin/python -m pytest -q`。两轮完整测试间仅补齐本地解释器路径，生产代码不变；保留首轮环境失败证据。
- 本地原始日志：`/tmp/management-stop-integration-red.log`、`/tmp/management-stop-executor-red.log`、`/tmp/management-stop-final-clock-focused.log`、`/tmp/management-stop-full-pytest-initial.log`、`/tmp/management-stop-environment-recheck.log`、`/tmp/management-stop-full-pytest.log`。

## 其他数值提取路径审查（仅报告）

| 路径 | 当前防线 | 本地验证/剩余缺口 |
|---|---|---|
| 开仓止盈及止损 | `entry_price_geometry.py:121` 校验有限正值、开仓区间和方向；`auto_trade_execution.py:657` 使用该校验 | 缺统一幅度上限。纯函数复现：long entry=79519、SL=79000、TP=158241758 返回 valid；short entry=79519、SL=158241758、TP=79000 也 valid。这仅证明几何门禁接受，未证明完整交易执行会成功 |
| 杠杆文本 | `mimo_v2_contract.py:414` 主要是有界参数文本；`message_recognition.py:3745` 提取数字+“倍” | 纯函数可将“使用158241758倍”提取为 158241758x，缺数值上限。当前 Deepcoin order builder/client 未找到消费该文本并设置杠杆的路径，因此不能声称发生过错误杠杆写入 |
| 管理减仓比例 | `management_directives.py:488` 汇总字段/百分比并拒绝矛盾；`strategy_management_sizing.py:30` 约束实际持仓、剩余数量及步长 | `_fraction_value` 越界返回 None；合成“减仓”+partial_take_profit+close_fraction=158241758，resolve_management_directive 返回 fraction=0.5、partial_risk_reduction。存在“非法值被当缺失后默认半仓”的同类风险，应另项改为明确拒绝 |
| 数量/风险倍率 | 减仓数量来自可信持仓和比例，不直接把正文整数当合约张数；entry quantity 按风险预算/价格距离计算；entry_fragment risk_multiplier 上限 1 | 未发现同样的无界正文整数直接下单通路；上述减仓比例默认化仍需单独处理。不能以此宣布所有数值路径均安全 |

## 回滚与后续接受条件

本次为未部署的独立候选；拒绝候选即可，无生产回滚动作。未来若批准部署，应单独确认阈值与报价时间要求，用新鲜目标持仓/报价验证拒绝与合理价通过，并观察 management_stop_rejected 的原因分布；不得通过放宽 provenance、重放原 uncertain 指令或改保本目标来消除拒绝。
