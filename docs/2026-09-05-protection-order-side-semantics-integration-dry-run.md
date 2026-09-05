# Protection order side semantics：集成与 dry-run

## 范围与候选

本轮授权只推进集成、stage、完整 dry-run；dry-run 通过后仍须所有者确认，
才能激活并开始针对 convergence 227 / leg 583 的生产验收。
未合入 `codex/context-hold-owner-alert`，不改 schema 或生产业务数据，
不人工调用任何交易所写路径，不处置 execution_uncertain。

只读核对发现交付分支尚未提交：`codex/protection-order-side-semantics` 原 tip
为 `41c618936f9a598b3641d7be4e6a04d8b98b0f3f`，实际修复在该 worktree。
按实施记录重算 src/tests 615 个文件指纹，精确等于已评审值
`57dbe389b432e68ce8c64d8ed382174091c93137cd27ce5831d44c033a5ddf94`，
才将明确列出的 4 个生产文件、4 个测试文件、2 份文档提交为
`356630dd882b876108bb541bd627d0a112edf334`；没有提交 `.venv` 或其他会话文件。

集成基线：`dfd0427256293a301a3cbe94e0e5a595b15489d5`。
合并代码候选：**`af8676dca5ce83acfc060a8b856ccf3884f25150`**。
该 merge 的两个 parent 分别为上述集成基线与修复提交。合并无冲突；
合并后 src/tests/deploy/config/scripts/pyproject.toml/uv.lock 与修复分支零差异。
本文件是后续操作记录，不改变代码候选。

## 集成复核与完整套件

按 executing-plans 执行分步门禁，并按 requesting-code-review / code-review
增加独立合并复核。评审 `side_merge_review` 对精确基线与修复 commit：
**P0/P1/P2 均无发现，可合并**。

- long/sell、short/buy 保护单接受；矛盾方向、未知订单方向、别名冲突拒绝。
- 原始订单 ID 先计数再过滤，异常重复与 legacy 竞争候选不会消失后产生伪唯一性。
- path A、活仓方向解释、exact fingerprint、通用 native matcher 共 10 个函数
  相对集成基线 AST 不变；当前数量→sz=0 与 ownership/write gates 保留。
- 没有混入告警分支；测试基于合并状态，不沿用分支测试结果。

首次合并全套：15 failed、7338 passed、4 skipped、32 warnings，485.43 秒。
所有失败均发生在部署 helper 测试启动子进程之前：隔离 worktree 缺少
`.venv/bin/python`，抛 FileNotFoundError 或 Planner Python is unavailable。
按 systematic-debugging 核对栈及会话2环境后，仅补齐现有虚拟环境的本地 symlink，
不改代码/测试断言。相关两组测试重新运行 **46 passed、1 skipped，6.17 秒**。
首次日志 `/tmp/protection-side-merged-full-pytest-20260905.log` 保留，不冒称通过。

最终合并全套：**7353 passed、4 skipped、32 warnings，479.74 秒**。
退出码 0；最终候选生产代码/测试未再改变。
最终日志 `/tmp/protection-side-merged-final-pytest-20260905.log`。

## Stage 与完整 dry-run

**已 staged，完整 dry-run 返回 status=validated、authorization_consumed=false。未激活。**
使用未修改的 `scripts/server_git_update.sh`（显式指定既有 planner Python）及其
`bootstrap_server_updater.sh` 标准传输路径。stage 时集成分支远端 tip 精确等于
af8676dc 候选，因此不需要另建 stage 分支；本操作记录的后续文档提交不改变 receipt。
所有旧 stage/release 保留不动，没有删除或重写。

action manifest 风险 L3（保护证据解释可能影响自动写入准入），
components=web/monitor/ingest/worker，schema_changed=false、production_data_mutation=false。
exchange_write_semantics_changed=true、authority_changed=true，明确声明风险，
不代表本次 dry-run 获得人工交易所写授权。
本轮不做生产 schema 或数据修复，所以不额外创建全库备份/副本。

Stage receipt：

- candidate：`af8676dca5ce83acfc060a8b856ccf3884f25150`。
- manifest SHA-256：`5ae834ad537676e849b0be58c128fc9721ebf52f5ba993b84e329f4c68a97b28`。
- content SHA-256：`b4b79619b7c7fe2ff75864243f2b2310cc93553dee98dd7d7a6255424b7fabdb`。
- tree：`06beb6a1e031b86f679fcea4908d76e1c2254baa`。
- stage action plan SHA-256：`8603298f33fdb68842f8e004c2f1cf9ec017c8ba5281163728f1d209700e5183`。

新鲜完整取证：

- **39/39 immutable release** 逐个通过激活器 `validate_release()`，含权限/所有者、
  manifest/receipt/content 全树摘要；`__pycache__` / `.pyc` / `.pyo` 为 0。
- 四个角色各自 rollback 均为实测已证明的
  `9501a5f39f0c5f196cc29f24f3e3b8786267126b`，分别绑定 manifest
  `2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`。
  不是从 /opt 工作树 git HEAD 推断；三个进程用各端口 runtime identity + PID/start ticks。
- 候选与 rollback 均不同 SHA；两者 runtime-support digest 一致：
  `07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b`。
  本次未修改 digest、迁移规范化或任何激活门禁。
