# 2026-09-29 管理指令预检被拒却冻成 uncertain · 根因与修复设计稿（待批准）

- 来源：`docs/plans/2026-09-29-event-bot-quality-design.md` Q5；两天两个生产样本（执行尝试 4631、4705）
- 基线：`origin/main` = 生产 = `83fcfba6`（文档提交之后）
- 核实方式：生产库只按主键 / 索引点查（`authoritative_execution_attempts` 主键与 status 索引、
  `message_instruction_items.raw_message_id`、`strategy_management_batches.raw_message_id`、
  `strategy_management_legs (management_batch_id, …)`、`strategy_management_components.management_batch_id`、
  `strategy_management_market_decisions.management_batch_id`、`execution_events` 经 `raw_messages (chat_id, message_id)`）；
  代码读 `execution_boundary.py`、`authoritative_recognition.py`、`authoritative_execution_attempts.py`、
  `strategy_management_executor.py`。没有交易所调用。
- 状态：**已批准（2026-09-29，用户：第 7 节全部按推荐）**；**已部署 `d244dfeb`（2026-09-29 13:15Z）**，L2 观察进行中，见第 9 节
- 风险级别：**L3**（改的是「跨过副作用边界之后的结果如何定性」，属 AGENTS.md 所说的交易所写入语义；
  **不改表结构、不修生产数据、不改任何真实下单 / 撤单代码**）

## 0. 一句话

管理批次在预检阶段就拒绝执行、一个字节都没发到交易所，但执行边界只看到条目载荷里一个
`ManagementBatchExecutionError`，证明不了「没接触交易所」，于是按最保守的方式冻结成「结果未知」。
**管理批次自己的账本其实能在结构上证明这一点**，只是边界从来不看它。本稿让执行边界读批次账本，
能证明没写过的，记成真实的终态；证明不了的，照旧冻结。

## 1. 生产事实

### 1.1 两个样本

| 尝试 | 消息 | 条目错误 | 批次 | 批次腿 | 组件 / 市价决策 | 执行事件 | 绑定 |
|---|---|---|---|---|---|---|---|
| 4631 | 陈哥 #19598（保本） | `ManagementBatchExecutionError: protection_rows_unattributed_on_exchange:…` | 184，blocked（先 recovery_required，60 分钟后超时） | 158、159 均 `planned`，无 client_order_id、无请求 / 响应 | 0 / 0 | 0 | 0 |
| 4705 | #19670（「82500 附近可以止盈 30%」） | `ManagementBatchExecutionError: management_stop_provenance_invalid` | 187，blocked | 163 `planned`，同上 | 0 / 0 | 0 | 0 |

两条的 `error_summary` 都是 `partial_failed no_exchange_write_tracked`，`evidence_refs_json=[]`。
两条已于 2026-09-29 10:58Z 用 `close-out-uncertain-attempts` 收口（见事件处理 bot 设计稿第 11 节）。

### 1.2 历史规模

- 已收口的 39 条 uncertain 里，**5 条**是同一种情况（09-23 → 09-29）：`management_stop_provenance_invalid` ×3、
  `protection_rows_unattributed_on_exchange` ×2。其余是 09-26 之前的老问题（26 条管理条目无错误载荷、
  入场类若干、1 条 `IntegrityError`），与本稿无关。
- 按现在的频率大约**每 1–2 天一条**。每一条都会：冻住消息、发两条（09-29 起一条）uncertain incident、
  worker 每 30 分钟打一行 WARNING、需要人工 L3 收口。

### 1.3 现有的 `failed_safe` 对管理消息是什么样

A-6 已经让「每个条目载荷都写着 `status: blocked` 等」的管理拒绝落在 `failed_safe`：生产上有 **7 条管理消息**是这样结束的
（09-13 → 09-22）。其中只有 #18375 之后又跑了一次（2 分钟内，消息处理任务自动重试，`attempt_count=5`），结果仍是 `failed_safe`。
全表 16 条「`failed_safe` 之后还有新尝试」的，间隔都在 **1 秒以内到 3 分多钟**之间，都是同一消息处理任务的自动重跑，没有一条是几小时后重放。

## 2. 根因

### 2.1 边界怎么判的

`execution_boundary.build_execution_boundary_outcome`：

