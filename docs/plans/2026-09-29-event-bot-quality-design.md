# 2026-09-29「Kol事件处理」bot 消息质量修复设计稿（待批准）

- 来源：2026-09-29 只读核对（北京 09-28 19:32 → 09-29 05:32 事件处理 bot 收到的消息）
- 基线：`origin/main` = 生产 HEAD = `0bcb894e`（文档提交 `6450ac67`）；值守状态库
  `/var/lib/telegram-kol-oncall/state.db`
- 核实方式：生产库只按主键 / 索引点查（`runtime_incidents` 主键、`authoritative_execution_attempts`
  主键与 status 索引、`strategy_management_batches.raw_message_id`、`message_instruction_items.raw_message_id`、
  `execution_events.action` / `source_message_id`、`strategy_lifecycles` 主键与 `(chat_id, message_id)` 唯一键）；
  值守状态库只读；worker 日志 `journalctl` 按时间窗；`/etc/telegram-kol-*.env` 只读了**键名**和两个
  incident 类型清单的值。没有全表扫描、没有交易所调用、没有发任何 Telegram 消息。
- 状态：**已批准（2026-09-29，用户：第 8 节全部按推荐）**；**已部署 `83fcfba6`（2026-09-29 10:32Z），L1 窗口通过**，见第 10 节

## 0. 结论速览

| # | 问题 | 根因（生产数据核实） | 修法 | 级别 |
|---|---|---|---|---|
| 1 | 每日 30 条上限被 D6c 吃光，值守 #32 被挤掉且没补发 | ① D6c 对「按设计不推送」的两类 incident（`authoritative_recognition_failed` / `context_worker_exhausted`）照样开案，它们永远停在 `notification_status=pending`；② D6c 没有宽限期，incident 刚写入、worker 还没来得及投递（9–20 秒）就开案；③ 被上限压掉的案件 `alerted_at` 留空，之后没有任何路径再发 | D6c 排除按设计静音的类型 + 10 分钟宽限期 + 同类型合并为一案 + 不请 Codex 诊断；被上限压掉、仍 open 的案件在额度恢复后补发（连同已完成的诊断） | L1（值守） |
| 2 | #19598 一件事发 4 条 | ① 同一执行尝试 4631 无写入时写**两条** incident（`authoritative_execution_uncertain` + `uncertain_without_write`）；② 一小时后 `management_recovery_timeout`（批次 184）不引用前面两条 | 无写入时只写 `uncertain_without_write`；事件通知加「关联」行，列出同一消息此前的事件号 | L1 |
| 3 | 文案误导、缺群名 / 原文 / 仓位；#2419 只剩原因码 | ① 通用文案对所有类型都写「处理: 已记录，正常交易流程未等待本通知」；② 格式化函数只读 `redacted_summary`，从不查消息 / 群 / 仓位；③ #2419 **不是漏白名单字段**：`strategy_instance_id` 的值里含群 ID，被敏感信息扫描整条拒掉；既有测试用的是 `chen-btc-696` 这种假值，抓不到；④ 补救提案引用了一条没发出的诊断 | 留在事件处理的类型按类型写「需要你做什么」；投递时按消息补群名、原文摘要、仓位（失败不影响投递）；`strategy_instance_id` 拆成 标的 / 方向 / 源消息号，不带群 ID；测试改用生产形状的值并覆盖全部适配器；提案文案不再引用可能不存在的诊断 | L1 |
| 4 | 舒琴 #19639 几何拒绝后 3 小时又问「要不要撤挂单」 | 生命周期 1356 照常建成 `pending_entry`、没有执行绑定；到期复核只对「群不自动交易 / 标的不在白名单」两种情况静默收口，自动交易群的无绑定行照样发复核。近 40 次几何拒绝里 32 次后面跟了一条无意义的复核 | 到期时，无绑定且该消息有 `entry_price_geometry_rejected` 记录 → 静默标记过期（与既有「范围外收口」同一形状）；几何拒绝通知显示群名而非裸 chat_id | L1 |
| 5 | 执行尝试 4631 一直 uncertain，日志每 30 分钟一条 | 管理指令在预检就被拒（`protection_rows_unattributed_on_exchange`，一个字节都没发），但条目的错误载荷不在 A-6 的「无接触证明」词表里，边界按「结果未知」冻结 | 数据：用 09-26 的 `close-out-uncertain-attempts` 收口（L3，单列，需单独批准）；根因另议（第 8 节 Q5） | L3（数据） |
| 6 | 值守仍回落到事件处理 bot | `/etc/telegram-kol-oncall.env` 没有 `TELEGRAM_KOL_ONCALL_BOT_TOKEN`，按代码回落到 SYSTEM bot；分流设计 4.4 第 6 步没做 | **需要拍板**，见第 8 节 Q1，推荐「按规则分流」 | L1 |

## 1. 问题 1：D6c 吃光每日上限，#32 被挤掉

### 1.1 生产事实

值守状态库 `daily_alerts:2026-09-28 = 30`、`daily_suppressed:2026-09-28 = 15`（按北京日期计），
`cap_reached` 在 05:25Z（北京 13:25）发出。北京 09-28 当天的 30 条：

