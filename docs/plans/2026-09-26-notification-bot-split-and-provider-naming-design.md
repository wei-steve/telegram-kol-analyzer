# 通知分流到「Kol运行通知」+ 供应商告警改名 · 设计稿

日期：2026-09-26
基线：本地 `6fc8e88e`，生产 HEAD `df0a54ab`
触发：2026-09-25 22:48Z 的两条 AI agent 通知（事件 2388 / 2389）
状态：**已批准，实施中**（2026-09-26 用户裁定见 4.3）。实施在 worktree `notif-split` / 分支
`notification-bot-split`，基于 `origin/main`。初稿的三处事实错误已在 4.1 / 4.2 / 4.4 更正。

---

## 0. 这份稿子要解决三件事

| # | 问题 | 结论 |
|---|---|---|
| A | 告警把主用模型叫成「MiMo 识别供应商」，可实际是 `gpt-5.6-luna` | 文案按链首模型动态生成，别硬写模型名 |
| B | 恢复通知无条件说「会按原顺序重新识别」，这次根本没有要补做的消息 | 有备用模型兜住的故障段，恢复通知不发补做承诺 |
| C | 需要点按钮/打命令的消息和纯通知全挤在「Kol事件处理」 | 通知走「Kol运行通知」，需人操作的留在「Kol事件处理」 |

A 和 B 是同一处文案，一起做。C 是本稿的主体。

---

## 1. 事件 2388 / 2389 的事实认定

先把结论钉死，后面的改动都建立在这上面。

### 1.1 出故障的不是 MiMo

生产 `config/ai_recognition.yaml`：

```yaml
stages:
  authoritative_recognition:
  - gpt-5.6-luna      # 链首（主用），provider = codex-proxy
  - mimo-v2.5         # 备用
```

`mimo_provider_health` 只统计**链首模型**的 attempt 行
（[`_chain_head_filter`](../../src/telegram_kol_research/mimo_provider_health.py) 第 461 行，注释：
备用模型答得上来不能证明主用模型活着）。所以这条告警的监控对象**就是当前链首**，
和 MiMo 这个牌子没有关系。

生产 `mimo_recognition_attempts` 在窗口内：

| model | status | error_code | 次数 | 区间（UTC） |
|---|---|---|---|---|
| gpt-5.6-luna | http_error | `mimo_provider_unavailable.server_error.http_502` | 15 | 22:46:57 – 23:23:50 |
| mimo-v2.5 | completed | — | 15 | 22:48:01 – 23:24:54 |
| gpt-5.6-luna | completed | — | 10 | 00:14:11 起 |

**502 的是 gpt-5.6-luna，顶上来的才是 MiMo。** 通知正文那句「已切换到备用模型 mimo-v2.5」是对的，
标题「MiMo 识别供应商不可用」正好说反了。

### 1.2 是上游 Codex 故障，不是我们这边挂了

- OpenAI status 2026-09-25「Issues with Codex」，影响 Codex Web / API / CLI / VS Code，
  23:54Z 宣告全部恢复；用户侧影响约 22:00–00:00 UTC。我们的 502 窗口 22:46–23:54Z 完整落在里面。
- `codex-proxy.service`（本机 127.0.0.1:3466，nginx `codex-api.dwpc.com.cn` 反代）进程
  自 2026-09-09 18:55 起**一次都没重启过**。
- nginx `error.log` 在该时段**没有任何 upstream 报错** —— 如果是 nginx 连不上 3466 会记。
- 502 响应体只有 101 字节 JSON，是 proxy 进程自己生成的 → **上游错误原样透传**。
- nginx 访问日志：北京时间 06 点 8 次 502、07 点 22 次 502，08 点起全部 200。

**识别一条都没断**：15 次链首失败对应 15 次 mimo-v2.5 成功，数量刚好对上。

---

## 2. 改动 A · 告警文案按链首模型生成

### 现状

面向 Telegram 的硬写文案集中在
[`system_operator_bot.py`](../../src/telegram_kol_research/system_operator_bot.py)：

| 行 | 现文案 |
|---|---|
| 274 | `MiMo 故障期间的入场未执行，需人工判断` |
| 287 | `MiMo 故障期间的管理指令未自动执行，需人工判断` |
| 296 | `MiMo 恢复后开始补做识别` |
| 334 | `MiMo 识别供应商不可用` |
| 355 | `MiMo 识别供应商已恢复` |
| 364 | `MiMo 识别连续失败` |
| 376 | `MiMo 每日探测失败，但识别正常` / `MiMo 每日探测失败` |
| 402 | `MiMo {check}本身失败` |

### 改法

