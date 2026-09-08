# Management Reliability Status（A 线：管理指令可靠性修复）

2026-09-07 两起管理指令未执行事故的修复项目。本文件是跨会话唯一的进度真相；
新会话只读本文件，再打开 `current_step_file` 指向的那一份步骤文件，不要读其他步骤文件。
与 B 线（`docs/rest-ws-trading-status.md`，REST + WebSocket 改造）并行，互不阻塞。

```yaml
project: management-reliability
plan_index: docs/plans/2026-09-07-management-reliability/README.md
diagnosis: docs/2026-09-07-management-instruction-incident-read-only-diagnosis.md
server_notes: docs/2026-09-07-management-instruction-incident-server-notes.md
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，执行会话完成后必须 send_message 到这里
integration_branch: codex/deepcoin-auto-trading-v1               # 每步完成后由指挥会话本地合并并 push
deploy: tg-deploy <sha>（AGENTS.md 部署一节）
current_step: 3
current_step_file: docs/plans/2026-09-07-management-reliability/step-3-deferred-resume-and-backlog.md
step_status: planned              # planned | claimed | in_progress | completed | blocked
claimed_by: null
last_completed_step: 2
last_completed_commit: ca66a2d5c095c22313d6009b9a61048840a88800
user_decisions_2026_09_07:
  risk_reducing_bypasses_frozen: true      # 全平 / 保本类指令绕过“未了结减仓批次”冻结
  ambiguous_target_notifies_user: true     # 目标不唯一或无活跃仓位 → 通知确认，不自动改指向
  stale_pending_items_void_and_notify: true  # 29 条积压指令项全部作废并逐条通知，不补执行
  rest_ws_phase5_continues_in_parallel: true
```

## 步骤总览

| 步 | 名称 | 风险 | 需用户单独批准 |
|---|---|---|---|
| 1 | 止盈收敛否决只作用于保护单行 | L2 | 否 |
| 1b | 被误判冻结的止盈收敛允许重试（为当前持仓重建分档止盈） | L2 | 是 |
| 1c | 止盈字段为字面量 `0` 不再被当作"存在未拥有的止盈单"（方案 B：只修判据） | L2 | 否（指挥会话裁定） |
| 2 | 告警补全：白名单三类、后台任务自愈、两条投递通道、健康端点 | L1 | 否 |
| 3 | 被推迟指令的恢复与超时；29 条积压作废并通知 | L2（作废积压为 L3 数据变更） | 是（作废积压那一步） |
| 3b | 联系方式里的数字串不得被解析为价格（大镖客 QQ 号被当止损价，2026-09-08 复发） | L2 | 否 |
| 4 | 账本修复：binding 337、批次 158、幽灵 1081、已成交仍活跃的止盈单 | L3 | 是 |
| 5 | 部分止盈成交后自动收敛止损数量；待恢复批次超时告警，不永久冻结 | L2 | 否 |
| 6 | 确定性拒绝不升级为结果未知；降风险指令绕过冻结 | L2（改交易语义） | 是 |
| 7 | 目标不唯一/无活跃仓位时通知确认；入场失败不得模拟为已入场；只通知群跳过用独立状态 | L2 | 否 |

## 执行会话的领取协议

1. 新建会话先读 `AGENTS.md`，再读本文件，再只读 `current_step_file`。
2. 确认 `step_status` 为 `planned`；若本步在“需用户单独批准”列为“是”，证据区必须已有该步的批准记录，没有就停下。
3. `step_status` 改 `claimed`，`claimed_by` 填本会话 ID，单独提交；开始改代码前改 `in_progress` 并提交。
4. 在独立工作树 `.worktrees/mgmt-step-N` 与分支 `mgmt/step-N-<slug>` 上工作，工作树里软链接 `.venv`。
5. 完成后按步骤文件“完成条件”更新本文件（`completed`、`last_completed_step`、`last_completed_commit`、`current_step` 推进、`step_status` 回 `planned`、`claimed_by` 置空、证据区追加），单独提交并 push；`send_message` 给 `brain_session_id`。
6. 全程遵守 AGENTS.md：不用 `git add -A`；部署用 tg-deploy；观察按 L2 规则用后台监视器。

## 硬性禁止（所有步骤）

- 不得用 symbol、方向、数量、价格、时间接近、ID 相邻、clOrdId 或 tag 单独认领归属（与 B 线同一条）。
- fail-closed 的结论必须留下可归因材料：每一次拒绝、冻结、跳过都要记下判定点的字段原文，不能只记结论。
- 任何一步不得顺手修另一步的问题；发现新问题写进证据区。
- 改交易语义的步骤（3 的作废、4、6）必须有用户对该步的单独批准记录。
- 不动 B 线的文件（`deepcoin_private_ws.py`、`deepcoin_ws_*.py`、影子表）。

## 证据记录

执行会话在此追加，格式：`- step-N (日期, 会话ID): 提交 SHA；做了什么；验证结果；遗留问题`。

