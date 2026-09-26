# 值守 Codex spool 案例目录权限：根因与修复方案

状态：**方案，待批准**（未改生产）。2026-09-27 只读排查。
来源：`docs/plans/2026-09-26-server-disk-usage-analysis.md` 第 3.5 节（分支 `claude/server-disk-usage`）。

## 1. 现象（服务器只读核对，2026-09-27 06:1x CST）

| 目录 | 模式 | 创建时间 (CST) |
|---|---|---|
| spool 根 | `drwxrws---` root:telegram-kol-oncall | runner 的 `ExecStartPre=+chmod 2770` 保证 |
| case-1, 4, 5, 6, 7, 8 | `drwxrws---` (2770) | 09-21 ～ 09-22 22:47 |
| case-9 ～ 14 | `drwx--S---` (2700) | 09-23 18:28 ～ 09-25 17:15 |

- 案例文件都是 `-rw-rw----`，问题只在目录：组没有 `x`，runner（root，无 `CAP_DAC_OVERRIDE`，只靠附加组）进不去。
- runner 每轮对 6 个案例各报一次 `Permission denied`，约 10 秒一轮，每天约 5.2 万行。
- 值守 state.db（`?mode=ro` 备份后读取）：case 9～14 的 `diagnoses` 全部是 `failed / timeout`，
  对应的 `cases` 全部是 `stale`。watcher 日志里 6 次 `oncall diagnosis was never answered`。
  **09-23 之后没有一个案例拿到过 Codex 诊断。**
- `codex:state=up`：每天的健康检查（login status）通过后把状态拉回 up，所以「连续 3 次超时 → Codex down」
  这条告警也被抹掉了——这是另一个盲区，见第 5 节。

## 2. 根因

**watcher 单元的 `RestrictSUIDSGID=yes` 拒绝任何带 setuid/setgid 位的 chmod；代码吞掉了这个错误。**

1. `c028ddad`（09-22 10:06 PDT，runner 在 09-23 01:06 CST 重启生效）为了让 runner 写的结果文件继承组，
   把 `Spool.dir_mode` 从 `0o770` 改成 `0o2770`（`src/telegram_kol_research/oncall_codex.py:629`）。
2. `Spool.enqueue` 先 `mkdir`，再 `os.chmod(directory, 0o2770)`，失败时 `except OSError: pass`（`oncall_codex.py:659-663`）。
3. watcher 单元（`deploy/systemd/telegram-kol-oncall.service`）有 `RestrictSUIDSGID=true`：seccomp 让
   mode 参数里含 `S_ISUID/S_ISGID` 的 chmod/fchmod/fchmodat 直接 `EPERM`。服务器上用临时单元复现：

   ```
   systemd-run --wait --pipe -p RestrictSUIDSGID=yes -p PrivateTmp=yes -p UMask=0077 sh -c '
     mkdir /tmp/p; chmod 2770 /tmp/p   -> Operation not permitted
                   chmod g+s  /tmp/p   -> Operation not permitted
                   chmod 0770 /tmp/p   -> rc=0'
   ```

4. chmod 失败后目录保持 `mkdir` 的结果：`UMask=0077` → `0700`，再加上内核从 setgid 父目录继承的 `S` →
   **`2700` = `drwx--S---`**。正好是现场看到的模式。
5. case-1～8 是 2770，因为它们在 `c028ddad` 之前创建（当时 `chmod 0o770` 不带 S 位，能成功），
   而且在 09-23 01:06～01:07 CST（修复部署、runner 重启的同一分钟）被手工补了 setgid——这几个目录的 ctime 正是这个时刻。
6. 测试没抓到：`tests/test_oncall_codex.py:532` 在没有 seccomp 的开发机上跑，chmod 总能成功；
   sandbox probe 以 runner 的身份测交接，没有在 watcher 单元的限制下建目录。

结论：不是 umask 变了，也不是 `os.makedirs` 的 mode 参数问题，是**上一次修复引入的 setgid 位撞上了 watcher 自己的加固项**，
而那次修复之后创建的每一个案例目录都坏了。

## 3. 修复方案

### A. 代码：建目录不再依赖 chmod 加 setgid（`oncall_codex.py`）

- 新函数 `_make_case_dir(directory)`：
  - 目录不存在时，在临时 `os.umask(0o007)` 下 `os.mkdir(directory, 0o770)`，随后恢复原 umask。
    mode 参数里不带 S 位，所以不会被 `RestrictSUIDSGID` 拦；setgid 由内核从 spool 根继承 → 结果 `2770`。
    （watcher 是单线程循环，临时改 umask 不会影响别的线程；改动只覆盖一次 mkdir。）
  - 目录已存在（同一案例的第 2 次尝试）且组权限不全：`os.chmod(directory, 0o770)`（不带 S 位，允许）。
    这会丢掉 setgid，但 runner 需要的只是组 `rwx`；runner 写回的 `run.json` / `verdict.json` 走
    `atomic_write`，本来就会 chown 到父目录的组（`c028ddad` 的第二道保险），watcher 仍读得到。
  - 最后 `stat` 自检：组权限仍不是 `rwx` 时，**打一条 WARNING 并抛出 OSError**——现有调用方
    （`oncall_service.py:415-428`）已经把它当作「spool 写不进去」处理，不登记诊断请求，
    避免再出现「请求写进去了、runner 永远读不到、30 分钟后记超时」的无声失败。
