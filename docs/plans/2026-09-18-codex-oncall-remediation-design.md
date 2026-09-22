# Codex 值守补救：消息指令没执行时，自动诊断并补救

日期：2026-09-18
状态：用户 2026-09-19 批准方向与第 6 节四项决定；阶段 0 进行中；未实施
关联：`docs/runtime-incident-agent-status.md`（旧的自定义 AI agent，本设计取代它的"诊断"职责）、
`docs/ARCHITECTURE.md` 第 4 / 4.8 节、`docs/management-reliability-status.md`

## 1. 要解决的问题

用户原话：消息要求**调整止损价、调整止盈价、临时离场**，但系统没能按消息在交易所操作时，
或者系统出问题时，要能**自动补救**——因为出问题时用户不可能及时看到消息。

旧的自定义 AI agent（runtime incident agent）做不到，原因见第 2 节。服务器上已装 Codex CLI
（ChatGPT 账号登录），`codex exec` 可以无界面运行（已核实官方文档：`--sandbox read-only|workspace-write|danger-full-access`、
`--output-schema <json schema>`、`-o <file>`、`--json`、`-C <dir>`；ChatGPT 登录态存在 `~/.codex/auth.json`）。

**第一版范围（做）**：对**已有仓位的减风险类管理指令**补救——
调整止损、保本、部分止盈、全平 / 临时离场、调整止盈（见 3.3 的前置缺口）。

**第一版明确不做**：开仓、加仓、放宽止损、任何增加风险的动作；自动改代码并部署；重启服务。
这些分别留给第 8 节的后续阶段，各自单独批准。

## 2. 旧 agent 为什么没用（代码核实结论）

1. **它没有任何执行权**：`actions_enabled=false`、两个白名单为空；6 个手册里已实现的 4 个全是只读的。
2. **AI 在流程末端**：发现问题靠写死的规则 + 16 种封闭违约类型，规则没见过的故障既无提醒也无诊断。
3. **提醒依赖 worker 进程，且代码默认值是关闭**（生产实际值已核实，见 7.1：前两个开关生产上是开着的，
   真正的问题是提醒多而不可操作、逐消息监督器关着、独立监控定时器没在跑）：
   - 所有告警发送循环只在 `worker` 角色里跑；worker 冻结 / 任务退出时告警一起哑（2026-09-15/16 两次事故正是这样）。
   - `TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES` 为空时，**一条事故都不落库**，
     `ALWAYS_NOTIFIED_INCIDENT_TYPES` 也救不回来（`config.py:565` 只在非空时才并入）。
   - `strategy_management_notification_delivery_after_id` 默认 `None` = 管理批次
     `blocked / partial_failed / submit_unknown / recovery_required` 的告警**只入库不发送**。
   - Stage 1 逐消息告警的水位线默认是 `_SQLITE_MAX_INTEGER`（永不认领）。
4. 模型是 MiMo、3 次取证、45 秒、16 KB 上下文，复杂问题只会得到"证据不足"。

## 3. 现有执行链路里与补救相关的事实

（完整链路图由子代理梳理，要点如下；文件行号以 2026-09-18 的 `5e621600` 为准）

### 3.1 已经有一条确定性的"重推"通道

`position_management_remediation.py`（CLI `repair-position-management`）：

- `build_position_management_remediation_plan(...)`：扫描权威识别产生的管理候选，挑出指令项
  `failed / unknown`、或 `succeeded` 但结果是 `skipped / shadow_planned` 的，生成**带指纹的动作清单**；
  交易所快照不完整则整份计划作废。
- `apply_position_management_remediation_action(..., action_id, expected_fingerprint)`：
  按顺序核对 live 开关 → 动作指纹 → 重新投影候选 → 计划批次 → **重建整份计划再比一次指纹** →
  交易所快照指纹 → 标记确认 → 走 `execute_management_batch`（即 4.8 节那套"先挂后撤 / 撤前回读 / TU 归属"）。

**这就是补救应该走的通道**：价格、目标、数量都来自权威识别和确定性计划器，AI 编不出来；
所有交易所写入仍经 `PositionMutationGateway` 的所有权闸门。缺的只是：它现在只能由人登服务器敲命令触发。

### 3.2 只有 worker 进程持有交易所密钥

