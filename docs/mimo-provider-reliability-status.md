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
current_step: 1
step_status: in_progress        # planned | claimed | in_progress | completed | blocked
claimed_by: (本执行会话，见证据区首条)
production_head_at_start: 0ed2d488aa5843187fa2e1e11ef9d986af7c648b
base_commit: 8046208c277efd06d045dc2e73d6caa729e8f485   # origin 共享分支尖端，相对生产只多文档
```

## 步骤总览

| 步 | 名称 | 风险 | 状态 |
|---|---|---|---|
| 1 | 供应商错误分类：402/401/403/429/5xx/超时/网络 → `mimo_provider_unavailable`（首条即发、每 30 分钟一条、恢复通知），与请求内容错误分开 | L1（新增告警，不改权威与交易） | in_progress |
| 2 | `ALERTED_REASONS` 遍历式守卫：凡"权威判定未产生"的 reason 必在告警集合；`mimo_authoritative_failed` 进 auto_trade 群告警 | L1 | planned |
| 3 | 补救窗口与供应商状态解耦；恢复后按序重放 auto_trade 群消息，管理类先核目标仓位；逐条记录并通知 | L2（恢复路径） | planned（入场重放策略待裁定） |
| 4 | 主动巡检：连续同码失败计数告警；每日 `max_tokens=1` 探测（不进业务表）；余额接口（如有） | L1 | planned |
| 5 | 用 step-18 的 494 次失败离线重放，验证 1–3 的判定与限流 | L0（离线） | planned |

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