1. 追踪器 `tracker.writes` 为空（没追踪到任何交易所写入）；
2. 消息级状态 `partial_failed` 属于 `_KNOWN_UNKNOWN_STATUSES` → 先定为 `outcome_unknown`；
3. A-6 的补救 `_items_prove_no_exchange_contact` 要求**每个条目**的载荷自证没接触交易所：
   载荷 `status` 在 `{blocked, deferred, skipped, shadow_planned, new_thread_required, failed}` 里，
   或 `reason/message` **精确等于** A-6c 的 6 个「拿执行权失败」原因之一；
4. 这里的条目载荷是 `{"type": "ManagementBatchExecutionError", "message": "protection_rows_unattributed_on_exchange:<posId>:<orderIds>"}`，
   没有 `status`，`message` 也不在词表里（还带后缀）→ 证明失败 → 维持 `outcome_unknown` → `mark_authoritative_execution_uncertain`。

### 2.2 为什么不能「把这两个原因码加进词表」了事

`strategy_management_executor.py` 里有 **118 处** `raise ManagementBatchExecutionError`，有的在预检里，
有的在已经预留了腿（`planned → reserved`，写入 client_order_id 与请求）之后、甚至在交易所返回之后。
同一个异常类型、同一种载荷形状，前后两种情况都有。按原因码逐条判定「这一处在写入之前」既脆弱又会随代码漂移：
下一个新加的 `raise` 没人记得归类，要么漏报（冻结），要么更糟，误判成没写过。

### 2.3 批次账本本来就是写前日志

执行器对每一种交易所写入都**先落账、再发请求**：

- 平仓腿：`transition_leg(planned → reserved, client_order_id=…, request=…)` 提交之后才调 `close_exact_position`（约 848 行）；
- 触发单救援：`rescue.status="reserved"`、`request_json` 提交之后才调 `submit_exact_position_sltp`（约 364 行）；
- 组件（`strategy_management_components`）带自己的 `status` / `attempt_count` / `evidence_json`；
- 保本市价决策先写 `strategy_management_market_decisions`。

所以「这个批次的腿全是 `planned` 且没有 client_order_id / 请求，没有组件越过初始状态，没有救援行，
没有市价决策，名下没有执行事件」是一个**结构性的**「没发过请求」证明，不依赖原因码。
4631、4705 都满足；而任何真正发过请求的批次都不满足（腿至少是 `reserved`）。

## 3. 修法

### 3.1 新的无接触证明（`execution_boundary`）

在 A-6 的条目证明失败之后、定性 `outcome_unknown` 之前，加第二条证明 `_management_batches_prove_no_exchange_contact`，
**全部满足**才成立：

1. 追踪器 `tracker.writes` 为空（原有条件，保持）；
2. 该消息的每个条目都是 `instruction_kind='management'`、条目 `status='failed'`、错误类型 `ManagementBatchExecutionError`；
   有任何一个入场条目、未完成条目、别的错误类型（如 `IntegrityError`、`RecoveryLiveSubmitError`）→ 不成立；
3. 该消息名下（`strategy_management_batches.raw_message_id`）每个批次：
   - 所有腿 `status='planned'`，`client_order_id`、`exchange_order_id`、`request_json`、`response_json` 都为空；
   - 没有组件，或所有组件仍是初始状态 `pending` 且 `attempt_count=0`、`evidence_json` 为空列表；
   - 没有 `strategy_management_market_decisions` 行；
   - 批次腿对应的 `execution_order_leg_id` / `pos_id` 上，没有在本次尝试 `side_effect_started_at` 之后创建、且已越过 `ready` 的
     `trigger_protection_stop_rescues` 行（救援表不按批次号关联，只能按腿 / 仓位 + 时间窗找；实施时确认管理批次路径是否会触发救援，
     若确认不会，这一条改为在审计结论里写明并保留为防御性检查）；
4. 该消息名下（经 `raw_messages` 的 `(chat_id, message_id)`）没有任何 `execution_events`。

任何一条读不到（异常、行缺失）→ 不成立，照旧冻结。证明作为 evidence_refs 记下来（每个批次一条：批次号、腿数、
「全部 planned、无请求」），这样事后的人能读到为什么判成没写过。

### 3.2 证明成立以后记成什么（**需要拍板，Q1**）

