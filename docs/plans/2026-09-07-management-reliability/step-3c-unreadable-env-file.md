# A-3c：不可读的 env 文件不得让消息处理失败（L1，热修）

状态文件：`docs/management-reliability-status.md`。先领取。分支 `mgmt/step-3c-unreadable-env`，工作树 `.worktrees/mgmt-step-3c`。

## 问题（A-3b 观察窗发现，2026-09-04 12:45Z 起持续发生）

worker 以 `User=telegram-kol-worker` 运行，`telegram_client._load_env_file_values` 的默认候选路径含
`config/telegram.env`（root 所有、0600），只判断 `exists()`/`is_file()` 不判断可读性，`read_text` 抛
`PermissionError` 一路冒穿到权威执行，作业失败、attempt 落 `authoritative_execution_outcome_unknown`
（raw 15449/15450 各失败多次，incident 2075）。`llm_chat.py:94` 与 `telegram_client.py:50` 都经这个加载器。
按凭据隔离设计，worker 本不该读 `config/telegram.env`（Telegram 会话只在 ingest）。

## 任务

1. `_load_env_file_values`：对每个候选文件先 `os.access(path, os.R_OK)`，不可读的跳过并记一条 warning（只记路径，不记内容），
   `read_text` 再包一层 `except OSError` 同样跳过。env 变量本就优先于文件，跳过等于"该文件不存在"。
2. 找出 worker 角色里所有走默认候选路径的调用（`load_telegram_auth_config` / `llm_chat` 的默认 `env_file_paths`），
   在 `cli.py` 的 `web` 启动路径按 `runtime_role` 传显式路径：`all` 保持默认；`worker` / `web` / `ingest` 只传各自
   需要的文件（`ingest` 需要 `config/telegram.env`，`worker` 只需要 `config/llm.env`，`web` 两者都不需要），
   不改 `llm_chat` 与 `telegram_client` 的默认值本身以免影响 CLI 与脚本。
3. 回归测试：不可读文件被跳过且加载继续；worker 角色启动时不触碰 `config/telegram.env`（用 monkeypatch 断言路径列表）。
4. 只读核实：部署后 journal 里 `PermissionError.*telegram.env` 不再出现；观察期内 `message_processing_jobs` 无因此失败的行。
   **不改服务器上的文件权限与属主**，那是用户的决定；本步只在代码里 fail-open。

## 禁止

- 不改任何文件权限。不改识别与交易逻辑。不用 `git add -A`。

## 验证（L1）

- focused 与全量 0 failed。部署 tg-deploy，记录回滚参考。部署后 15 分钟或 5 条消息：journal 无该 PermissionError，
  新作业成功率恢复。

## 完成条件

更新状态文件到 `current_step: 4`、`current_step_file: docs/plans/2026-09-07-management-reliability/step-4-ledger-repair.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚参考、journal 核实结果、自 09-04 起因此失败的 raw id 列表（只读统计）。
