# Project Workflow

- Make code changes locally. Local checks are useful for syntax/unit coverage that does not require secrets or network identity, but real project verification must run on the server because the Telegram session, Deepcoin API IP allowlist, and production keys only work there.
- Push reviewed local commits to GitHub on `codex/deepcoin-auto-trading-v1`.
- Update production by pulling from GitHub on the server, reinstalling the editable package, and restarting `telegram-kol.service`.
- Send no notifications during active work, including commentary updates, tool calls, tests, reviews, commits, pushes, deployments, or other intermediate progress. Send exactly one notification only when the current turn's work stops and control is being returned to the user, whether because the task completed, work paused for user input, or progress is blocked. Immediately before that final response, run `python3 "$(git rev-parse --show-toplevel)/scripts/codex_telegram_notify.py" "<short non-sensitive status summary>"`. Telegram is the primary stop notification. Never include credentials or sensitive values in the summary. If Telegram delivery fails, use `osascript -e 'display notification "工作已停止，请查看 Codex。" with title "Telegram 获取消息项目"'` as the fallback and clearly report the Telegram failure in the final response. If both notification methods are unavailable, state that clearly in the final response.
- Prefer the existing helper after pushing:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

# Runtime Incident AI Agent

- When the user says `请执行自定义ai agent的下一步实施`, first read:
  1. `docs/plans/2026-07-28-runtime-incident-agent-design.md`
  2. `docs/plans/2026-07-28-runtime-incident-agent.md`
  3. `docs/runtime-incident-agent-status.md`
  4. `docs/runtime-incident-agent-runbook.md`
- Continue the phase named by `current_phase` in the status file. If it is
  `in_progress`, resume it; if it is `planned`, start it. Never implement more
  than one phase in one user turn.
- The existing first-pass recognition and contextual multi-information strategy
  resolution remain authoritative. The runtime incident agent must not replace,
  bypass, or duplicate strategy targeting or contextual resolution.
- Every runtime phase must be introduced dormant or shadow-only, preserve the
  current production path, and have a tested disable/rollback path before it can
  be enabled. Do not restart or deploy during an active time-sensitive strategy
  operation. If a safe deployment window cannot be proven, finish local work,
  leave the phase `in_progress`, and record the exact server verification still
  required.
- Documentation-only phases do not require a production service restart.

# Deepcoin API Docs

- If the browser/web reader gets `403 Forbidden` for Deepcoin docs, retry from the local shell with PowerShell/.NET HTTP requests. The docs page `https://www.deepcoin.com/docs/zh/authentication` was confirmed readable from the local environment with HTTP `200 OK` on 2026-07-05.
- The authentication page title is `接入指南 | DeepCoin API`. It documents API URL `https://api.deepcoin.com`, private REST headers `DC-ACCESS-KEY`, `DC-ACCESS-SIGN`, `DC-ACCESS-TIMESTAMP`, and `DC-ACCESS-PASSPHRASE`, and the signing string `timestamp + method + requestPath + body` using HMAC SHA256 with Base64 output.
