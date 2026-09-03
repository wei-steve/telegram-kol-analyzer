# Recognition execution lease recovery 生产 schema 与激活阻断记录

日期：2026-09-03（UTC）

候选代码：`392a74730d5406d23e2080324e472fcdfdb1ea67`

实施记录：`8d994dec2b2774415013b20b72918936fdb27f73`

风险等级：L3（schema 独立动作）/ L2（计划中的 runtime 激活与观察）

## 结论

- 生产 schema 独立动作成功：只新增了
  `authoritative_execution_attempts`、`entry_assembly_wakeup_executions`、
  `recognition_execution_scan_cursors` 三张空表及设计规定的约束和索引；既有表结构和业务数据未改动。
- 候选 release 已完成 stage，激活前 33/33 个 immutable release 全树校验通过，未发现
  `__pycache__` 或 `.pyc`。
- worker-only runtime 激活在创建授权、调用激活器和任何服务控制之前停止。标准激活器把 `worker`
  归入 authority component，并强制 authority activation 同时声明 `web`、`monitor`、`ingest`、
  `worker`；这与本轮明确授权的 worker-only scope 冲突。没有绕过激活器，也没有自行扩大为四角色激活。
- 因未激活，30 分钟/5 条真实消息的 L2 观察没有开始，不能声称新 runtime 已在生产验证。
  三角色仍运行原版本，现存 29 条 `execution_running` 记录完全未触碰。

## 第一步：生产 schema 独立动作

### 备份与动作边界

- 证据目录：
  `/var/lib/telegram-kol-cutover-evidence/392a74730d5406d23e2080324e472fcdfdb1ea67/recognition-execution-production-schema-20260903T050629Z`
- 生产库：`/opt/telegram-kol-analyzer/data/research.db`
- root-owned mode-0600 压缩备份：`pre-recognition-execution-production-schema.db.zst`
- 原始数据库大小：`853946368` bytes
- 原始数据库 SHA-256：
  `e5865d6e370396b664fa7c814db20eab56c1f4544058b3f5593711aa503cd0dc`
- 压缩备份 SHA-256：
  `b765ae2585ef92cd65d705e2d2536de706c7dd86cbc36b931a324683b0dedca7`
- 解压流 SHA-256 与原始数据库 SHA-256 完全相同，`roundtrip_verified=true`。
- schema plan SHA-256：
  `ed2a95303a8d50ac58c9a1b5c1276c889c32b5f42a3cebdb3ad2cad432d8afa4`
- 动作在 `/run/telegram-kol-update.lock` 下以单事务执行；开始时间
  `2026-09-03T05:09:12Z`，结束时间 `2026-09-03T05:09:19Z`。
- 本步骤没有重启、停止或切换任何服务。

### 完整性与幂等性

| 检查 | 动作前 | 动作后 |
|---|---:|---:|
| `PRAGMA quick_check` | `ok` | `ok` |
| 外键检查结果行数 | 0 | 0 |
| `PRAGMA query_only`（验证连接） | 1 | 1 |
| `execution_running` | 29 | 29 |
| `execution_uncertain` | 0 | 0 |

首次显式初始化只创建三张目标表；第二次执行返回 `created_tables=[]`、`changed=false`。
精确校验确认三张表的列、外键、check constraint、唯一约束和普通索引均与计划一致，错误清单为空。
三张新表在动作后均为 0 行。

### 关键业务表前后计数

| 表 | 动作前 | 动作后 |
|---|---:|---:|
| `raw_messages` | 14547 | 14547 |
| `recognition_decisions` | 14545 | 14545 |
| `message_processing_jobs` | 2786 | 2786 |
| `message_instruction_items` | 942 | 942 |
| `signal_candidates` | 2161 | 2161 |
| `strategy_lifecycles` | 1063 | 1063 |
| `strategy_management_batches` | 154 | 154 |
| `execution_bindings` | 331 | 331 |
| `execution_order_legs` | 570 | 570 |
| `execution_events` | 3924 | 3924 |
| `context_resolution_attempts` | 4497 | 4497 |
| `worker_command_jobs` | 1 | 1 |

`schema-apply-summary.json` SHA-256 为
`77dfa2c34d7ff790568a59ab246bcfd25084a574d2a1bd2d7778d96e985efcdb`。

### 回滚边界

