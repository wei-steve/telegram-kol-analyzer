# MiMo Provider Reliability Status（识别模型供应商故障的检测、告警与恢复）

2026-09-12 MiMo 余额耗尽、识别静默停摆 14 小时 42 分钟（A 线 step-18 只读排查）的修复项目。
本文件是跨会话唯一的进度真相。事实基础见 `docs/management-reliability-status.md` 的 step-18 条目，
本文件"事实基础复核"一节记录本项目开工时对它的独立复核结果。

```yaml
project: mimo-provider-reliability
brain_session_id: local_858790fe-37cd-426c-a0eb-cbf304066815   # 指挥会话，每步完成后 send_message 到这里
integration_branch: codex/deepcoin-auto-trading-v1
deploy: tg-deploy <sha>（AGENTS.md 部署一节，四步）
worktree_pattern: .worktrees/mimo-step-N
current_step: 3
step_status: in_progress        # planned | claimed | in_progress | completed | blocked
claimed_by: (本执行会话，见证据区首条)
production_head_at_start: 0ed2d488aa5843187fa2e1e11ef9d986af7c648b
base_commit: 8046208c277efd06d045dc2e73d6caa729e8f485   # origin 共享分支尖端，相对生产只多文档
step_1_deployed: cf0a0e1400501236388a88a957e23b6610711526   # 2026-09-12T23:50Z，回滚参考 0ed2d488；L1 窗 2026-09-13T00:31:33Z 达标
step_2_deployed: 0b2a4eb2bdb9d1723c12649f82df1c0e933890db   # 2026-09-13T00:32Z，回滚参考 cf0a0e14；L1 窗 2026-09-13T00:50:01Z 达标
step_3_candidate: e13ccd3a   # 全量 8614 passed / 0 failed、变异 20/20；等指挥会话确认后部署
step_5_completed: 2026-09-13   # 离线重放 L0，OVERALL PASS（修后）
```

## 步骤总览

| 步 | 名称 | 风险 | 状态 |
|---|---|---|---|
| 1 | 供应商错误分类：402/401/403/429/5xx/超时/网络 → `mimo_provider_unavailable`（首条即发、每 30 分钟一条、恢复通知），与请求内容错误分开；每次失败留一行带错误码的日志 | L1（新增告警，不改权威与交易） | **completed**：部署 `cf0a0e14`，L1 窗 00:31:33Z 达标（上线后一次真实误报，由第 3 步规则 A 修正） |
| 2 | `ALERTED_REASONS` 遍历式守卫：凡"权威判定未产生"的 reason 必在告警集合；`mimo_authoritative_failed` 进 auto_trade 群告警 | L1 | **completed**：部署 `0b2a4eb2`，L1 窗 00:50:01Z 达标（16 采样、0 重置；窗内无真实失败样本，照实记） |
| 3 | 补救窗口与供应商状态解耦；恢复后按序重放 auto_trade 群消息，管理类先核目标仓位；逐条记录并通知；规则 A（孤立失败不算故障）；请求总时长上限 | L2（恢复路径） | 候选 `e13ccd3a`（含第 5 步暴露的规则 A 对称修正）：全量 8614 passed / 0 failed、变异 20/20；第 2 步已收窗，等指挥会话确认新候选后部署；入场按裁定 (b) 一律不重放 |
| 4 | 主动巡检：连续同码失败计数告警；每日 `max_tokens=1` 探测（不进业务表）；余额接口（如有） | L1 | planned |
| 5 | 用 step-18 的 494 次失败离线重放，验证 1–3 的判定与限流 | L0（离线） | **completed**：修前 3 FAIL（暴露规则 A 对称缺陷），修后 OVERALL PASS；证据 `/root/evidence/mimo-step5-replay/` |

## 事实基础复核（2026-09-12，只读，生产库 + journal）

- **494 的出处是 `ai_prompt_invocations`**：窗口 02:50–18:10Z，`feature='message_recognition' AND status='failed'`
  逐小时相加 71+72+35+10+10+15+10+10+9+79+74+40+21+28+10 = **494**，全部错误文本以
  `MiMo failed after 2 attempts: attempt 1: Client error '402 Payment Required'` 开头。
  **每行是一次带内部重试（`MIMO_AUTHORITATIVE_MAX_ATTEMPTS=2`）的调用**，所以实际 HTTP 请求约为两倍。
- `mimo_recognition_runs` 同窗口失败 **496** 个、`mimo_recognition_attempts` `http_error` **496** 行。
  **差的 2 条已查明**：raw 16346、16413 的 `final_error_message` 是 `message has no readable text or image`
  ——空输入提前返回，不写调用记录，**与供应商无关**。所以"402 失败 = 494 次调用"成立，496 不是另一个口径的 402 数。
- 受影响消息：失败 run 覆盖 **116 个不同 raw_message_id**（与 step-18 的 116 条一致）。
- **journal 里没有任何 MiMo 失败的日志行**：三个 unit 同窗口 `grep -ci mimo` / `xiaomimimo` / `payment` 全为 0
  （worker 22253 行）。**这个 0 是"失败不写日志"，不是"没有失败"**——同窗口数据库有 494 行。
  推论：本项目的任何观察判据**不得从 journal 数 MiMo 失败**，只能从上面两张表数。
- 当前 `mimo_recognition_attempts.error_code` 对 v1 失败一律写 `v1_authoritative_failed`（src 与 tests 无消费者），
  **402 与"请求内容错误"在落库层面同形**——与 step-18 第 (6) 条一致。

## 设计要点（第 1 步）

- **分类只看结构化信号，不看文本**：在 `_call_mimo_direct_model` 捕获 `httpx.HTTPStatusError`（取 `status_code`）、
  `httpx.TimeoutException`、`httpx.TransportError` 时，把分类写进该次尝试的遥测；v1 尝试行的 `error_code`
  由 `v1_authoritative_failed` 细分为 `mimo_provider_unavailable.<kind>`（仅当该次调用的**全部**请求都是供应商不可用类）。