- monitor 有效 unit/env/drop-in 与同 commit/manifest diagnostic 独立证明成功。
  diagnostic 时间 **2026-09-04T22:04:33.892734Z**，晚于配置最大 mtime
  **2026-09-04T22:03:28.107848876Z**；diagnostic unit Result=success、ExecMainStatus=0。
  未重新触发诊断，不使用 healthy 作为额外门禁。
- 候选主 ExecStart 经 EnvironmentFile 覆盖之后的 prospective 导入路径验证为
  `/opt/telegram-kol-releases/af8676dca5ce83acfc060a8b856ccf3884f25150/src`，
  三个 monitor service 均通过；不是仅验证 ExecStartPre 或声明值。
- 活跃交易所写 **0**；只读 quiescence 检查 global authority 为 idle。
- 标准 helper 再次独立执行上述激活器门禁并返回 validated，未消费授权、未进入服务控制。

新 canonical v3 authorization：root-owned、0400，
`/run/protection-side-dryrun-af8676dca5ce.json`，有效窗口
**2026-09-05T14:31:22.615771Z—14:36:22.615771Z**（5 分钟）。
绑定精确四角色映射与：

- activation action plan SHA：`d0925eac6299bad24500cf98470745b2407f5f56ca10f212b638882a3258e6b6`。
- controller commit：与候选相同。
- exact Git archive control bundle SHA：`32c4260345565074fdc1b6cb78a73636e8431ac6fe0d775f51eea23d42368cf7`。
- authorization SHA：`37777ea80be1c262864c006517187aab2ff12a9a5cc280ad839932f5044fdd56`。

**14:32:22.928541Z** 核验源文件仍在、摘要相同、consumed marker 不存在；
**14:32:22.930667Z** 删除这份短期授权，确认源文件与 consumed marker 均不存在。
真正激活必须重新生成，不能复用本次授权。

## Dry-run 后的实际状态与证据

2026-09-05T14:32:23.089434Z 复核：

| 角色 | 实际 release | PID | NRestarts | 身份/状态 |
|---|---|---:|---:|---|
| web | 9501a5f3 | 1525321 | 0 | loaded_artifact_verified=true、active |
| ingest | 9501a5f3 | 1525328 | 0 | loaded_artifact_verified=true、active |
| worker | 9501a5f3 | 1525316 | 0 | loaded_artifact_verified=true、active |
| monitor | 9501a5f3（配置+diagnostic） | 0 | 0 | oneshot 当前非运行；主 service 为 failed |

三个运行角色的 release/manifest/PID/start ticks/NRestarts 及
entry_admission_frozen=false 均前后相同。**running=0、uncertain=4**，
四条 uncertain 全字段相同，未处置任何历史记录。
binding 339 与 leg 583 均 active、pos_id=1001125135694798；任务 227 仍为
`waiting_backup_stop / convergence_waiting_backup_stop`。
这只是 dry-run 后只读 DB 状态，不是已取得新候选 TP 收敛验收。

monitor timer 实测 `is-active=active`、`is-enabled=enabled`。
**主 monitor 自然 service 的 failed/exit-code 是本轮开始时已存在的状态，
dry-run 前后未改变；不能把 diagnostic 的 success 写成主 monitor 已健康。**
本轮不重启、不 reset-failed、不改监控门禁。自然业务健康与 diagnostic 身份证据分别记录。
没有读取到独立 settings 行的 auto_trade_enabled，本轮不据此推断其值；
真实激活及观察时仍须从有效设置来源确认，不能用记录缺失当 false/true。
结束磁盘可用 **7,100,059,648 字节**，没有新增全库备份、删除证据或改业务数据。

服务器 root-owned 证据目录：
`/var/lib/telegram-kol-cutover-evidence/af8676dca5ce83acfc060a8b856ccf3884f25150/protection-side-dryrun-20260905T1430Z`。
含 release-integrity、runtime-identities-before、monitor-rollback-proof、
monitor-candidate-proof、runtime-support-digests、active-write-precheck、units-before、
database-before、activate-action、authorization-metadata/unconsumed/deleted、post-dryrun
的 JSON，以及标准 stage/dry-run 输出和一次性取证脚本。

一次性取证脚本首次在新 evidence 父目录不存在时 FileNotFoundError 退出，
发生在门禁/授权创建前；仅补齐 mkdir parents 后重新从头取证。
这不是激活器失败，也未修改候选或标准 helper。
所有 immutable import 均为 Python -B + PYTHONDONTWRITEBYTECODE=1。
生产运行状态文档不将 staged 候选写成已上线；当前运行仍是 9501a5f3。

## 激活后的验收目标（未执行）

用户要求关注 convergence 227 / leg 583 / binding 339 / lifecycle 1088 /
pos 1001125135694798：是否从 waiting_backup_stop 进入 ready 并由系统自行挂出
80200/81000/81700 三档止盈，记录真实订单 ID、价格与数量；
若不 ready，记录新的原因码及实际阻断点。主止损 77500 不得变差。
这些必须在另行批准激活后通过真实证据验收，不能用本地 fixture 或 dry-run 代替。

届时四组件身份、冻结/解冻、0 running / 4 uncertain 基线、自然 monitor、
30 分钟消息样本及冻结窗口 stale expiry 仍须按所有者要求重新核验。