当前旧 runtime 完全忽略三张新表，且三张表仍为空。若在 runtime 使用前另行授权回滚，
可按逆依赖顺序只删除这三张空表。runtime 一旦写入新表，必须先导出并校验新表数据，
先回滚 runtime，再由单独授权决定是否删表；不得直接丢弃 lease 或 child-fence 证据。

## 第二步：stage、激活前校验与 fail-closed 停止

### Stage 结果

- 为满足 exact-commit stage 约束，远端临时集成分支
  `codex/lease-recovery-runtime-392a7473` 精确指向候选 commit；未改写正式集成分支历史。
- release tree：`9184060f7b9dbfad5f7993db3f94b0dc7953fbde`
- release content SHA-256：
  `9158f538d14e1cc5b701eba3f6a542575f214fd5af60fc260af9db47fbb2d389`
- release manifest SHA-256：
  `b7eaa675a6cdc25b29c1e81ee84f032537a1d5bd71a91ab4374bacd021cd7bfc`
- stage action-plan SHA-256：
  `f82eb73ec06e4395c2b991b830f8c0699d66652f16d95b567429b4bbdac9f3a0`
- action manifest 的 component 为 `worker`，并声明 `schema_changed=false`；生产 schema 已在第一步独立完成。

### 激活前证据

33 个保留的 immutable release 全部通过实际激活校验器的全树校验；无失配，且
`__pycache__`/`.pyc` 路径计数为 0。只读 worker 快照完整，记录到 2 个仓位、0 个未结订单，
快照指纹为 `3af53f115e955d9cb09fad19dd3e4af360a9e33500086d54e47e1d0baa9e5472`；
未发起任何交易所写请求。

实测 runtime 身份如下；这些值来自各角色的 `/api/runtime/deployment-identity`，不是
`/opt/telegram-kol-analyzer` 工作树 HEAD：

| 角色 | release commit | manifest SHA-256 | PID | verified |
|---|---|---|---:|---|
| web | `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` | `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8` | 1396631 | true |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | 3315585 | true |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | 3315574 | true |

三服务均为 `active/running`、`NRestarts=0`；`auto_trade_enabled=true`、
`entry_admission_frozen=false`。计划中的 worker rollback commit 因而是实测的
`0de19c1cbb2089fd58b8940d9b01a65096f9a063`。

### 阻断原因

候选的 `src/telegram_kol_research/scoped_release_activation.py::activate_release()` 包含以下约束：

```text
authority_components and set(components) != _AUTHORITY_RUNTIME_SCOPE
=> ActivationError("authority activation must declare web, monitor, ingest, and worker")
```

`worker` 属于 `_AUTHORITY_COMPONENTS`，而 `_AUTHORITY_RUNTIME_SCOPE` 是
`web + monitor + ingest + worker`。因此 worker-only action manifest 会被确定性拒绝；
该约束不因本次 manifest 的 `authority_changed=false` 而放行。

手工绕过激活器会破坏已审核的授权/rollback/identity 合约；扩大为四角色激活又超出本轮
worker-only 授权。故在以下动作之前停止：

- 未创建或消费 activation authorization；
- 未调用 activation helper；
- 未停止、启动或重启任何服务；
- 未激活候选 runtime；
- 未开始 L2 观察，也没有伪造 post-activation 全树校验结论。

阻断证据：
`/var/lib/telegram-kol-cutover-evidence/392a74730d5406d23e2080324e472fcdfdb1ea67/recognition-execution-runtime-activation-20260903T051200Z/preactivation-blocker.json`，
SHA-256 `b17f3d7bec788b29a6ce8ae35c3535bdbebf719930d6a60366f84ba5c08a3915`。

## 当前安全状态与待决项

- 三张 schema 表已精确安装但均为空；旧 runtime 安全忽略它们。
- 候选只处于 staged 状态；生产 worker 仍为 `0de19c1c...`。
- `execution_running` 仍为 29，`execution_uncertain` 为 0；现存行未被更新、解锁或重跑。
- 观察期未开始，因此“新代码不再产生卡住行”、scanner、backlog expiry 保护和真实消息通行性
  尚未获得生产证据。
- 下一步需要所有者单独决定：修改并评审激活器以合法支持本次 worker-only scope，或明确扩大为
  四角色 activation。本文不替所有者作此范围决定。