- **状态不新增表**：供应商"事故期"由 `mimo_recognition_attempts`（append-only、已有 `(status, created_at)` 索引）推导：
  从最新一行往回数，连续的 `mimo_provider_unavailable.*` 行即当前事故期。
- **每 30 分钟一条靠指纹**：事故摘要只放稳定字段（原因、事故期起点、30 分钟桶序号），同桶内重复出现只增
  `repeat_count`（`record_runtime_incident` 按指纹合并），跨桶产生新行、触发一次投递。
- **没有消息时也要按时说话**：检查挂在 worker 的 `authoritative_gap_recovery_loop` 每轮（20s）上，不依赖新消息到达。
- **恢复通知**：事故期之后出现一次"供应商回答了"的尝试（完成、或非供应商类失败），且该事故期发过告警 →
  `mimo_provider_recovered`，以事故期起点为键合并，只发一次。

## 裁定记录

- **2026-09-12，指挥会话（第 3 步重放策略 + 第 1 步追加项）**：
  - **入场**：默认按 (b)——**一律不重放**，逐条通知"供应商故障期间的入场未执行，需人工判断"，附原文、群、时间、当时价格区间。
    (a)（有效年龄 ≤15 分钟且墙钟 ≤60 分钟才重放入场）是新的交易语义规则，由指挥会话请示用户；**用户批准后才把入场分支从"只通知"改为 (a)**。
    请示依据：入场执行链上 grep 未找到基于消息时间的信号年龄检查（`recovery_live_submit.py` 不引用 `posted_at`；`recovery_scan.py` 只用于时间窗匹配）。
  - **管理**：有效年龄（扣除供应商不可用时长）≤15 分钟**且**目标仓位仍在交易所 → 自动重放；否则不自动执行，进 `awaiting_user_confirmation`，
    发 `management_target_needs_confirmation`（附 /choose /dismiss），由用户决定。
  - **追加到第 1 步**：MiMo 失败必须在 journal 留一行带错误码的日志（09-12 的 494 次失败日志 0 行）。
- **2026-09-13，指挥会话（第 1 步上线后的首次真实告警是误报）**：
  - **规则 A**：失败请求执行期间有其他调用成功，就不算故障期；随第 3 步实现，用两条用例锁住（真实行序重放不发告警 + 连续无成功照发）。
  - **请求总时长上限**：httpx 超时改为总时长上限，僵尸请求能在限定时间内结束；随第 3 步实现。
  - **第 1 步观察窗**：按 (a) 记"判据未达成——真实事件，已逐行解释"，从 00:07Z 以相同判据重起 15 分钟窗，达标后部署第 2 步。
  - 故障开始时间取最早一行、中文原文清洗两处修正随第 3 步上线。
  - 其后指挥会话接受流式读时限与 240 秒取值，并要求把"超时那次不重试、最坏 300 秒不超过认领过期"写进本文件（见第 3 步证据）。

## 第 1 步 L1 观察窗判据（起窗前写定，收窗照此判，不放宽）

窗口：部署后连续 15 分钟；消息数是外界分母，只记录。服务器端观察器每 60 秒采样，pidfile + `WINDOW_MET`/`WINDOW_NOT_MET` 标记文件，
journal 一律 `--since "@<epoch>"`。读不到的量记 `-`，**`-` 不是通过值**。任一样本不满足即重置窗口，并在采样行写明原因。

完成条件（**每个样本**都要满足）：
1. `head_ok == 1`：实时读生产 HEAD，等于本步部署 sha。
2. `units_ok == 1`：worker / web / ingest 均 active。
3. **哨兵**：部署后 worker journal 中 `mimo provider health tick state=` 行数 `hb >= 1`，且首条 `rows_read > 0`
   （证明 journal 可读、健康检查在生产上跑、读到了真实行）。
4. `tick_failed == 0`：部署后 `mimo provider health tick failed` 行数。
5. `alerts == 0`：部署后 `runtime_incidents` 中本步三类类型（`mimo_provider_unavailable` / `_recovered` / `_health_check_failed`）新增行数。
   **分母限定本步三类类型**，不数全局事故表。供应商当前健康，应为 0；非 0 须逐行解释。
6. `bad_codes == 0`：部署后 v1 失败尝试行的 `error_code` 必须属于
   {`mimo_provider_unavailable.*`, `mimo_request_rejected.*`, `mimo_response_invalid`, `v1_authoritative_failed`}。

记录项（不作门槛）：部署后 v1 尝试总数与完成数；`mimo authoritative call failed` 日志行数；
A 线基线 `status='retired'` 的保护行数（A 线给的 664 = 账本 208 + 腿 456，**起窗前已用这个已知值核对查询**；变了不是本步造成，要告知 A 线）。

能证明 / 证不了：
- **能证明**：健康检查在生产 worker 上每轮在跑且读到真实行（3）；健康时不误报（5）；新错误码写得对（6）；未换版（1）。
- **证不了**：真实故障时的首条告警、30 分钟提醒、恢复通知——生产上没有故障可观察。这一半由测试与变异、第 5 步离线重放、第 4 步探测承担。
  **"窗口达标"不是"告警在真实故障下有效"的证据。**
- **09-12 那 496 行失败是旧码 `v1_authoritative_failed`，推导时属于中性行，部署后不会误报"不可用"**：
  03:00:53Z 之后生产尝试表里一条完成行都没有，若旧行被当成"不可用"，部署的第一个 tick 就会误发告警，判据 5 会抓住。

## 第 2 步 L1 观察窗判据（起窗前写定，收窗照此判，不放宽）