- `Spool.dir_mode` 常量改成表达意图的 `0o770` + 注释说明 setgid 来自继承、为什么不能 chmod 加 S。
- `atomic_write` 里吞掉 chmod 错误的 `except OSError: pass` 不动（文件 mode 0660 不带 S 位，不受影响）。

### B. 代码：runner 同一案例同一错误只报一次（`oncall_codex_runner.py`）

- 模块级（或 `run_runner_loop` 持有的）`_rejections: dict[str, str]`，键是案例目录名，值是错误描述。
- `process_case_dir` 读 `request.json` 失败时：与上次相同 → 降为 DEBUG；不同或首次 → WARNING，
  文案加一句「repeats suppressed until it changes」。
- 该案例下次读成功、或目录消失（`scan_once` 每轮用当前目录列表修剪字典）→ 清掉记录，并打一条 INFO「case-N readable again」。
- 进程重启后字典为空，每个仍坏着的案例会再报一次——可以接受，也正好让重启后的日志说明现状。
- 同样的去重也用于 `rejected case file ...` / `rejected request ...` 两处？不需要：这两处会写 `run.json`
  （`FAILURE_CONTRACT`），下一轮指纹相同直接跳过，本来就不刷屏。

### C. 测试

- 模拟 seccomp：monkeypatch `os.chmod`，mode 含 `S_ISGID` 时抛 `PermissionError`；在 umask `0o077` 下调用
  `Spool.enqueue`，断言案例目录组权限为 `rwx`（修复前这条会失败，先写测试确认它红）。
- 已存在的 `0o700` 目录再次 enqueue → 修成组可读写。
- 自检失败（chmod 全部拒绝）→ `enqueue` 抛 OSError。
- runner：同一 `PermissionError` 连续 3 轮 → 只有 1 条 WARNING；错误变化 → 再报；恢复 → INFO 一条。
- 现有 `tests/test_oncall_codex.py:532` 的 S 位断言在 Linux 上保留、在 macOS（BSD 语义不继承 S 位）上跳过或改断言组。
- 风险等级 L1（值守是只读旁路，不碰交易与生产库），聚焦测试 + 最终候选跑一次全量。

### D. 现存目录的一次性处理（服务器，root）

case-9～14 都已 `stale`，watcher 早已记为超时，**不会再读它们的结果**。如果只是 `chmod 2770`，
runner 会立刻对这 6 个过期案例各跑一次 Codex（每次约 1 分钟 + token），产出的结论没人消费。所以建议**挪走而不是修权限**：

```bash
ARCH=/var/lib/telegram-kol-oncall/codex-spool-archive/20260927-unreadable
install -d -m 0750 -o telegram-kol-oncall -g telegram-kol-oncall "$ARCH"
for n in 9 10 11 12 13 14; do mv /var/lib/telegram-kol-oncall/codex-spool/case-$n "$ARCH"/; done
ls -la /var/lib/telegram-kol-oncall/codex-spool
```

- 可逆（`mv` 回来即可），不删除任何内容；归档目录在 runner 的命名空间外，runner 看不到。
- 这一步不依赖代码部署，**做完刷屏立即停止**；不需要重启任何服务。
- 同一 case id 以后不会复用（`cases.id` 自增），挪走不会与新案例冲突。
- 备选：若你希望补看这 6 个案例的诊断，改为 `chmod 2770` 让 runner 补跑，但结果只写在 spool 里，不会发 Telegram。

## 4. 部署与重启边界（需要你决定）

- 改的是 `src/`，按 AGENTS.md 只能用 `tg-deploy <sha>` 上线，而 **tg-deploy 会重启 worker → web → ingest**，
  这与本任务「不要重启值守之外的服务」冲突；值守两个单元还得另外手工重启（tg-deploy 不重启它们）。
- 可选：
  1. **推荐**：先做 D（立刻止血，零重启）；A+B+C 代码合入后，随下一次别人的 tg-deploy 一起上线，
     然后只 `systemctl restart telegram-kol-oncall telegram-kol-oncall-codex`。在那之前代码留在我自己的分支，
     不进 `origin/main`（避免共享分支领先生产代码）。
  2. 你同意这次由我 tg-deploy（会重启交易三服务，不动自动交易开关），再重启值守两单元。
- 上线后验证（L1）：等下一个真实案例，确认目录 `drwxrws---`、runner 产出 `run.json`、watcher 记 `done`；
  runner 日志 15 分钟内无重复 `rejected`。

## 5. 顺带发现（不在本次范围）

- 健康检查只测 `login status` / 每日一次最小 exec，测不出「runner 读不到请求」。6 次 `never answered`
  期间 `codex:state` 一直是 `up`，没有任何告警。可以考虑让 runner 在 `health.json` 里带上「读不了的案例数」，
  watcher 据此告警。先不做，待你决定。
