# 历史 monitor 回滚兼容性反证与容量门禁评估

## 结论与边界

2026-09-05T06:18:42Z 的只读验证推翻了“清除 monitor 专用 env 键后，
`0de19c1c` / `5aa7ca07` 必然无法回滚”的判断。按所有者停止条件，
**保留 digest 迁移规范化，不修改生产代码，不把两个旧 release 标记为已退役。**

本次没有执行 stage、dry-run、激活、服务控制、数据库访问或交易所调用；
没有创建授权，没有删除备份。本报告不等同于完整回滚演练或对未来激活的授权。
代码未改，因此未进入 RED/GREEN、完整套件及代码独立评审阶段。

## 1. 旧模板依赖不等于当前实际启动依赖

两个 release 的 `deploy/systemd/telegram-kol-monitor.service:14`、
`telegram-kol-monitor-diagnostic.service:14`、
`telegram-kol-monitor-test-notification.service:14` 确实都包含：

```text
ExecStart=/usr/bin/env PYTHONPATH=${TELEGRAM_KOL_MONITOR_RELEASE_PATH}/src ...
```

但是当前生产 `systemctl show` 返回的三个有效主 ExecStart 均直接执行
`/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research monitor-production-safety ...`，
没有 `/usr/bin/env` 覆盖前缀。三个 FragmentPath 均在 `/etc/systemd/system/`，
EnvironmentFiles 均为 `/etc/telegram-kol-monitor.env`，
DropInPaths 均为各自的 `10-telegram-kol-release.conf`。

env 文件 mtime 为 `2026-09-04T11:18:03.794212885Z`；本次仅筛选身份键读取，
未发现 release 身份键或 PYTHONPATH 赋值，未输出其他配置或凭据。
当前 monitor 的有效通用 PYTHONPATH 为
`/opt/telegram-kol-releases/9501a5f39f0c5f196cc29f24f3e3b8786267126b/src`。

标准控制器（`9501a5f3` 的 `scoped_release_activation.py`）的回滚分支
`1214–1259` 调用 `publish_component_release_dropins()`、daemon-reload、启动与验证。
`render_release_dropin():712–736` 写入通用 PYTHONPATH/COMMIT/MANIFEST；
`publish_component_release_dropins():784–802` 只替换 release drop-in。
**此路径不重装旧 release 自带的 canonical unit，也不恢复旧 env 文件。**
因此不能把旧 unit 模板的启动行为套用到当前标准回滚路径。

两个旧 release 的 `runtime_deployment_identity.py:324–360` 从实际 module 路径和
manifest 校验身份；`:459–462` 读取通用 `TELEGRAM_KOL_RELEASE_COMMIT` /
`TELEGRAM_KOL_RELEASE_MANIFEST_SHA256`。该身份逻辑并不要求 monitor 专用键。
对两个旧 release 的 `src/` 搜索专用键，只命中旧激活器生成 drop-in 的代码
`scoped_release_activation.py:611–614`，不是 monitor 主诊断导入旧代码的必要条件。

## 2. 可区分假设的只读验证

使用生产 `9501a5f3` 的既有 `validate_release(..., expected_uid=0)` 和
`SystemRuntimeAdapter.prove_monitor_candidate_release()`，分别把两个旧 release
作为待验证导入目标。后者读取真实 systemctl 有效配置与 env 文件，
仅在内存中计算标准 drop-in 将提供的通用环境，不发布文件、不控制服务。