窗口：部署后连续 15 分钟；消息数是外界分母，只记录。每 60 秒采样，pidfile + 标记文件。
**起算点用 worker 进程的实际启动时刻**（`当前 epoch − ps -o etimes`），并每个样本核对 worker MainPID 未变——
第 1 步窗口两次栽在起算点上（见第 1 步证据），这里直接用已验证的写法。读不到的量记 `-`，`-` 不是通过值；任一样本不满足即重置并写明原因。
观察器 `/root/evidence/mimo_step2_observe.sh`（sha256 前 16 位 `4bfbf49fcb23da10`，服务器 `bash -n` PASS），**起窗前需按上述起算点改参数**。

完成条件（每个样本）：
1. `head_ok == 1`（实时读生产 HEAD = 本步部署 sha）；`units_ok == 1`；worker MainPID 未变。
2. **哨兵**：起算点后 worker journal 中第 1 步心跳行 `mimo provider health tick state=` 计数 `hb >= 1`，首条 `rows_read > 0`。
3. `alert_failed == 0`：`expired recovery gap alert failed` 行数（本步新增的吞异常路径，不允许静默失败）。
4. `worker_failed == 0`：`message processing worker task failed` 行数（本步改了 worker 启动参数）。
5. `alerts_outside_auto_trade == 0`：本步两类 reason 的 `authoritative_recognition_failed` 告警里，chat 不属于 auto_trade 群的行数。
   chat_id 用 `json_extract` 取，**取不到的按群外计**（`COALESCE(..., 0)`，最小回退摘要没有 chat_id；服务器 sqlite 已验证）。

记录项（不作门槛，分母限定本步两类 reason）：`new_step2_alerts`、`new_failed_decisions`、`new_expired_decisions`。

能证明 / 证不了：
- **能证明**：worker 正常启动并消费（4）、第 1 步健康检查仍在跑（2）、新告警路径未吞异常（3）、没有向非 auto_trade 群发告警（5）、未换版（1）。
- **证不了**：一次真实识别失败或过期在 auto_trade 群产生告警——取决于外界，大概率"本窗无样本"，照实记。
  这一半由遍历式守卫 + 行为用例（auto_trade 产生、notify_only 不产生）+ 7 项变异 + 第 5 步离线重放承担。

## 第 3 步 L2 观察窗判据（起窗前写定，收窗照此判，不放宽）

风险级别 L2（恢复路径：过期判定改用有效年龄、恢复后重放、执行关口）。窗口：连续 30 分钟，封顶 24 小时。
**起算点 = worker 进程实际启动时刻（当前 epoch − `ps -o etimes`），每个样本核对 worker MainPID 未变。**
每 60 秒采样，pidfile + 标记文件；读不到记 `-`，`-` 不是通过值；任一样本不满足即重置并写明原因。
观察器 `/root/evidence/mimo_step3_observe.sh`（sha256 前 16 位 `29d89cdf36480c70`，服务器 `bash -n` PASS）；
**所读生产表与列起窗前已用 `pragma_table_info` 核实全部存在**（`execution_events.action/created_at`、`raw_messages.created_at`、
`message_processing_jobs.status/last_reason/completed_at`、`runtime_incidents.incident_type/created_at`）——写错一个就会每轮读成 `-`、窗口永远收不了。

完成条件（每个样本）：
1. `head_ok == 1`、`units_ok == 1`、`pid_ok == 1`。
2. **哨兵（journal 可读 + 两个检查都在跑）**：起算点后 worker journal 中 `mimo provider health tick state=` 行数 `>= 1` 且首条 `rows_read > 0`；
   `provider outage replay tick state=` 行数 `>= 1`（首轮状态从无到有，必记一行）。
3. `health_failed == 0`、`replay_failed == 0`、`replay_enqueued_lines == 0`。
4. `outage_incidents == 0`：起算点后 `incident_type LIKE 'provider_outage_%' OR LIKE 'mimo_provider_%'` 的新增行数。
   **当前供应商健康，任何一行都是异常**，不是"样本"。按规则 A，09-13 00:02 那次僵尸请求不再构成故障期。
5. `replay_events == 0`（`execution_events.action='provider_outage_replay'`）且 `replay_jobs == 0`（`last_reason='provider_outage_replay'` 的作业）——没有故障就不该有重放。
6. `alert_failed == 0`、`worker_failed == 0`（沿用第 2 步）。

记录项（不作门槛）：起算点后真实消息数（外界分母）；`status='expired'` 的作业数（有效年龄只对被故障耽误过的消息生效，普通消息过期行为不应变化）；
A 线基线 `status='retired'` 保护行数（**只作记录，口径见"执行教训"**）。

能证明 / 证不了：
- **能证明**：两个检查每轮在生产上运行且不抛异常（2、3）；没有故障时不产生任何重放、通知或执行事件——即"不误触发"（4、5）；
  worker 在 30 分钟内正常消费、未重启（1、6）；普通消息的过期行为未见异常（记录项）。
- **证不了**：真实故障后的重放、入场拒绝、管理放行与转人工——生产上没有故障可观察，**本窗必然无样本，照实记**。
  这一半由行为用例（每个关口同一夹具两种结局）+ 18 项变异 + 09-13 真实行序重放 + 第 5 步离线重放承担。
- **消息数不是门槛**：它归外界管，放进完成条件等于让窗口等群里有人说话。

## 证据记录

格式：`- step-N (日期, 会话): 提交 SHA；做了什么；验证结果；遗留问题`。

