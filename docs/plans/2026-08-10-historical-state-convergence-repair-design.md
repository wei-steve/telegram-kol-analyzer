# 历史状态收敛与安全修复设计

## 背景

生产库存在三类长期不收敛的状态：

- 23 条 Telegram 源消息删除退出任务停在 `cancelling_entries`，尝试次数持续增长。
- 1 条已被 Deepcoin 明确拒绝的止盈提交被标记为 `submit_unknown`。
- 13 条已平仓历史仓位的止盈收敛记录仍为 `submitted`，其下 23 条止盈台账仍为 `active`。

这些状态并不代表交易所仍有在途操作。只读 Deepcoin 快照已证明：相关历史委托均不在当前委托中，相关历史仓位均不在当前持仓中。当前唯一活跃 BTC 仓位的收敛记录必须排除在修复范围之外。

## 根因

### 1. 源消息删除退出任务

`_bind_deletion_event_in_session` 只要找到归档原始消息，就会把退出任务从 `unbound` 推进为 `pending`，即使该消息没有策略生命周期、执行绑定或任何交易证据。这与原设计中“缺少策略关联应作为 `non_strategy_or_unlinked` 忽略”冲突。

工作器在同一事务中更新状态并插入通知事件。兼容迁移在生产 SQLite 上创建的是部分唯一索引，但插入语句使用了指定列的 `ON CONFLICT`；SQLite 无法将它匹配到该部分索引，导致状态转换整体回滚。测试使用全量唯一索引，因此没有复现生产差异。同时轮询循环吞掉了异常，导致问题只表现为无限重试。

### 2. 止盈提交结果分类

止盈收敛执行器对提交异常统一标记为 `convergence_submit_unknown`。但下层网关已经能区分：

- `DeepcoinDefiniteRejection`：交易所明确拒绝，结果已知。
- `DeepcoinRequestOutcomeUnknown`：超时等原因导致结果不确定。

执行器没有保留这一区分，所以把明确拒单错标成未知。

### 3. 历史止盈台账终态判定

对账器要求收敛记录的 `pos_id` 仍存在执行绑定的 `pos_id` 字段中。但执行绑定进入终态后会有意清空该字段。因此，即使入场腿已终态、仓位已消失、委托也已消失，历史记录仍永远无法完成。

## 目标与不变量

修复后必须满足：

- 保留所有历史行和审计链，不物理删除数据。
- 历史修复过程不对 Deepcoin 发起任何下单、撤单或平仓写操作。
- 仅在本地身份链完整、仓位快照完整、委托快照完整，且精确 `pos_id`/订单均不存在时终态化。
- 任何歧义、快照错误或当前活跃仓位都使计划拒绝应用。
- 应用前后当前 Deepcoin 持仓和委托集合完全一致。
- 重复干跑为零动作；旧指纹、旧确认令牌或动作数不匹配必须拒绝。

## 运行时修复

### 删除退出收敛

- 模型索引元数据与生产兼容迁移统一为部分唯一索引。
- 通知事件插入使用不指定冲突目标的 `ON CONFLICT DO NOTHING`，兼容已有的全量和部分索引。
- 无策略生命周期、无执行绑定、无交易证据的源消息直接收敛为 `succeeded/non_strategy_or_unlinked`，源事件为 `ignored`，不创建交易操作或通知。
- 生命周期、绑定和入场腿均已终态的任务进入只读对账路径，不再请求撤单或平仓。
- 轮询循环保留取消异常语义，并对其他异常记录完整堆栈，不再静默吞掉。

### 止盈收敛

- 明确拒单标记为 `conflicted/convergence_submit_rejected`。
- 超时或返回结果不可确定仍保留 `submit_unknown/convergence_submit_unknown`。
- 对活跃绑定仍要求 `binding.pos_id` 强一致；只有绑定已终态时，才允许使用不可变的收敛记录与入场腿 `pos_id` 完成身份证明。
- 对“明确拒单且从未创建止盈订单”的记录，仅在精确入场腿已终态、持仓快照完整且精确仓位不存在时，收敛为 `completed/convergence_submit_rejected_position_terminal`。

## 历史修复工具

新增一个默认只读的 CLI。它首先从数据库和 Deepcoin 只读 API 构建修复计划，计划包含：

- 每一条候选行的本地身份证据、交易所不存在证据、目标状态和拒绝原因。
- 数据库快照指纹、交易所快照指纹和总计划指纹。
- 预期动作数、显式排除的活跃收敛 ID，以及任何阻断性冲突。