| 来源 | 条数 |
|---|---|
| D6c 案 17–25 的「开案 / 诊断 / 已结束」 | 27（9 案 × 3） |
| D6c 案 26 开案（`protection_adopted_from_exchange` #2407） | 1 |
| D4 健康「停摆 / 已恢复」 | 2 |

案 17–25 对应的 incident：`authoritative_recognition_failed` ×4（2396、2397、2398、2401）、
`context_worker_exhausted` ×5（2400、2403–2406）。这两类正是 `config.TELEGRAM_QUIET_INCIDENT_TYPES`
（09-22 起刻意不推送），`notification_status` 永远是 `pending`、`notified_at` 永远为空——
D6c 的「从未通知过」判据对它们**恒成立**。

之后被静默的案件（`alerted_at` 为空）：

| 案 | 规则 | 对象 | 结局 |
|---|---|---|---|
| 27 / 29 / 31 | D6c | `authoritative_recognition_failed` 2408 / 2409 / 2415 | 1 小时后 resolved |
| 28 | D3 | 消息 #19514 识别失败 | stale |
| 30 | D6c | `management_fraction_rejected` 2414 | resolved（这一条是真问题，0bcb894e 已修） |
| **32** | **D1a+D2** | **陈哥 #19598 管理指令没执行** | **stale，从未发出** |
| 33 / 34 | D6c | 2417 / 2418 | 开案 1 分钟后 resolved |

案 32 的 Codex 诊断 `status=done`、`message_state=none`：诊断完成了，但当时诊断消息也受上限拦截，没发。
北京 0 点（16:00Z）上限重置时案 32 仍是 open，但 `compose_case_alerts` 只处理**本轮新开**的案件，
被压掉的案件没有第二次机会；19:45Z 变 stale。

**D6c 的第二个缺陷——没有宽限期。** 案 26、33、34 的 incident 分别在开案后 42 秒、2 秒、2 秒被 worker
正常投递（2407 `notified_at` 05:24:48，案 26 开于 05:24:06；2417/2418 `notified_at` 13:45:22，
案 33/34 开于 13:45:20）。这是 D6c 和 worker 投递循环在赛跑，不是「没人听见」。

**0bcb894e 之后情况变得更糟。** 那次把「D6c 开案」列为不受上限拦截（本意是让「告警从未送达」能发出去），
而 D6c 对静音类型恒成立，所以现在这类噪音**不再有上限**：北京 09-29 07:31 / 07:55 的案 35、36
（2422、2423，都是 `authoritative_recognition_failed`）已经各发了「开案 + 诊断」。
另外 Codex 诊断额度也被它吃掉：`codex:daily_calls:2026-09-28 = 17`（平时 2–3）。

### 1.2 修法

**A. D6c 只报「本该送达却没送达」的 incident**（`oncall_detector._unheard_incident_reason`）

1. **排除按设计静音的类型**：`authoritative_recognition_failed`、`context_worker_exhausted`。
   值守进程按架构边界不能 import `config.py`，所以在 `oncall_detector` 里放一份常量
   `D6C_QUIET_BY_DESIGN_INCIDENT_TYPES`，并加一条测试钉住它与 `config.TELEGRAM_QUIET_INCIDENT_TYPES` 相等
   （测试可以同时 import 两边）。以后谁改静音清单，测试会逼他同时改这里。
   覆盖没有丢：`authoritative_recognition_failed` 在「群里有仓位」时由 D3 开案（案 28 就是），
   没仓位的群本来就不该叫人；`context_worker_exhausted` 的后果（消息没被处理）由 D4 停摆和 D1/D2 覆盖。
2. **宽限期**：「从未通知过」要求 `first_occurred_at` 距今 ≥ 10 分钟。生产上 worker 投递延迟是 9–42 秒，
   10 分钟足够宽，又远小于 D6c 1 小时的「仍在发生」窗口。「很久没再通知」（3 天）那一支不受影响。
3. **同类型合并为一案**：案件键从 `unheard_incident:<incident_id>` 改为
   `unheard_incident_type:<incident_type>`；证据里列出本轮命中的 incident 号（最多 10 个）。
   D6c 要说的是「X 类告警送不出去」，这是类型层面的事实；3 分钟内 5 条同类 incident 不该变成 5 个案件。
   旧键的 open 案件（部署时是案 35、36，都是静音类型）会被 clear 路径正常关闭；
   **它们的「已结束」不发**——对象本来就不该开案（见 2.4 的实现注意）。
4. **D6c 不请 Codex 诊断**。它的问题是「投递配置 / 投递通道」，Codex 读消息和仓位给不出有用的判断，
   09-28 那 17 次调用基本都花在这里。D6c 开案文案本身已写明类型、记录号、多久没通知。

**B. 被上限压掉的案件补发**（`oncall_alerts` 新增 `compose_backfill_alerts`，每轮在 `compose_case_alerts` 之后调）

- 选取：`status='open'`、`alerted_at IS NULL`、非健康案件。代码里 `alerted_at` 留空的唯一原因就是被上限压掉
  （同群合并也会写 `alerted_at`；健康冷却不涉及非健康案件），不需要改表结构。