- step-1（2026-09-12，本执行会话）：分支 `mimo/step-1-provider-unavailable`（工作树 `.worktrees/mimo-step-1`）。
  提交：`6f8c38fd` 分类 + 告警 + 恢复；`e9d4dc98` 健康时心跳行带 `rows_read`，推导拆为纯函数；`ba7521aa` 两个全量才暴露的缺陷；
  `af5881dd1c98e81c298fbb2addc2a661ebbfea0d` 每次失败留一行带错误码的日志（按裁定追加）。
  **做了什么**：
  1. `mimo_provider_health.classify_provider_failure` 只看异常链：`HTTPStatusError` 取状态码（402 余额不足 / 401·403 鉴权 / 429 限流 / 5xx 服务端），
     `httpx.TimeoutException` 与 `TimeoutError` 算超时，`httpx.TransportError` 算网络错误；其余 4xx 算"请求被拒"，2xx 但校验失败算"响应无效"。
     **文本里写着 402 的字符串不算证据**（有用例）。
  2. v1 重试循环逐次请求写分类；尝试行与 run 的错误码由 `v1_authoritative_failed` 细分为
     `mimo_provider_unavailable.<kind>.http_<status>` / `mimo_request_rejected.http_<status>` / `mimo_response_invalid`。
     说不出原因的失败（空输入、图片不可读、请求发出前失败）**保留**旧码，推导时算中性行。
  3. worker 的 `authoritative_gap_recovery_loop` 每轮（20 秒）调 `run_mimo_provider_health_tick`，从 `mimo_recognition_attempts` 推导最近一次故障期：
     未恢复时每 30 分钟桶一条 `mimo_provider_unavailable`，以 `source_record_id=outage_<epoch>_b<n>` 去重，重启不重发也不丢；
     出现"供应商回答了"的行时发一次 `mimo_provider_recovered`；**从未告警过的故障期不发恢复通知**。
     健康检查自身连续失败第 3 次起记 `mimo_provider_health_check_failed`。三类都进 `ALWAYS_NOTIFIED_INCIDENT_TYPES`，中文专用文案直写原因。
  4. 健康时第一次检查、状态变化时、约每 30 分钟各打一行心跳，带 `rows_read`（能失败的量）。
  5. 每次 v1 失败（返回失败与抛异常两条路径）打一行 `mimo authoritative call failed ... error_code=...`，**不带供应商正文或异常文本**。
  **验证**：
  - 变异（每项转红、按内存备份还原、与 HEAD 逐字核对）：前两个提交 13/13 PASS；修复提交重跑 13/13 PASS；日志行提交（15 项）**15/15 PASS**。
  - **第一次全量（`e9d4dc98`）：8561 passed / 4 skipped / 2 failed**，两条都是真缺陷，聚焦测试与变异都没抓到：
    (a) 事件循环阻塞普查报 `run_authoritative_gap_recovery_loop -> utc_now`：健康检查失败告警在**循环体里**读时钟。
        **修法是把读时钟挪进线程**（`record_health_check_failure`），**不是往白名单里加一行**。
    (b) 心跳用例单跑绿、全量红。**先复现再修**：`tests/test_unreadable_env_files.py` 排在前面即稳定转红。
        成因是 `configure_application_logging` 把包级 logger 设为 `propagate = False`，`caplog` 的 root 处理器收不到；改为把处理器直接挂到模块 logger。
  - **第二次全量（`ba7521aa`）：8563 passed / 4 skipped / 0 failed**，全量后工作树 0 改动。按裁定追加日志行后成为新候选。
  - 日志行用例第一版在单跑时抓到 2 行而不是 1 行：处理器既挂在模块 logger 上、又经传播到达 root，同一条记录被收了两次。
    改为挂处理器期间关闭传播，**单跑与排在配置日志测试之后两种顺序都验证了"恰好一行"**。
  - **第三次全量（`af5881dd`，完整输出落盘 + `-rfE`）：8564 passed / 4 skipped / 0 failed**，全量后工作树只有本状态文件的未提交改动。
  - **本会话自己的一次过失**：第一次全量输出接进了 `tail -25`，失败详情被截掉，事后抽取得到空——**"没抽到"不是"没有报错"**，只能逐条单跑取回。之后改为完整输出落盘 + `-rfE`。
  **生产事实**（只读）：见"事实基础复核"。受影响 116 条消息按群模式：auto_trade **16**（5 个群，80 次失败调用）+ notify_only 85 + 未配置 15。
  证据 `/root/evidence/mimo-step18-replay/`：`attempts.csv` 510 行（sha256 前 16 位 `2ee7b8170ba12b75`）、`invocations.csv` 508 行（`bb3c47980487e33c`）、`chat_trading_modes.csv`。
  **部署前场地核对**：A 线、B 线均回复无在跑的观察窗（各自以 `kill -0` 逐个核对，存活 0 个）；本会话核对服务器 22 个 pidfile 全部已死。
  全量枚举进程时发现一个他人的等待循环 `until grep -q WINDOW_MET /root/evidence/release-gates/observe.log`，已空转约 31 小时（该目录已有 `DONE`），**非本线进程，未动**。
  **部署前检查自检**：共享分支判定式检查已用一正一反两个输入验过（`6f8c38fd` 必 FAIL、origin 尖端必 PASS）。
  **余额接口**：公开资料只指向控制台的余额页（WebSearch + 充值公告页 WebFetch），**未找到程序化接口**；第 4 步以 `max_tokens=1` 探测为主。

