# Recognition execution 四组件激活只读门禁停止记录

日期：2026-09-03（UTC）

候选：`392a74730d5406d23e2080324e472fcdfdb1ea67`

请求组件：`web`、`monitor`、`ingest`、`worker`

请求 rollback：`0de19c1cbb2089fd58b8940d9b01a65096f9a063`

## 结论

四组件激活没有执行。只读门禁确认 `0de19c1c...` immutable release 本身完整有效，
但现有标准激活器不能从当前分角色版本状态，以该 commit 直接执行四组件激活。

`activate_release()` 在任何服务控制之前调用：

```text
prove_release_runtime(
    expected_releases={role: rollback for role in affected_runtime_roles},
    components=components,
    require_authority=True,
)
```

当 authority component 存在时，`prove_release_runtime()` 固定核验 `web`、`ingest`、`worker`，
并要求每个角色当前身份都匹配同一个 rollback release。当前 Web 实测为 `5aa7ca07...`，
不等于请求的 `0de19c1c...`；使用真实 `SystemRuntimeAdapter` 和候选代码执行一次只读 preflight，
结果为 `ActivationError("runtime identity proof failed")`。

因此，虽然 `0de19c1c...` 是有效的四组件历史 release，它不能作为当前 split-runtime 状态下
这次标准激活调用的统一 pre-state。按照门禁要求，本轮在此停止；没有尝试绕过激活器。

## 实测身份

身份来自三个角色各自的 `/api/runtime/deployment-identity`，并由激活器的
`SystemRuntimeAdapter.runtime_identity()` 交叉核验 systemd PID、进程 start ticks、cwd 与命令角色。

| 角色 | release commit | manifest SHA-256 | PID | verified | entry frozen |
|---|---|---|---:|---|---|
| web | `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` | `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8` | 1396631 | true | false |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | 3315585 | true | false |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | 3315574 | true | false |

三服务均为 `active/running`、`NRestarts=0`，PID 与前一轮一致。

## Rollback release 独立校验

候选 release 内实际激活器的 `validate_release()` 接受了
`0de19c1cbb2089fd58b8940d9b01a65096f9a063`：

- manifest SHA-256：
  `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1`
- 全树 content SHA-256：
  `17f4476b2127340653df653b44a19c78468172e41e2563d18d58f51bd6ade120`
- ownership、mode、manifest、stage receipt 与完整内容摘要均通过校验。

失败点不是 rollback release 污染，而是当前 Web runtime identity 不等于激活器为所有受影响角色
统一要求的 rollback identity。

## Schema 与存量边界

只读数据库核对结果：

| 对象 | 行数 |
|---|---:|
| `authoritative_execution_attempts` | 0 |
| `entry_assembly_wakeup_executions` | 0 |
| `recognition_execution_scan_cursors` | 0 |
| `recognition_decisions.execution_running` | 29 |
| `recognition_decisions.execution_uncertain` | 0 |

本轮没有重复执行 schema 动作，也没有修改任何 decision、job 或业务数据。

## 停止边界

以下动作均未发生：

- 未创建或消费 activation authorization；
- 未调用 activation helper；
- 未停止、启动或重启任何服务；
- 未切换任何 drop-in 或 runtime release；
- 未开始 L2 观察；
- 未执行任何交易所写操作。

root-owned mode-0600 证据：

`/var/lib/telegram-kol-cutover-evidence/392a74730d5406d23e2080324e472fcdfdb1ea67/recognition-execution-four-component-preflight-20260903T062647Z/preflight-stop.json`

SHA-256：`67877dc3961128a0786dd493869cc34dff6ee3e6cff5a70ce7b619e84a9c10c8`

## 后续边界

不能直接把 Web 单独激活到现有 `0de19c1c...` release 来满足 uniform rollback precondition：
该 release 的 immutable stage manifest 本身绑定 `web`、`monitor`、`ingest`、`worker` 四组件，
而激活器要求 activation declaration 与 stage declaration 完全相同。

因此，在“不修改、不绕过现有激活器”、不手工控制服务且只使用现有 immutable release 的约束下，
目前没有已证明可行的路径把 split-runtime pre-state 收敛成统一 `0de19c1c...` 后再激活候选。
后续需要单独设计并授权一个符合现有安全语义的收敛步骤；本文不猜测或执行该步骤。
