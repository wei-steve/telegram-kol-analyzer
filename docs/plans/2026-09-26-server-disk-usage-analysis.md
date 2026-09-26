# 生产服务器磁盘占用排查与治理方案（2026-09-26）

状态：**阶段一（只读排查）完成；阶段二方案待批准。未删除、移动、截断任何文件，未重启任何服务，未改任何配置。**

采样时间：2026-09-27 02:49～03:00（服务器时间，CST）。生产 HEAD `05f013f1`。
所有 `du` 都用 `nice -n 19 ionice -c3` 跑。生产库只跑了 PRAGMA；按表统计（`dbstat`）跑在
两份**已有的**快照上（`/tmp/research-snapshot-20260916.db`、`/root/phase2-snapshot.db`，均以
`mode=ro&immutable=1` 打开），没有碰生产库。

## 1. 结论

- 磁盘 50 GB，已用 46 GB（91%），剩 4.9 GB。inode 只用了 4%，不是 inode 问题。
- **空间的 60% 以上是历次变更留下的整库副本。** 共 34 份，每份 0.77～1.24 GB，合计约
  **29 GB**，最早 2026-08-30，最近 2026-09-25。没有任何机制退役它们。
- **一直在涨的有三处**：
  1. 历次 L3 变更 / 分析时新做的整库副本：每次 1.2～2.5 GB，阶梯式上涨（这是「经常报满」的直接原因）；
  2. `/var/log/messages`：**从 2026-06-24 起从未轮转过**，已 1.2 GB，每天约 40～57 MB。
     根因是服务器上缺 rsyslog 的 logrotate 配置，而日志量本身又被几条循环刷屏的日志放大；
  3. 生产库 `research.db`：每天约 12.6 MB，几乎全部来自两张只增不删的表。
- 稳态增长 ≈ 45 + 13 ≈ **每天 58 MB（约每月 1.75 GB）**，另加每次 L3 变更的 1.2～2.5 GB 阶梯。
- 磁盘历史（文档记录）：09-10 84%（42 GB 已用）→ 09-19 84% → 09-26 91%（46 GB）。
  09-19 之后多出的 ~3.5 GB 中，09-25 那天就做了两份整库副本（2.36 GB）。
- **一次性清理 C1～C7 约可释放 31 GB**（磁盘从 91% 降到约 30%，剩余约 36 GB）；再加上需要用户判断的 C8，最多约 35 GB。见第 4 节。

## 2. 占用排行