`mimo_provider_health` 已经算出了链首模型名（`chain_head_model` / `_resolve_chain_head`），
把它作为 `head_model` 写进 `redacted_summary`，文案里填进去：

```
权威识别主用模型不可用
模型: gpt-5.6-luna
原因: 供应商服务端错误（HTTP 502）
```

`head_model` 取不到时（读链失败会回落到「统计每一行 attempt」）退成「权威识别主用模型」，
**不要退成任何具体牌子名**。

### 边界：只动文案，不动契约

| 层 | 本次动不动 | 理由 |
|---|---|---|
| Telegram 文案（上表 8 处） | **改** | 零风险，纯展示 |
| `incident_type`（`_MIMO_PROVIDER_INCIDENT_TYPES` 里的 8 个） | 不改 | 去重键、配置白名单、库里 2400+ 行都认这个值 |
| `component: "mimo_provider"` / `source_kind` | 不改 | 同上 |
| 模块名、表名（`mimo_recognition_attempts` …） | 不改 | 迁移成本，另立项 |

这条和 2026-09-24 记下的三层改名顺序一致：纯命名 → 表名 → 持久化枚举值。本次只做第一层里
**用户能看见的那一小块**。

---

## 3. 改动 B · 恢复通知别乱承诺补做

### 现状

`mimo_provider_recovered` 的文案是固定模板，无条件带这一行：

> 补做: auto_trade 群在故障期间未完成识别的消息会按原顺序重新识别；入场一律不执行、逐条通知，
> 管理指令满足条件才执行，否则转人工确认

2389 这条就这么发了，但那段时间备用模型把 15 条全接住了，**没有任何消息需要补做**。
读的人会以为有一批消息正在重放。

### 改法

不可用告警已经会区分「有没有备用模型在顶」（`fallback_note` / `impact` 两个取值：
`authoritative_recognition_on_fallback_model` vs `authoritative_recognition_unavailable`）。
恢复通知按同一个事实分岔：

- 故障期**全程有备用模型成功应答** → 恢复文案写
  「期间识别未中断，由备用模型 `mimo-v2.5` 完成，无消息需要补做」；
- 否则 → 保留现有补做文案。

判据用已有的 `_fallback_model_answering_since`，不要新发明一套。
**实施时请复核**：该函数只回答「有没有一个非链首模型答过」，不回答「是不是每一条都被接住」。
如果要说「无消息需要补做」这么硬的话，判据得是「故障期内 `authoritative_recognition_failed`
事件数为 0」，而不是「有备用模型答过」。两者取严。

---

## 4. 改动 C · 通知分流到「Kol运行通知」

### 4.1 现状：运行通知 bot 早就在跑，只是没人把通知类挪过去

> **2026-09-26 复核更正。** 本稿初稿说「`NOTIFICATION_BOT_CHAT_ID` 为空、整条通道没启动」，
> **那是错的**。初稿读的是 `/opt/telegram-kol-analyzer/config/*.env`（那里确实是空的），
> 但进程真正读的是 systemd 的 `EnvironmentFile`，而且 `split_runtime` 下
> `load_notification_bot_config(env_file_paths=[])` **只读 `os.environ`，根本不读那些文件**。
> 下面是复核后的事实。

生产三个 bot：

| 环境变量前缀 | bot | 名字 | chat_id |
|---|---|---|---|
| `TELEGRAM_KOL_SYSTEM_BOT_*` | `@steve_kol_event_bot` | **Kol事件处理** | 8129644952 |
| `TELEGRAM_KOL_NOTIFICATION_BOT_*` | `@steve_kol_msg_bot` | **Kol运行通知** | 8129644952 |
| `TELEGRAM_KOL_ALERT_BOT_*` | `@steve_kol_signal_bot` | Kol信号 | （信号推送，本稿不动） |

两个 chat_id 相同不是 bug：私聊里 `chat_id` 就是**用户 id**，两个 bot 各自和同一个人有一条独立会话。

按角色看 `EnvironmentFile`：

| 角色 | 单元 env | SYSTEM | NOTIFICATION |
|---|---|---|---|
| worker | `/etc/telegram-kol-worker.env` | ✅ 8129644952 | ✅ 8129644952 |
| web | `/etc/telegram-kol-web.env` | ❌ 无 | ❌ 无 |
| ingest | `/etc/telegram-kol-ingest.env` | ❌ 无 | token 有、chat_id 空 → 禁用 |

`RUNTIME_ROLE_SINGLETON_TASKS`（[`web_app.py:421`](../../src/telegram_kol_research/web_app.py)）
把 `runtime_incident_notification` / `strategy_management_notification` /
`system_operator_bot_command` / `telegram_bot_command` **全部划给 worker**。
所以本稿关心的通道几乎都活在 worker 进程里，而 worker 两个 bot 都配齐了。

