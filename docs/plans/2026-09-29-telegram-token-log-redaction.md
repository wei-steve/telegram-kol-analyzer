# 2026-09-29 Telegram bot token 写入日志的修复

## 问题

`journalctl -u telegram-kol-worker` 出现
`httpx.HTTPStatusError: Server error '502 Bad Gateway' for url 'https://api.telegram.org/bot<TOKEN>/getUpdates?...'`
（以及 `deleteWebhook`）。`raise_for_status()` 把完整 URL（含 token）写进异常消息，
后台任务监督器 `_supervise_restartable_background_task` 以 `exc_info` 记录回溯，
于是 token 进入 journald 和应用日志文件 `telegram-kol.log*`（同一批 record 走同一个
包 logger 的文件与 stderr 两个 handler）。服务器核实：近 24 h 3 行、30 天 13 行，
全部在 worker；web/ingest/oncall/oncall-codex 为 0。

## 修复（两道边界）

1. **调用点**：新模块 `telegram_bot_api.py` 的 `raise_for_telegram_status(response)`
   取代全部 11 处 Bot API 的 `raise_for_status()`（`telegram_bot_commands` 7、
   `system_operator_bot` 2、`strategy_alerts.send_strategy_alert_bot_message` 1、
   `oncall_remediation_runtime` 1）。抛出的 `TelegramBotApiStatusError` 仍是
   `httpx.HTTPStatusError` 子类（现有 `except` 与 `exc.response.status_code` 不变），
   消息只有方法名与状态码，且 `from None` 不链接原异常。
2. **日志**：`app_logging.install_secret_log_redaction()` 安装进程级 LogRecord
   factory，含 token 的 record 会把 message、回溯（预填 `exc_text`）、stack 中的
   `bot<id>:<secret>` 替换为 `bot[REDACTED]`；不含 token 的 record 原样不动。
   用 record factory 而非 handler filter，是因为 uvicorn 的 `dictConfig` 会在之后
   装自己的 handler，filter 覆盖不到。`configure_application_logging` 与 CLI
   `main()` 都会调用；同时把 `httpx`/`httpcore` logger 钉在 WARNING（今天 root 为
   WARNING，INFO 请求日志本来就没开，这里只防将来被打开）。

已核对不需要改的：`oncall_alerts`（urllib，已只抛状态码）、`production_safety_monitor`
（loopback 请求，不含 token）、`scripts/codex_telegram_notify.py`（urllib `HTTPError`
的 `str` 不含 URL）。

## 验证

- 聚焦测试 `tests/test_telegram_token_log_redaction.py`：两道边界各自做过变异验证
  （关掉任一道，对应测试失败）。
- 全量（最终候选）：10626 passed / 15 failed / 4 skipped（基于 origin/main 5ba66e8a）；`test_minimal_server_updater.py` 8 个与
  `test_server_update_scripts.py` 7 个失败在 origin/main 基线上同样失败，与本次无关。
  这是 worktree 内缺 `.venv` 的已知现象（调度会话确认），不是回归。

## 部署与之后（风险级别 L1）

- 排期：等调止盈 L2 观察窗结束、调度会话通知后第一个部署。部署前 rebase 到最新
  origin/main，跑受影响测试 + 全量，再经用户确认后 `tg-deploy`。

- 纯日志/异常文本改动，无 schema、无交易写语义、无依赖变化、无 systemd 单元变化。
- 部署后可在服务器用计数方式核对（不打印内容）：
  `journalctl -u telegram-kol-worker --since '<部署时间>' | grep -c 'api.telegram.org/bot[0-9]'`
  应为 0；日志文件同理。
- **需用户决定**：部署后经 @BotFather 轮换受影响的 bot token（旧 token 已进入
  journald 与 `telegram-kol.log*` 轮转文件）；旧日志行可 `journalctl --vacuum-time`
  清理或等待自然过期，轮转日志文件同理。
