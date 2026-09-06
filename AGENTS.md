# Current Architecture

- 先读 `docs/ARCHITECTURE.md`。

# Project Workflow

- Make code changes locally. Local checks are sufficient for documentation,
  static configuration, and code paths whose acceptance does not depend on
  secrets or network identity. Run the production-dependent part of verification
  on the server only when acceptance actually depends on the live Telegram
  session, Deepcoin API IP allowlist, production keys, or deployed runtime state.
- Any temporary analysis, diagnostic, or audit that imports code directly from
  an immutable release directory must run with `python -B` or
  `PYTHONDONTWRITEBYTECODE=1`. A source-adjacent `__pycache__`/`.pyc` write changes
  the release content digest and can make that otherwise valid release unusable
  as a rollback target.
- Push reviewed local commits to GitHub on `codex/deepcoin-auto-trading-v1` when
  the active plan or user request calls for integration. Deploy when the
  approved phase requires production verification.
- Treat a user-approved phase as one coherent execution scope. Normal steps
  explicitly included in that scope do not require repeated confirmation.
  Pause only when the work materially expands beyond the approved phase or an
  irreversible action was not included. Exact SHAs, action manifests, fresh
  evidence, backup/rollback boundaries, and fail-closed handling remain
  technical requirements rather than separate conversational approval gates.
- **Never run `git add -A` in this repository.** Multiple sessions share this
  checkout, so `-A` commits other sessions' unfinished work. Stage explicit
  paths and verify with `git diff --cached --name-only` before committing.
- Send no Telegram notifications during active work. Concise in-app commentary
  is allowed at meaningful milestones, state changes, or blockers; do not stream
  repetitive polling or unchanged status. Send exactly one Telegram notification
  when the current turn's work stops and control is being returned to the user,
  whether because the task completed, work paused for user input, or progress is
  blocked. Immediately before that final response, run
  `python3 "$(git rev-parse --show-toplevel)/scripts/codex_telegram_notify.py" "<short non-sensitive status summary>"`.
  Telegram is the primary stop notification. Never include credentials or
  sensitive values in the summary. If Telegram delivery fails, use
  `osascript -e 'display notification "工作已停止，请查看 Codex。" with title "Telegram 获取消息项目"'`
  as the fallback and clearly report the Telegram failure in the final response.
  If both notification methods are unavailable, state that clearly in the final
  response.
- Deployment path (since 2026-09-06, when the deployment gates were retired;
  see `docs/2026-09-05-codex-handover-closeout.md` section 7): push the exact
  reviewed commit to `origin/codex/deepcoin-auto-trading-v1` first, then run
  `/usr/local/bin/tg-deploy <full-40-character-sha>` on the server. It fetches,
  hard-resets the server checkout (branch `live`) to that SHA, clears bytecode,
  and restarts worker → web → ingest, printing the resulting HEAD and PIDs.
  Record the pre-deploy production HEAD as the rollback SHA; rollback is
  `tg-deploy <that-sha>`. tg-deploy does not install Python dependencies: when
  `pyproject.toml` dependencies change, `pip install` them into
  `/opt/telegram-kol-analyzer/.venv` on the server before running tg-deploy.
  The former stage/activate helper
  (`scripts/server_git_update.*`, `deploy/telegram-kol-stage|activate`) and
  `docs/deployment-action-gates.md` describe the retired immutable-release
  flow; do not use them unless the gates are reinstated.

# Risk-Adaptive Verification

- Use the lowest verification level that fully covers the change's actual risk.
  Do not automatically turn vague language such as "full session", "complete
  evidence", or "real verification" into the highest-cost interpretation.
  Explicit user requirements and phase-specific checks that directly address the
  phase's core failure mode still take precedence.
- **L0 — documentation or static configuration:** run only relevant formatting,
  parsing, or static checks. No full suite, deployment, restart, or production
  observation is required.
- **L1 — additive dormant or shadow behavior with no authority takeover and no
  exchange write:** run focused tests while developing and one full suite on the
  final code candidate. After deployment, observe either 15 continuous minutes
  or 5 real messages, whichever comes first. A restart and complete exchange
  history are not required unless the change directly concerns lifecycle or
  trading protection.
- **L2 — authority cutover, durable consumer, recovery, or process separation:**
  run focused concurrency/recovery/rollback tests while developing and one full
  suite on the final code candidate. Observe 30 continuous minutes and at least
  5 real messages; try to cover 2 chats. If 5 messages do not arrive within the
  30-minute window, stop rather than extending the observation indefinitely,
  leave the phase `in_progress`, and record the limited traffic. Restart once
  only when restart recovery or process lifecycle is part of the phase's core
  claim. Check backlog, duplicate processing, and direct exchange history when
  the path can affect execution.
- **L3 — schema change, production data repair, or exchange-write semantics:**
  require an exact change and rollback plan. Rehearse on a production database
  copy only when schema/bootstrap/migration files change or a production data
  mutation is planned. Preserve a backup, `PRAGMA quick_check`, and before/after
  counts for affected and critical business tables; do not hash every table
  unless an anomaly requires a wider audit. Any change to real exchange-write
  semantics must be explicitly included in the approved phase scope.
- During development, use focused tests for each edit. Run the full suite once
  after all production-code changes are assembled into the final candidate. If
  production code changes after that run, it becomes a new final candidate: run
  the affected focused tests and one final full suite on that candidate.
  Documentation-only changes after the run do not require another full suite.
- Concentrate production checks at no more than four normal checkpoints:
  pre-deploy, post-cutover, post-restart when required, and observation end. Use
  a quiet server-side monitor between checkpoints and report only state changes,
  anomalies, and the final summary instead of emitting repetitive polling.
- Treat an incomplete external query as unknown, never as zero or healthy. One
  reasoned retry is allowed. If the retry is still incomplete, fail closed,
  leave the phase `in_progress`, and record the missing evidence instead of
  retrying indefinitely.
- Keep raw JSON, detailed order rows, and long logs in a server-side evidence
  file. Status documents and final responses should record only the commit,
  window, modes, required metrics, anomalies, and evidence path. Expand evidence
  only when a required gate fails or an anomaly appears.

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
- Apply the shared Risk-Adaptive Verification levels above to testing,
  deployment, restart, observation, and evidence collection for this workflow.
- Documentation-only phases do not require a production service restart.

# Deepcoin API Docs

- If the browser/web reader gets `403 Forbidden` for Deepcoin docs, retry from the local shell with PowerShell/.NET HTTP requests. The docs page `https://www.deepcoin.com/docs/zh/authentication` was confirmed readable from the local environment with HTTP `200 OK` on 2026-07-05.
- The authentication page title is `接入指南 | DeepCoin API`. It documents API URL `https://api.deepcoin.com`, private REST headers `DC-ACCESS-KEY`, `DC-ACCESS-SIGN`, `DC-ACCESS-TIMESTAMP`, and `DC-ACCESS-PASSPHRASE`, and the signing string `timestamp + method + requestPath + body` using HMAC SHA256 with Base64 output.