| # | 位置 | 大小 | 内容 / 时间 | 能不能删 | 删的风险 |
|---|---|---|---|---|---|
| 1 | `/var/lib/telegram-kol-cutover-evidence/` | 15 GB | 18 份整库副本（preflight / transaction-backup / rehearsal / pre-*-schema），08-30～09-06；其余是 KB～MB 级 JSON 证据 | 能（整库副本） | 这些副本对应的变更都已关闭；用它们恢复等于抹掉之后 3～4 周的真实交易状态，已无回滚价值。小体积 JSON 证据应保留 |
| 2 | `/var/lib/telegram-kol-maintenance-evidence/` | 8.3 GB | 10 份整库副本（before / rehearsal / research-before），08-31～09-05 | 能（同上） | 同上 |
| 3 | `/root/evidence/` | 2.8 GB | 3 份：`authority-reset`（937M，09-09）、`phase-6-pre-5`（942M，09-10）、`step10a-reclaim/research.db.bak-20260910T032253Z`（949M，09-10） | 能 | 同上；09-10 那次已按「只留最近两份」讨论过 |
| 4 | `/root/research-backup-preA2-20260925T130524Z.db` | 1.18 GB | 09-25 A2 部署 + L3 收口前备份，sha256 已记在 `docs/stale-pending-entry-convergence-status.md:289` | 建议压缩保留一段时间 | 是最近一次 L3 的恢复点 |
| 5 | `/root/phase2-snapshot.db` | 1.18 GB | 09-25 22:03 分析用快照（本次按表统计也用了它） | 能 | 纯分析副本，不是恢复点 |
| 6 | `/tmp/research-snapshot-20260916.db` | 1.1 GB | 09-16 分析快照，`docs/plans/2026-09-18-codex-oncall-remediation-design.md:249` 已记为「待用户决定」 | 能 | 同上 |
| 7 | `/opt/telegram-kol-analyzer/data/research.db` | 1.27 GB（+WAL 6.8 MB，正常） | 生产库 | **不能删** | — 见第 3.3 节 |
| 8 | `/var/log/messages` | 1.18 GB | rsyslog，06-24 至今从未轮转 | 能轮转压缩（gzip 后约 1/10） | 轮转不丢内容；截断会丢 |
| 9 | `/var/log/secure` / `/var/log/cron` | 255 MB / 60 MB | 同上从未轮转；secure 主要是 SSH 爆破失败记录 | 能轮转压缩 | 同上 |
| 10 | `/root/projects/` | 1.6 GB | `brale` 1.3G、`ai-analysis` 368M、`codex-proxy` 33M，最后修改 09-04 / 09-09 | **不是本项目，需用户判断** | `codex-proxy` 可能仍在用 |
| 11 | `/root/.codex/` | 1.2 GB | `logs_2.sqlite` 650M（Codex 自身的 DEBUG/TRACE 日志库，最早记录 09-16，每天写入 15～50 MB，看起来有约 10 天窗口但未找到配置证实）、`packages` 320M、`cache` 142M | 暂不动 | 值守 Codex runner 以 root 跑、用这个目录 |
| 12 | `/opt/telegram-kol-releases/` + `/opt/telegram-kol-candidates/` + `/opt/telegram-kol-candidate-cf68980/` | 875M + 187M + 27M | 已退役的 immutable release 流程遗留，最后一份 09-06 | 能 | tg-deploy 用 git 检出，不读这些目录；systemd 单元的 ExecStart 都指向 `/opt/telegram-kol-analyzer`（已核实） |
| 13 | `/opt/telegram-kol-analyzer/data/backups/` | 502 MB | 08-22～08-23 phase6 证据，主要是 27～30 MB 的行级 preimage / decision manifest JSON，没有整库副本 | 建议压缩不删 | 行级 preimage 是那次数据修复的唯一原始记录 |
| 14 | `/var/log/journal/` | 482 MB | 已有上限 `SystemMaxUse=500M`（`/etc/systemd/journald.conf.d/size-limit.conf`） | 不需要处理 | — |
| 15 | `/www/` | 2.1 GB | 宝塔面板（panel 986M、backup 123M） | 不属于本项目 | — |
| 16 | `/root/.vscode-server` / `.antigravity-server` / `.cargo` / `/opt/google` | 422M / 353M / 529M / 380M | 远程 IDE、Rust 工具链、Chrome | **需用户判断** | 删了下次远程连接会重装 |
| 17 | `data/media/` | 207 MB | 媒体缓存；`telegram-kol-media-cleanup.timer` 每天 03:30 在跑，09-26 删了 180 个文件 | 不需要额外处理 | 见第 3.4 节 |
| 18 | `data/logs/` | 49 MB | 应用自己的 RotatingFileHandler（10 MB × 10），有上限 | 不需要处理 | — |
| 19 | `/var/lib/telegram-kol-oncall/` | 5 MB | codex-spool 364K、state.db 468K | 不需要处理 | — |
| 20 | docker | 2 MB + containerd 281 MB | 1 个运行中容器，可回收 41 MB | 不值得 | — |

**核对过、不是原因的：** WAL 没有膨胀（6.8 MB）；生产库空闲页只有 554 页（2.2 MB），**现在 VACUUM 回收不到东西**；
`deepcoin_ws_events` 只有 931 行（代码考古曾把它列为头号嫌疑，生产数据否定）；journald 已有上限；
媒体清理定时器确实在生效；pip/uv 缓存 4 MB；codex-spool 很小。

## 3. 为什么一直在涨

### 3.1 整库副本只进不出（阶梯式，最大头）

L3 流程要求「备份 + quick_check」，演练还要一份副本；每做一次 L3 就留下 1～2 份整库，
而库本身从 08-30 的 768 MB 长到今天的 1.27 GB，每份越来越大。没有任何规则说这些副本什么时候退役。
08-30～09-10 这十天就留下了 31 份（约 25 GB）。到 09-26 剩余空间已不够再做一份
`VACUUM INTO`（需要 ≥ 1.3 GB，演练再加 1.3 GB，再加安全余量），于是 uncertain-attempt 收口只能改用单表 dump。

### 3.2 `/var/log/messages` 从未轮转 + 日志刷屏

