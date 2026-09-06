# 阶段 5：限价入场从 trigger-order 迁到 order，并由新绑定链驱动

风险等级：**L3**（改变交易所写入语义）。这是本项目第一个真正动交易所写入的阶段。

本文件自包含。执行会话只读 `AGENTS.md`、`docs/ARCHITECTURE.md`、
`docs/rest-ws-trading-status.md` 和本文件，不要读其他阶段文件。

**领取前必须取得用户对本阶段的单独批准。** 批准必须针对本阶段，
不能引用阶段 1/2/4 的 schema 批准，也不能由"阶段 4 差异报告好看"自动推导。
批准前必须先向用户出示阶段 4 的差异报告数字与本文件"前置受控实验"的结论。

## 目标

把入场腿的交易所写入语义改成：

- `market` 腿：**接口不变**（今天已经是 `POST /deepcoin/trade/order`），
  改为由新绑定链驱动其保护归属。
- `limit` 腿：从 `POST /deepcoin/trade/trigger-order` 改为
  `POST /deepcoin/trade/order`（`ordType=limit`），并由新绑定链驱动。

**仅限新入场。** 已经在交易所上的仓位、已挂出的保护、历史 binding 一律不动。

## 前置（缺一不可）

- 阶段 4 已 `completed`，差异报告已产出，`shadow_only` 与 `ledger_only`
  每条都有归因。
- 用户已针对本阶段单独批准。
- **前置受控实验已完成**（见下节），且实验结论支持迁移。
- 阶段 2 的 `ws_observation_permits_new_entry()` 已实现且在生产里可用。

## 前置受控实验（必须先做，实验本身也需要用户单独批准）

交接文档 12 项补测里的第 1、2、3、6、11 项落在这里。它们都需要真实下单，
所以是一次**独立的、最小规模的、用户逐条批准的受控实盘实验**，
在改任何生产代码之前做完。

实验用现成的 `scripts/deepcoin_order_tpsl_experiment.py` /
`scripts/deepcoin_rest_ws_tpsl_experiment.py` 的模式：独立目录、独立 venv、
一次性锁、无重试、原始请求/回执落证据、只撤本次未成交余量、不自动平已成交仓位。
每一笔都必须在下单**之前**先建立 WS 订阅（否则拿不到 `TU` 变化）。

| # | 补测项 | 实验设计要点 |
|---|---|---|
| 1 | 普通限价多单 | 与已成功的空单对称：最小量、市价减 1 的限价、TP/SL 各 10 USDT。验证多单方向的 `posSide=long` / `side=buy` 组合与 `TU` 变化 |
| 2 | 部分成交和多次成交 | 下一笔数量足够被拆成多次成交的限价单，观察 `Order.VT`（VolumeTraded）多次推送、多条 `Trade`、`Position.Po` 递增，确认影子链在部分成交时保持 `partially_filled` 而不是提前 `filled` |
| 3 | 未成交撤销 | 挂一笔远离盘口的限价单后精确按 ordId 撤销，观察撤销后附带的 TPSL 是否一并消失；这一项决定"撤单后要不要额外清理保护" |
| 6 | 两张同方向、同数量、同价格订单并发 | **DuplicateAction 根因专项。** 单变量法：先单笔不带 clOrdId（已知成功）；再单笔带 clOrdId；再并发两笔不带 clOrdId；再并发两笔带不同 clOrdId。四组各自独立、各自一次性锁，不在同一轮自动切换参数重试 |
| 11 | REST 响应丢失但交易所实际接受时的恢复 | 在**客户端侧**注入极短超时使响应丢失（不要用网络中断，那不可控），确认代码抛 `DeepcoinRequestOutcomeUnknown` 且**不重发**；然后用 REST 按时间窗查询确认交易所是否真的接受了，验证恢复流程能把它认出来 |

第 6 项是本阶段的成败关键。阶段 0 已确认：**生产市价单一直带 clOrdId 且成功**
（149 条成功市价入场均提交并回传同值），而实验里被 `DuplicateAction` 拒绝的是
`ordType=limit` + `clOrdId` + `tpTriggerPx`/`slTriggerPx` 的组合。
所以判重键与 `ordType` 或附带 TPSL 参数有关，不是"该账户拒绝一切 clOrdId"。
现成的 `build_deepcoin_place_order_payload`（`recovery_live_submit.py:2979`）
**无条件写入 `clOrdId`**，直接拿来用大概率复现 `DuplicateAction`。

