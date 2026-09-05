# 部署收尾核查：锁、digest 迁移豁免与备份保留

核查时间：2026-09-05 05:29 UTC 起。基线 `f4c3b618d277e2881e4216f9dd13d4d8d85a87e9`。
文档提交基于远端集成分支 `9501a5f39f0c5f196cc29f24f3e3b8786267126b` 的隔离工作区；共享工作区分支未切换。
本轮无 stage、activation、服务控制、schema 或业务数据变更，未删除任何生产文件。
工作区原有未跟踪文件 `docs/2026-09-05-pending-trigger-protection-intents-read-only-diagnosis.md` 保留，不纳入本次提交。

## 1. 更新锁：没有持锁残留

`/run/telegram-kol-update.lock` 为 root:root、0644、0 字节，inode 547439；
mtime 为 `2026-09-04T12:26:34.936759064Z`（服务器显示 20:26:34 +08:00）。
lslocks 未列出该路径，fuser 无打开者，未发现正在运行的 stage/activate/server_git_update 进程。
进程检查中的 bash/pgrep 为本次查询自身，不是部署进程。

通过运行 release 的 `exclusive_runtime_control_lock()` 做非阻塞获取，成功后退出 context，再次获取也成功。
结束时 descriptor 已关闭，inode/mtime 未变，保留空文件。执行用 python -B 并设置 PYTHONDONTWRITEBYTECODE=1，
未运行 activation 入口、未生成或消费授权。

代码 `scoped_release_activation.py:102–142` 已有 `finally: os.close(descriptor)`。
这是 flock 文件描述符锁；文件存在和 mtime 不等于持锁。没有证据支持“缺少 finally”或“异常退出留下内核锁”。
本轮无需强制释放或 unlink；unlink 一个部署锁文件反而可能制造两个 inode 上的不同锁。
这项以“已证明部署锁可用”收口，不登记为软件故障。

## 2. Digest：当前安装已迁移，历史回滚兼容仍有依赖

三个已安装 service 的旧前缀命中均为 false：

| /etc/systemd/system/ 下文件 | SHA-256 |
|---|---|
| telegram-kol-monitor.service | 5ba0f4784de7a697a706dd306d16fdc5cb5be4f3cb700013ba6229cda4569289 |
| telegram-kol-monitor-diagnostic.service | 47d98856a8afc636f93063c21add7e13728469fc268a56ef559cad38859744db |
| telegram-kol-monitor-test-notification.service | f016e05348802f7951a1195255e39e2ef7501781bfb9c022e3fcbe66bb564746 |

枚举 38 个保留 immutable release；逐个计算现有 normalized digest 和去掉规范化后的 strict digest，
没有修改 release 或激活器。关键结果：

| release | 旧前缀 | normalized digest | strict digest |
|---|---|---|---|
| 9501a5f3 当前运行 | 无 | 07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b | 同左 |
| 877fbc33 最近一次 activation 的绑定 rollback | 无 | 07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b | 同左 |
| 6a493d15 前一代四角色 release | 无 | 07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b | 同左 |
| 0de19c1c 历史四角色 rollback | 三个 unit 均有 | 07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b | 812e87daf719c8d52d7ac2880c507f56c60706b5d8e074fba477fd60477a8304 |
| 5aa7ca07 历史 Web rollback | 三个 unit 均有 | 07355cd28d3fe44875c2e120339cdc0a202d033269335d5db7a21f7c83cd222b | 812e87daf719c8d52d7ac2880c507f56c60706b5d8e074fba477fd60477a8304 |

以上五个 release 均又通过真实 `validate_release()` 全树校验；不是用 manifest 单文件自检代替。
0de19c1c manifest 为 `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1`；
5aa7ca07 manifest 为 `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8`。

当前三个 HTTP 身份分别为 web PID 1525321、ingest PID 1525328、worker PID 1525316，
均运行 9501a5f3、manifest `2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`、verified=true。
monitor 以已有成功部署记录和本轮 systemctl 有效配置为上下文，本轮没有启动新的 diagnostic。
最近激活授权记录：
`/var/lib/telegram-kol-cutover-evidence/9501a5f39f0c5f196cc29f24f3e3b8786267126b/geometry-activation-20260904/activate.json`
绑定四角色 rollback 全为 877fbc33。