- rsyslog 通过 imjournal 把 journald 的内容再写一份到 `/var/log/messages`（`*.info`），
  但 `/etc/logrotate.d/` 下**没有 rsyslog 的配置**（有 btmp、wtmp、nginx 等，唯独没有它），
  所以 messages / secure / cron 从 06-24 起一直追加。logrotate.timer 本身是 active 的。
- 每天写入量：09-20 38.6 MB，09-25 56.7 MB，09-26 40.2 MB。
- 09-26 这一天按来源拆（字节 / 行数）：

| 日志 | 每天字节 | 每天行数 | 性质 |
|---|---|---|---|
| `oncall runner rejected case-9..14: Permission denied .../request.json` | 8.1 MB | 51,822 | **值守 Codex runner 的真实故障**，见 3.5 |
| `web_app recognition execution finding family=active_authoritative_attempt ...`（ERROR） | 5.7 MB | 28,760 | 同几行记录每轮循环重复报 |
| `web_app deepcoin_reconcile_round {...}`（INFO，每行 ~2.8 KB 完整 JSON） | 3.5 MB | 1,304 | 每轮对账打一整份 JSON |
| `runtime_incident_adapters Runtime incident capture / detailed` | 6.0 MB | 28,232 | 最近 1 小时已为 0，可能已被近期部署消掉 |
| `stop_loss_size_convergence stop-loss resize skipped` | 1.7 MB | 11,181 | 同一情况每轮重复 |
| `source_deletion_exit_timeout source deletion exits ...` | 1.1 MB | 7,057 | 最近 1 小时为 0 |
| uvicorn 访问日志（`/api/monitor-status`、`/api/freshness`、`/api/runtime/loop-health`） | ~1.3 MB | ~13,000 | 前端轮询 |

  约 **2/3 的日志量是同一条内容在循环里反复打**。这些行也同时进 journald，
  把 500 MB 上限内能保留的历史压短了——排查事故时可回看的时间因此变少。

### 3.3 生产库每天约 12.6 MB

09-16 快照 1,129 MB → 09-25 快照 1,238 MB（8.6 天 +109 MB）。按表：

| 表 / 索引 | 09-16 | 09-25 | 增量 | 说明 |
|---|---|---|---|---|
| `context_resolution_attempts` | 517.5 MB | 555.6 MB | +38.1 MB | 6,714 行，**`request_summary_json` 平均 64 KB/行**（最大 129 KB），即每次上下文解析把完整请求上下文存了一份。占全库 45% |
| `pending_tpsl_snapshot_observations` | 344.1 MB | 370.9 MB | +26.8 MB | 196 万行，自 07-21 起，**每天约 3 万行**，只增不删 |
| `ix_pending_tpsl_snapshot_instrument_time` | 99.1 MB | 115.0 MB | +16.0 MB | 上表的索引 |
| `recognition_decisions` / `recognition_experiments` | 39.6 / 30.5 | 44.9 / 35.9 | +5.3 / +5.3 | 次要 |
| 其余 | | | < 4 MB 每项 | |

- `pending_tpsl_snapshot_observations`：代码里唯一的读者是 `strategy_records.py:752-760`，只取每个币种**最新一条**。
  写入在 `protection_snapshot.py:576`。模型注释自称 append-only 完整性证据（`models.py:2570-2587`）。
  旧观测对运行无用，只有审计价值。
- `context_resolution_attempts.request_summary_json`：读者是 `context_resolution_worker.py:117-123, 315-321`（同一条消息的最新一次尝试）、
  `web_queries.py:1856`（消息卡片详情）、`context_analysis_backfill.py:141, 743`（回填分析）。
  旧消息的请求全文只用于回看和回填。（更正：初稿曾写「6,713 行 `created_at` 为 NULL」，是我查询时取错了列；
  复核快照 `created_at` / `updated_at` 均 0 行 NULL。保留期任务仍按消息时间做第一道筛选，理由是这些列排在 64 KB 大字段之后，逐行读会扫约 0.5 GB。）
- 项目文档早有记录但未跟进：`docs/known-issues-and-deferred-work.md:67`（当时两表约 334 MB / 282 MB）。

### 3.4 媒体：在控，但被磁盘水位挤压