| 目标 commit | manifest SHA-256 | 全树校验 | 三个主 ExecStart 的前瞻导入证明 |
|---|---|---|---|
| `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | 通过 | 通过，均导入该 release 的 `/src` |
| `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` | `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8` | 通过 | 通过，均导入该 release 的 `/src` |

再以 `runuser -u telegram-kol-monitor` 启动一次性 `python -B`，仅设置通用
PYTHONPATH/COMMIT/MANIFEST（不提供任何 monitor 专用 release 键），导入各自旧版
`runtime_deployment_identity`，调用其 `_loaded_release_evidence()`：

- 两次进程退出码均为 0，stderr 均为空。
- 实际 `module.__file__` 分别位于对应旧 release 的 `src/telegram_kol_research/`。
- 两次返回值均为 `(True, 对应完整 commit, 对应完整 manifest SHA-256)`。
- 全部 release 导入使用 `python -B` / `PYTHONDONTWRITEBYTECODE=1`；结束复查这两个
  release 的 `__pycache__` / `.pyc` 均为零。

这不是完整 systemd sandbox/业务 diagnostic 演练，不能证明任意未来回滚必定成功；
但足以反驳“缺专用键必然导入失败”。现有规范化下的身份兼容路径仍存在，
不能据此取消历史目标资格。若将来要主动收窄支持的回滚集合，应由所有者另行决定，
不能记录为本次已发现的不可用事实。

## 3. 其他状态与锁口径

更新锁误判已在前次报告和已知问题清单更正：文件存在不代表 flock 被持有；
前次标准锁函数可获取、释放、再次获取，且有 finally 关闭描述符。
本次未删除锁文件，也未重新执行释放动作。

`2026-09-05T06:19:08Z` 实测 timer 为 `active` / `enabled`；主 service 为
`ActiveState=failed, Result=exit-code, ExecMainStatus=1`。
此前 systemctl 输出显示最近自然运行在 `06:01:33–06:01:40Z`，ExecStartPre 成功、主命令退出 1。
本轮没有运行 diagnostic，也没有重置 failed 或控制服务；其业务原因不在本次调查范围，
不得把它解释为旧 release 导入验证失败。

## 4. 部署容量门禁：单独评估，未实现

建议优先独立安排这项预防措施，不等再次满盘再处理。与备份删除分开授权。
前次盘点为 54 份、21.326 GiB，其中仅四份前序与后继字节相同，合计 0.787 GiB；
本轮沿用该清单、不重做哈希、不删除。2026-09-05T06:19:08Z 根分区实测可用
`12,475,150,336` 字节（约 11.618 GiB），使用率 77%。

### 建议准入契约

在目标文件系统上计算：

```text
可用字节 - 本操作尚未分配的峰值 - 并发 monitor 尚未分配峰值
         - WAL/其他并发增长预算 >= 8 GiB
```

- 20% 余量告警与 8 GiB 操作后保留门禁分开：前者是预警，后者阻止新增存储压力。
- 以 `statvfs` 的调用者可用空间为准，逐文件系统核算，不能只看根目录百分比。
- 已占用空间已被可用值扣除，不再重复扣减；只预留尚未分配的并发峰值。
- 按历史 monitor 峰值 3.30 GiB 加一次约 850 MB 库副本粗算，本次余量在不计
  WAL 增长前也会降至约 7.5 GiB，低于拟议 8 GiB 门槛。这是建议门槛下的计算，
  不是当前已有门禁拒绝，也不是必然会满盘的预测。
- 估算缺失、读取失败或无法界定峰值应拒绝开始本次部署存储动作并报告预算缺项，
  不把未知当零；明确 inode 耗尽的独立拒绝条件。

### 放置与失败语义

1. 标准部署服务端为强制执行点，客户端预检只做提前反馈；覆盖 stage、独立备份/
   schema 工具和激活预检，不能只在 PowerShell 客户端加一个提示。
2. stage 在 `deploy/telegram-kol-stage` 创建临时 repository/archive/release
   之前检查（当前临时目录建立约 `592` 行）；预算包含这些同时存在的副本。
3. 激活在**首次冻结之前**且消费授权之前检查，不能等服务已停才拒绝。
   现有 `scoped_release_activation.py:1141–1164` 的 dry-run 返回、授权消费边界
   是需要覆盖的控制点；还必须审计 helper 外层是否更早冻结，防止放错位置。
4. 回滚与解冻不得被“下一次新部署容量不足”的准入门禁卡住；其最小落盘预算
   必须提前保留。事故恢复若连这部分空间也不足，应明确报告，不得绕过身份验证。
5. 不在冻结窗口做压缩/清理/长扫描。检查不是空间预留，并发自然写入仍可消耗余量；
   一次静态检查不能宣称彻底防满盘，后续仍需存储监测与独立卷规划。

建议作为部署安全检查的独立代码任务，重点测试临界字节数、不同挂载点、
已有副本不重复计费、并发预算、统计失败、inode 不足、dry-run 与真实执行一致，
以及拒绝发生在冻结/授权消费之前、不会阻止必要回滚解冻。
这是准入逻辑设计评估，不改变识别、交易或备份保留数据；无需 schema 变更。
实现应聚焦测试、最终完整套件与独立安全评审，真实部署另行授权。