Deepcoin 密钥只在 `/etc/telegram-kol-worker.env`，只有 `telegram-kol-worker` 用户的进程读得到；
`position_authority_lock` 是**进程内**锁，正确性依赖"worker 是唯一写入者"。
所以补救**必须由 worker 执行**，外部进程只能通过队列 / 回环端点请求它，绝不能另起进程直连交易所。

### 3.3 梳理中发现的三个缺口（与补救直接相关）

| # | 缺口 | 影响 |
|---|---|---|
| G1 | **"调整止盈价"根本没有自动化路径**：`resolve_management_directive` 不产出该意图，`SUPPORTED_INTENTS` 里也没有；交易所层的 `adjust_take_profit` 能力存在但没有生产者 | 这类消息今天必然不执行——不是故障，是功能缺失；补救通道同样覆盖不到，需先补意图（第 8 节阶段 5） |
| G2 | **`/choose` 之后没有消费者**（疑似缺陷，待验证）：`choose_management_target` 把指令项改回 `pending`，但不重新入队；唯一的重试循环只认带 `visibility_first_failed_at` 的项 | 用户回答了确认问题，指令仍不执行 |
| G3 | `recovery_required` 批次超时后只解冻、**永不重跑**（`management_recovery_timeout.py:18` 明文）；`request_management_target_confirmation` 无可停放项时**不落任何事故** | 失败后无人接手、无人知晓 |

## 4. 方案总览

原则一句话：**Codex 是判断者，不是执行者。** 它读证据、下结论、在"确定性计划器已经算好的动作"里选；
真正写交易所的仍是 worker 里现有的那条通道，外加一道不含 AI 的确定性闸门。

```
┌─ 值守进程 telegram-kol-oncall（独立用户、独立 systemd 单元、只读挂载生产库）─┐
│ ① 检测器  每 60 s，按 id 水位线做轻量查询 → 发现"该执行却没执行"→ 建案件      │
│ ② 即时告警 自己持 bot token 直发中文 Telegram（不经过 worker）                │
│ ③ Codex 运行器  codex exec 只读沙箱，单飞；输入案件包，输出 schema 约束的裁决 │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ 回环 HTTP（共享令牌）→ worker:8002
┌─ worker（唯一持密钥者）──────▼──────────────────────────────────────────────┐
│ ④ 确定性闸门  白名单 / 不可推翻的拒绝 / 时效 / 幂等 / 限额 / 熔断             │
│ ⑤ 现有通道   remediation plan → apply（指纹）→ execute_management_batch       │
│ ⑥ 回读验证   结果写回案件                                                     │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               ▼
                 ⑦ 值守进程发中文结果通知（做了什么 / 为什么 / 现在仓位状态）
```

### 4.1 检测器（确定性，不用 AI）

值守进程自己的状态放在 `/var/lib/telegram-kol-oncall/cases.db`，**不写生产库**（不参与写锁竞争——
09-16 的锁事故教训）。生产库只读挂载，只做按主键 / 水位线的小查询（遵守"不在生产库上重扫描"）。

| 规则 | 条件 | 走哪条线 |
|---|---|---|
| D1 | 管理类 `message_instruction_items` 终态为 `failed / unknown`，或 `succeeded` 但结果 `skipped / shadow_planned`，或 `awaiting_user_confirmation` 超过 10 分钟 | A 线 |
| D2 | `strategy_management_batches` 处于 `blocked / partial_failed / recovery_required / submit_unknown` 超过 2 分钟 | A 线 |
| D3 | 来自**当前有持仓的群**的消息，决策行是"有损"跳过：`target_not_verifiable`、`mimo_authoritative_failed`、`authoritative_gap_recovery_expired`、`lifecycle_apply_failed`、上下文 `unresolved / exhausted` | B 线 |
| D4 | `message_processing_jobs` 有 `pending / claimed` 超过 3 分钟 | 健康 |
| D5 | worker / ingest 的 systemd 状态非 active，或 `/api/runtime/loop-health` 连续 3 次无响应 | 健康 |

每个案件按 `(raw_message_id, 规则)` 唯一，建案即发②的告警。健康类案件第一版只诊断 + 告警（不重启服务）。

### 4.2 Codex 运行器

