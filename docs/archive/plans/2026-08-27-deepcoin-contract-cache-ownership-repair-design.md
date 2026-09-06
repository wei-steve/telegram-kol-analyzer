# Deepcoin 合约规格缓存所有权修复设计

日期：2026-08-27
状态：已批准，等待实施计划
风险级别：本地实现阶段 L1；生产恢复与自动入场重新启用阶段按 L2 执行

## 1. 结论

保留现有合约规格缓存路径、sticky 数据目录、严格校验和原子发布语义，
只修正缓存文件的唯一写入者合同：

- `deepcoin_contract_specs_cache.json` 必须由
  `telegram-kol-worker:telegram-kol-runtime` 所有，权限为 `0660`；
- worker 是唯一允许发布新缓存的运行时身份；
- Web 与 ingest 只能通过共享运行组读取；
- Runtime Agent 必须继续无权读取或写入缓存；
- Telegram session、SQLite、备份等现有权限边界保持不变。

生产恢复采用受控方式：先冻结未来自动入场，再部署、迁移所有权、刷新并验证，
最后由所有者单独批准恢复未来新信号。已经安全拒绝的历史信号永不自动重放。

## 2. 问题陈述

生产缓存由曾经以 root 运行的 monolith 创建，当前属性为：

```text
owner=root
group=telegram-kol-runtime
mode=0660
parent=data, owner=root, mode=1777
```

split runtime 的执行角色是 `telegram-kol-worker`。它可以写目录，也可以修改
缓存文件内容，但 Linux sticky 目录规则不允许它删除或替换 root 所有的目标目录项。
缓存发布代码执行以下安全流程：

```text
下载完整 Deepcoin 产品快照
  -> 严格校验
  -> 在同一目录创建临时文件
  -> 写入并 fsync
  -> 重新读取并校验临时文件
  -> os.replace(temporary, cache)
  -> fsync 父目录
```

最后的 `os.replace()` 因目标 owner 不匹配而失败。provider 捕获异常并拒绝接受
候选快照，旧缓存继续保留；旧缓存过期后启动加载也会 fail closed，于是新的入场合同
以 `contract_spec_sync_unavailable` 进入 `verified_refusal`，且
`attempted_exchange_write=0`。

这不是 Telegram、队列、AI 识别、Deepcoin 网络或产品接口故障。直接只读查询仍能
取得完整产品列表，消息队列也正常完成；故障只发生在“已校验候选快照的原子发布”阶段。

## 3. 目标

1. 让 worker 能在不增加特权的情况下原子发布动态合约规格缓存。
2. 保留缓存完整性、摘要、TTL、严格解析和 fail-closed 语义。
3. 保留 `data/` sticky bit、Runtime Agent 隔离和 split runtime 身份边界。
4. 让部署、重启、Runtime Agent sanitizer 重跑后，缓存 owner 仍保持正确。
5. 让缓存过期、owner 漂移和连续规格同步拒绝进入生产监控。
6. 以受控冻结方式恢复未来自动入场，不补单、不重放历史信号。

## 4. 非目标

- 不改变允许交易的 symbol、TTL、规格数值或下单计算。
- 不切换到静态 YAML 兜底。
- 不移除原子替换或改为原地覆盖。
- 不移除 `data/` sticky bit。
- 不给 worker 增加 `CAP_FOWNER` 或其他特权。
- 不递归修改整个生产数据目录权限。
- 不修复、重放或重新解释历史拒绝信号。
- 不在本修复中改变 per-chat、queue、worker command 或识别权威语义。

## 5. 方案比较

### 方案 A：保留路径，worker 独占缓存所有权（采用）

将目标缓存改为 worker-owned，共享运行组保留读写权限。缓存临时文件本来就由 worker
创建，因此成功替换后新 inode 会自然保持 worker owner。

优点：改动最小，不迁移路径，不削弱安全边界，不改变发布协议。
缺点：必须同步修正 worker 启动准备、Runtime Agent sanitizer 和部署文档。

### 方案 B：迁移到 worker 专用 StateDirectory（不采用）

建立独立 worker-owned 目录，并让三个角色使用新路径。

优点：目录所有权模型更直观。
缺点：涉及路径迁移、三个 unit、回滚兼容和更多生产状态，超出最小修复范围。

### 方案 C：特权发布代理或直接覆盖（拒绝）

让 root 代理完成替换，或放弃原子替换直接写旧文件。

优点：可以绕过 sticky 目录 owner 限制。
缺点：扩大特权面，或破坏缓存原子性和崩溃安全，不符合项目安全合同。