**结论：运行通知 bot 是活的，而且已经在发东西。** 生产计数：

| 通道 | 已投递 | 待投（被闸门挡住的历史积压） |
|---|---|---|
| 策略管理通知 | 75（max id 144） | 69（max id 97） |
| 持仓保护事件 | 87（max id 518） | 402（max id 422） |

所以这件事**不是「接通一条死通道」，而是「把已经在跑的第二个 bot 用起来」** —— 风险比初稿设想的低得多。

### 4.2 现有出口清单

| # | 通道 | 入口 | 跑在 | 现用 bot | 闸门（**生产当前值**） |
|---|---|---|---|---|---|
| 1 | runtime_incidents（AI agent通知） | `deliver_runtime_incident_notifications` | worker | SYSTEM（`web_app.py:6040` 硬传） | `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID` = **2069** |
| 2 | 消息操作异常 第1/2阶段 | 同上循环内 | worker | SYSTEM | `..._STAGE1_AFTER_CONTRACT_ID` = 0 / `..._STAGE2_AFTER_HANDOFF_ID` = 0 |
| 3 | 持仓归因审计 | `deliver_pending_position_attribution_incidents` | worker / ingest | **两处调用不一致**（见下） | `position_attribution_audit_delivery_after_id` = **3844** |
| 4 | 持仓保护事件 | `deliver_pending_position_protection_incidents` | worker / ingest | **两处调用不一致** | `position_protection_incident_delivery_after_id` = **422** |
| 5 | 策略管理通知 | `run_strategy_management_notification_loop` | worker | NOTIFICATION（**在跑**，已投 75 条） | `strategy_management_notification_delivery_after_id` = **97** |
| 6 | 待入场到期复核 **（唯一带按钮）** | `send_pending_entry_expiry_review` | worker（lifecycle_monitor） | SYSTEM | 无（直发） |
| 7 | 识别冲突复核 / 语义分歧 / 停摆致过期 | `send_*` 三个直发函数 | worker | SYSTEM | 无 |
| 8 | 值守提醒 / 值守正常 | `oncall_service` 独立进程 | 独立 service | 回落 SYSTEM token | 无 |

**三个闸门都已经落好了**（初稿以为还没落）。剩下的就是代码里的路由，不需要再动闸门。

**#3/#4 的既存不一致**：同样两个 deliverer，
[`worker_command_executor.py:336`](../../src/telegram_kol_research/worker_command_executor.py) 传
`notification_bot_config`，而 [`web_app.py:10663`](../../src/telegram_kol_research/web_app.py) 传
`system_operator_bot_config`。生产里 worker 那条路是活的（保护事件已投 87 条 → 走的
**运行通知**），web_app 那条挂在 ingest 的对账循环上，而 ingest 没有 SYSTEM 环境变量，
`system_operator_bot_enabled()` 判 false，整块被跳过 —— **今天是哑的，所以没人发现**。
哪天角色划分一变它就醒过来，同一类消息去两个 bot。本次顺手统一成 `notification_bot_config`。

### 4.3 分流判据：用库里已有的事实，不要手写清单

runtime_incidents 的泛用文案（`format_runtime_incident_notification` 第 461 行）对**所有**
非供应商类型统一收尾：

> 处理: 已记录，正常交易流程未等待本通知。

也就是说，**按系统自己的说法，绝大多数 incident 本来就是纯通知**。真正需要人动手的，
在 `redacted_summary` 里都留了痕迹。生产 2026-09-01 起的实际分布：

#### 需人操作 → 留「Kol事件处理」

| incident_type | 依据字段 | 近一月条数 | 要做什么 |
|---|---|---|---|
| `management_target_needs_confirmation` | `source_status=awaiting_user_confirmation` | 12 | `/choose` 或 `/dismiss` |
| `duplicate_entry_needs_confirmation` | `impact=entry_withheld_awaiting_user_confirmation` | 1 | 决定要不要入场 |
| `provider_outage_entry_not_replayed` | `impact=entry_not_executed_needs_person` | 1 | 手动下单或放弃 |
| `provider_outage_management_not_replayed` | 文案写死「请人工核对仓位后决定」 | 0 | 人工核对 |
| `management_recognition_unresolved` | `impact=management_instruction_not_executed` | 7 | 人工判读指令 |
| `severe_protection_incident` | `source_status=recovery_required` | 31 | 到交易所核对 |
| `management_recovery_required` | `source_status=recovery_required` | 2 | 人工恢复 |
| `source_deletion_exit_stuck` | `source_status=recovery_required` | 2 | 人工恢复 |
| `uncertain_without_write` | `impact=frozen_without_evidence_of_contact` | 11 | 自动管理已冻结，人工决定 |
| `revision_cancel_outcome_unresolved` | `impact=cancel_outcome_unknown_batch_frozen` | 2 | 人工核对撤单结果 |
| `revision_batch_too_stale_to_resume` | `impact=frozen_revision_intent_older_than_horizon` | 2 | 人工决定要不要重做 |