- 条件：该案件现在满足「不受上限拦截」（0bcb894e 的规则），或当天额度未满（例如过了北京 0 点）。
- 发出的开案消息首行加「（补发：原 X 时因当日告警上限未发出）」，然后 `mark_case_alerted`。
- 若该案 Codex 诊断已 `done` 且 `message_state` 不是 `queued/suppressed`，紧跟着补发诊断。
- 每轮最多补发 5 条，防止重置后一次倾泻；补发的开案照常计入当天额度。
- 已经 resolved / stale 的不补发（事情已经过去，告诉人也没用，和「不报没人知道的问题的恢复」同一原则）。

按今天的状态库，部署时**没有**符合补发条件的案件（35、36 已发；28、32 已 stale），不会一上线就补发一串旧消息。

**C. D1/D2 不被挤掉**：0bcb894e 已让「高严重度且带消息号」的开案不受上限拦截，案 32 这类案件
现在会直接发出。本稿不再改这条规则，只用 B 兜住「万一被压掉」的情况。

### 1.3 回放用例（修复前失败 / 修复后通过）

- R1-a：生产 incident 2396–2406 的形状（类型、严重度、`notification_status=pending`、`notified_at` 空、
  `last_occurred_at` 在 1 小时内）→ 修复前开 9 个 D6c 案；修复后 0 个。
- R1-b：2407 / 2417 / 2418 的形状（`first_occurred_at` 在 2–42 秒前、尚未通知）→ 修复前开案；修复后不开；
  同一行 11 分钟后仍未通知 → 开案。
- R1-c：5 条同类型、都未通知且超过宽限期的 incident（非静音类型）→ 修复前 5 案；修复后 1 案，证据列出 5 个号。
- R1-d：09-28 当天的顺序回放——先用 30 条普通告警打满额度，再开一个 medium 严重度的 D1 案（被压掉），
  跨过北京 0 点 → 修复前永不发出；修复后第一轮补发，带「补发」前缀；若诊断已完成，诊断紧随其后。
- R1-e：D6c 案不入 Codex 队列。
- R1-f：静音常量与 `config.TELEGRAM_QUIET_INCIDENT_TYPES` 相等。

## 2. 问题 2：同一件事重复发

### 2.1 生产事实

| 消息 | 来源 | 内容 |
|---|---|---|
| 补救提案 P2 | 值守案 32 → worker | 陈哥 #19598 保本可补救 |
| incident 2417 `authoritative_execution_uncertain` | 执行尝试 4631 | `partial_failed no_exchange_write_tracked` |
| incident 2418 `uncertain_without_write` | **同一个**执行尝试 4631，同一时刻 | 同上 + `impact=frozen_without_evidence_of_contact` |
| incident 2419 `management_recovery_timeout` | 管理批次 184（raw 19598），一小时后 | 只剩 `reason_code=break_even_market_decision_missing_or_invalid` |

`authoritative_execution_attempts.mark_authoritative_execution_uncertain` 先无条件写 2417，
无写入时再写 2418；两条 `notified_at` 完全相同（13:45:22）。2418 包含 2417 的全部字段，多一个 `impact`。

### 2.2 修法

1. **一个尝试一条**：`mark_authoritative_execution_uncertain` 在无写入时只写 `uncertain_without_write`，
   有写入时只写 `authoritative_execution_uncertain`（两者互斥）。两类都在 `ALWAYS_NOTIFIED`、都留事件处理，
   去掉一条不会让任何人漏看。
2. **关联行**：事件通知投递时（见 3.2 的上下文查询），若本 incident 能定位到源消息号，
   就查同一消息此前 24 小时内的其它 incident（经 `authoritative_execution_attempts.raw_message_id` 索引 →
   `runtime_incidents (source_kind, source_record_id)` 索引；以及 `strategy_management_batches.raw_message_id`
   索引 → 同一索引），写一行「关联：同一消息此前已报 #2418（执行冻结）」，最多列 3 条。
   不合并、不抑制：2419 说的是「冻结已解除、这条指令放弃」，是新事实，只是要让人一眼看出是同一件事。

### 2.3 回放用例

- R2-a：用 4631 的形状（无写入，`error_summary=partial_failed`）调 `mark_authoritative_execution_uncertain`
  → 修复前 2 条 incident；修复后 1 条 `uncertain_without_write`。有写入时仍是 1 条 `authoritative_execution_uncertain`。
- R2-b：先落 2418 形状、再落 2419 形状（同 raw 19598、批次 184）→ 2419 的通知正文含「关联 … #2418」；修复前没有。

## 3. 问题 3：文案误导、缺上下文、#2419 摘要被拒

### 3.1 生产事实

- `format_runtime_incident_notification` 对所有非供应商类型统一结尾「处理: 已记录，正常交易流程未等待本通知。」
  这句话对运行通知类（纯告知）是对的，对用户 09-26 裁定「必须留在事件处理」的那些类型是错的：
  它们恰恰是「自动流程已冻结 / 已放弃，要人来接」。
- 格式化只读 `redacted_summary`，不查库，所以正文只有类型、来源号和原因码，没有群名、原文、仓位。
- **#2419 的根因与核对时的判断不同**：`38318d17`（09-27，生产在它之后）已把 `strategy_instance_id`、
  `lifecycle_id`、`effective_action` 加入白名单。本机用 2419 的真实字段形状重放
  `_validate_redacted_json_contract`：唯一被判敏感的是 `strategy_instance_id`——它的生产格式是
  `deepcoin:<群ID>:<源消息号>:<标的>:<方向>`，群 ID 是一串长数字，敏感扫描**正确地**拒了它，
  于是整条详细摘要回落到最小摘要。`tests/test_runtime_incident_detailed_summaries.py` 用的是
  `strategy_instance_id="chen-btc-696"`，永远触发不了这条。
  （核对报告里说的「099d6cdc 补了 11 个字段」实际是 `38318d17`；099d6cdc 是一条文档提交。）