## 6. 所有权合同

### 6.1 缓存文件

```text
path: /opt/telegram-kol-analyzer/data/deepcoin_contract_specs_cache.json
type: regular file
links: exactly 1
owner: telegram-kol-worker
group: telegram-kol-runtime
mode: 0660
agent ACL: telegram-kol-agent:---
```

目标缺失是允许状态：worker 可以在第一次成功刷新时创建它。以下状态必须失败：

- 符号链接；
- 非普通文件；
- 硬链接数不为 1；
- owner 不是 root 或 `telegram-kol-worker`；
- group 或 ACL 无法收敛到合同状态。

### 6.2 其他共享文件

缓存不得继续与 Telegram session 文件共用同一个“全部 root-owned”的处理函数。
权限 helper 必须把共享文件分为：

- worker-owned：动态合约规格缓存；
- root/shared：Telegram session、session lock 及其 sidecar 文件；
- Runtime Agent 可写：已审查的 SQLite 文件；
- Runtime Agent 禁止：备份、probe、session backup 及未明确允许的生产数据。

## 7. 组件设计

### 7.1 worker 缓存准备 helper

增加一个窄范围 Python helper，由 worker unit 的 root `ExecStartPre` 调用。helper：

1. 使用 `O_NOFOLLOW` 打开父目录和目标；
2. 只接受缺失、root-owned 或 worker-owned 的单链接普通文件；
3. 通过文件描述符设置 owner、group、mode 和精确 ACL；
4. 重新 `fstat` 验证最终合同；
5. 任一未知状态非零退出，阻止 worker 启动；
6. 支持只读 `--check`，供部署门禁和监控验证使用。

helper 不读取缓存内容，不接触凭据、数据库或 Telegram session。

### 7.2 worker systemd unit

在 worker 启动前执行 helper。`ExecStartPre` 只对这个固定目标运行，不接受任意路径，
不使用 shell glob，不递归修改目录。

这样可以覆盖：

- 当前 root-owned 缓存首次迁移；
- worker 正常重启；
- monolith 回滚后重新切回 split runtime；
- Runtime Agent 安装或 sanitizer 重跑后的状态收敛。

### 7.3 Runtime Agent sanitizer

将动态缓存从统一 `SHARED_RUNTIME_DATA_FILES -> root owner` 逻辑中拆出。
sanitizer 对缓存设置 worker owner，对 session 文件继续设置 root owner，并保持 Agent
拒绝 ACL。sanitizer 必须是幂等的。

### 7.4 部署与操作文档

将缓存迁移命令从 `chgrp + chmod` 修正为精确 worker owner 合同，并记录：

- 必须在 monolith 停止且 active-write gate 为 0 后迁移；
- 禁止递归 `chown/chmod`；
- `0660` 组写权限不能替代 sticky 目录的 owner 要求；
- 历史拒绝信号不得重放。

### 7.5 运行时发布代码

保留现有 `mkstemp -> fsync -> strict reload -> os.replace -> directory fsync`。
不为权限问题添加静态兜底、直接覆盖、重试风暴或特权能力。

## 8. 生产切换设计

实施分四个独立授权阶段。

### 阶段 1：本地 RED→GREEN

- 写失败测试；
- 实现 helper、sanitizer 分类、unit 和文档；
- 增加监控与门禁；
- 运行 focused tests 和一次最终完整套件；
- 只创建本地候选，不推送、不部署。

### 阶段 2：候选集成

- 重新验证精确 SHA 和变更路径；
- 只显式暂存相关文件，禁止 `git add -A`；
- 经单独批准后非强制推送；
- 不修改生产。

### 阶段 3：冻结状态下部署

部署前只读证明：

- 精确生产 SHA、干净工作树、monitor expected HEAD；
- worker/Web/ingest active，monolith inactive；
- Telegram session 仅由 ingest 持有；
- WAL、`quick_check=ok`；
- active write、claimed job、active management、worker command、revision claim 均为 0；
- Deepcoin 仓位、普通委托、trigger/TPSL 和历史查询完整。

门禁通过后，经独立生产授权：

1. 记录原始设置和最大 `raw_message_id`；
2. 将全局自动入场设为 false 并立即验证；
3. 等待所有在途执行归零；
4. 使用精确 SHA 部署，最多一次计划内 split-runtime 重启；
5. 由 `ExecStartPre` 迁移缓存 owner；
6. worker 启动后立即运行现有后台刷新；
7. 验证 owner、freshness、TTL、摘要、Deepcoin 只读连通性和队列健康；
8. 保持自动入场关闭，等待恢复授权。