- 身份：**直接用服务器上已登录的 Codex，不复制凭据**（用户 2026-09-19 决定）。值守单元以"已登录 Codex 的那个系统用户"运行
  （阶段 0 核实是哪个用户）。**注意**：`--sandbox read-only` 只拦写入和命令联网，**不拦读取**——Codex 能读到该用户能读的一切。
  所以无论是哪个用户，值守的 systemd 单元都必须用 `InaccessiblePaths=` 把 `/etc/telegram-kol-worker.env`、
  `/etc/telegram-kol-*.env`、`config/*.env`、`data/telegram.session*`、`data/backups` 挡在该单元的命名空间之外；
  若已登录用户是 root，这一条是硬性前提（否则交易所密钥可能被读到并随上下文发给 OpenAI），并在阶段 1 验收时
  用"让 Codex 去读这些路径、应当失败"的探针验证。
- 调用：`codex exec --sandbox read-only -C <案件目录> --output-schema verdict.schema.json -o verdict.json --ephemeral "<固定提示词>"`，
  超时 8 分钟，全局单飞，同一案件最多 2 次，每天上限 20 次（防 ChatGPT 额度被一个反复告警烧光）。
- 案件目录内容：`case.json`（检测器导出的**有界**证据：消息原文、决策行、候选、指令项、批次 / 腿 / mutation intent、
  执行事件、相关事故、仓位与保护单快照、过滤后的近 10 分钟 journal 摘录）+ 已部署源码的只读检出 +
  （A 线）worker 返回的**补救计划**（动作清单 + 指纹）。Codex 不直接碰生产库。
- 消息原文在 `case.json` 里标注为**不可信外部文本**；只读沙箱里命令无网络、不能写文件；
  Codex 唯一的产出是 schema 约束的 JSON，而这份 JSON 还要过④的闸门。注入的最坏结果被限制在闸门白名单之内。
- **Codex 可用性判断（用户 2026-09-19 要求）**，三层，任何一层失败都绝不静默：
  1. **例行自检（用户 2026-09-19 关心 token 消耗后收紧）**：每 6 小时只跑 `codex login status`（本地检查，**不耗 token**）；
     真正的最小 `codex exec`（"Reply with exactly: OK"，证明凭据真的能换到回答，而不只是文件存在）**每天只 1 次**，
     与 09:00 报平安合并。失败 → 告警"Codex 不可用：<类别>"，值守进程继续跑检测和即时告警。
     **token 只花在真实案件上**：每 60 秒的检测是纯 Python + SQLite，不调用任何模型；Codex 仅在建案时按需调用
     （阶段 0 数据：约每两天 1 条 + 偶发停摆），另有每日 20 次硬上限。
  2. **每次调用的失败分类**：复用 `scripts/codex_exec_smoke_test.py` 的 `classify_failure`——
     `登录凭据失效`（not logged in / 401 / token expired / refresh failed）、`额度用尽或限流`（usage limit / 429）、
     `网络不通`、`超时`、`输出不满足裁决契约`、`其他`。案件标 `codex_unavailable:<类别>`，告警里写明类别和"需人工"。
  3. **降级与恢复**：连续 3 次不可用 → 进入 `codex_down` 状态，此后新案件只发即时告警、不再尝试调用（不白等 8 分钟）；
     例行自检通过后自动恢复，并补发一条"Codex 已恢复"。登录失效类需要用户重新 `codex login`，告警里直接给出这条命令。
- 上线前先用冒烟脚本验证：`python3 -B scripts/codex_exec_smoke_test.py`（纯合成数据，6 步各自打印 PASS/FAIL，
  退出码区分失败类别：10 找不到 / 11 登录 / 12 额度 / 13 网络 / 14 超时 / 15 裁决契约 / 16 沙箱 / 19 其他）。
  第 4、5 步的合成消息里带一段提示词注入，验证 Codex 不被带偏、且"瞬时故障→选计划动作 / 安全拒绝→判合理拒绝"两个方向都判对。

裁决 schema（要点）：

```json
{
  "case_id": 0,
  "verdict": "apply_planned_action | propose_instruction | legitimate_refusal | no_action_needed | need_human",
  "action_id": "...", "expected_fingerprint": "...",
  "proposed": {"intent": "...", "target_lifecycle_id": 0, "stop_price": null, "fraction": null},
  "confidence": "low | medium | high",
  "explanation_zh": "给用户看的大白话：消息要什么、系统为什么没做、我打算怎么办",
  "root_cause_zh": "...", "suspected_bug": true, "code_paths": ["src/..."]
}
```

### 4.3 两条补救线

**A 线（重推）**：识别是对的，执行没成。Codex 只能从 worker 给的计划里选一个 `action_id + 指纹`。
价格 / 目标 / 数量全部来自权威识别，Codex 无从编造。