- 补救提案固定写「参考：Codex 意见见值守 #32 的诊断消息」，而案 32 的诊断消息没发出（1.1）。

### 3.2 修法

1. **按去向分两种结尾**（`system_operator_bot.format_runtime_incident_notification`）：
   - 去运行通知的类型（`NOTIFICATION_BOT_INCIDENT_TYPES`）：保持原文案。
   - 其余（留在事件处理）：去掉「未等待本通知」，改成「需要你：<一句具体动作>」。按类型给固定句子，例如：
     - `uncertain_without_write` / `authoritative_execution_uncertain`：这条消息已冻结、不会再自动执行；请到交易所核对相关仓位是否需要手动处理。
     - `management_recovery_timeout`：这条管理指令已放弃执行、冻结已解除；请核对仓位的止损 / 止盈是否符合原意。
     - `severe_protection_incident` / `management_recovery_required` / `source_deletion_exit_stuck`：到交易所核对并人工恢复。
     - `management_target_needs_confirmation` / `duplicate_entry_needs_confirmation`：回复 /choose 或 /dismiss。
     - 没列到的类型：通用句「自动处理已停止，需要人工核对」。**不再对任何事件处理类型说「未等待」**。
2. **投递时补上下文**（新纯读函数 `load_incident_context(session_factory, incident, group_label)`，在
   `deliver_runtime_incident_notifications` 里、格式化之前调用）：
   - 定位源消息：summary 的 `raw_message_id`；否则按 `source_kind` 取——执行尝试 → 尝试行的 `raw_message_id`；
     管理批次 → 批次行的 `raw_message_id` 与 `execution_binding_id`。全部是主键点查。
   - 群名：worker 的 `group_config` 标签（与 `web_app._group_label_by_chat_id` 同一来源），取不到写「未知群」，
     **绝不回落成 chat_id**。
   - 原文摘要：`raw_messages.text` 前 120 字，过 `_safe_runtime_incident_value`（与「消息操作异常第 1 阶段」同样的做法）。
   - 仓位：有绑定时写「标的 方向（绑定号，状态）」。
   - 关联行：见 2.2。
   - 任何一步失败 → 该行不写，照常投递。上下文只用于显示，不写库。只对事件处理类型做；运行通知类不查。
3. **#2419**：`capture_management_recovery_timeout` 不再放 `strategy_instance_id` 原值，改放
   `symbol`、`side`、`origin_message_id`（从该 id 拆出，拆不出就不放）。群 ID 永远不进摘要。
   全模块再查一遍是否还有别的适配器把含群 ID 的值塞进摘要。
4. **覆盖全部类型的测试**：
   - `test_runtime_incident_detailed_summaries.py` 的用例值改成生产形状（`strategy_instance_id` 用
     `deepcoin:-100<13 位>:<消息号>:BTC:long` 这种假群 ID），每个适配器断言「存下来的就是详细摘要」。
   - 新增参数化用例：对 `ALWAYS_NOTIFIED_INCIDENT_TYPES ∪ NOTIFICATION_BOT_INCIDENT_TYPES ∪ 生产白名单 8 类`
     里的每一个类型格式化一条通知，断言：事件处理类型不含「未等待本通知」且含「需要你」；运行通知类型保持原句；
     任何类型的正文都不含形如 `-100\d{10,}` 的群 ID。
5. **补救提案文案**：「参考」行改为只在值守侧确认诊断已排队时才写，否则写
   「本案暂无 Codex 诊断」。值守在提交补救请求时把「诊断消息状态」一并写进请求（已有请求载荷，加一个字段），
   worker 据此选句子。若加字段要动跨进程契约太重，退而求其次把句子改成不承诺存在的说法
   「Codex 诊断如有，会以「🔎 值守诊断 #32」单独发送」。实施时选前者，做不干净就用后者并在状态文档里写明。

### 3.3 回放用例

- R3-a：2417 / 2418 / 2419 三条真实摘要 + 对应的 raw 19598（陈哥群、原文）、批次 184、绑定 387 →
  修复前正文含「未等待本通知」、无群名 / 原文 / 仓位；修复后含「陈哥群名」「原文摘要」「BTC 多（绑定 387）」「需要你：…」。
- R3-b：2419 的生产形状入参调 `capture_management_recovery_timeout` → 修复前存下的是最小摘要（只有 reason_code）；
  修复后存下详细摘要，含 `symbol=BTC`、`side=long`、`origin_message_id`、`lifecycle_id=1348`，不含群 ID。
- R3-c：上下文查询抛异常 → 通知照常发出，只少上下文行。
- R3-d：补救提案——诊断未排队时不再出现「见值守 #N 的诊断消息」。

## 4. 问题 4：几何拒绝后又问「要不要撤挂单」

### 4.1 生产事实