**结论：仅考虑当前及最近两代回滚，删除规范化不会造成 digest 差异；但不能把这等同于所有保留历史回滚目标均安全。**
0de19c1c、5aa7ca07 曾被明确授权并实证为 rollback，仍通过完整性校验，未找到明确退役记录。
移除规范化会取消它们与新式 release 的 digest 兼容性。
历史 release 通过完整性校验不意味着现在可直接激活；per-role observed 身份与其余激活门禁仍必须全部满足。

依照本轮“任何仍可能用作 rollback 的 release 因此失败就停止”门禁，暂停本项修改。
需所有者明确保留回滚集合（例如仅 9501a5f3/877fbc33/6a493d15）并确认旧集合退役后再继续。
这不要求删除旧 release，只是不能自行改变其回滚资格。
未移除规范化、未将已知问题标记为已处理；没有生产代码候选，因此本轮未运行完整 pytest 或进行代码独立评审。

## 3. 备份盘点与待批准清单

机器可读完整清单见 `2026-09-05-deployment-closeout-backup-inventory.json`。
范围覆盖 cutover-evidence、maintenance-evidence、monitor、data/evidence、data/backups，
并补充 data 根目录 research.db.*；排除运行中的 research.db/WAL/SHM。
对 >=1 MiB 文件检查 SQLite magic，对压缩归档另查；额外小文件扫描只发现一个 0 字节历史占位 DB，
已单独记录，未计作有效备份。原始消息和交易正文未提取。

共 **53 份未压缩 SQLite 备份 + 1 份 zstd 备份，22,898,968,730 字节（21.326 GiB）**。
每份记录完整路径、大小、UTC mtime、现算 SHA-256、哈希前后 stat 稳定性和分类。
使用低 CPU/I/O 优先级流式读取，没有复制生产库，没有对备份执行修改性 PRAGMA。

目录占用是整个目录的 allocated bytes，不等于备份文件 logical bytes：

| 目录 | allocated bytes |
|---|---:|
| /var/lib/telegram-kol-cutover-evidence | 14200623104 |
| /var/lib/telegram-kol-maintenance-evidence | 2550759424 |
| /var/lib/telegram-kol-monitor | 40960 |
| /opt/telegram-kol-analyzer/data/evidence | 4363862016 |
| /opt/telegram-kol-analyzer/data/backups | 1819607040 |

### 必须保留与分类口径

- 最新 schema 动作真正绑定的生产回滚备份是：
  `/var/lib/telegram-kol-cutover-evidence/392a74730d5406d23e2080324e472fcdfdb1ea67/recognition-execution-production-schema-20260903T050629Z/pre-recognition-execution-production-schema.db.zst`。
  现算 compressed SHA-256 为 `b765ae2585ef92cd65d705e2d2536de706c7dd86cbc36b931a324683b0dedca7`，与 backup-summary.json 一致，
  压缩大小 56,150,170 字节。原始 853,946,368 字节，原始/当时解压 SHA 均为
  `e5865d6e370396b664fa7c814db20eab56c1f4544058b3f5593711aa503cd0dc`。
  当时 quick_check=ok、FK 0、前后计数证明保存在同目录；本轮只复验压缩哈希，未重复解压或谎称重新跑过 quick_check。
- 所有者之前明确要求保留的
  `.../recognition-execution-lease-rehearsal-20260903T044159Z/pre-recognition-execution-schema.db`
  为演练备份（853,778,432 字节），与上面的正式生产 schema 备份不是同一个文件；本轮也继续保留。
- 最近 877fbc33 和 9501a5f3 的运行时激活无 schema/data 动作，当前 rollback 是代码 release，
  不能为了“每次部署两代备份”随意把某份旧库认作这次运行时回滚必需品。
- 其余唯一历史备份分别属于历史数据修复、schema 演练或前置核查；“不属于当前 runtime rollback”
  不自动等于“失去历史审计价值”。未证实可被同内容后继替代者全部保留，后续可评估验证后归档。