**机械判据**：`source_status ∈ {awaiting_user_confirmation, recovery_required}`
或 `impact` 含 `awaiting_user` / `needs_person` / `frozen` / `not_executed`。
这条判据能覆盖上表全部 11 类，无需手写白名单 —— 但**新类型必须由捕获方主动打这个标**，
判据要写进 `runtime_incident_adapters` 的模块 docstring，否则下一个新类型会静默落到通知侧。

#### 纯通知 → 去「Kol运行通知」

`mimo_provider_*`（5 类）、`provider_outage_replay_started`、`authoritative_recognition_failed`(67)、`context_worker_exhausted`(91)、
`position_marked_manually_closed`(27)、`protection_adopted_from_exchange`(17)、
`message_processing_queue_stalled`(16)、`deferred_instruction_expired`(14)、
`entry_admission_expired`(3)、`management_fraction_rejected`(19)、`management_stop_rejected`(5)、
`entry_revision_authority_blocked_reset`(3)、`management_cancel_precheck_observed`(1)、
`unclassified_operation_failure`(1)、`notification_delivery_failure`、
`background_task_restart_exhausted`，以及 #8 值守的全部三种（提醒 / 已恢复 / 每日正常）。

#### 用户裁定（2026-09-26）

| incident_type | 裁定 |
|---|---|
| `authoritative_execution_uncertain` | → **事件处理** |
| `management_recovery_timeout` | → **事件处理** |
| `management_target_refused` | → 运行通知 |
| `unclassified_operation_failure` | → 运行通知 |

「事件处理 bot 连续 N 天零消息」的值守判据：**本次不做**。

#### 改成白名单方向：默认不变，只搬明确是通知的

初稿写的是「机械判据决定谁留下」。复核后改成**反过来**，理由是安全方向：

- 机械判据靠 `source_status` / `impact` 打标，**靠的是捕获方记得打**。漏打一个，
  需要人动手的事件就静默溜进通知 bot —— 这正是本稿风险表第二行担心的事。
- 真正被投递的类型不止表里这些：`ALWAYS_NOTIFIED_INCIDENT_TYPES`
  （[`config.py:162`](../../src/telegram_kol_research/config.py)）有 **39 个**，
  加上 `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` 手配的 8 个，
  一共四十多类。要我逐个替用户判断「这个算不算要人管」，错判的成本不对称。

所以最终规则是一句话：

> **默认维持现状（去事件处理）；只有下面这份显式清单里的类型改去运行通知。**

新类型不在清单里 → 行为和今天完全一样，不会有人被静默。清单要改，是一次显式的代码改动。

#### 改去「Kol运行通知」的显式清单（19 类）

```
mimo_provider_unavailable        mimo_provider_recovered
mimo_provider_failure_streak     mimo_provider_probe_failed
mimo_provider_health_check_failed
provider_outage_replay_started
authoritative_recognition_failed context_worker_exhausted
position_marked_manually_closed  protection_adopted_from_exchange
message_processing_queue_stalled deferred_instruction_expired
entry_admission_expired          management_fraction_rejected
management_stop_rejected         entry_revision_authority_blocked_reset
management_cancel_precheck_observed
management_target_refused        unclassified_operation_failure
```

（`notification_delivery_failure` / `background_task_restart_exhausted` **不在清单里**：
投递本身坏了要是也投到那个可能正坏着的 bot，就没人知道了。它们留在事件处理。）

留在事件处理的那些（不必显式列，是默认）包含 4.3 上表 11 类、用户裁定的 2 类、
`severe_protection_incident`、以及 `market_fill_attribution_unverified` /
`naked_market_fill_safety_net` / `conditional_entry_absent_from_exchange` /
`management_submit_unknown` / `stop_resize_replace_incomplete` 等
「交易所上可能有一个没有保护的真实仓位」的那一类 —— `ALWAYS_NOTIFIED` 的注释写的就是它们。

#### 通道级归属