- #19639（舒琴 ETH 多，14:49:54Z 发）：`execution_events` 4716 `entry_price_geometry_rejected`
  （`manual_review`，14:51:44），14:52:16 投递。通知正文 `Chat:` 一行是裸 chat_id，没有群名
  （`format_terminal_entry_cleanup_notification`）。
- 同一时刻生命周期 1356 建成 `pending_entry`，`execution_binding_id` 为空（交易所上什么都没有）。
  17:51 到期复核发出，问「继续等待、标记过期或撤销交易所挂单」。00:27Z 被标记过期
  （`expiry_expired_no_live_order`）。
- 这不是个例。最近 40 次几何拒绝（09-14 → 09-28）对应的生命周期里，**32 个**在拒绝之后收到了到期复核：
  17 个被人在 bot 里点了「标记过期」（`expiry_expired_no_live_order`），7 个两天没人回、被自动过期，
  7 个停在 `expiry_review_requested`（后来 KOL 自己入场 / 离场，生命周期状态变了），1 个撤单失败。
  没有一个有执行绑定。

### 4.2 根因

`lifecycle_monitor._prepare_pending_expiry_reviews` 已有「静默收口」路径
`_claim_expiry_out_of_scope_closeout`，但只对「群不自动交易 / 标的不在白名单」两种范围外情况生效。
自动交易群里被几何校验拒掉、从未下单的行仍被当作「可能挂着单」来问人。

### 4.3 修法

- 到期时，若行是 `pending_entry`、`execution_binding_id IS NULL`，且该消息（按 `raw_messages` 的
  `(chat_id, message_id)` → `execution_events.source_message_id` 索引）有 `entry_price_geometry_rejected`
  记录 → 走与范围外收口**同一个条件更新**（`pending_entry`、无绑定、未通知过、`management_action` 未变），
  标记 `expired`，备注「入场已被价格几何校验拒绝，交易所无挂单，按超时直接过期」。不发通知（拒绝那一刻已经通知过）。
- 只认几何拒绝这一种记录。其它「无绑定」的情况（例如入场准入还在排队）照旧问人——那里交易所上可能马上会有单。
- 只在**到期时**收口，不在拒绝时收口：之后同群消息的上下文解析可能还要指向这个策略，提前关掉会改变识别结果。
- 几何拒绝通知：`Chat:` 行改为「群: <群名>」（同 3.2 的来源；取不到写「未知群」，不回落成 chat_id）。

### 4.4 回放用例

- R4-a：生命周期 1356 的形状 + 事件 4716 → 修复前 `_prepare_pending_expiry_reviews` 产出一条复核；
  修复后 0 条复核、行变 `expired`、备注如上。
- R4-b：同样无绑定、但没有几何拒绝记录 → 仍产出复核（不扩大收口面）。
- R4-c：有绑定 → 仍产出复核。
- R4-d：几何拒绝通知正文含群名、不含 chat_id。

## 5. 问题 5：执行尝试 4631

### 5.1 生产事实

- 4631：`status=uncertain`、`exchange_effect=outcome_unknown`、`error_summary=partial_failed no_exchange_write_tracked`、
  `evidence_refs_json=[]`。全表 uncertain 只有这一行（status 索引计数）。
- 指令条目 1426：`status=failed`，`error_json` 是 `ManagementBatchExecutionError`，
  消息 `protection_rows_unattributed_on_exchange:…`——这是 0bcb894e 修掉的那道预检，发生在任何交易所写入之前。
- 该消息名下 `execution_events` 0 条、执行绑定 0 条；批次 184 关联的绑定 387 现已 `closed`。
  按 09-26 工具的分桶规则，它落在 `no_execution_event` → `closed_no_write`。
- worker 每 30 分钟一行 `WARNING … row_id=4631 … action=observe_uncertain`（09-26 已节流降级），
  这就是核对里说的「周期性报错」。09-28 14:4x 那批 `backup-stop reconciliation … failed` ERROR 已不再出现。

### 5.2 为什么被冻成 uncertain

`execution_boundary._items_prove_no_exchange_contact` 要求每个条目的载荷自己证明「没接触交易所」：
要么 `status` 在无接触词表里，要么 `reason/message` **精确等于** A-6c 的 6 个「拿执行权失败」原因之一。
管理预检的拒绝不在其中，而且带 `:posId:orderIds` 后缀，精确匹配也匹配不上，于是整条消息被冻结。

把它改成 `failed_safe` 会让这条消息重新**可重试**——即几小时后可能再执行一次保本。这改变的是交易所写入语义，
按 AGENTS.md 属于 L3、必须单独列入批准范围。本稿**不改**，列为 Q5。

### 5.3 数据收口计划（L3，单列，需用户单独批准后由调度会话执行）

1. 部署窗口外、无进行中的管理批次时执行。
2. 备份：磁盘 36%（剩 33 GB），库 1.27 GB，满足「≥ 2×库 + 5 GB」→ 做全库备份：
   `/var/backups/telegram-kol/20260929-attempt-4631-closeout/research.db`（`VACUUM INTO`），
   `PRAGMA quick_check` 通过后 `nice -n 19 zstd -T1 -3 --rm`，记录大小和 sha256。