`telegram-kol-media-cleanup.timer` 每天跑
`media-cleanup --apply --retain-days 14 --max-media-dir-gb 5 --min-free-disk-gb 10`。
因为现在剩余空间 < 10 GB，它按水位条件在加速删；清理完磁盘后会自动回到 14 天保留。
另有 492 个超过 30 天的文件（35 MB）没被删，应是仍被策略记录引用的媒体，属设计内行为。
**这个 service/timer 单元只存在于服务器 `/etc/systemd/system/`，仓库 `deploy/systemd/` 里没有**，是配置漂移。

### 3.5 顺带发现：值守 Codex runner 从 09-23 起读不了案例（不是磁盘问题，但是日志第一大户）

`telegram-kol-oncall-codex.service` 以 root 运行，但 `CapabilityBoundingSet=cap_setfcap`（没有 `CAP_DAC_OVERRIDE`），
只能靠附加组 `telegram-kol-oncall` 读 spool；而 case-9～14 目录权限是 `drwx--S---`（组没有任何权限），
所以每 10 秒左右对 6 个案例各报一次 `Permission denied`，这些案例**从来没被处理**。case-1 的目录是 `drwxrws---`，
说明创建目录时的 umask / 权限在某次改动后变了。这是值守功能故障，应单独开任务处理，不放进本次磁盘治理。

## 4. 一次性清理清单（未执行，逐项待批准）

「退役」统一指：先把 `路径 \t 字节数 \t sha256` 追加到清单文件
`/var/lib/telegram-kol-retired-backups/manifest-20260926.tsv`（同时抄进仓库状态文档），再删除。
这满足 AGENTS.md「退役的备份留下 size + sha256」。算 sha256 约 30 GB 读盘，用 `nice -n 19 ionice -c3`，预计 5～15 分钟。

| # | 对象 | 命令要点 | 预计释放 | 可逆性 |
|---|---|---|---|---|
| C1 | 两个 `/var/lib/telegram-kol-*-evidence` 下的 28 份整库 `*.db`（只删 `.db` / `.db-wal` / `.db-shm`，保留 JSON、txt 等小证据） | `find ... -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) -size +1M` → 写 manifest → `rm` | ~23 GB | 不可逆（留 sha256） |
| C2 | `/root/evidence/` 下 3 份整库副本 | 同上 | ~2.8 GB | 不可逆 |
| C3 | `/tmp/research-snapshot-20260916.db`、`/root/phase2-snapshot.db` | 同上 | ~2.3 GB | 不可逆（纯分析副本） |
| C4 | `/root/research-backup-preA2-20260925T130524Z.db` | 推荐：`zstd -T1 -3 --rm` 压缩（先核 sha256 与状态文档一致），到 2026-10-09 再退役 | ~1.0 GB（压缩后约 80～150 MB，同类文件实测 814 MB → 54 MB） | 可逆（解压即回原文件） |
| C5 | `/var/log/messages`、`secure`、`cron` | 先装 L1 的 logrotate 配置（见 5.1），再 `logrotate -f /etc/logrotate.d/rsyslog` 触发首次轮转并压缩 | ~1.3 GB | 可逆（gzip 保留全部内容） |
| C6 | `/opt/telegram-kol-releases/`、`/opt/telegram-kol-candidates/`、`/opt/telegram-kol-candidate-cf68980/` | 先 `grep -r telegram-kol-releases /etc/systemd/system /usr/local/bin /usr/local/libexec` 确认无引用，再 `rm -rf` | ~1.09 GB | 不可逆，但都能从 git 重建 |
| C7 | `data/backups/` 里 > 5 MB 的 JSON（08-22～08-23 phase6 preimage / manifest） | `zstd -T1 -19 --rm`（JSON 压缩比通常 > 10 倍） | ~0.45 GB | 可逆（解压即回原文件） |
| C8 | `/root/projects/brale`、`ai-analysis`；`.vscode-server`、`.antigravity-server`、`.cargo`、`/opt/google` | **由用户决定**；我不判断这些是否在用 | 最多 ~3.3 GB | 不可逆 |

C1～C7 合计约 **31 GB**；加上 C8 最多约 35 GB。执行后剩余空间约 36～39 GB。
所有步骤都只动文件，不重启服务、不碰生产库和交易开关。

## 5. 持久治理

### 5.1 系统日志轮转（L0：静态配置）

新增 `/etc/logrotate.d/rsyslog`（同时进仓库 `deploy/logrotate/rsyslog` 以免再漂移）：