实验结论必须明确回答：**限价 order 的哪一组字段组合是可用的。**
回答不了就不要开始改代码，把阶段留在 `blocked` 并报告用户。

## 任务（实验通过后才开始）

### 1. 限价腿改走普通 order

`recovery_live_submit.py` 的 `limit` 分支：
`build_deepcoin_trigger_order_payload` + `trigger_order`
→ 新的限价 payload builder + `place_order`。

- payload 字段组合以前置实验的结论为准，**不要**直接复用现成的
  `build_deepcoin_place_order_payload`。若实验证明必须去掉 `clOrdId`，
  就去掉，并在代码注释里写清楚这是实测结论、引用证据路径。
- 保护字段：普通 order 的创建表明确列出的只有 `tpTriggerPx` / `slTriggerPx`。
  **不要**把 trigger-order 的 `slOrdPx=-1` 等参数原样搬进来——
  `docs/2026-09-05-order-tpsl-fields-and-test.md` 明确指出这是未经实测的扩大。
- 幂等：本地生成并持久化自己的幂等键，**不依赖 clOrdId 或 tag 作为交易所所有权证明**。
  REST 成功回包的 `ordId` 是主订单身份。

### 2. 真正的条件触发策略保持不变

**这是硬约束，不是可选项。** 交接文档迁移顺序第 7 步："真正的条件触发策略继续使用
trigger-order，并保留独立父子归属流程。"

本阶段只迁移"`triggerPrice` 恒等于 `price`、触发语义为空"的入场限价腿。
任何带真实突破/回落条件、`last`/`mark`/`index` 价格来源选择的策略腿
**必须继续走 trigger-order**。代码里要有明确判据区分这两类，
判据不明确就不迁那一类。

### 3. 新绑定链驱动

- 入场提交成功后，用阶段 4 的影子链逻辑（此时转正）建立绑定：
  五条判据同时成立才写 `exact` 绑定。
- 不成立 → 保护保持 `unverified`，**禁止自动修改、撤销或认领**，
  按现有路径走告警与人工。
- market 腿同样接上这条链（接口不变，只是归属改由新链给出）。

### 4. 接上"暂停新入场"

把阶段 2 建好的 `ws_observation_permits_new_entry()` 接到入场路径：
WS 未订阅或处于 gap 状态时**暂停新入场**（fail-closed，状态未知返回 False）。

暂停必须是"不提交新入场"，不是"提交后再撤"。暂停要产生可观测的原因码，
不能静默吞掉入场意图。

### 5. 失败关闭

- REST 写入超时/响应不完整 → `unknown_exchange_outcome`，**绝不自动重发**。
  沿用 `DeepcoinRequestOutcomeUnknown`，不要在新代码里降级成普通异常。
- 外层 `code=0` 必须逐条检查 `data[].sCode`，走
  `_raise_for_deepcoin_business_error`。`DuplicateAction` 是 `sCode=14` 的软拒绝，
  外层是 200 + code 0，**这是最容易被误判为成功的一种失败**。

### 6. 回滚开关

本阶段必须有一个**不需要改代码就能回到 trigger-order 限价腿**的路径。
最简单可靠的是"回滚到上一个 SHA"（`tg-deploy <pre-deploy-sha>`），
这已经足够，**不要**为此引入运行时模式开关
（`docs/ARCHITECTURE.md` 第 6 节禁止重新引入模式开关做灰度）。
回滚 SHA 必须在部署前记录并写进汇报。

## 禁止

- 禁止在前置受控实验完成并得出明确字段组合结论之前修改任何生产下单代码。
- 禁止把真正带触发条件的策略腿改成普通 order。
- 禁止对已有仓位、已挂保护、历史 binding 做任何迁移动作。本阶段只管新入场。
- 禁止用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领。
- 禁止把 `unknown_exchange_outcome` 自动重发。
- 禁止把外层 `code=0` 当成功。
- 禁止在同一轮实验里自动切换参数重试（单变量法）。
- 禁止为了赶进度跳过任何一项前置补测。
- 禁止引入运行时模式开关做灰度。
- 禁止用 `git add -A`。