- step-1 部署与 L1 窗（2026-09-12/13，本执行会话）：**部署 `cf0a0e14`（2026-09-12T23:50Z，四步全 PASS，回滚参考 `0ed2d488`）；
  L1 窗 2026-09-13T00:31:33Z `WINDOW_MET`（重起后 16 采样、0 重置、连续 904 秒）。**
  **起窗过程三次失败，各自留档**：
  1. `/root/evidence/mimo-step1-v1-partial/`：起算点取 tg-deploy 返回后的时刻（23:50:41Z），而新 worker 23:50:32Z 已启动、23:50:39.9Z 已打出启动心跳；
     心跳按设计只在首轮、状态变化、每约 30 分钟打，于是此后 `hb=0`——**起算点的错，不是生产问题**。查明前曾怀疑补救循环卡在健康检查上，
     逐项排除（journal 查法能命中已知行、日志级别 INFO、生产源码含心跳文字、事件循环无卡顿）后定位到起算点。
  2. `/root/evidence/mimo-step1-v2-bad-anchor/`：起算点用 `ExecMainStartTimestamp`（`Sun 2026-09-13 07:50:32 CST`）经 `date -d` 解析，
     **CST 被当成美国中部时间**，起算点落在 14 小时后。**首样本自检当场判 FAIL**。此后一律用"当前 epoch − `ps -o etimes`"。
  3. `/root/evidence/mimo-step1-v2-unmet-real-event/`：**判据 5 未达成——真实事件，已逐行解释**（18 采样 / 14 重置）：
     - raw 16436（notify_only 群）run 7253 于 23:52:28Z 开始、00:02:34Z 以 `mimo_provider_unavailable.network_error` 失败，耗时 606 s、2 次请求
       （配置 `timeout_seconds: 60.0`；httpx 超时按单次读写计，缓慢滴流可远超）；期间同群 run 7249/7250/7251 均成功——**供应商一直在应答**。
     - 该消息作业 23:57:28Z 已结算，23:57:29Z 起的第二个 run 21 秒内成功；7253 是**决策产生之后才迟迟写入**的旧请求线程。
     - 00:02:47Z 发 `mimo_provider_unavailable`（事故 2114，已投递）；00:06:30Z 下一次调用成功，00:06:48Z 发 `mimo_provider_recovered`（事故 2115，已投递）。
     - **无重复执行**：16436 只有执行尝试 1480，`blocked / source_message_deleted`。
     - **正面**：原判据写着"证不了"的一半在生产真实跑通——分类、错误码、失败日志行、13 秒内首条即发、恢复只发一次、投递、中文文案。
     - **反面**：**误报**，一条挂死的旧连接不代表供应商不可用。
  **裁定（指挥会话 2026-09-13）**：(A) 失败请求执行期间有其他调用成功即不算故障期，随第 3 步实现，并加请求总时长上限；
  窗口按 (a) 从 00:07:00Z 以完全相同的判据重起。
  **重起窗（`/root/evidence/mimo-step1-v2/`）**：起算点显式 UTC 转 epoch 并自检；worker pid 849718 全程未变；
  末样本 `hb=2 first_rows_read=5000 tick_failed=0 alerts=0 v1_total=5 v1_completed=5 bad_codes=0 fail_lines=0 a17_retired_rows=664`。
  **三次起窗失败都由"首样本必须满足哨兵"的自检或逐行核对抓住**，没有一次靠放宽判据收窗。
  A 线基线 `status='retired'` 保护行的**记录项**全程读到 664，已告知 A 线。**这个值不能读成"历史未被改动"的证据**——
  A 线更正：新关闭的 binding 会合法地让它增长，真正的异常是"关闭早于部署的 binding 下出现晚于部署的退役行"，要用快照法另核（见"执行教训"）。

- step-2（2026-09-12，本执行会话）：分支 `mimo/step-2-alerted-reasons-guard`（工作树 `.worktrees/mimo-step-2`），
  提交 `feat(mimo): a missing authoritative decision can never be un-alerted`；变基到第 1 步部署提交 `cf0a0e14` 之上，尖端 `4001c235`
  （变基前 `7b490c17`，**已核对变基只带进文档、无代码差异**，所以其上的全量结论仍然有效）。
  **事实（只读核对）**：写"终态 `authoritative_failed` 决策"的调用点全仓只有两处——
  `authoritative_recognition.assess_message_authoritatively`（→ `mimo_authoritative_failed`）与
  `telegram_live_listener._record_expired_authoritative_recovery_gap_in_session`（→ `authoritative_gap_recovery_expired`）；
  租约执行路径只在非失败时进入。过期路径所在的 worker tick **原本拿不到群交易模式**（web_app 启动 worker 时未传）。
  `_failure_point_for` 以 `.get(reason, reason)` 结尾，缺键不会 KeyError。
  `web_queries` 把 `ALERTED_REASONS` 展开进"系统未安全接纳"集合；过期消息本就是 `识别失败`、先命中前一分支，**页面显示不变**。
  **做了什么**：
  1. `recognition_failure_attribution`：`MIMO_AUTHORITATIVE_FAILED` / `GAP_RECOVERY_EXPIRED` 常量，`AUTHORITY_NOT_PRODUCED_REASONS`，
     写入点登记表 `AUTHORITY_NOT_PRODUCED_WRITERS`，并入 `ALERTED_REASONS`。
  2. `_failure_point_for` 补两条失败点文案。
  3. `run_message_processing_worker_tick` 接受 `group_trading_mode_provider`；过期记录后经同一告警关口（仅 auto_trade 群、同消息一问）告警，
     告警失败只记日志、不阻塞结算；web_app 启动 worker 时传入。
  **守卫**：点名式（两个 reason 各在集合里）+ 遍历式（AST 扫全 src：写入调用点集合 == 登记表；`agreement_status="authoritative_failed"`
  赋值点 ⊆ 登记函数；两个计数哨兵 ≥2；登记 reason == 集合 ⊆ 告警集合；登记 reason 字面量确实出现在写入模块里）
  + 逐个接线（两个 reason 各走真实调用方：auto_trade 群产生事故行、notify_only 群同一夹具不产生）
  + 链路（tick 签名、loop 以 `**tick_kwargs` 透传、web_app 启动调用处确实在传；再从 loop 走一遍证明透传）。
  **开发中测试当场抓到的缺陷**：第一版只改了 tick 签名、加了告警函数，**过期分支里调用它的那一行没加**——两条过期用例转红，补上后通过。
  **验证**：新用例 8 条；受影响现有测试 421 passed；**变异 7/7 PASS**（每项转红、还原后与 HEAD 逐字一致；锚点变基后复核各 1 次）；
  **全量（`7b490c17`，完整输出 + `-rfE`）：8572 passed / 4 skipped / 0 failed**，全量后工作树 0 改动。
  **部署**：第 1 步 L1 窗 00:31:33Z 达标后，2026-09-13T00:32Z 按四步部署 `0b2a4eb2`（回滚参考 `cf0a0e14`）；
  部署前指挥会话确认无其他观察窗、本会话 `kill -0` 逐个核对 pidfile 除第 1 步自己的观察器外无存活；
  部署前两向核对、"超出生产的代码只能是本步 5 个文件"白名单、推自己分支、tg-deploy、推共享分支、部署后两向核对，全部 PASS。
  **L1 窗起窗**：起算点 = 当前 epoch − `ps -o etimes`（00:32:01Z，worker 862728 由本次部署重启，etimes 44 s）。
  **第一次起窗（`/root/evidence/mimo-step2-v2-crlf-defect/`，3 采样）首样本判 FAIL，观察器缺陷**：
  群模式 CSV（`/root/evidence/mimo-step18-replay/chat_trading_modes.csv`）由 Python `csv` 模块写出，**35/35 行以 `\r\n` 结尾**，
  awk 读到的第二列是 `auto_trade\r`，一个群也匹配不上，于是 `alerts_outside_auto_trade` 每轮读成 `-`（按规则不算通过）。
  **先证实再修**：原写法读出 0 个 auto_trade 群，去掉行尾 `\r` 后读出 9 个（与此前已知数一致）。其余判据首样本全部通过。
  v3 只改这一行（sha256 前 16 位 `c34b018e4fd0d595`），同一起算点与 worker pid 重起，**首样本读出 9 个群、全部判据满足**。
  **L1 窗 2026-09-13T00:50:01Z `WINDOW_MET`**（`/root/evidence/mimo-step2/`，v3 观察器）：16 采样、0 重置、连续 903 秒；
  末样本 `hb=1 first_rows_read=5000 alert_failed=0 worker_failed=0 new_step2_alerts=0 alerts_outside_auto_trade=0 new_failed_decisions=0 new_expired_decisions=0`。
  **能证明**：worker 正常启动并消费、第 1 步健康检查仍在跑、新告警路径未吞异常、没有向非 auto_trade 群发告警、未换版。
  **证不了（本窗无样本）**：窗内没有真实的识别失败或过期，"auto_trade 群真实告警"这一半照实记为无样本，由遍历式守卫、行为用例、7 项变异与第 5 步离线重放承担。
  **step-2 completed。**