**B 线（补识别）**：识别本身失败 / 过期 / 目标歧义。Codex 给出结构化指令（意图、目标 lifecycle、价格 / 比例），
由 worker 投影成 remediation 候选后走同一条通道。B 线的额外闸门更严（见 4.4 第 7 条）。

### 4.4 确定性闸门（在 worker 内，apply 之前；不含 AI）

1. 模式开关 `oncall_remediation_mode = off | shadow | live`，外加一键总闸（Telegram `/oncall_off`）。
2. 意图白名单：`adjust_stop_loss`（仅收紧）、`move_stop_to_break_even`、`partial_take_profit`、`full_exit`、`partial_then_break_even`。**永不**开仓 / 加仓 / 放宽止损。
3. **不可推翻的拒绝**——原始拒绝原因属于下列时只告警、不补救：
   所有权未验证（`*_ownership_not_verified`、`exact_position_write_gate` 拒绝）、保护单冻结（`protection_authority_frozen:*`、`protection_order_unattributable`）、
   源消息已删除、`explicit_stop_adjustment_not_risk_tightening`、`management_price_implausible`、`management_stop_direction_invalid`、
   `operator_dismissed`、自动交易对该群 / 该 KOL 关闭。
4. 时效：全平 / 临时离场 / 部分止盈 ≤ 60 分钟，止损类 ≤ 120 分钟（可调）；超时只告警。目标仓位必须此刻仍在仓。
5. 幂等与限额：每条消息每个意图只补救一次；同一仓位 10 分钟冷却；每天 ≤ 10 次；**连续 2 次补救失败或回读不符 → 自动降到 shadow 并告警**。
6. 现有通道的全部检查原样保留（指纹二次比对、快照指纹、网关所有权闸门、撤前回读）。
7. B 线追加：`confidence == high`；价格必须**逐字出现在消息原文**里（保本价除外，取自入场均价）；
   目标必须是**同一个群**里已验证绑定的在仓仓位；候选 ≥ 2 且 Codex 无法给出唯一目标 → `need_human`。

### 4.5 通知（全部中文、由值守进程直发）

- 建案即发：`⚠️ 【龚有财群】消息 #15660「止损上移到 2484」已过 3 分钟，交易所未见对应操作。卡在：目标仓位快照过期。Codex 正在诊断。`
- 裁决后：`🔧 诊断：……。将执行：ETH 空单止损 2500 → 2484（计划动作 a1b2）。` / `✅ 属于合理拒绝：……，无需处理。` / `🙋 需要你决定：……`
- 执行后：`✅ 已补救：新止损 2484 已挂并回读确认，旧止损已撤净。` 或 `❌ 补救失败：……，已降级为只提醒。`
- shadow 模式下的第二条改为"本来会执行：……"，并附 `/fix <案件号>` 供用户一键批准。

## 5. 隔离与安全小结

| 风险 | 对策 |
|---|---|
| Codex 拿到交易所密钥 / Telegram 会话 | 独立 OS 用户 + systemd `InaccessiblePaths`；密钥只在 worker 环境里 |
| Codex 直接写交易所或改生产库 | 只读沙箱；生产库只读挂载；唯一出口是 schema JSON → worker 闸门 |
| KOL 消息里的提示词注入 | 原文标注不可信；产出受 schema + 闸门白名单 + "价格须在原文中"约束 |
| 数据外流到 OpenAI | 发送内容限于消息原文、仓位 / 订单状态、源码；不含任何密钥（用户需知悉并接受） |
| 补救本身出错 | 只做减风险动作；走现有指纹通道；回读验证；连续失败自动熔断 |
| 值守进程自己挂了 | systemd `Restart=always` + 现有 30 分钟 monitor 增加一条"值守心跳过期"检查 |
| 额度 / 登录失效 | 每日上限、单飞；失败即告警"需人工" |

## 6. 用户已拍板的决定（2026-09-19）

1. **B 线允许 Codex 在目标歧义时替用户选仓位**——先 shadow（`/fix` 一键批准），攒够 5 个用户认可的样本再转 live。
2. **时效上限**：离场类 60 分钟、止损类 120 分钟，超过只提醒不操作。
3. **旧 agent**：现在起不再投入；值守补救的阶段 2 上线稳定后停掉侧车（`telegram-kol-runtime-agent.service`），
   保留事故台账表（只是数据）。