应用模式必须同时提供 `--apply`、刚才干跑输出的预期指纹、预期动作数和一次性确认令牌。工具会在同一进程内重新加载快照并重建计划；任一输入不一致就拒绝。应用只修改本地状态和证据，并写入一条 `notification_status=not_needed` 的审计摘要事件，不产生逐行通知风暴。

## 历史数据的目标收敛

- 无策略关联的 21 条删除退出：`succeeded/non_strategy_or_unlinked`，源事件 `ignored`。
- 已过期且无执行证据的 1 条：`succeeded/strategy_terminal_without_execution`。
- 生命周期、绑定、入场腿均终态且精确委托/仓位均不存在的 1 条：`succeeded/strategy_already_terminal`，保存精确空仓证据。
- 13 条历史 `submitted` 止盈收敛：`completed/convergence_position_terminal`；其 23 条活跃止盈台账为 `expired`，增加终态化证据。
- 1 条明确拒单且仓位已终态的收敛：`completed/convergence_submit_rejected_position_terminal`。
- 当前活跃仓位的收敛记录保持原状态，并在计划中显式标记为排除项。

## 部署与回滚

1. 使用只读生产快照证明没有时敏交易操作，并记录当前持仓/委托指纹。
2. 停止 `telegram-kol.service`，防止工作器与修复事务竞争。
3. 使用 SQLite 在线备份机制创建带时间戳的数据库备份，验证备份完整性。
4. 从 GitHub 拉取经审查提交，重新安装可编辑包，运行服务器测试。
5. 运行历史修复干跑，核对动作数、指纹、排除的当前活跃仓位。
6. 使用相同指纹、动作数和确认令牌应用；紧接着重新干跑，必须为零动作。
7. 比对 Deepcoin 前后快照，核对数据库收敛数量和审计事件。
8. 启动服务，检查日志、监控、当前仓位与保护单不变。

如应用或启动前验证失败，保持服务停止，从已验证备份恢复数据库，必要时将代码回退到上一生产 SHA。

## 验收标准

- `cancelling_entries` 和过期 claim 均为 0。
- 23 条删除退出均终态化且可审计。
- 明确拒单不再被标记为 `submit_unknown`。
- 13 条历史收敛完成，23 条历史止盈台账过期。
- 当前活跃收敛和止盈保护不变。
- 应用后干跑为零动作，交易所前后快照指纹一致。

## 生产操作手册

以下命令只能在安全窗口内使用。所有数据库修复命令必须在 `telegram-kol.service` 已停止后执行。

```bash
systemctl stop telegram-kol.service
systemctl is-active telegram-kol.service
```

使用 Python SQLite backup API 创建带 UTC 时间戳的备份，不使用未锁定的普通文件拷贝。备份后对源库与备份库分别执行 `PRAGMA integrity_check`，两者都必须返回 `ok`。

```bash
python -m telegram_kol_research.cli repair-historical-state-convergence \
  --database-path /opt/telegram-kol-analyzer/data/research.db
```

干跑 JSON 必须同时满足：

- `conflicts` 为空。
- `actions` 只包含本设计列出的历史类别。
- 当前活跃仓位的收敛 ID 出现在 `exclusions`，原因是 `exact_position_or_order_still_live`。
- `exchange_fingerprint` 已与停服前只读快照记录对比。

将同一次干跑输出的值原样填入：

```bash
python -m telegram_kol_research.cli repair-historical-state-convergence \
  --database-path /opt/telegram-kol-analyzer/data/research.db \
  --apply \
  --expected-fingerprint '<64-hex-fingerprint>' \
  --expected-action-count '<exact-action-count>' \
  --confirmation-token '<16-hex-token>'
```

应用成功后立即再次执行干跑；`action_count` 必须为 0，且原指纹必须无法再次应用。然后核对数据库计数、审计事件与 Deepcoin 前后快照，最后启动服务：

```bash
systemctl start telegram-kol.service
systemctl is-active telegram-kol.service
journalctl -u telegram-kol.service --since '10 minutes ago' --no-pager
```

如在启动前任一验证失败，保持服务停止，先对当前库留存事故副本，再用已通过完整性检查的 SQLite 备份恢复原库；恢复后再次执行 `PRAGMA integrity_check`。若代码验证同时失败，将工作树回退到已记录的上一生产 SHA，重新安装后才可启动服务。