- step-3（2026-09-12/13，本执行会话）：分支 `mimo/step-3-outage-aware-recovery`（工作树 `.worktrees/mimo-step-3`），
  变基到 `9f1de979` 之上（**已核对变基无代码差异**）。提交：`a5d05550` 主体（有效年龄、重放、执行关口、故障开始取最早、中文原文）；
  `b35018da` 补两处变异暴露的用例缺口；`0cc63c8d` 规则 A 与请求总时长上限（按 09-13 两项裁定）；`c4a79bd3` 文档；`e13ccd3a` 规则 A 的对称情形（见 (f)）。
  按指挥会话 2026-09-12 裁定实现 (b)：**入场一律不重放**。
  **决定设计形状的事实（只读核对）**：
  1. **重放标记不能放在作业上**：`claim_message_processing_jobs` 认领时把 `last_reason` 覆盖为 `worker_claimed` / `stale_claim_reclaimed`，
     原值只作为内存里的 `claim.source_reason` 传给 tick，执行器拿不到；worker 重放途中崩溃、作业被重新认领后标记即丢，
     一条 14 小时前的入场会按普通入场执行。**所以"是否被故障耽误过"在执行时由持久事实判断**：该消息名下是否有（非孤立的）
     `mimo_provider_unavailable.*` 尝试行（run 审计 append-only）。
  2. **两条执行路径汇合在 `_auto_process_single_message_trade_signal`**：指令项路径对每一项也调它（带 `instruction_kind`），
     函数内先走管理分支、否则入场分支——关口放在这一个函数的两个分支里即覆盖两条路径。
  3. **终结只接受 `executing`**：`finish_message_instruction_item` 默认 `expected_current_statuses=("executing",)`，更新落空即抛
     `RuntimeError("instruction item is missing or not executing")`；`FINISH_STATUSES` 里没有 `awaiting_user_confirmation`。
     所以**管理转人工不能在执行关口里做**，改为在 `auto_process_message_trade_signal` 入口、指令项尚为 `pending` 时停放；
     `claim_next_message_instruction_item` 只认领 `pending`，停放的项不会再被认领。
  4. **`skipped` 正常终结**：`interpret_instruction_outcome` 对 `status="skipped"`（无已提交、无 `submit_unknown` 腿）给 `verified_refusal`，旧映射得 `succeeded`。
  5. **`request_management_target_confirmation` 没停放任何项时不发通知**（A-7b）。生产只读计数：近 30 天权威识别的管理候选 218 条，
     **没有指令项的 0 条**——无项路径目前不会走到，但仍给它单独一条事故（`provider_outage_management_not_replayed`），不靠"生产不会发生"。
  6. **只需改 worker 一处过期判定**：`_load_gap_recovery_candidates` 挑的是无决策行的消息，被故障耽误的消息都已有失败决策行。
  **做了什么**：
  - `provider_outage_replay`：按消息自己的（非孤立）不可用尝试行算故障跨度与有效年龄；`replay_verdict`；`management_replay_allowed`
    （有效年龄 ≤15 分钟且 `verify_lifecycle_targets` 全为 verified；快照过旧 `None` 按"不知道"不执行）；入口停放；
    重放 tick（等恢复通知已记录 → 只挑 auto_trade 群、两类"权威判定未产生"原因、名下有不可用行、**恢复时刻之后尚无新 run** 的消息 →
    按发出时间升序 `resume_terminal_jobs=True` 入队 → 以故障期为键发一次开始补做通知）。
  - `message_processing_worker._classify_claim_expiry`：被耽误过的消息按有效年龄判过期。
  - `auto_trade_execution`：入口停放；入场分支拒绝并通知（原文、群、时间、价格区间）；管理分支兜底拒绝并通知。
  - 补救循环每轮调重放 tick（状态变化记 INFO、真正入队记 WARNING、异常记日志不打断）；web_app 传入群交易模式。
  - 三种事故类型进 `ALWAYS_NOTIFIED`；摘要词表加 `message_posted_at` / `entry_summary`；中文文案；第 1 步恢复通知里"不会自动补做"一句改为补做规则。
  - **规则 A**：一条"不可用"失败，只有在它执行期间（开始到完成）**没有任何**"供应商回答了"的尝试完成，才计入故障期；否则是孤立失败——
    照常写错误码、打失败日志行，但不开故障期、不告警。推导读取行改为 `(状态, 错误码, 开始, 完成)`，回答行在**整个读取范围**里找
    （09-13 的 7249–7251 在 id 上位于 7253 之下）。**同一规则用于消息自己的故障跨度**：僵尸请求不能让正常入场被当成"故障期间入场"拒绝。
  - **请求总时长上限**：`_call_mimo_direct_model` 改为流式逐块读取、每块后检查 `MIMO_REQUEST_TOTAL_DEADLINE_SECONDS = 240`，超出抛
    `MimoRequestDeadlineExceeded(TimeoutError)`，分类为 `timeout`。
  **开发中抓到的缺陷与方案取舍**：
  (a) **中文原文被清洗掉**：`_safe_sentence` 只保留 ASCII 字母数字，"BTC 77000 多"变成"BTC 77000"。新增 `_safe_text`。
      A-16a 的 `instruction_excerpt` 用的是同一个 `_safe_sentence`，**既有告警同样有这个限制，本步未改**，记为遗留。
  (b) **故障开始时间取了"最后读到的行"**（第 1 步已上线代码）：id 顺序不是时间顺序，开始时间偏晚、重放 `since` 过滤掉更早完成的消息。改为开始取最早、最近失败取最晚、恢复取最早。
  (c) **变异脚本暴露两处用例缺口**：删掉入口停放调用、按 id 排序重放，补用例前均无用例咬住；补上后两项转红。
  (d) 开发中漏改了数据库加载函数（仍返回三元组），聚焦测试当场一串解包失败，补上后 220 passed。
  (e) **总时长上限先实测两种做法**（本地滴流服务，对照组无时限滴流 19.2 s，证明实测有效）：
      看门狗（到时从另一线程 `client.close()`）**失败**——计时器触发了，阻塞中的读没有被打断，拖到 61 s 才因单次读超时抛 `ReadTimeout`；
      流式逐块读 + 每块后检查**成功**，在 2.0 s 时限处准时中止。于是改为流式；7 个只实现 `post` 的假客户端同步补 `stream`
      （流式响应体按真实响应构造：有 `json()` 的给 JSON 原文，没有的给错误正文）。
  (f) **规则 A 的对称情形（第 5 步离线重放发现，指挥会话 2026-09-13 同意）**：
      09-12 最后一次成功（尝试 6749）03:00:21 开始、03:00:53 完成，**在它执行期间两次 402 已完成**（6747 于 03:00:25、6748 于 03:00:42）。
      修前的推导在 03:00:40 判故障开始、03:01:00 把这次在途成功判为"已恢复"、03:01:20 另开新故障期——
      **线上第 1 步代码与第 3 步候选 `c4a79bd3` 在真实故障开始时都会推送"不可用 → 已恢复 → 不可用"**。
      离线重放据此报 3 条 FAIL：告警 32（应 31）、恢复通知 1（应 0）、故障期被切成两段（494 次只算进后一段的 492 次）。
      **本会话最初猜"2 次失败被规则 A 判孤立"是错的**，实际孤立 0 次，492 是假恢复切段所致。
      修法：一次"成功"若在其执行期间已有供应商不可用的失败完成，就是故障开始前发出的旧请求——在推导中为中性行、不用于判定失败孤立、
      也不作为消息自己故障跨度的恢复时刻。两边核对：09-13 僵尸案例 7249–7251 执行期间无失败完成，仍是有效成功，7253 仍孤立、不告警；
      09-12 故障期从 03:00:25 连续到底。锁住的用例：09-12 行序 6745–6753 在 03:00:40 / 03:01:00 / 03:01:20 / 03:02:00 四个时刻推导，
      始终是同一个未恢复的故障期；旧成功不缩短故障期内消息的有效年龄。提交 `e13ccd3a`。
      变异脚本第一次跑时"去掉规则 A"一项锚点失配（代码由 `if` 改为 `elif`），脚本按设计判 FAIL 停下、未进入全量；修正锚点后重跑。
  **240 秒取值按生产实测校准**：近 30 天成功调用 6159 次，p95 62.8 s、p99 106.7 s、>240 s 共 11 次、其中单请求 >240 s 仅 1 次、最长单请求 259.3 s——
  即每月约误杀 1 次本可成功的调用（该消息仍会走作业自身的重试）。指挥会话接受该取值，并要求写入本文件的原句：
  > **超时的那次尝试不再重试：一次尝试最坏 240 s（总时限）+ 60 s（最后一次阻塞读的单次读超时）= 300 s，不超过作业认领过期的 300 s。**
  > 若照常重试，同一消息会被重新认领、并行跑出第二个 run——正是 09-13 那次僵尸请求的来路。
  > 此不变量由 `test_the_deadline_plus_one_blocked_read_fits_inside_the_job_claim_lease` 锁住（总时限 + 默认单次读超时 ≤ 认领过期）。
  **锁住规则 A 的用例**：生产行序 7249–7255 分别在 00:02:47（7254 未写入）与 00:06:48 两个时刻重放，推导不出故障期，真实 tick 不调用任何捕获；
  对照：三次失败、期间无任何成功，照开故障期，之后一次成功判恢复；消息名下只有一条孤立失败时不算"被耽误过"。
  **验证（候选 `e13ccd3a`）**：变异 **20/20 PASS**（入场关口、管理兜底、入口停放、过期判定、重放去重、等恢复通知、群模式过滤、按发出时间排序、故障开始取最早、
  中文原文、web_app 接线、循环透传、重放默认开启、`ALWAYS_NOTIFIED`、规则 A 两处、**对称规则两处**、总时限检查、超时不重试），每项变异后源文件逐字节还原；
  **全量（`e13ccd3a`；完整输出 + `-rfE`）：8614 passed / 4 skipped / 0 failed**，全量后工作树 0 改动。
  前一候选 `4f7a8ed7`（变基前）全量 8609 passed / 0 failed、变异 18/18；再前 `2c72041a` 8601 passed、变异 14/14。
  **待做**：指挥会话确认新候选后按四步部署，起 30 分钟 L2 窗（判据见上）。