- #5 策略管理通知 → 运行通知（它本来就绑 NOTIFICATION，**已经在跑**，不动）
- #3 归因审计 / #4 保护事件 → 运行通知（生产已经是了；把 web_app 那处统一成 `notification_bot_config`）
  - 例外：`severe_protection_incident` 走的是 #1 通道，留事件处理
- #2 消息操作异常 第1/2 阶段 → **跟随它所属 incident 的路由**，两阶段必须同去一个 bot
  （第 2 阶段把第 1 阶段的 `telegram_message_id` 存下来做幂等，分到两个 bot 那个 id 就失去意义）
- #6 到期复核（带按钮） → **保持事件处理，一个字都别动**
- #7 三个直发（识别冲突复核 / 语义分歧 / 停摆致过期） → 运行通知（都是「告诉你一声」，没有可点的东西）
- #8 值守 → 运行通知（配独立 env，见 4.4）

### 4.4 实施顺序

初稿把「落闸门」列为第一步。复核后发现**三个闸门生产上早就落好了**
（3844 / 422 / 97，runtime incident 的 env 闸门 = 2069），`NOTIFICATION_BOT_CHAT_ID`
在 worker 单元里也早就写了。所以那两步作废，剩下的是纯代码 + 一处 env。

**代码（本次 worktree 内做完）**

1. 改动 A：`head_model` 进 summary，8 处文案按它生成
2. 改动 B：恢复通知的补做分岔
3. **改 deliver 循环**：`deliver_runtime_incident_notifications` 现在只收一个 `config`，
   要按 `incident_type` 选 bot 就得**同时拿到两个 config**。这是唯一有结构改动的地方，
   不是「把参数从 A 换成 B」。`deliver_message_operation_stage1/stage2` 同理
4. 统一 #3/#4：`web_app.py` 那处改成 `notification_bot_config`
5. #7 三个直发函数改传 `notification_bot_config`

**部署时（回调度会话确认顺序后再做）**

6. 值守单独配 `TELEGRAM_KOL_ONCALL_BOT_TOKEN` / `TELEGRAM_KOL_ONCALL_CHAT_ID` 指向运行通知，
   不再回落 SYSTEM（写 `/etc/telegram-kol-oncall.env`，不是 `/opt/.../config/`）
7. `tg-deploy` 管 web/worker；**值守服务要单独重启**，它不跟 web 走

**回滚**：改动全在代码里，回滚 = `tg-deploy <前一个 sha>`；第 6 步的 env 回滚 = 删掉那两行再重启值守。

### 4.5 收口判据

- 运行通知 bot 收到第一条 `mimo_provider_*` 或 `context_worker_exhausted`
- 事件处理 bot 收到的下一条消息，要么带按钮，要么是 4.3 默认集里的类型
- 两个 bot **不出现同一条 incident**（`runtime_incidents.notification_status` 只有一列，
  一条只会投一次）
- 供应商告警标题里出现 `gpt-5.6-luna` 而不是 `MiMo`
- 闸门未被本次改动碰过：部署后 `trading_settings.global` 三个 `*_after_id` 仍是 3844 / 422 / 97

---

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 积压冲垮新 bot | 三个闸门生产已落好（4.2 表内数值），本次不动它们 |
| 新增 incident 类型静默落到通知侧，需人操作的没人看见 | **改成白名单方向**：默认留事件处理，只有 19 类显式搬走（4.3）。新类型行为不变 |
| #3/#4 双投 | 统一成 `notification_bot_config`；且 ingest 侧今天本就是哑的 |
| 投递本身坏了，告警投到坏掉的 bot | `notification_delivery_failure` / `background_task_restart_exhausted` 不进搬迁清单 |
| 改文案时误动 `incident_type` | 第 2 节的边界表；改完 grep 确认 `_MIMO_PROVIDER_INCIDENT_TYPES` 八个字面量未变 |
| 与正在进行的其它会话冲突 | 另一会话在 `uncertain-attempt-closeout` 改 `authoritative_execution_attempts.py` / `authoritative_recognition.py` / `cli.py` / `db.py` / `models.py`（含 schema，L3，部署排在本次前面）。本次**不碰这 5 个文件** |
| 用户裁定的 `authoritative_execution_uncertain` 与另一会话的工作重叠 | 本次只决定它投到哪个 bot，不动它的产生逻辑 |

---

## 6. 不在本稿范围

- 表名、模块名、`parse_source` 枚举值的改名（三层里的第二、三层）
- `Kol信号` bot 的任何改动
- 值守判据本身（D6 那条线见 `docs/plans/2026-09-26-oncall-d6-silent-stall-rules-design.md`）
- codex-proxy 的上游容错（这次是 OpenAI 侧故障，备用模型已经按设计接住了，无需改）