- **方案 B（推荐）：记成不可自动重试的终态。** 尝试行 → `closed_no_write`（09-26 引入的终态，表约束里已有，
  **不用改表结构**），`error_class='ManagementPreflightRefusal'`，`error_summary` 写拒绝原因码 +
  `refused_before_write`；决定行 → `comparison_status='completed'`、`automation_status='failed'`、
  `automation_reason=<拒绝原因码>`。`closed_no_write` 在 `RETRY_BLOCKING_ATTEMPT_STATUSES` 里，
  所以**任务自动重试、上下文重分析都会被挡住**，重试行为和今天（冻结、永不重试）完全一样；
  区别只是账本说真话、不再有 uncertain 告警和每 30 分钟的日志、不再需要人工 L3 收口。
  人手动点「重新识别」仍然可以发起新一代识别——那是人的决定。
- 方案 A：记成 A-6 的 `failed_safe`。与生产上已有 7 条管理 `failed_safe` 同一语义，但会让这类消息**变成可自动重试**：
  任务几分钟内的自动重跑会再识别一次（花一次 AI 调用）、再规划一次。按生产记录，重试都在 3 分钟内、结果都是再次被拒，
  但若拒绝条件在这几分钟里消失（例如 0bcb894e 修掉的那种保护不明确），保本 / 减仓会晚几分钟**真的执行**。
  这正是「交易所写入语义」的变化，也是推荐 B 的原因：本稿要修的是「标错了」，不是「要不要重试」。

`closed_no_write` 目前的约定是「只有审计过的运维工具会写」（`authoritative_execution_attempts.py` 注释）。方案 B 要把这句改成
「运维工具，或执行边界在批次账本证明无写入时」，并在 `error_class` 上区分两者，事后可分辨。

### 3.3 告警（**需要拍板，Q2**）

证明成立时不再写 `uncertain_without_write`（它的定义就是「矛盾，值得报警」，而这里没有矛盾）。
「这条管理指令没执行」这件事今天已由别的渠道告诉人：值守 D1/D2（自动交易群、有仓位）+ 补救提案，
以及各拒绝原因自己的 incident（如米娅修复加的 `management_partial_take_profit_future_level_blocked`）。

- 推荐：**新增一个事件处理类型 `management_refused_before_write`**（high，必发，留事件处理），
  正文带群名 / 原文 / 仓位（沿用 83fcfba6 的上下文），「需要你：这条管理指令在下单前被拒、没有执行；请决定是否手动处理」。
  理由：值守 D1/D2 只覆盖有仓位的群，拒绝原因各自的 incident 不齐全；一个统一的「没执行」出口最不容易漏。
  同一消息若已有值守案件 / 补救提案，靠 83fcfba6 的「关联」行串起来。
- 备选：不加新类型，完全依赖现有渠道（少一条消息，但覆盖面取决于每个拒绝原因是否各自报警）。

### 3.4 不做的

- 不改任何 `raise ManagementBatchExecutionError` 的位置或文案；不改批次 / 腿 / 组件的状态机；
- 不碰入场类条目（入场的 uncertain 另有 A-6c 规则）；
- 不改 `_KNOWN_UNKNOWN_STATUSES`；消息级 `partial_failed` 仍默认冻结，只有 3.1 的证明能解除；
- 不回溯修改已收口的 39 条。

## 4. 实施要求（给实施者）

1. **先审计写前日志这条前提**，这是整个证明的地基：列出 `strategy_management_executor.py`（及它调用的
   `close_exact_position`、`submit_exact_position_sltp`、撤单 / 改 TPSL 等）里**每一个**交易所写方法的调用点，
   逐个写明它之前提交了哪一行账本（腿 reserved / 组件状态 / 救援 reserved / 市价决策）。**如果发现任何一个写调用之前没有落账，
   停下来报告**——那说明 3.1 的证明对它不成立，要先补写前日志或把那一类排除在证明之外。
2. 加一条守卫测试：用一个假的 Deepcoin 客户端，让每个写方法一被调用就抛异常，驱动执行器走到每条写路径，
   断言抛异常那一刻账本里已有对应的非 `planned` 记录。以后谁加了一条先发请求后落账的路径，这条测试会失败。
3. 3.1 的所有查询走现有索引（`strategy_management_legs (management_batch_id, status)`、
   `strategy_management_components.management_batch_id`、`strategy_management_market_decisions.management_batch_id`、
   `ix_execution_events_*`），在边界所在的同一 session 里读。
4. 子代理提示词要写明：不运行 `scripts/codex_telegram_notify.py`，不再派子代理，署名写真实模型。

## 5. 回放用例（修复前失败 / 修复后通过）