4. **Codex 登录**：服务器上已登录，直接使用，不复制凭据；必须加"登录失效 / `codex exec` 不可用"的判断（见 4.2）；
   先用一个小脚本测试 `codex exec` 的效果（`scripts/codex_exec_smoke_test.py`）。

## 7. 动手前的生产只读核实（P0，由指挥会话做）

- 三个告警开关的生产实际值（第 2 节第 3 点）——这决定"连提醒也做不好"到底是没配还是发了没看到。
- G2（`/choose` 无消费者）用生产上两条历史确认记录验证。
- 近 30 天"调整止盈价"类消息的实际去向（验证 G1）。
- 在服务器上以"已登录 Codex 的那个用户"跑 `python3 -B scripts/codex_exec_smoke_test.py`，记录 6 步结果与每步耗时
  （本机 macOS 0.149.0 的结果不能代表服务器：Linux 沙箱是 landlock/seccomp，内核不支持时第 6 步会暴露）。
  **结果（2026-09-19）**：服务器 `codex-cli 0.153.4`、**登录在 root 下**（ChatGPT）、内核 6.6.92、Python 3.11.6；
  6 步全 PASS——最小调用 5 s、两次带注入文本的裁决 19 s / 14 s 且方向都对、只读沙箱拦住写入。本机（macOS 0.149.0）同样全 PASS。
  推论：Codex 登录在 root 下，4.2 的 `InaccessiblePaths` 隔离是硬性前提。
- 生产库体积。
- 遵守：先 `VACUUM INTO` 快照，在快照上查。

### 7.1 阶段 0 核实结果（2026-09-19，生产 `VACUUM INTO` 快照，26 s，查完已删）

**告警开关（更正第 2 节第 3 点的猜测）**：`CAPTURE_TYPES`、`TELEGRAM_ENABLED=true`、`TELEGRAM_TYPES` 都已配置且非空，
所以常发类型基线有效；近 30 天实际**送达约 280 条**事故通知（`severe_protection_incident` 117、`management_target_refused` 33、
`authoritative_recognition_failed` 25 ……），管理批次通知 38 送达 / 11 pending。**提醒不是没发，而是发得多、不可操作、且发出时用户无法及时处理。**
真正关着的是：`TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED=false`（逐消息"该执行却没执行"的监督器）——
`message_operation_contracts` 只有 21 行、停在 raw 10007（现已 17750），Stage 1 outbox 0 行、handoff 0 行；
`telegram-kol-monitor.timer` 为 **inactive**（独立 30 分钟监控没在跑）；`management_recovery_required` 不在 Telegram 类型表里（4 条 pending 未发）。

**旧 agent 的实际产出**：全部历史 2 条 diagnosed、1 条 escalated、1 条 closed，2221 条 pending；
`runtime_agent_model_usage` **0 行**——自 2026-08-10 预算启用以来一次模型调用都没有发生过。

**近 30 天管理指令（`message_instruction_items`，kind=management，共 220 条）**：

| 去向 | 条数 | 性质 |
|---|---|---|
| `succeeded` 但结果是 `skipped / kol_or_group_auto_trade_disabled` | 121 | 该群 / KOL 没开自动交易，正常 |
| 真正在交易所执行成功（批次 succeeded：全平 12、保本 3、部分止盈 2；保护替换 2） | ≈ 19 | 成功 |
| `failed: target_strategy_binding_visibility_retry_expired` | 35 | **35 条全部没有执行绑定、没有仓位**——系统当初就没入场，无仓可管；不是漏操作，但被记成失败并产生噪音 |
| 有仓位却没执行：`prior_partial_batch_unresolved` 6、`confirmation_timeout` 5、`stale_pending_voided_*`（系统停摆）10、`protection_*` 4、`management_stop_action_conflict` 2、`close_final_preflight_failed` 1、`unknown` 若干 | ≈ 30 | **这才是补救的真实目标，约每天 1 条**，Codex 额度绰绰有余 |
| `adjust_stop_loss` 候选 29 条，对应批次 | **0** | 30 天里"调整止损价"一次都没在交易所执行过 |