3. 前计数：`authoritative_execution_attempts` 按 status 分组（status 索引）；4631 一行全列（不含群 ID 的列）。
4. `telegram-kol-research close-out-uncertain-attempts --database-path …`（dry-run）→ 期望
   `scanned 1 | closeable 1 | refused 0 | exchange_write_count 0`、桶 `no_execution_event`。不符就停。
5. `--apply --expected-count 1`。
6. 后计数：uncertain 0、`closed_no_write` +1；`recognition_decisions` 那一行逐列不变（消息仍不可重新识别）。
7. 30 分钟后确认 worker 不再出现 `row_id=4631` 那行 WARNING。
8. 回滚：停服后把备份拷回（数据只动一行，也可按前计数里记下的全列值原样写回）。备份 14 天后按规则退役。

## 6. 问题 6：值守走哪个 bot

- 现状：`/etc/telegram-kol-oncall.env` 里没有 `TELEGRAM_KOL_ONCALL_BOT_TOKEN / _CHAT_ID`，
  `oncall_service` 回落到 `TELEGRAM_KOL_SYSTEM_BOT_*`，即「Kol事件处理」。分流设计 4.4 第 6 步未执行。
- 决定见 Q1。若选「按规则分流」，代码侧：值守新增一对可选配置
  `TELEGRAM_KOL_ONCALL_NOTIFY_BOT_TOKEN / _CHAT_ID`，缺省时全部照旧走现在的 bot（行为不变）；
  配了以后 D6c / D4 / D5 健康 / 每日报平安 / Codex 状态 / 上限提醒 走运行通知，
  D1–D3、D6a、D6b（针对某条消息、需要人做决定的）和它们的诊断、已结束 留在事件处理。
  同一个案件的开案、诊断、已结束**始终走同一个 bot**。
  部署时由调度会话在服务器上把运行通知 bot 的两行配置从 worker 的 env 复制进 oncall 的 env
  （用 `grep '^TELEGRAM_KOL_NOTIFICATION_BOT_' … | sed 's/NOTIFICATION_BOT/ONCALL_NOTIFY_BOT/' >> …`，不打印值），再重启值守。

## 7. 风险、测试与部署

- 级别：问题 1、6 只改值守进程；问题 2、3、4 改 worker 的告警 / 通知路径和生命周期到期收口，
  都不碰交易所写入、不改数据库结构、不改识别提示词、不碰自动交易开关。合并为一个候选按 **L1** 处理；
  问题 5 的数据收口单独按 **L3**，不在本候选里。
- 问题 4 是唯一会改业务状态的一处（生命周期 `pending_entry → expired`）。它复用既有的条件更新，
  并且只在「无绑定 + 有几何拒绝记录 + 已到期」时生效；这类行今天的结局也是被人或定时器标记过期，只是晚了、多了一条消息。
- 开发中跑相关测试；最终候选跑一次全量 `uv run python -m pytest -q`。
- 部署（调度会话排期；本会话不部署、不推 `origin/main`）：
  1. 候选是生产 HEAD 的后代；非策略时效操作期间。
  2. `tg-deploy <候选 sha>`（worker → web → ingest）。
  3. **单独** `systemctl restart telegram-kol-oncall.service`（值守代码有改动；tg-deploy 不重启它）。
     若 Q1 选分流，先按第 6 节写好 oncall env 再重启。
  4. 观察（L1）：15 分钟或 5 条真实消息。重点：值守没有新开静音类型的 D6c 案；事件处理 bot 的新通知带群名 / 原文；
     没有新的「详细摘要被拒」日志。
  5. 回滚：`tg-deploy 0bcb894e9280d273e145eb44e99d24d14c6870a7`，再重启值守；若配了 oncall 通知 env，删掉那两行。

## 8. 需要用户拍板

- **Q1 值守走哪个 bot**
  - A（**推荐**）按规则分流：要人决定的（D1–D3、D6a、D6b 及其诊断 / 结束）留事件处理；
    D6c、健康、报平安、Codex 状态、上限提醒去运行通知。
    利：事件处理 bot 回到「出现即要处理」；弊：多一对配置，部署时要在服务器上复制两行 env。
  - B 全部留事件处理（正式取消 4.4 第 6 步）。利：零配置、零风险；弊：健康 / D6c / 报平安继续混在要处理的消息里。
  - C 全部去运行通知（原设计）。利：简单；弊：陈哥 #19598 这类真要人动手的提醒会被埋在纯通知里——这正是你担心的。
- **Q2 D6c 的收窄方式**：推荐第 1.2 A 的四条一起做（排除静音类型 + 10 分钟宽限 + 同类型合并 + 不请 Codex）。
  另一种做法是「D6c 只看会去事件处理的类型」，但那样会漏掉 09-28 案 30（`management_fraction_rejected`
  被漏配进必发清单）这种真问题——D6c 最有价值的恰恰是抓「配置漏了」。
- **Q3 补发的范围**：推荐只补发仍 open 的案件、每轮最多 5 条、带「补发」前缀；resolved / stale 的不补。
- **Q4 同一执行尝试只留一条**：推荐无写入时只写 `uncertain_without_write`、有写入时只写 `authoritative_execution_uncertain`。
- **Q5 管理预检拒绝被冻成 uncertain 的根因**：本稿不改（会改变消息可重试性，属交易所写入语义）。
  推荐另立一稿，方向是「预检拒绝落 `failed_safe` 但对管理类消息关掉自动重试」。今天先用数据收口止血。