- step-2 (2026-09-08, local_bc413965-dc4a-4e5c-b471-dad8cf6c80bf): **completed**。分支 `mgmt/step-2-alerting`，代码提交 `ca66a2d5c095c22313d6009b9a61048840a88800`（已 fast-forward 进 `codex/deepcoin-auto-trading-v1` 并部署，2026-09-08T06:41Z）。部署前生产 HEAD `7a4d852a31708515aa92e58313a941c206f5637c`（B 线阶段 5）为回滚参考。
  **做了什么（六个任务）**：
  (1) **白名单**：不再依赖"代码默认值"——生产 env 显式设了 `TELEGRAM_TYPES`，改默认值在生产为零效果。改为**代码强制基线与非空 env 选择器取并集**，capture 与 telegram 两处共用**一个集合、一个并集函数**（与 B 线阶段 5 的 `ALWAYS_NOTIFIED_INCIDENT_TYPES` 合并，A-2 五类并入，`_with_always_notified_types`）。两个关断位原样保留：键**缺席**=全类型，键为**空串**=capture-only。`context_worker_exhausted` 的 `operation` 前缀过滤在**投递侧**：认领后不合格的行落 `notification_status="suppressed"`（终态、不再认领），payload 读不出或无 `operation` 字段时仍投递（fail-closed 朝多发一条）。
  (2) **uncertain 事件**：`mark_authoritative_execution_uncertain` 在冻结 commit **之后**生成 `authoritative_execution_uncertain`（high），summary 含 `raw_message_id` / `attempt_id` / `error_summary` / `operation=raw_message_N`；捕获自身异常全吞，账本问题绝不会把已定的冻结变回异常。为此在 `runtime_incidents._SUMMARY_FIELDS` 这个封闭字段集新增 5 个键（指挥会话裁定接受，见 step-2-rulings），并有测试锁住"值只能是整数或安全标签、凭据串必被 redacted"。
  (3) **后台任务自愈**：`system_operator_bot_command`、`runtime_incident_notification`、`telegram_bot_command` 三个任务走 `_supervise_restartable_background_task`：1s→60s 指数退避、每次 warning + 计数、连续 10 次停止并生成 `background_task_restart_exhausted`（critical）。取消照常穿透，正常返回不重启。**存活超过 300 秒视为恢复、重置计数与阶梯**（裁定接受），否则每隔几天抖一次的网络异常会在第十次把任务永久停掉。
  (4) **health**：`deployment-identity` 的 `health` 新增 `runtime_incident_notification`、`system_operator_bot_command` 存活、`background_task_supervision` 计数、`runtime_incident_last_notified_at`。该端点有一条既有的"绝不碰数据库"测试契约（`test_web_app.py` 用会抛 AssertionError 的 session_factory 守着），故上次投递时间改为**启动时从 `max(notified_at where delivered)` 播种、之后由通知循环的 `delivery_observer` 刷新**，端点本身零 I/O。
  (5) **两条停投通道**：见下"根因"。非代码缺陷，按裁定不做路由回落，记为遗留交 step 5。
  (6) `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空**只报告，未改服务器配置**。
  **两条通道停投的根因（同一个）**：`TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` 为空 → `system_operator_bot_enabled()` 要求 token 与 chat_id 同时非空 → `app.state.notification_bot_config` 恒为 `None`（`web_app.py:5654`）。于是 (a) `strategy_management_notification` 任务在 `isinstance(app.state.notification_bot_config, SystemOperatorBotConfig)` 判据下**根本不会被 create_task**；(b) `deepcoin_reconcile` 循环启动时传的 `system_operator_bot_config=app.state.notification_bot_config`（`web_app.py:5081`），使 `deliver_pending_position_protection_incidents` 与 `deliver_pending_position_attribution_incidents` 被 `system_operator_bot_enabled()` 挡住；同一处的 `terminal_entry_cleanup_bot_config` 传的才是已设置的 `system_operator_bot_config`，所以只有这两条被关掉。**不是循环没启动，也不是异常被吞，是配置使通道整体处于禁用态。** 实测：`position_protection_incidents` pending 399 / not_required 20 / **delivered 0**（最早 pending id=1，2026-07-24，历史上一条都没投递成功过，且无任何行停在 delivering 或 failed）；`strategy_management_notifications` delivered 28（末次 notified 2026-07-21 15:21:03）/ pending 67（最早 created 2026-07-22 05:18）；另 `position_attribution_audits` pending 2834 同源。三者合计约 3300 条积压——这正是不做路由回落的理由。**事故投递本身不受影响**：`runtime_incidents` 的 Telegram 投递走 `system_operator_bot_config`（SYSTEM_BOT token+chat_id 均已设置），与空 chat_id 的 notification bot 是**两条独立通道**。
  **AFTER_ID 水位线**：`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID` **272 → 2069**（部署那一刻的最大 incident id）。env 已备份为 `/etc/telegram-kol-worker.env.bak-mgmt-step-2-20260908T064155Z`，diff 只有这一行，worker 进程内生效值已核实为 2069。
  **抬水位线的代价已量化为零损失**：`id > 272` 且属于**当时**白名单八类的行——`severe_protection_incident` 156、`management_target_refused` 38、`unclassified_operation_failure` 2——**全部 `delivered`，pending 0**，所以抬高不会丢掉任何一条该发未发的告警。反之若不抬：`id > 272` 且属于**新增**白名单的 `context_worker_exhausted` **1464 条 pending** + `management_recovery_required` **8 条 pending**，共 1472 条会在部署后几分钟内全部发出。
  **被水位线永久压制的 8 条 `management_recovery_required`（severity 全 critical），批次 id 列此供 step 4 复核**：id 330/batch **123**（management_reconciliation_identity_mismatch）、331/batch **127**（take_profit_cancel_retry_exhausted）、332/batch **129**（take_profit_order_identity_conflict）、360/batch **133**、396/batch **144**、1924/batch **150**、2020/batch **153**、2058/batch **158**（close_final_preflight_failed，2026-09-04 09:22:59；batch 158 本就在 step 4 范围内，其余 7 个批次为本步新增的复核项）。
  **步骤文件的一处前提被实测推翻**：任务 1 原写前缀过滤是为"避免 1466 条历史噪音"。实测 `id > 272` 的 1464 条 pending **全部**带 `operation=raw_message_*`（`json_extract` 分组核实，distinct 前缀只有 `raw_message_9x/1xx`），**前缀过滤一条都不过滤**。它只是对未来非消息来源（backfill / scanner）的守卫；真正挡住历史积压的**唯一承重是 AFTER_ID 水位线**。代码注释已按实测改正，指挥会话已同步修正步骤文件。
  **部署前发现并修掉的一个会让本步失效的缺陷（B 线 local_4a6676b0 提醒后复现）**：`runtime_incidents._validate_redacted_json_contract` 有条"32 字符以上混合字符类长串疑似密钥"的启发式，而 `_safe_label` 用下划线连词，把普通错误消息变成单个长 token。实测五条现实错误摘要**四条被拒**（`Deepcoin returned sCode 51004 for ordId 1001125163581378`、`httpx.ReadTimeout: ...`、`DeepcoinRequestOutcomeUnknown: POST ...`、`ConnectionResetError errno 104 ...`），而 `_capture` 吞掉异常 → 权威执行落 uncertain 却既无事件也无告警，**即本步的修复会被自己静默绕过**。修法：新增 `_safe_sentence` 用**空格**连词（每词各自成短 token；真凭据 blob 无空格仍是长 token 仍被拦，`QUJDREVG...` 实测仍 True，`DC-ACCESS-SIGN=` 仍在更早的 marker 检查变 `redacted`），并为两个新适配器加"详细 summary 被拒则改用仅含固定标签与整数的最小 summary 再记一次"的兜底。含四条参数化回归测试。
  **测试**：focused 30 项（白名单三分支、前缀过滤五种 payload、suppressed 落库、uncertain 事件字段、脱敏契约、退避阶梯与 10 次上限、300 秒重置、取消穿透、health 投影、启动播种与 observer）。全量 `pytest -q` 在最终候选 `ca66a2d5` 上：**7801 passed, 4 skipped, 0 failed**；`tests/test_runtime_event_loop_blocking_census.py` 通过（新增的 supervisor while 循环未引入新的阻塞调用）。
  **部署前闸门**：`active_write_count=0`；在途管理批次（planned/executing/reconciling）0；在途 `worker_command_jobs`（pending/claimed/executing）0；在途权威执行尝试（claimed/executing）0。
  **L1 观察窗（合格）**：证据 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-2/`（`observer.sh` / `observer.jsonl` / `DONE`）。`2026-09-08T06:42:32Z` 起 **906 秒、31 个采样、anomaly_count=0**，三个 unit 全程 active，1 条真实消息 / 1 个群（按 L1"15 分钟或 5 条消息，先到为准"，以时长达标）。窗内 `runtime_incidents` 零新增（max id 全程 2069）、`delivered` 全程 199、`suppressed` 全程 0。**积压零补发已由此证实**：1472 条本可被投递的历史行一条都没发出。
  **自愈验证（合格，本步的核心生产证据）**：注入方式三次，前两次失败且原因已查明并记录——
    - `06:42:42Z` 与 `06:42:54Z` `ss -K`：**零效果**，本机内核 `CONFIG_INET_DIAG_DESTROY is not set`，`ss -K` 退出码 0 但匹配零套接字（空操作，无副作用）。
    - `06:43:17.698Z`–`06:43:22.706Z`（5.008s）iptables OUTPUT REJECT tcp-reset → 149.154.166.110:443：**未命中**。空闲的 keep-alive 长轮询期间没有出站包，OUTPUT 链匹配不到东西。
    - `06:44:45.948Z`–`06:44:53.956Z`（8.008s）同一规则，但先用 `ss -tni` 的 `lastsnd` 读出 25 秒长轮询相位（`_get_updates` 服务端 timeout=25、客户端 read=35），等到 `lastsnd_ms=22029` 再插规则对准下一次发送：**命中**。
    两个防火墙窗口各 ≤10 秒（裁定上限），合计 13.0 秒，每次用 trap 保证移除并事后 `iptables -S` 核实 clean；该时段告警投递与 Bot 发送一并被打断，但当时水位线之上 pending 为 0，无损失；ingest 的 Telethon 走 MTProto 不同 IP，未受影响。精确时刻已 send_message 通报 B 线 local_4a6676b0。
    **结果**：`system_operator_bot_command_task` 与 `telegram_bot_command_task` 双双抛 **`ConnectError`**（**不是 `TimeoutException`——正是 09-06T19:38Z 那次逃逸并杀死任务的形态**，`_get_updates` 只捕获超时），退避阶梯 **1s→2s→4s** 在日志中清晰可见（`consecutive_failures=1/2/3`，`restarts=0/1/2`），规则一撤两个循环立即重连并恢复（`06:44:58Z` "System operator bot command loop started"）。窗末两个任务均存活，`restart_exhausted_at` 均为 null。**改动前这一下就是又一次 6h48m 静默。**
  **无样本的两项（诚实记录）**：窗内**没有**产生任何新的 `runtime_incidents`，因此"新白名单事件确认送达"**无样本**；窗内**没有**新的 uncertain 尝试，因此任务 2 的生产路径**无样本**（其正确性由 focused 测试与最小 summary 兜底覆盖）。二者均非被否决，是窗内未出现该输入。**一个近距离旁证**：attempt **581**（raw 15402）于 `06:26:37Z` 落 uncertain，恰在本步部署前 15 分钟、仍跑旧代码，因而**没有生成任何事件**——正是任务 2 要消灭的那一类。
  **`runtime_incident_notification_task` 的计数为 0 的原因**：该循环内部把所有异常都吞掉、从不向外抛，所以它的 supervisor 是纯冗余保险。这与第三轮调查"该任务从没崩过"一致。
  **新发现（本步未修，写入证据区备后续步骤）**：`management_stop_rejected` 是又一类**告警不到任何人**的管理失败。生产首次出现即 id **2069**（severity **high**，`2026-09-08 06:26:24Z`，本步部署前 15 分钟），`notification_status=pending`。它**既不在** `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`、**也不在** `..._CAPTURE_TYPES`（env 里根本没有这个串），**也不在**本步的 `ALWAYS_NOTIFIED_INCIDENT_TYPES` 基线里——它能落库说明其产生路径直接调 `record_runtime_incident`、不过 `captures()`（与 B 线 `market_fill_attribution_unverified` 同一形态）。本步任务 1 只被授权加三类，加第四类属扩范围，按"任何一步不得顺手修另一步的问题"未动，请指挥会话裁定是否补进基线。另：`provider_retry_exhausted`（124 条）、`monitor_adapter_failure`（9 条）、`notification_delivery_failure`（1 条）同样是累计零投递，但它们不属管理指令失败，未纳入本步。
  **回滚路径**：生产 HEAD 已前移到 `ca66a2d5`。撤销本步须在最新 HEAD 上 revert 本步的代码提交后重新部署，**并把 env 的 AFTER_ID 从 2069 改回 272**（备份文件在服务器上）；不可用单条 `tg-deploy 7a4d852a`，那会连带回退 B 线阶段 5。
  **遗留问题**：(a) 两条停投通道 + `position_attribution_audits` 共约 3300 条积压，根因为空的 `NOTIFICATION_BOT_CHAT_ID`，交 step 5——按裁定先给各通道加 AFTER 门槛，再由用户决定补 chat_id 还是改路由；(b) 上列 7 个批次（123/127/129/133/144/150/153）补进 step 4 复核清单；(c) `management_stop_rejected` 等零投递类型是否补进基线待裁定。