冻结期间到达的信号正常识别并明确记录为自动交易关闭，不积压、不补单。

### 阶段 4：显式恢复未来自动入场

只有所有者再次批准后：

- 重跑全部只读门禁；
- 确认缓存 fresh、worker-owned，恢复水位后没有新的同步拒绝；
- 对照冻结前后交易所状态；
- 恢复 `auto_trade_enabled` 原值；
- 只处理恢复时刻之后的新信号；
- 执行 L2 观察。

## 9. 错误处理与回滚

- 冻结前任一门禁失败：不做生产修改。
- 冻结后任一部署、权限、刷新或健康检查失败：保持自动入场 false。
- 代码异常：治理脚本回滚至部署前精确 SHA。
- 缓存异常：不得切换静态规格，不得放宽 TTL 或校验。
- 未知交易结果、重复处理、SQLite 锁或异常 exchange history：立即停止并保持冻结。
- 代码回滚不自动恢复交易设置。
- owner 迁移是可逆权限变化，但回滚到 monolith 时必须允许 root 重新创建缓存；
  下一次切回 split runtime 时由 helper 再次收敛到 worker owner。

## 10. 监控影响

增加 worker-owned 的只读合约规格健康投影，输出仅包含：

- `state`: `fresh`、`stale` 或 `unavailable`；
- `fetched_at`、`expires_at`、`last_success_at`；
- `last_refresh_succeeded`；
- 有界错误类别；
- owner/group/mode 合同是否满足。

不得输出原始规格 JSON、凭据、签名、异常详情或 Deepcoin 响应体。

监控规则：

- `auto_trade=true` 且规格模式为 `live` 时，缓存非 fresh -> unhealthy；
- 恢复水位后出现新的 `contract_spec_sync_unavailable` -> unhealthy；
- 自动入场冻结时缓存异常 -> `restore_ready=false`，不误报交易安全事故；
- 仍有 fresh 快照但本轮刷新失败 -> 提前预警，不立即废除有效快照。

如需扩展现有 monitor adapter 闭集，capture、projection、evaluator、HTTP allowlist、
systemd 参数和测试必须同一候选同步更新。

## 11. 门禁影响

新增只读门禁：

- 目标类型、链接数、owner、group、mode、ACL；
- fresh 时间、TTL、摘要和规格边界；
- Deepcoin 产品查询完整性；
- 恢复水位之后的新同步拒绝数；
- 自动入场恢复前的 active-write/queue/management/command 零状态。

这些门禁只会加强安全合同。现有 fail-closed、唯一 worker 写入、交易所写入归属、
session 隔离和部署 exact-SHA 门禁均保持不变。

## 12. 测试设计

严格 RED→GREEN：

1. 先证明现有 sanitizer 会错误地把缓存设为 root；
2. 证明新 helper 迁移为 worker owner，且不改变 session 权限；
3. 覆盖缺失目标、root/worker owner、未知 owner、符号链接、硬链接、目录和幂等性；
4. 增加 Linux 跨 UID sticky 目录测试：修复前 worker replace 失败，修复后成功；
5. 验证发布后新 inode 仍是 worker-owned、`0660` 且无临时文件；
6. 覆盖监控 fresh/stale/unavailable、冻结状态、owner 漂移、新拒绝和错误有界性；
7. 验证 helper 在 worker 启动前执行；
8. 验证失败回滚后自动入场仍为 false；
9. 运行相关 focused tests；
10. 最终候选运行一次完整测试套件。

## 13. 生产验收

冻结部署和恢复自动入场分别按 L2 观察：

- 30 分钟且至少 5 条自然消息，尽量覆盖 2 个群；
- 流量不足时到期停止，不无限延长，阶段保持 `in_progress`；
- 不制造 Telegram 消息或测试订单；
- worker/Web/ingest PID 与 `NRestarts` 稳定；
- 队列无 missing、orphan、duplicate、stuck；
- 无新增 `SQLITE_BUSY`、未知交易结果或异常 exchange history；
- sanitizer 重跑及一次计划内 worker 重启后 owner 仍正确；
- 恢复水位之后没有新的规格同步拒绝。

## 14. 成功标准

修复完成需要同时满足：

1. worker 能持续原子发布 fresh 缓存；
2. 重启、sanitizer 和 split/monolith 转换不会再次造成 owner 漂移；
3. 自动入场开启时监控能及时发现缓存不可用；
4. 所有原有交易语义和 fail-closed 边界不变；
5. 未来新信号恢复正常规格检查；
6. 历史拒绝信号保持原终态，零重放、零补单。