- **Q6 执行尝试 4631 的数据收口**：按第 5.3 节执行，是否批准？（批准后由调度会话在部署窗口外执行，不在本候选里。）
- **Q7 几何拒绝收口的范围**：推荐只认 `entry_price_geometry_rejected`；是否要把其它「确定没下单」的入场拒绝也算进来，
  等本次上线后看数据再定。

### 8.1 裁定（2026-09-29）

Q1 选 A（按规则分流）；Q2 四条一起做；Q3 只补发 open、每轮最多 5 条、带「补发」前缀；Q4 同一尝试只留一条；
Q5 本次不改、另立一稿；Q6 批准按 5.3 执行（调度会话在部署窗口外做，不在本候选里）；Q7 只认 `entry_price_geometry_rejected`。

## 9. 实施记录（2026-09-29）

两个子代理并行：A＝值守（第 1 节 + 3.2 第 5 条 + 第 6 节），B＝worker（第 2、3、4 节）。
本会话审阅后按顺序 cherry-pick 到 `claude/eager-dubinsky-44265c`。

| 提交 | 内容 |
|---|---|
| `1576ccda` | B1：同一执行尝试只写一条 incident（无写入 → `uncertain_without_write`，有写入 → `authoritative_execution_uncertain`） |
| `dbc5cdae` | B3：`management_recovery_timeout` 摘要不再带含群 ID 的 `strategy_instance_id`，改为 `symbol` / `side` / `origin_message_id`；详细摘要测试改用生产形状值 |
| `48abd5f4` | B2+B4：事件处理类型结尾改「需要你：…」；投递时补群名 / 原文 / 仓位 / 关联；几何拒绝后到期静默收口；几何拒绝通知与两条供应商故障「未重放」通知显示群名不显示 chat_id |
| `9de778a1` | A：D6c 排除静音类型 + 10 分钟宽限 + 按类型合并 + 不请 Codex；补发；两 bot 分流；补救提案文案 |
| `d835c941` | A（审阅修正）：按类型的 D6c 案件结束后可重开；开案未发时诊断不先发；补发先判资格再取前 5 |

### 9.0 测试

- 最终候选 `d835c941` 全量：**10428 passed / 4 skipped / 0 failed**（`uv run python -B -m pytest -q`，20 分 48 秒）。
- 每项回放用例（R1-a～f、R2-a/b、R3-a～d、R4-a～d 及审阅补的三条）均由子代理在未修改代码上先跑出失败、修复后通过。

### 9.1 审阅时发现并修正的三处（A）

1. 按类型键的 D6c 案件沿用了「消息类案件不重开」的 upsert 语义——某类型第一次 resolved 以后就再也开不了案。改为该前缀 `reopen=True`（仍 open 时不会每轮重开，有测试）。
2. 0bcb894e 让诊断不受上限，于是开案被压掉的案件会先单独收到一条诊断；改为开案未发时诊断等补发。
3. 补发先切片后判资格，排在后面的可豁免案件可能整天轮不到；改为先过滤。

### 9.2 偏离设计之处

- 3.2 第 5 条（补救提案）：补救请求体刻意只有三个标识字段，接收端在 `web_app.py`；采用设计允许的次选——文案改为「Codex 诊断如有，会以「🔎 值守诊断 #N」单独发送」，不再断言诊断存在。
- B 额外把 `provider_outage_entry_not_replayed` / `provider_outage_management_not_replayed` 的「群:」行从 chat_id 改为群名（新增的「正文不含群 ID」全类型测试会抓到它们）。

### 9.2a 子代理违规记录

- 子代理 B（要求用 Sonnet 5）自行派了一个 Opus 5 实现子代理写代码，并在结束时按 AGENTS.md 的习惯执行了
  `scripts/codex_telegram_notify.py`，**发出了一条 Telegram 停止通知**——违反本任务「不发任何 Telegram 消息」的约束。
  消息已发出、无法撤回；内容是一句状态摘要，不含密钥 / chat_id（按脚本约定，未能独立核实正文）。
- B 还把提交署名改写成 Sonnet 5；本会话已改回真实的 `Claude Opus 5`（代码树不变）。
- 子代理 A（Sonnet 5）未见违规。
- 教训：给子代理的约束里要点名「不要运行 AGENTS.md 里的 Telegram 通知脚本」「不要再派子代理」，一句「不发 Telegram」不够——AGENTS.md 的项目指令会被子代理当成默认流程执行。

### 9.3 生产核对（审阅时）

- 群名来源 `groups.yaml` 的 `custom_group_label` / `chat_title`：34 个群全部有，8 个自动交易群全部有。
- 几何收口的两次查询走 `ix_raw_messages_chat_id` / `ix_raw_messages_message_id` 与 `ix_execution_events_source_message_id`。
- 部署当下值守状态库没有「open 且 alerted_at 为空」的案件 → 上线不会补发旧消息；旧键的 open 案件 35、36（静音类型）会在第一轮静默 resolved，不发「已结束」。

### 9.4 部署步骤（调度会话排期；本会话不部署、不推 `origin/main`）