```
/var/log/cron /var/log/maillog /var/log/messages /var/log/secure /var/log/spooler {
    daily
    rotate 14
    maxsize 200M
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        /usr/bin/systemctl kill -s HUP rsyslog.service >/dev/null 2>&1 || true
    endscript
}
```

验证：`logrotate -d` 干跑无报错 → 执行一次 → 确认 rsyslog 继续往新的 messages 写。
上限约 14 天 × 每天 ~5 MB（压缩后）。journald 维持现有 500 MB 上限，不改。

### 5.2 在源头消掉刷屏日志（L1：代码，只改日志行为）

- `recognition execution finding`：同一 `(family, row_id, phase)` 只在首次出现和状态变化时打，其余计数汇总。
- `deepcoin_reconcile_round`：INFO 只打计数摘要，完整 JSON 降到 DEBUG 或只在有动作时打。
- `stop-loss resize skipped`、`source deletion exits`、`Runtime incident capture/detailed`：同样改为「状态变化时打一次 + 周期汇总」。
- 值守 runner 的 Permission denied：修 3.5 的权限故障后自然消失（单独任务）。
- 预计把日志量从每天 ~45 MB 降到 ~10 MB 以下，journald 500 MB 能保留的历史相应拉长数倍。

### 5.3 数据库保留期（L3：生产数据删除）

- `pending_tpsl_snapshot_observations`：保留最近 **7 天** + 每个 `(venue, instrument_id)` 最新一条；
  新增每日任务，分批（每批 ≤ 5,000 行、单独短事务、批间 sleep）删除，避免长时间持写锁。
  首次执行可删约 180 万行，腾出约 440 MB 库内空间。
- `context_resolution_attempts`：超过 **30 天**的行，把 `request_summary_json` 换成只含
  `rendered_prompt_sha256` 与大小的占位摘要（该表已存 `rendered_prompt_sha256` 与 `request_component_sha256_json`），
  其他字段不动。实现前要逐一确认三个读者（3.3 列出）对占位值的处理，并注意 `context_resolution.py:727` 对 `"{}"` 有特判。
  首次执行预计腾出 250～350 MB。
- **VACUUM 策略：不做定期 VACUUM，也不开 auto_vacuum。** 删掉的页会被 SQLite 复用，
  只要保留期任务在跑，文件就不再长。若想把文件本身缩小（1.27 GB → 约 0.5 GB），可在清理后做**一次**
  VACUUM：需剩余空间 ≥ 库大小，并独占写锁约 1～2 分钟（worker 的 busy_timeout 是 30 秒，期间写入会失败），
  所以只能在停 worker 的短窗口做，且不能在时效性策略操作期间做。这一项单独问用户。
- 按 L3：先在快照上演练删除语句与计数，备份 + `PRAGMA quick_check` + 前后计数；
  演练与备份都要在一次性清理之后才有空间做。

### 5.4 整库备份与证据文件的保留规则（L0：写进 AGENTS.md）

1. 统一放 `/var/backups/telegram-kol/<日期>-<主题>/`，不再散落在 `/root`、`/tmp`、各 evidence 目录。
2. **演练副本不是恢复点**：演练结束、结果记进文档后当场删除（留 size + sha256）。
3. 恢复点备份：`quick_check` 通过后用 `zstd -T1 -3` 压缩（约 1/10），变更关闭后再保留 14 天，然后退役（size + sha256 写进 manifest）。
4. 任何时刻整库恢复点最多保留 3 份；分析用快照用完即删，不留过夜。
5. 做 `VACUUM INTO` 前先核：剩余空间 ≥ 2 × 库大小 + 5 GB，否则先清理再做，不降级为「不备份」。

### 5.5 磁盘水位告警（L1：值守新判据，只读）

在值守 watcher 加一条只读判据（`os.statvfs('/')`，每 10 分钟一次）：
剩余 < 10 GB 发「Kol运行通知」提醒，< 5 GB 升级为高优先级，并附上当日 `research.db` 大小与
`/var/log/messages` 大小，方便一眼看出是谁在涨。同一水位只报一次，回升后复位。
值守服务需要单独重启（见记忆「值守 D6 三条判据」）。

### 5.6 其他

- 把 `telegram-kol-media-cleanup.service/.timer` 收进仓库 `deploy/systemd/`（内容照服务器现状），消除漂移。L0。
  注意 tg-deploy 不同步 systemd 单元，改单元要手工 `cp` + `daemon-reload`。