- step-5（2026-09-13，本执行会话）：**离线重放 09-12 的 494 次失败**，L0，不部署。证据 `/root/evidence/mimo-step5-replay/`
  （`step5_replay.py` sha256 前 16 位 `01fd96af8793c37b`；输入三份 CSV 与 `/root/evidence/mimo-step18-replay/` 逐字节一致，
  `attempts.csv` `2ee7b8170ba12b75`、`invocations.csv` `bb3c47980487e33c`）。
  **方法**：09-12 尝试行的错误码都是旧的 `v1_authoritative_failed`，按消息 id 与完成时间（±5 s）对到调用记录，错误文本含 `402 Payment Required` 的映射为
  `mimo_provider_unavailable.insufficient_balance.http_402`（494 条）；对不上的 2 条空输入保留旧码（中性）。
  以第 3 步分支的纯函数，从 02:50 到 18:10 每 20 秒模拟一次健康检查（只用该时刻前已完成的行）。
  **修前（`replay-before-mirror-fix.out`，sha256 `df04c47ed1ac8643`）：3 FAIL**——暴露上面 (f) 的缺陷。
  **修后（`replay-after-mirror-fix.out`，sha256 `7c29c79005de08ea`）：OVERALL PASS，预期一字未改**：
  - 第 1 步：首条 402 03:00:25，首条告警在 03:00:40 的检查发出（一个 20 秒周期内）；故障期 03:00:25 起连续、494 次失败；
    每 30 分钟一条共 31 条；恢复通知 0 条（数据里故障开始后到 18:10 没有任何有效成功）。
  - 第 2 步：失败消息按群 auto_trade 16 / notify_only 85 / 未配置 13；**auto_trade 群 16 条各告警一次**，其余不告警。
  - 第 3 步：假设 17:45 恢复，按原顺序补做 16 条 auto_trade 消息；全部判为"被故障耽误过"（无孤立）；17:46 时有效年龄 1.0–4.9 分钟，全在 15 分钟管理窗口内。
  - **限流**：整次 14.7 小时故障，人最多收到 31（故障提醒）+ 16（第 2 步逐条）+ 1（开始补做）+ 16（第 3 步逐条结果）= **64 条**。
  **证不了**：重放结果里入场与管理的具体分布（导出不含识别出的指令类型）；第 3 步逐条结果按上限计。