**7.1 补充核对（2026-09-19，用 `/tmp/research-snapshot-20260916.db`）**：上表"有仓位却没执行 ≈ 30"偏高。
用三种方式交叉判断当时是否有真实仓位（lifecycle 的执行绑定、指令项 `strategy_instance_id` 的绑定、同群同币同向的在仓绑定）后，
**确有仓位而未执行的约 15 条 / 30 天**：`protection_missing_cancellable_order_id` 2、`protection_price_or_size_mismatch` 2、
`prior_partial_batch_unresolved` 3、`management_stop_action_conflict` 2、`revision_replacement_incomplete` 1、
`protection_recovery_bypassed_for_full_exit` 1、`visibility_retry_expired` 但同群同币同向有仓位 2（疑似目标没对上），
另有 5 条 `confirmation_timeout` 目标未定、无法归类。`stale_pending_voided_*` 10 条三种方式都查不到仓位。
**`adjust_stop_loss` 的 28 条**：18 条所在群未开自动交易、9 条无仓位，**有仓位而未执行的只有 1 条**
（item 949 / raw 14602，2026-09-03，BTC 多单止损→75850，`prior_partial_batch_unresolved`）——"零批次"不是功能坏了，而是几乎没有合格样本。
结论：补救的真实目标约每两天 1 条；同等重要的是**降噪**——约 280 条 / 月的告警里，大部分对应"无仓位"或"未开自动交易"的消息。

`/choose` 生产上从未被使用过（0 条），5 条确认全部超时——印证"用户无法及时回应"，G2 暂无生产样本，留待阶段 5 用测试验证。

**对设计的影响**：(1) 检测器必须先判"目标有没有真实仓位"，无仓位的不建案、不告警（直接消掉最大的噪音源）；
(2) A 线的真实目标集中在 `prior_partial_batch_unresolved`、确认超时、系统停摆作废、保护单不一致这几类，阶段 3 的验收用它们的历史样本做回放；
(3) `adjust_stop_loss` 零执行需要在阶段 1 前单独查清 29 条的去向（多数可能落在"未开自动交易"或"无仓位"，但必须确认）；
(4) `/tmp/research-snapshot-20260916.db`（1.1 GB，其他会话遗留）仍在服务器上，磁盘 84%，是否删除由用户决定。

## 8. 分阶段实施（每阶段单独派工、单独验收）

| 阶段 | 内容 | 写交易所 | 验证等级 |
|---|---|---|---|
| 0 | 第 7 节的生产核实；顺手把已有告警开关配对（若确认是没开） | 否 | L0 |
| 1 | 值守进程：检测器 + 案件库 + 独立中文告警 + 心跳监控 | 否 | L1 |
| 2 | Codex 运行器，只诊断：告警附"原因 + 是否合理拒绝 + 建议" | 否 | L1 |
| 3 | worker 回环端点（plan / apply）+ 闸门 + A 线，**shadow + `/fix`** | 仅经 `/fix` 人工批准 | L3（休眠上线） |
| 4 | A 线转 live | 是 | L3，首笔实盘样本逐项核对 |
| 5 | 补 `adjust_take_profit` 意图（G1，主链路功能，单独小设计）；修 G2 / G3 | 是 | L3 |
| 6 | B 线 shadow → live | 是 | L3 |
| 7（另议） | 运维白名单（重启冻结的 worker 等）、Codex 在分支上出修复补丁待批准部署 | — | 另行设计 |

**你要的"第一版能补救"= 阶段 0–4**（A 线 live），B 线与调止盈紧随其后。
实施方式沿用现行约定：指挥会话出规格，Opus 子代理在 worktree 里实现，指挥会话审阅后部署。

## 9. 补充决定（2026-09-23）

- 值守 / Codex 诊断使用的 bot 已向用户确认：`@steve_kol_event_bot`（"Kol事件处理"），会话即 `config/system_operator_bot.env` 里的 `TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID`；2026-09-23 已用它发过一条测试消息（HTTP 200）。
- 阶段 3 的 `/fix` 改为 **Telegram 内联按钮**（用户提议）：诊断消息末尾附「✅ 执行补救」「❌ 忽略」两个按钮，按下走 `callback_query`，由现有系统 bot 命令循环
  （今天处理 `/choose`、`/dismiss` 的那条循环）接收；只接受来自 `TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID` 且 `from.id` 等于用户本人 id 的回调，其余一律忽略；
  按钮携带案件号 + 动作指纹，按下后先回一条"确认执行 X？"再要第二次点击，避免误触；按钮 30 分钟后失效。保留 `/fix <案件号>` 文本命令作为兜底。