## 验证等级与具体检查项

等级 **L3**（交易所写入语义变更）。

### 前置受控实验

- [ ] 第 1、2、3、6、11 项各自有独立证据目录、独立一次性锁、原始请求/回执。
- [ ] 第 6 项四组对照全部执行，给出"哪组可用"的明确结论。
- [ ] 实验产生的任何仓位在实验结束时的处置已记录（平仓/保留/仍有保护）。
- [ ] 实验期间零意外写入：每一次 POST 都能在证据里找到对应的批准与回执。

### schema / 数据

- [ ] 本阶段若不加表不加列，则无需副本演练；若加，则按 L3 全套执行。
- [ ] 记录 `execution_bindings`、`position_protection_ledger`、
      `trigger_protection_intents`、`position_take_profit_orders` 的
      部署前后行数与变化归因。

### 测试

- [ ] focused：新限价 payload 的字段白名单（多一个字段就失败）、
      条件策略腿不被迁移的判据、五条绑定判据、`unverified` 时禁止自动动作、
      `sCode=14` 被正确识别为拒绝、`unknown_exchange_outcome` 不重发、
      `permits_new_entry=False` 时不提交。
- [ ] 回滚测试：确认回滚 SHA 上的限价腿仍然走 trigger-order 且能正常提交
      （静态或离线验证即可，不需要真下单）。
- [ ] 最终候选跑一次全量套件（记录已知既有失败）。

### 生产观察

- [ ] 观察 30 分钟，覆盖至少 5 条真实消息、尽量 2 个群；
      不足 5 条则停止、留 `in_progress`、记录流量不足。
- [ ] **必须直接查交易所历史**：窗口内每一笔入场写入逐条核对，
      确认接口、字段、方向、数量与预期一致。
- [ ] 每一笔新入场的保护是否挂上必须逐笔确认。
      `docs/2026-09-05-codex-handover-closeout.md` 第七节写明：
      "以损定量、亏损有界"的前提是**止损确实挂上**；止损未挂时亏损边界是爆仓价。
      本阶段窗口内出现任何一笔"成交但无可验证保护"，立刻回滚并报告。
- [ ] `authoritative_execution_attempts` 新增 `uncertain` 条数与归因。
- [ ] 重启 worker 一次并确认恢复正确（本阶段涉及写入路径与恢复，重启是必须的）。
- [ ] 若窗口内没有发生真实新入场，**不要**为了验证去下单；
      把阶段留 `in_progress`，记录"未获得真实新入场样本"，
      并说明还需要什么才能结项。

## 完成条件

1. 前置受控实验全部完成并有明确结论。
2. 上面全部检查项通过，或流量不足/无真实样本已如实记录且阶段留 `in_progress`。
3. 提交已推送并 `tg-deploy` 部署，回滚 SHA 已记录且回滚路径已验证。
4. 更新 `docs/rest-ws-trading-status.md`：推进到阶段 6
   （`phase-6-protection-authority.md`），证据区追加一行。
5. 发消息给 `brain_session_id`，摘要必须包含每一笔新入场的保护挂载确认结果。

## 汇报格式

```text
阶段 5 完成 / 阻塞 / 无真实样本留 in_progress
用户批准：<引用用户批准的原话时间点>
分支与 SHA：<branch> <40位sha>
部署：tg-deploy <sha>，回滚 SHA <pre-deploy-sha>，回滚路径已验证
前置受控实验：
  第 1 项（限价多单）：
  第 2 项（部分/多次成交）：
  第 3 项（未成交撤销）：
  第 6 项（并发与 clOrdId 四组对照）：可用字段组合 =
  第 11 项（响应丢失恢复）：
  实验仓位处置：
迁移范围：limit 腿迁移；market 腿接口不变仅换归属；条件策略腿未迁（判据说明）
测试：focused N passed；全量 N passed / N skipped / N failed（列出既有失败）
观察窗口：<起> ~ <止>（30 分钟），真实消息数、覆盖群数
新入场笔数：N，逐笔保护挂载确认：N/N
新增 uncertain：条数与归因
重启验证：
账本行数变化与归因：
异常与遗留：
证据路径（服务器）：
```