## 执行教训（本项目执行中记下）

- **zsh 把 `$VAR:refs/...` 里的 `:r` 当成修饰符，推送引用被静默改写**（2026-09-13）：`git push origin "$TIP:refs/heads/..."`
  展开成 `9f1de9...efs/heads/...`，远端报找不到引用，`|| exit` 守卫当场停住、共享分支未被推。**若改写后的字符串恰好是另一个存在的引用，就会静默推错。**
  部署脚本不受影响（显式用 `bash` 跑）。同一会话另一次 zsh 差异：`git add $FILES` 不分词、暂存 0 个文件，被"暂存清单必须等于预期"的守卫拦下。
  **做法**：zsh 下拼接 git 引用一律写 `${VAR}`；多步命令整段交给 `bash`；文件列表用数组。
- **观察器的"读不到"有三种来源，这次各撞一次**：起算点晚于被观察事件（第 1 步 v1）；时区缩写被误解析（第 1 步 v2，`CST`）；
  数据文件行尾（第 2 步 v2，Python `csv` 写出 `\r\n`）。**三次都由"首样本必须满足全部判据"的自检当场抓住**，没有一次拖到收窗、也没有一次靠放宽判据收窗。
  **做法**：起窗后立即核对首样本；起算点用"当前 epoch − `ps -o etimes`"；读外部数据先用已知答案（"9 个 auto_trade 群""664 行"）核一次查法；
  观察器所读表与列起窗前用 `pragma_table_info` 核实存在。
- **A 线基线 664 的口径**（A 线 2026-09-13 更正）：`status='retired'` 保护行数不是"永远不该变"的数——A-17 之后任何 binding 新关闭都会合法地让它增长。
  **真正的异常只有一种**：关闭时间早于部署的 binding 下，出现 retired_at 晚于部署的行。区分用快照法（开窗时拍下已关闭 binding 的 id，只看快照之外的）。
  所以本项目观察器里"664 不变"只是记录项读到的值，**两个方向都证明不了"历史未被改动"**；本项目各步不写交易所、不改保护行，不把它升为判据。
- **单元用例全绿 + 变异全 PASS，仍被离线重放抓出真实故障开始时的假恢复**（2026-09-13）：用例是按"规则 A 要防的那一种行序"写的，
  没有覆盖"故障开始那一刻恰有一次成功在途"。真实的 494 行一喂进去，告警多 1、恢复通知多 1、故障期被切两段。
  **做法**：判定逻辑改完先用真实事故数据整段重放、预期写死再跑；重放暴露的行序原样收为用例（6745–6753），并为修法补变异。
- **"要检查的东西先实测再实现"比"先实现再发现"便宜**：请求总时长的看门狗方案读起来完全合理，实测却打断不了阻塞读；
  240 秒的取值若按设计草案的 120 秒，会每月误杀约 45 次成功调用——两者都是先查了数据或先跑了原型才避开的。