- incident-2026-09-08 (A-2 只读取证，指挥会话记录): raw 15402 大镖客 06:25:30Z“第一止盈位已过，锁定利润，移动止损”被识别为 partial_then_break_even，但签名 QQ:158241758 被解析为显式止损价，止损价闸门正确拒绝（batch 160 management_stop_action_conflict），减仓 50% 与移止损均未执行，incident 2069 无人收到，attempt 581 落 uncertain 无事件。仓位 binding 345 ETH 空单入场约 2484.67，止损仍在 2530 / 2535.06（亏损侧），TP1 2470 疑已成交。新增 step 3b 修上游识别；step 3 完成后先做 3b 再做 4。
- step-2-rulings (2026-09-07, 指挥会话裁定): (1) 白名单在生产由 env 显式设定，代码默认值无效——采用“代码强制基线集合与 env 集合取并集”（capture 与 telegram 两处），基线含 management_recovery_required / management_submit_unknown / context_worker_exhausted（raw_message_ 前缀过滤）/ authoritative_execution_uncertain / background_task_restart_exhausted / market_fill_attribution_unverified（B 线阶段 5 新增）；部署只改 env 的 AFTER_ID。(2) position_protection_incidents（delivered 0，pending 399）与 strategy_management_notifications（pending 67）以及 position_attribution_audits（pending 2834）停投的共同根因是 TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID 为空使 notification_bot_config 恒为 None，两条通道整体禁用；不在本步做路由回落（会一次性放出约 3300 条积压），交 step 5：先加各通道 AFTER 门槛，再由用户决定补 chat_id 或改路由。(3) 后台任务自愈的连续失败计数在存活超过 300 秒后重置。
- step-1c-deploy-ruling (2026-09-07, 指挥会话): 确认按计划部署 1c。执行会话只读预演发现收敛 231（binding 342 / leg 588 / pos 1001125164628529，ETH 多单）与 230 同样冻在 convergence_exact_leg_not_verified，方案 B 下两者都不会被本步触发；231 已补进 step 4 范围。用户已被告知 BTC 与 ETH 两个活跃仓位目前都只有止损、没有分档止盈，可自行手动挂。
- step-1c-decision (2026-09-07, 指挥会话裁定；用户在 1b 会话与指挥会话均表示“由总指挥判断”): 采用**方案 B**——A-1c 只修 `_row_has_take_profit_fields` 判据，不复位收敛 230，不做任何生产数据修改；230 留给 step 4 账本修复一并处理；“重试路径按即时状态重判、瞬时失败变终态”的性质列入 step 5。据此 1c **不追溯作用于当前被冻结的收敛（230、231）**；它撤掉的是一个误判否决，部署后此后正常 ready 的收敛会照系统设计把止盈挂到交易所，这与 A-1 同一类（收窄误判、恢复正常挂止盈），因此**改判为无需单独批准**。回滚路径：生产 HEAD 已含 B 线 5a，撤销本步须在最新 HEAD 上 revert 1c 的代码提交后重新部署，不能 tg-deploy 退回旧 SHA。用户被告知可自行在交易所为 pos 1001125163581280 手动挂止盈。
- step-1b-approval (2026-09-07, 用户在指挥会话 local_858790fe 明确批准): 允许把 `convergence_pending_alias_conflict` 加入收敛重试白名单，让被误判冻结的止盈收敛（预期 230，BTC 多单 binding 341）按正常路径为当前持仓重建分档止盈；部署前须先只读列出会被拉回的记录出示给用户。
- step-1c (2026-09-08, local_16ec4b63-93bc-4cac-811c-a290343b49f9): **completed**。分支 `mgmt/step-1c-zero-take-profit-field`，代码提交 `e402692c149e8d7ac0a993cf73e17e8b3c330156`（已 fast-forward 进 `codex/deepcoin-auto-trading-v1` 并部署）。部署前生产 HEAD `618a85247ef9763e4bbd7d375e3d921536fa3e60` 为回滚参考。
  **做了什么**：只改 `trigger_take_profit_convergence_executor._row_has_take_profit_fields` 一个函数，使其与 `normalize_native_tpsl` 对"什么是止盈价"的判定一致（后者走 `_first_positive_decimal`，`"0"` 解析为 `None`）。四分支语义：`None`/空串 → 无止盈；能解析且 `> 0` → 有止盈；能解析且 `<= 0`（`"0"`、`"0.0"`、负数）→ 无止盈（本步唯一的行为变化）；解析不了或非有限值（垃圾串、`NaN`、`Infinity`、非字符串对象，经 `_decimal` 返回 `None`）→ 仍算"有止盈"，**保持 fail-closed**。未动 `_row_has_protection_fields` 与 `is_protection_order_row`，未动止盈档位/数量/价格计算与止损，未新增写入方式，未做任何生产数据修改。
  **测试**：四组。(1) 止盈别名全为字面量 `"0"` 的止损单不再触发 `convergence_unowned_take_profit_present`；(2) 带真实正数止盈价且不属本地账本的行仍触发该否决；(3) 无法解析的值（垃圾串、`NaN`）仍被拦住——实测被更早的 `_pending_alias_conflict_detail` 先拦下，故断言"仍 `conflicted`"而不锁定具体原因码；(4) 对判据四个分支的直接单元测试。其中 (1)(4) 在移除修复后确实失败，非空测试。全量 `pytest -q` 在 rebase 后的最终候选上重跑：**7660 passed, 4 skipped, 0 failed**。
  **部署前只读预演**（生产真实数据，`mode=ro` + `no_autoflush`，`_prepare_plan` 只构造 payload 不写库）：对当时 12 条非终态收敛逐条模拟"若被 ready"的计划期结论。当前实现 vs 候选修法只有两条不同——**230**（`unowned_take_profit_present` → 放行、会写 3 张止盈）与 **231**（同上 → 放行、会写 1 张止盈，binding 342 / leg 588 / pos 1001125164628529）。231 系本步新发现，已按指挥会话裁定补进 step 4 范围。两条均冻在 `convergence_exact_leg_not_verified`（不在重试白名单），方案 B 下不会被拉回，故本步不触发它们。其余 10 条两种实现结论相同。**该预演的局限已记录**：那 10 条现在 `pos_id` 为 NULL，强制置 ready 后是在 `_prepare_plan` 510 行因 `pos_id` 为空而返回 `exact_leg_not_verified`，与本次改动无关；预演能证明"当前被冻的两行不会被本步触发"，**不能**证明"此后不会有新的交易所写入"——后者正是系统设计要的行为。
  **部署前闸门**：`active_write_count=0`、无 planned/executing/reconciling 批次。基线：收敛 ≤235、止盈单 ≤197、mutation intent ≤642、`authoritative_execution_attempts`=517、uncertain=13。
  **L2 观察窗（合格）**：证据 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-1c/`。`2026-09-07T21:54:12Z`–`23:38:17Z`，**105 个采样、1 小时 44 分、anomaly_count=0**；窗末 30 分钟 **5 条真实消息 / 3 个群**（超过 2 群门槛，未用宽限）。窗内所有账本零变化：`position_take_profit_orders` 197、`position_mutation_intents` 642、`position_protection_ledger` 666、`position_protection_legs` 903、`execution_bindings` 344、`execution_order_legs` 592、`trigger_protection_intents` 188；uncertain 13→13 无新增；`authoritative_execution_attempts` 517→521 为正常交易活动。**窗内零非预期写入。**
  **逐笔核对：无样本。** 窗内没有任何新建止盈单可核对（`position_take_profit_orders` 未增行）。原因是窗内没有出现"仓位活跃 + 收敛正常 ready + 分档数量可分配"三者同时成立的样本，而非本步改动被否决。
  **窗内生产版本被 B 线换过三次**：`22:14:07Z`→`323b98b4`、`22:29:34Z`→`97011c68`、`22:40:44Z`→`230ba1cc`，每次伴随三个 unit 重启。三个 SHA 均含本步修复（`merge-base --is-ancestor` 已验证，且直读生产源码确认 `parsed > 0` 分支在位），因此本步改动在整个窗口内持续生效，零写入结论对四个版本都成立。与 1b 同样的限制：重启落在分钟采样之间，`anomaly_count=0` 不构成"未发生重启"的证据。**回滚路径**：生产 HEAD 已多次前移，撤销本步应在最新 HEAD 上 revert `e402692c` 的代码改动后重新部署，并与 B 线协调；不可用单条 `tg-deploy 618a8524`，那会连带回退 B 线的限流改动。
  **窗后观察（不作为本窗证据，记录备查）**：`2026-09-08T01:23:10Z` 新建 mutation intent 643 —— 这是 **止损**写入（`_ledger_purpose=stop_loss`、`operation=set_position_sltp`、`slTriggerPx=83166`、`sCode=0`、`confirmed`），来自 backup stop 执行器，与本步无关，是新 BTC 空单 binding 343 的正常保护。同一时刻收敛 232（binding 343 / leg 589 / pos 1001125178552543）落到 `convergence_target_size_below_minimum`。**该结果与本步改动无关**：`_allocate_sizes`（578 行）在被改的判据（705 行）**之前**执行，232 在分档数量那一步就返回了，从未走到 `_unowned_pending_take_profit_present`。另于 `00:13:36Z` 出现 1 条新的 uncertain（id 533），同样在窗后。
  **新发现（本步未修，写入证据区备后续步骤）**：仓位规模过小时无法建出分档止盈。pos `1001125178552543` 在仓 3 张（止损账本 `size_text=3`），desired 为 50%/30%/20%，BTC-USDT-SWAP 的 `quantity_step=1`、`min_quantity=1`，1.5/0.9/0.6 无法满足每档 ≥1 张，因此 fail-closed 为 `convergence_target_size_below_minimum`，无任何写入。这是既有行为、非本步引入，是否需要小仓位降档策略属后续步骤判断。
- step-1b (2026-09-07, local_16ec4b63-93bc-4cac-811c-a290343b49f9): **本步任务范围 completed，但验收目标未达成**（分档止盈未建成，原因见下；剩余目标移交 A-1c）。分支 `mgmt/step-1b-convergence-retry`，代码提交 `7c2fc797b6dd08686c93114fca14171010229eaf`（已 fast-forward 进 `codex/deepcoin-auto-trading-v1` 并部署）。回滚 SHA `b1c12213ad2740e08ac1ecdba39e843e55ce239f`。
  **做了什么**：`execution_bindings.py` 的 `conflicted` 收敛重试白名单只加入 `convergence_pending_alias_conflict` 一个原因码（未加 `convergence_exact_leg_not_verified` 或任何其他码），重试沿用原流程，未改止盈档位/数量/价格规则，未动止损，未新增写入方式。测试覆盖：该原因码被重新 ready；`convergence_exact_leg_not_verified` / `convergence_partial_position_unexplained` / `convergence_pending_alias_conflict_before_write` 三种仍原样冻结（status、reason_code、updated_at 均不动）；被拉回但仓位已平的行落到 `waiting_backup_stop` 且不产生写入。全量 `pytest -q` **7618 passed, 4 skipped, 0 failed**。
  **部署前只读清单**（证据 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-1b/preflight-pullback-list.txt`）：全库 26 条 `conflicted` 收敛中，新白名单只拉回 **227 与 230** 两条，与预期一致（其余 22 条 `convergence_partial_position_unexplained` + 2 条 `convergence_exact_leg_not_verified` 仍被跳过）。227：binding 339 BTC long 已 closed、leg 583 `manually_closed`、pos 1001125135694798 已不在实时持仓。230：binding 341 BTC long active、leg 586 active/verified、pos 1001125163581280 在仓 **8 张**（avgPx 80118.3），desired 50%@80700 / 30%@81400 / 20%@82100，用生产合约规格（quantity_step=1、min=1）实算分档为 `80700@4 / 81400@2 / 82100@2`，合计 8 = 在仓量。部署前 `active_write_count=0`、无 planned/executing/reconciling 批次；基线 收敛 ≤235、止盈单 ≤197、mutation intent ≤642。
  **部署后结果**：227 按预期落到 `waiting_backup_stop`（仓位已平被实时校验挡下），**零写入**。230 被成功拉回并重新 `ready`，但执行器在计划期被**另一个判据**否决：`convergence_unowned_take_profit_present`，随后再被拉回（该原因码本就在白名单内），形成 `ready → conflicted → ready` 的循环。窗内 `position_take_profit_orders` 与 `position_mutation_intents` **零新增**（仍为 197 / 642），fail-closed 完整，无任何交易所写入。
  **阻塞原因（新发现的第二处同族误判，本步未修）**：`trigger_take_profit_convergence_executor._row_has_take_profit_fields` 只把 `None` 和空串当作"无止盈字段"，因此把 **只有止损、止盈字段为字面量 `"0"`** 的 TPSL 行判定为"带止盈字段"；随后 `normalize_native_tpsl` 对 `"0"` 返回 `take_profit_trigger_price=None`，`_unowned_pending_take_profit_present` 走到 `order is None or take_profit_trigger_price is None → return True`，判成"存在本地不拥有的止盈单"。BTC-USDT-SWAP 当前 3 行 TPSL 保护单全部命中（判定点读到的字段原文，只读快照）：
    - `ordId=1001125163581378` `triggerOrderType=TPSL` `side=sell` `posSide=long` `sz=0` `slTriggerPrice=78500` `closeSLTriggerPrice=78500` `tpTriggerPrice="0"` `closeTPTriggerPrice="0"` `tpPrice="0"`
    - `ordId=1001125163582992` 同型，`slTriggerPrice=78343` `closeSLTriggerPrice=78343` `tpTriggerPrice="0"` `closeTPTriggerPrice="0"`
    - `ordId=1001125167675480` `sz=10` `slTriggerPrice=78500` `closeSLTriggerPrice=""` `tpTriggerPrice="0"` `closeTPTriggerPrice=""`
    另 4 行 `triggerOrderType=Conditional`（posSide=short，入场条件单）按 A-1 修正后正确跳过，`_pending_alias_conflict_detail` 对本快照返回 `None`，即 A-1 的修正本身有效、误判未复发。
  **L2 观察窗（已合格）**：服务器端只读监视器，证据 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-1b/`。`2026-09-07T17:24:55Z`–`20:29:04Z`，**185 个采样、3 小时 4 分、anomaly_count=0**；窗末 30 分钟内 5 条真实消息 / 1 个群（超过 2 小时宽限后按 1 群门槛，符合 L2）。`position_take_profit_orders` 全程停在 197、`position_mutation_intents` 全程停在 642、`position_protection_ledger` 666、`position_protection_legs` 903 均无变化，**零非预期交易所写入**；`authoritative_execution_attempts` 502→515 为正常交易活动，自部署起 `uncertain_at` 新增 0。窗内 `convergence_pending_alias_conflict` / `_before_write` 零出现，即 A-1 的修正未复发。


  **观察窗的一处重要更正：窗内生产版本被 B 线换过。** 生产 git reflog 显示 `2026-09-07T17:22:08Z` 重置到本步的 `7c2fc797`，随后 **`17:41:15Z` 被重置到 B 线的 `86825b8915377574b6c7fed7d98ab3d2e792ac4e`**（REST+WS 阶段 5a），三个 unit 于 `17:41:16Z`–`17:41:21Z` 重启。因此：观察窗（`17:24:55Z`–`20:29:04Z`）的前 16 分钟跑在 `7c2fc797` 上，其余约 2 小时 48 分（**包含合格所需的最后 30 分钟**）跑在 `86825b89` 上。`7c2fc797` 是 `86825b89` 的祖先（已用 `merge-base --is-ancestor` 验证），且已直读生产源码确认白名单行仍在（`execution_bindings.py:1340`），因此**本步的改动在整个窗口内持续生效**，零写入结论对两个版本都成立。但先前"三个 unit 全程 active"的说法不准确：重启确实发生了，只是落在两次分钟采样之间，监视器未捕捉到，`anomaly_count=0` 不构成"未发生重启"的证据。
  **回滚路径随之改变**：`tg-deploy b1c12213` 现在会同时回退 B 线的阶段 5a，不再是本步的定点回滚。若需单独撤销本步，应在最新 HEAD 上 revert 白名单那一次提交后重新部署，并先与 B 线协调。

  **窗内新发现：收敛 230 被一个瞬时状态推进了不在白名单的终态。** 逐分钟轨迹（`open-observer.jsonl`）：`17:24:42 ready` → `17:25:41 conflicted/convergence_unowned_take_profit_present` → `17:26:39` 起被拉回并连续停在 `ready` 至 `17:35:47` → **`17:36:36 conflicted/convergence_exact_leg_not_verified`，此后 `updated_at` 再无变化**。该时刻落在两轮 `deepcoin_reconcile_round`（`17:36:11`–`17:36:25` 与 `17:36:55`–）之间，写入来自止盈执行器的 `_prepare_plan`。用只读连接 + `no_autoflush` 在 `19:0x` 与 `20:4x` 两次复算同一 `_prepare_plan`，当前实现均返回 `convergence_unowned_take_profit_present` 而**不是** `convergence_exact_leg_not_verified`，可证 `17:36:36` 依据的是一个已经消失的瞬时条件。直接触发的子条件**未能确凿定位**：`_prepare_plan` 510 行大条件的各子项（leg 586 的 `status` / `attribution_status` / `pos_id`、binding 341 的 `pos_id` 拆分）现在全部满足，`17:20`–`18:05` 区间 `position_attribution_audits` 对 leg 586 无任何记录（最近一次抖动是 `18:02:48` 的 `verified → evidence_unavailable`，晚于 `17:36`）。不猜测直接原因，只记录可证事实。

  **这一性质不是 1b 引入的**：重试路径对原有三个白名单原因码同样成立——被拉回的行会按每一轮的即时状态重新判定，因而可能落到比原来更严格的终态。1b 只是让 230 进入了这条路径，于是撞上了它。

  **后果**：230 现在冻在 `convergence_exact_leg_not_verified`。该原因码不在重试白名单内，且 1b 的步骤文件明确禁止把它加入（"那些是真实的 fail-closed"）。因此**即使 A-1c 修好了 zero 止盈字段的误判，230 也不会自动被拉回重试**，pos `1001125163581280`（8 张 BTC 多单）会继续只有止损、没有分档止盈。让 230 恢复需要改这一行账本数据（L3），须用户单独批准，属 step 4 或 1c 的扩展范围，本步未动。

  **收尾结论（交接给 A-1c）**：本步的任务范围（白名单只加一个原因码、测试、部署前只读清单、L2 观察窗）全部完成且证据齐备；**验收目标"为 pos `1001125163581280` 重建分档止盈"未达成**，`position_take_profit_orders` 仍为 197，无任何新建止盈单可供逐笔核对。未达成的两个原因按发生顺序是：(1) zero 止盈字段误判 → `convergence_unowned_take_profit_present`（A-1c 的主题）；(2) 随后 230 被瞬时状态推入 `convergence_exact_leg_not_verified`，该码不在白名单且本步禁止加入，**因此仅修 (1) 已不足以让 230 恢复**。230 的最终态是 (2) 而不是 (1)，A-1c 开始前须先就是否连带复位 230 取得用户决定（选项见 1c 计划文件末节）。

  **遗留问题**：修 `_row_has_take_profit_fields` 属于改动守护交易所写入的 fail-closed 判据，需要用户对该改动的单独批准，按"不得顺手修另一步的问题"本步未动。在它修好前，收敛 230 不会为 pos 1001125163581280（8 张 BTC 多单）建出分档止盈，该仓位目前只有止损保护。
- step-1 (2026-09-07, local_912f7e68-19a2-43f3-bd62-62ff8d223a2a): 提交 `b1c12213ad2740e08ac1ecdba39e843e55ce239f`（分支 `mgmt/step-1-tp-veto-scope`，已 fast-forward 进 `codex/deepcoin-auto-trading-v1`）。
  **做了什么**：新增 `native_tpsl.is_protection_order_row(row)`，判据只看订单类型字段（`triggerOrderType` 别名集合唯一且为 `TPSL`），不用价格或数量；类型缺失或自相矛盾时仍按保护单处理，保留 fail-closed。判据来源见该函数 docstring（引用 `docs/2026-09-05-deepcoin-order-vs-trigger-order.md` 与本项目诊断文档 4.2 节的实测行）。`trigger_take_profit_convergence_executor.py` 的两个否决点（计划期 506、写前复核 746）收窄为只作用于保护单行；真保护单之间仍不一致时否决保留，并把冲突行 ordId 与判定点读到的全部字段原文写进 `trigger_take_profit_convergences.error_json`（该表无 `reason_detail` / `evidence_json` 列，`error_json` 是等价字段，**未加列**）。同一判据应用到 `trigger_backup_stop_executor.py` 的 644/702/734/746 四处；647 那处原本已有等价的类型分支，改写后逐 case 等价。未改其他收敛判据、交易所写入参数、已有账本行，未引入模式开关。
  **验证结果**：复现测试证明修复前两个判定点分别返回 `convergence_pending_alias_conflict` 与 `convergence_pending_alias_conflict_before_write`，修复后不再否决且三档止盈 `sz=5/3/2` 正常提交；判据正反用例覆盖入场条件单 / TPSL / 市价 reduceOnly / 缺字段行 / 类型自相矛盾行。全量 `pytest -q` **7617 passed, 4 skipped, 0 failed**。部署前 `active_write_count=0`、无 planned/executing/reconciling 批次；回滚 SHA `294bd54b2881b6a44e2d749d9ec479d453985d36`。观察窗（服务器端只读监视器，证据在 `/var/lib/telegram-kol-cutover-evidence/mgmt-step-1/`）：`2026-09-07T10:09:35Z`–`10:38:36Z`，30 个采样，5 条真实消息 / 2 群，三个 unit 全程 active，**anomaly_count=0**。窗内 `trigger_take_profit_convergences` 零新增记录，`convergence_pending_alias_conflict` / `_before_write` 零出现（worker 日志自部署起同样 0 次）；`position_mutation_intents` 全程停在 642，`position_take_profit_orders` 停在 197，无任何账本行数下降，即**零非预期交易所写入**。窗末直读交易所条件单（`exchange-pending-at-window-end.txt`）：BTC 3 行、ETH 3 行，全部 `triggerOrderType=TPSL`、`side=sell` / `posSide=long`，`is_protection_order_row` 全为 True，无一会被新判据否决。
  **止盈重建逐笔确认：无样本。** 窗内没有新建任何分档止盈，原因有两条且都不是本步引入：（a）触发该缺陷的入场条件单 `1001125163581473`（`triggerOrderType=Conditional`）已在观察前成交（腿 587 现持 `pos_id=1001125167675481`），当前 pending 快照里已无 Conditional 行，本次窗口不具备复现旧缺陷的输入；（b）见下面的遗留问题。判据修正本身已由复现测试证明，待下一次持仓且出现「未成交入场条件单与 TPSL 行并存」时复核分档止盈的方向、数量、价格与账本一致性。
  **遗留问题（新发现，本步未修，留给后续步骤）**：`execution_bindings.py:1330-1335` 只把 `reason_code` 属于 `convergence_verified_stop_missing` / `convergence_unowned_take_profit_present` / `convergence_exchange_preflight_unavailable` 三者之一的 `conflicted` 收敛拉回重试，`convergence_pending_alias_conflict` 不在白名单内。因此被旧缺陷冻死的收敛 **227 与 230 是终态，本次修复不会追溯解冻它们**（230 的 `updated_at` 至今仍是 `2026-09-07 01:00:12`，腿 586 为 `active`/`verified` 但从未被重新 ready）；231 的 `convergence_exact_leg_not_verified` 同样不在白名单。这属于「被冻结项的恢复」范畴（step 3 / step 5 主题），本步按「不得顺手修另一步的问题」未动。