- `/root/.codex/logs_2.sqlite`：观察一周，若超过 1 GB 再考虑给 runner 设更低的 Codex 日志级别。暂不动。

## 6. 验证级别（按 AGENTS.md）

| 项 | 级别 | 验证 |
|---|---|---|
| 一次性清理 C1～C7 | 文件操作，不涉及代码与生产库；按不可逆操作对待 | 先写 manifest（size + sha256）→ 删除 → `df -h` 前后对比；不重启，不碰交易开关 |
| 5.1 logrotate | L0 | `logrotate -d` 干跑 + 一次真实轮转 + 确认 rsyslog 仍在写 |
| 5.2 日志去重 | L1 | 聚焦测试 + 最终全套测试；部署后观察 15 分钟或 5 条真实消息，对比每小时日志行数 |
| 5.3 数据库保留期 | L3 | 快照演练、备份、quick_check、前后计数；确认没有时效性策略操作时执行 |
| 5.3 一次性 VACUUM（可选） | L3 | 同上，外加停 worker 的短窗口；单独批准 |
| 5.4 备份规则写进 AGENTS.md | L0 | 文档 |
| 5.5 水位告警 | L1 | 聚焦测试（阈值、去重、复位）+ 全套；部署后确认首轮判据跑过、未误报 |
| 5.6 媒体单元入库 | L0 | 与服务器 `systemctl cat` 输出逐字比对 |

建议顺序：先 C1～C7（立刻解除 91% 危险）→ 5.1 + C5 → 5.4 → 5.5 → 5.2 → 5.3。

## 7. 需要用户拍板的问题

1. **旧整库副本（08-30～09-10，C1 + C2，约 26 GB）**：全部退役（只留 size + sha256），还是每个主题留一份压缩版？
   我的建议：全部退役，这些已没有回滚价值。
2. **09-25 的两份与 09-16 的一份（C3、C4）**：`phase2-snapshot.db` 和 `/tmp/research-snapshot-20260916.db` 直接退役；
   preA2 备份压缩保留到 10-09 —— 同意吗？
3. **非本项目的目录（C8）**：`/root/projects/brale`、`ai-analysis`、`codex-proxy`，`.vscode-server`、`.antigravity-server`、`.cargo`、`/opt/google`（Chrome）——哪些还在用？
4. **旧 release 目录（C6）**：immutable release 流程已于 09-06 退役，可以删吗？
5. **系统日志**：保留 rsyslog 并轮转 14 天（推荐，secure 日志对查 SSH 登录有用），还是干脆停掉它对 journald 的重复写入？
6. **数据库保留期**：TP/SL 观测留 7 天、上下文解析请求全文留 30 天，可以吗？要不要做一次停 worker 1～2 分钟的 VACUUM 把文件缩小？
7. **告警水位**：10 GB 提醒 / 5 GB 高优先级，合适吗？
8. **备份规则**（5.4）写进 AGENTS.md，作为以后所有会话的硬规则，同意吗？

## 8. 用户裁定（2026-09-27，经调度会话转达）

- C1～C7 按建议执行；C4 preA2 压缩保留到 2026-10-09；C8（非本项目目录）**全部保留**。
- rsyslog logrotate：每天、14 天、压缩；TP/SL 观测留 7 天；上下文请求全文 30 天后换摘要；
  水位 10 GB 提醒 / 5 GB 高优先级；备份规则写进 AGENTS.md；**本次不做 VACUUM**。
- 值守相关（水位判据、runner 的 Permission denied）等 Codex 阶段 3 与 runner 修复会话落地后再做，本线不改 `oncall_*`。

### 8.1 执行前补充核对

- `/opt/telegram-kol-releases/0335de71…` 仍被三个安全监视器单元的 drop-in 引用
  （`/etc/systemd/system/telegram-kol-monitor*.service.d/10-telegram-kol-release.conf` 的 PYTHONPATH / ReadOnlyPaths）。
  监视器是刻意停用的（timer disabled），但为了不让它的单元失效，**C6 保留这一份（24 MB），只删其余**。
- 当前运行的 telegram-kol 进程的 cwd 都在 `/opt/telegram-kol-analyzer`、`/root`、`/var/lib/telegram-kol-oncall`，没有进程在 release 目录下；`lsof` 无打开。
- 已落仓库：`deploy/logrotate/rsyslog`（`logrotate -d` 在服务器上干跑无错误）、
  `deploy/systemd/telegram-kol-media-cleanup.{service,timer}`（与服务器现状逐字一致）。