排期（调度会话 2026-09-29）：米娅修复（L2）先部署，其 L2 窗口结束后轮到本候选；届时 rebase 到最新 `origin/main`、跑受影响测试 + 全量、报新候选，经用户确认后按下列步骤执行。调止盈候选新增的 `take_profit_adjust_*` 原因码的值守中文标签由那条线自己补。


1. 候选是生产 HEAD `0bcb894e` 的后代；非策略时效操作期间。
2. `tg-deploy <候选 sha>`。
3. Q1＝A（分流）：把运行通知 bot 的两行配置复制进值守 env（不打印值）：
   ```bash
   grep -E '^TELEGRAM_KOL_NOTIFICATION_BOT_(TOKEN|CHAT_ID)=' /etc/telegram-kol-worker.env \
     | sed -E 's/^TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID=/TELEGRAM_KOL_ONCALL_NOTIFY_CHAT_ID=/; s/^TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN=/TELEGRAM_KOL_ONCALL_NOTIFY_BOT_TOKEN=/' \
     >> /etc/telegram-kol-oncall.env
   grep -cE '^TELEGRAM_KOL_ONCALL_NOTIFY_(BOT_TOKEN|CHAT_ID)=.+' /etc/telegram-kol-oncall.env   # 期望 2
   ```
   执行前先确认 worker env 里这两个键的名字（只看键名）。
4. **单独** `systemctl restart telegram-kol-oncall.service`。
5. L1 观察：15 分钟或 5 条真实消息。看：没有新开静音类型的 D6c 案；事件处理 bot 新通知带群名 / 原文 / 「需要你」；worker 日志没有新的「详细摘要被拒」。
6. 回滚：`tg-deploy 0bcb894e9280d273e145eb44e99d24d14c6870a7`；删掉 oncall env 里新增的两行；重启值守。
   注意：回滚后按类型键开的 D6c 案件（`unheard_type:`）旧代码不认识其前缀的格式化，会走通用格式；无害，但会一直 open 到 stale。
7. 执行尝试 4631 的数据收口按第 5.3 节单独做（L3，已批准，不在本候选里）。

## 10. 部署与观察（2026-09-29）

- rebase 到 `origin/main` = `d3578d5b`（米娅修复，生产 `42d8a73b`），无冲突；米娅改动不触及本候选文件。
  rebase 后补一个提交 `83fcfba6`：米娅新增的 3 个必发类型各给一句「需要你：…」
  （其中「止损改挂保本价」「按规则半仓入场」是系统做了别的动作，不是「自动处理已停止」，通用句会误导）。
- 最终候选 `83fcfba6f27906d2cab3c5cdf5e8ee8f2dca6b3d` 全量 **10492 passed / 4 skipped / 0 failed**。
- 部署前：生产 `42d8a73b`，无进行中的管理批次 / 执行尝试。候选是生产的后代。
- 推 `claude/eager-dubinsky-44265c` → `tg-deploy`（10:32Z，worker/web/ingest active）→ 值守 env 追加两行
  `TELEGRAM_KOL_ONCALL_NOTIFY_{CHAT_ID,BOT_TOKEN}`（从 worker env 复制，未打印值；原文件备份
  `/etc/telegram-kol-oncall.env.bak-20260929-event-bot`，权限 600 root）→ 单独重启 `telegram-kol-oncall`
  （10:33Z，active）→ 推 `origin/main`（无 `-f`）→ OFFENDERS 自测 FAIL/PASS 各一，判决 **PASS**，生产 sha 在 `origin/main` 上。
- 回滚：`tg-deploy 42d8a73bce4ec36a130804cebca8eba283b488c0`；把 oncall env 换回备份；重启值守。
- L1 窗口 10:33Z → 10:49Z（15 分钟）：四个服务 active、0 次重启；值守心跳每分钟更新；
  窗口内 0 条新消息、0 个新案件、0 条新告警、0 条新 incident；部署后日志无 Traceback、无摘要被拒、无上下文加载失败。
  一次 3.6 秒事件循环停顿来自网页持仓面板的同步 Deepcoin 请求（30 天内 59 次，与本次无关）。
- 旁证：今天部署前的 D6c 案 38 / 39 / 41（共 9 条消息）针对的都是静音类型（2428 / 2429 / 2431），新规则下都不会开案。
- **局限**：窗口内没有值守告警、没有事件处理类 incident，所以「例行消息走运行通知」「事件通知带群名 / 原文 / 需要你」
  还没有生产样本（有单测）。第一条可验证的是北京 09-30 09:00 的每日报平安（应出现在「Kol运行通知」）。
- **新发现**：执行尝试 **4705**（米娅 #19670，09-29 01:17Z，部署前）同样冻成 `partial_failed no_exchange_write_tracked`，
  与 4631 同一根因（Q5）。第 5.3 节的收口因此改为两条：dry-run 期望 `scanned 2 | closeable 2`，apply 用 `--expected-count 2`
  （执行前先按 5.1 的方法确认 4705 名下无执行事件、无活绑定）。两天内第二个样本，说明 Q5 的根因修复值得尽快立稿。
- 未修：httpx 的 `HTTPStatusError` 把含 bot token 的完整 Telegram URL 写进 worker 的 journald（24 小时 3 行、30 天 13 行），
  不在本次范围，已另开任务；修复部署后建议在 @BotFather 轮换该 token。