- 4 个字节完全相同的前序可列入待批准删除清单，保留后继即可保留同一数据库快照。
  合计 **844,906,496 字节（0.787 GiB）**。本轮删除量为 **0**。
  若文件系统存在共享块，实际释放量仍以未来获批执行后的 df 为准。

### 待批准 1

- 前序：`/var/lib/telegram-kol-cutover-evidence/21314fc44fd4f7a05d3bbbd4842e73a825523fee/attempt-2/transaction-backup.db`
- 大小：805208064 字节；mtime UTC：2026-08-30T08:10:33.088535+00:00。
- SHA-256：`eb7241a70b3bb66868e819108240da426c904d746b12b3450cb32148d15e09af`。
- 保留后继：`/var/lib/telegram-kol-cutover-evidence/7af12a535a786d33c1338e4f6d41d66aff088618/attempt-3/transaction-backup.db`
- 后继 mtime UTC：2026-08-30T08:31:15.397376+00:00；大小与 SHA-256 与前序完全相同。
- 当场验证：两文件均可读、哈希期间 stat 稳定、没有非空 WAL，fuser 未发现打开者。只申请删除前序文件，保留后继及全部原部署日志/摘要；原路径的审计引用须由本清单保留映射。

### 待批准 2

- 前序：`/opt/telegram-kol-analyzer/data/research.db.bak-before-chenge-correction-20260622234903`
- 大小：8015872 字节；mtime UTC：2026-06-22T15:50:38.520784+00:00。
- SHA-256：`1863055a6a3d8b3786bd476b2c2017341c7d2d1a2ba3638c1c70353f87d806bf`。
- 保留后继：`/opt/telegram-kol-analyzer/data/research.db.bak-before-chenge-correction-20260622235053`
- 后继 mtime UTC：2026-06-22T15:50:57.432739+00:00；大小与 SHA-256 与前序完全相同。
- 当场验证：两文件均可读、哈希期间 stat 稳定、没有非空 WAL，fuser 未发现打开者。只申请删除前序文件，保留后继及全部原部署日志/摘要；原路径的审计引用须由本清单保留映射。

### 待批准 3

- 前序：`/opt/telegram-kol-analyzer/data/research.db.bak-before-ouyang-entered-at-fix-20260623003450`
- 大小：8085504 字节；mtime UTC：2026-06-22T16:34:55.644733+00:00。
- SHA-256：`d061b5c46112a0bdccfbc3f5625cfcf252e9bb638cfb5b6e58c886b1525434bd`。
- 保留后继：`/opt/telegram-kol-analyzer/data/research.db.bak-before-ouyang-protective-exit-20260623004223`
- 后继 mtime UTC：2026-06-22T16:42:27.683604+00:00；大小与 SHA-256 与前序完全相同。
- 当场验证：两文件均可读、哈希期间 stat 稳定、没有非空 WAL，fuser 未发现打开者。只申请删除前序文件，保留后继及全部原部署日志/摘要；原路径的审计引用须由本清单保留映射。

### 待批准 4

- 前序：`/opt/telegram-kol-analyzer/data/research.db.pre-partial-close-reconcile-20260717-200226.bak`
- 大小：23597056 字节；mtime UTC：2026-07-17T12:02:26.191298+00:00。
- SHA-256：`f2b6633248db709234796e8278b3e6902c0f5b077595b27071fe11845f80bc92`。
- 保留后继：`/opt/telegram-kol-analyzer/data/research.db.pre-audit-recovery-reconcile-20260717-200632.bak`
- 后继 mtime UTC：2026-07-17T12:06:32.658705+00:00；大小与 SHA-256 与前序完全相同。
- 当场验证：两文件均可读、哈希期间 stat 稳定、没有非空 WAL，fuser 未发现打开者。只申请删除前序文件，保留后继及全部原部署日志/摘要；原路径的审计引用须由本清单保留映射。

## 4. 可执行的保留机制方案（本轮不实现）

推荐独立的“盘点/生成计划/按审核计划执行”工具作为核心，先不自动删除：