### 8.2 数据库保留期上线计划（L3，未执行）

代码：`src/telegram_kol_research/db_retention.py`（`python -m telegram_kol_research.db_retention`，默认 dry-run），
`deploy/systemd/telegram-kol-db-retention.{service,timer}`（每天 04:10，Nice 19，IO idle）。候选 sha 见 8.3。

1. **前置**：一次性清理完成后剩余空间 ≥ 2 × 库大小 + 5 GB（约 8 GB），满足 AGENTS.md 新规则第 5 条。
2. **备份**：`VACUUM INTO /var/backups/telegram-kol/<日期>-db-retention/before.db` → 在备份上 `PRAGMA quick_check` → `zstd -T1 -3 --rm`；记 size + sha256。
3. **演练**：在另一份快照上跑 `--apply`，记录两表前后行数、`request_summary_json` 总字节、占位行数；
   同时核对关键业务表（`execution_bindings`、`execution_order_legs`、`authoritative_execution_attempts`、`raw_messages`、`strategy_threads`）行数前后不变。演练结束删除演练副本（留 size + sha256）。
4. **部署**：按 AGENTS.md 流程 tg-deploy 候选 sha（读者对占位值的兼容改动需要随代码上线）；部署时避开时效性策略操作。
5. **生产 dry-run**：`python -B -m telegram_kol_research.db_retention --database-path data/research.db`，候选数应与演练一致。
6. **首次 apply**：手动跑一次（单次最长 10 分钟，分批 ≤ 5000 行、短事务、遇锁即退）；未跑完的部分由之后每天的定时器接着做。
   跑的时候看 worker 事件循环健康（`/api/runtime/loop-health`）与消息处理积压。
7. **装定时器**：`cp deploy/systemd/telegram-kol-db-retention.* /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now telegram-kol-db-retention.timer`（tg-deploy 不同步单元）。
8. **事后**：两表行数与预期一致、关键业务表不变、`PRAGMA quick_check`（在事后快照上跑，不压生产库）；
   库文件大小不会变小（不做 VACUUM），但之后应基本不再增长——一周后复核 `research.db` 大小。
9. **回滚**：停 timer（`systemctl disable --now telegram-kol-db-retention.timer`）即停止继续删；
   已删的观测行与被替换的请求全文只能从第 2 步的备份取回（按 id 选择性回灌，不整库恢复）。

### 8.3 候选与复核（2026-09-27）

- 代码候选：`8468277c` 之上改正一处代码注释（见上面的更正），最终候选 sha 见本节末。全量测试（在 `8468277c` 上）：9802 通过、4 跳过、0 失败。
- 改动：`db_retention.py`（新增）、`context_request_storage.py`（新增 `storage: "retention_stub"` 占位契约）、
  `context_resolution_worker.py`（占位时从占位里取候选线程 id，指纹与替换前一致）、`context_analysis_backfill.py`（导出跳过全是占位的消息）、
  `stop_loss_size_convergence.py`（「resize skipped」按 pos_id 节流，每小时最多一次并带被抑制次数）、
  `deploy/systemd/telegram-kol-db-retention.{service,timer}`（User=telegram-kol-worker，避免 root 新建 WAL 锁住服务）。未改 `web_app.py`、`cli.py`、`oncall_*`。
- 终态白名单：`exhausted`、`superseded`、`failed`、`blocked_disabled`、`blocked_execution_terminal`、`reanalysis_capped`，
  以及 `completed` 中不再可能被重排的行；快照上的状态分布：completed 4894、exhausted 1528、superseded 221、failed 68、reanalysis_capped 2、blocked_execution_terminal 1。
- 生产核对：SQLite 3.42.0（行值比较需 ≥ 3.15，满足）；磁盘调度器是 `mq-deadline`，所以单元里的 `IOSchedulingClass=idle` **不起作用**，
  IO 压力靠分批（每批 ≤ 5000 / 上下文 200 行）+ 批间 0.2 s + 单次 10 分钟上限控制。首次 apply 时按 8.2 第 6 步盯 loop-health。
- **最终候选 sha：`b03fbb908f520ad354f836eab9bb9670ebb8135d`**（相对 `8468277c` 只改了一段 docstring，聚焦测试 50 通过；最终全量在 `b03fbb90` 上：9802 通过、4 跳过、0 失败）。