- R5-a：4631 的完整形状（条目 1426 的错误载荷、批次 184、腿 158/159 planned、无组件 / 救援 / 市价决策 / 执行事件，
  消息级 `partial_failed`、追踪器 0 写入）→ 修复前 `outcome_unknown` / 尝试 `uncertain`；
  修复后尝试 `closed_no_write`（方案 B）、决定行 `completed/failed`、`automation_reason=protection_rows_unattributed_on_exchange`，
  evidence 含批次 184 的证明，不写 `uncertain_without_write`（按 Q2 写或不写新类型）。
- R5-b：4705 形状（批次 187、腿 163、`management_stop_provenance_invalid`）→ 同上。
- R5-c（必须仍冻结）：同 R5-a，但一条腿是 `reserved` 且有 client_order_id → `uncertain`。
- R5-d（必须仍冻结）：有一个组件 `attempt_count=1` → `uncertain`。
- R5-e（必须仍冻结）：有一条尝试开始后创建并已越过 `ready` 的救援行 / 一条市价决策 / 一条执行事件 → 各自 `uncertain`。
- R5-f（必须仍冻结）：条目错误是 `IntegrityError` → `uncertain`；混有入场条目 → `uncertain`。
- R5-g：证明中途读库抛异常 → `uncertain`（fail-closed）。
- R5-h：方案 B 下，自动重试（任务 `attempt_count>0`）与上下文重分析（`explicitly_retrying=True`）都被 `AutomaticRetryBlocked` 挡住。
- R5-i：第 4 节第 2 条的写前日志守卫测试。

## 6. 风险、验证与部署

- **级别 L3**：改变的是边界对「跨过副作用边界之后」的定性。它的失败方向是关键：
  误判「没写过」会让一条真实发出过请求的消息失去冻结保护。对策是证明只用**结构性**证据（写前日志），
  并用第 4 节的审计 + 守卫测试钉住前提；任何读不到都回落到冻结。方案 B 下即使误判，消息也**不会被自动重试**，
  最坏结果是账本把一条「结果未知」标成了「没写过」——和今天人工 L3 收口时的判断同一依据，只是提前自动做。
- 不改表结构、不修生产数据、不碰交易所写路径、不碰自动交易开关。
- 验证：聚焦测试 + 最终候选全量；部署后 L2 观察窗口（30 分钟、≥5 条消息）；另外记录上线后第一个管理预检拒绝样本，
  核对它落在 `closed_no_write` 且账本证明与事实一致（若 7 天内没有样本，记为待验证，不算失败）。
- 回滚：`tg-deploy <上线前 sha>`。已按新规则收口的行保持 `closed_no_write`，旧代码认识这个状态（09-26 起），无需数据回滚。

## 7. 需要用户拍板

- **Q1 证明成立后记成什么**：推荐 **B**（`closed_no_write`，不可自动重试，行为与今天的冻结一致，只是账本说真话、不再告警 / 收口）；
  A（`failed_safe`，可自动重试，拒绝条件几分钟内消失时指令会晚几分钟真的执行）。
- **Q2 告警**：推荐**新增 `management_refused_before_write`**（事件处理、必发、带上下文和「需要你」）；备选不加，依赖现有渠道。
- **Q3 部署级别**：推荐按 L3 评审、L2 观察（30 分钟 ≥5 条消息），不另做数据演练（无结构改动、无数据修复）。
- **Q4 写前日志审计若发现例外**：推荐「把那一类写路径排除在证明之外、照旧冻结」，而不是在本稿里顺手改执行器；
  改执行器另立一稿。

### 7.1 裁定（2026-09-29）

Q1＝B（`closed_no_write`，不可自动重试）；Q2 新增 `management_refused_before_write`；Q3 L3 评审 + L2 观察；Q4 审计发现例外就排除在证明之外、照旧冻结。

## 8. 实施记录（2026-09-29）

| 提交 | 内容 |
|---|---|
| `e8cf42d9` | 子代理（Sonnet 5）：写前日志审计（`docs/management-preflight-refusal-status.md`）、`management_batches_prove_no_exchange_contact`、`record_management_preflight_refusal`（`closed_no_write` / `ManagementPreflightRefusal` / `exchange_effect=not_started`，决定行 completed/failed）、新类型 `management_refused_before_write`（必发、留事件处理、「需要你」一句）、R5-a～i |
| `ca17518c` | 本会话审阅修正：账本证明只在执行边界**没追踪到任何写入**时才运行；更正调用点注释（Q1=B 不可重试）；两条端到端测试 |