1. 清单字段固定为 operation ID/type、backup role、原始与压缩大小/hash、source snapshot 时间、
   quick_check/FK/counts 证据、successor ID、pin 原因、owner-approved expiry、inode/mtime、是否存在 WAL、归档位置。
   删除操作必须绑定清单 hash 和精确路径，执行前再验证 inode/hash/后继可读性；状态不全则跳过。
2. 本机热备保留最近 **2 代已经验证的完整生产库**；当前 schema/未关闭数据修复/事故调查备份额外 pin，
   不因代数或天数自动解除。schema-only 只作为附加证据，不能代替整库。
3. 超出热备代数的完整库在操作关闭后 24 小时进入归档计划，48 小时内转独立卷/远端；
   zstd 建议低并发、低优先级。原件删除前验证压缩 hash、完整解压 hash 与 quick_check/FK；
   解压校验目录必须在容量预算内，最好置于独立卷。保留引用清单与文本证据。
   远端普通历史归档建议至少 30 天；事故/schema pin 持续到所有者关闭，不按 30 天自动删除。
4. 本机备份及归档预算建议 **8 GiB**，其中普通压缩历史归档不超过 **4 GiB**；
   超限进入“需归档/阻止新增大副本”的状态，不能通过删除 pin 来强行达标。
   20% 空闲预警；每次大备份/压缩解压操作前，必须证明
   `可用空间 - 本操作峰值增量 - 同期 monitor 峰值 - WAL/其他并发预留 >= 8 GiB`。
   如果已经在峰值中，避免重复扣算同一已分配空间。容量检查失败时保持服务运行，只拒绝该存储操作并报告。
5. 本轮初始空闲 12,483,362,816 字节（约 11.626 GiB）；仅扣 3.30 GiB monitor 和 850 MB 新备份，
   就低于 8 GiB，尚未包括 WAL 预留。4 份重复副本获批后也只能小幅改善，不能据此宣称完成长期容量整改。
6. 当前 lsblk 只有根卷 vda1，没有已挂载的独立数据卷。因此“把 monitor 临时副本移到另一个目录”
   本身不释放根卷容量。需要单独卷/远端存储与挂载验证；不要用 tmpfs 转移成内存/OOM 风险。
   先量化 monitor 各阶段需要哪些并发副本，后续可设计顺序复用同一只读快照或减少同时存在的副本，
   但必须保留快照一致性和完整性验证，不可直接删除审计步骤。本轮未修改 monitor。

| 机制 | 实现代价（粗估） | 风险与定位 |
|---|---|---|
| 独立清单/计划/执行脚本 | 中；约 2–4 工程日，含路径、pin、hash/ABA、容量、失败恢复与测试 | 推荐先做。dry-run 默认，无批准计划不可删除；备份可追溯，执行不耦合交易服务 |
| systemd timer | 在上述工具之上再约 0.5–1 日 | 初期只做每日盘点/预警；自动删除或并行压缩容易与自然 monitor 审计争空间，需单独授权 |
| 部署流程内自动归档 | 约 2–4 日，需激活/失败/回滚路径回归 | 适合仅加入容量门禁；不要在入场冻结窗口内压缩/清理。会扩大部署安全关键路径，不作为首选 |
| 独立卷与 monitor 临时目录迁移 | 基础设施与运行时配置另案 | 实际解除根卷竞争；需要挂载存在性、权限/sandbox、容量监测与回滚验证，不在本轮范围 |

实施顺序建议为：精确批准重复副本清单 → 独立保留工具与容量门禁 → 验证后归档旧唯一备份 →
独立卷/monitor 临时副本整改。今天仅交付计划，没有实施任何机制。

## 验证与交付边界

- 锁只调用标准 context manager，没有服务控制。
- 摘要探针对比和 release 校验只读，全部 release import 使用 python -B/PYTHONDONTWRITEBYTECODE=1。
- 备份流式读取时均保持 stat 稳定，4 对精确重复文件均可读且无非空 WAL、无打开进程。
- 只新增本报告及 JSON 清单，并补充已知问题的门禁事实；不改生产代码。
- 第二项停在所有者明确设置的回滚兼容门禁，未进入 RED/GREEN、完整套件或独立代码评审阶段，
  不把“只读核查通过”写成“规范化已移除”。