### 8.1 审阅发现

- **账本不是完整的见证。** 子代理审计发现 `_cancel_deferred_entry_legs` 先撤单后落账，并论证「成功 / 异常两种结局都会补执行事件」。
  漏了第三种：撤单成功后快照复核失败，直接抛 `deferred_entry_cancel_leg_not_pending`，**不写任何事件**——交易所撤过单，账本却像没动过。
  而原调用点只看 `exchange_effect == "outcome_unknown"`，写过交易所的矛盾结果也会走到这里。
  修正：`boundary.evidence_refs` 里有任何 `deepcoin_write` 就直接冻结、不查账本。整条自动执行链路用 `TrackedDeepcoinClient`，
  它拦截包括 `cancel_order` / `cancel_trigger_order` 在内的全部写方法。回放 `test_ledger_proof_is_never_consulted_after_a_tracked_write`
  在修正前失败、修正后通过。4631 / 4705 的 `evidence_refs_json` 都是 `[]`，修正后仍会被正确收口。
- 子代理本次遵守了三条禁令（未运行通知脚本、未派子代理、署名为 Sonnet 5）。

### 8.2 rebase 与测试

- rebase 到 `origin/main` = `5ba66e8a`（调止盈上线）无冲突。调止盈改了 `strategy_management_executor.py` 并新增
  `take_profit_adjustment_executor` 等：同步执行链路里它用的是传入的被追踪客户端，写入会被追踪器记录；
  worker 里 `get_client()` 那条是批次之后的异步执行，不属于执行尝试。
- 受影响测试 204 passed；最终候选 `ca17518c69b469e51629c1823a3c91915ce89b89` 全量 **10649 passed / 4 skipped / 0 failed**。

### 8.3 部署（调度会话排期；本会话不部署、不推 `origin/main`）

1. 候选是生产 HEAD 的后代；非策略时效操作期间；无进行中的管理批次 / 执行尝试。
2. `tg-deploy <候选 sha>`。值守代码未改，**不需要**重启 `telegram-kol-oncall`。
3. L2 观察：连续 30 分钟、≥5 条真实消息；看 `authoritative_execution_attempts` 没有新的 `uncertain`、无新的 `closed_no_write`
   之外的异常、worker 无 Traceback。上线后第一个管理预检拒绝样本出现时，核对它落在 `closed_no_write`（`error_class=ManagementPreflightRefusal`）、
   evidence 里批次证明与账本一致、事件处理 bot 收到一条 `management_refused_before_write`；7 天内没有样本记为待验证。
4. 回滚：`tg-deploy <上线前 sha>`。已按新规则收口的行保持 `closed_no_write`，旧代码认识这个状态，无需数据回滚。

## 9. 部署与观察（2026-09-29）

- rebase 到 `origin/main` = `bd49850b`（token 脱敏修复，生产 `4d342bbe`），无冲突；token 修复与本候选都改了
  `system_operator_bot.py`，受影响测试 192 passed。代码候选 `870d1795`，部署 sha `d244dfeb53de0b5dd3f050ab237cb00aad430270`
  （其上只多一个文档提交）；全量 **10663 passed / 4 skipped / 0 failed**。
- 部署前：生产 `4d342bbe`；进行中的管理批次 0、执行中的尝试 0、uncertain 0（`closed_no_write` 37、`failed_safe` 34）。
- 推 `claude/eager-dubinsky-44265c` → `tg-deploy`（13:15Z，worker/web/ingest active）。值守加载的模块未改，**未重启值守**。
  → 推 `origin/main`（无 `-f`）→ OFFENDERS 自测 FAIL/PASS 各一，判决 **PASS**；服务器 `git ls-remote` 核对远端 main 与分支均为 `d244dfeb`。
- 回滚：`tg-deploy 4d342bbe138718b28a8a93e0f81587f32db4e08c`（无需重启值守、无需数据回滚）。
- L2 观察：只读监视 `/root/observe-q5-preflight.sh`（`systemd-run` 单元 `observe-q5-preflight`），13:17:48Z 起，
  证据 `/var/lib/telegram-kol-evidence/20260929-q5-preflight/`。基线：raw 19790、attempt 4830、incident 2434。
  不健康即重置窗口：服务 / HEAD、部署后新增 uncertain、`management_refused_before_write` 10 分钟未送达、submit_unknown、critical。
