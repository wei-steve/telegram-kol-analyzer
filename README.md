# Telegram KOL Research

Local-first research tooling for syncing archived Telegram strategy groups,
preserving raw messages, parsing mixed text and image signals, and generating
per-KOL win-rate reports on macOS.

## Current Status

This repository is being built task-by-task from the implementation plan in
`docs/plans/2026-04-07-telegram-kol-winrate.md`.

## Local Development

Create a virtual environment and install the package in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

If your local Python packaging environment is unstable, use the working project
venv that has been verified during implementation:

```bash
source .venv313b/bin/activate
```

Run the CLI help:

```bash
telegram-kol-research --help
```

Run tests:

```bash
python3 -m pytest -v
```

## Web Workbench

Launch the local web workbench:

```bash
telegram-kol-research web --host 127.0.0.1 --port 8000
```

The workbench shows:

- Telegram group list ordered by latest activity, using configured aliases when available
- Reverse-chronological message timeline with text and media
- Message filtering by free-text search and sender name
- Incremental history browsing with a Load more button for older messages
- Grounded AI chat panel that defaults to the current group's latest 50 messages
- Database freshness and refresh-mode status in the message header
- SSE-based browser live updates for new messages
- Periodic reconcile replay to reduce missed-message gaps after reconnects
- A manual `立即刷新` action that runs a one-shot reconcile and reports
  credential errors directly in the page

Inside the message panel you can:

- Click a group without reloading the whole page
- Search message text within the current group
- Filter the current timeline by sender name
- Load older messages while keeping the current filter state

Inside the AI panel you can:

- Ask natural-language questions without choosing scope controls manually
- Let the system default to the current group's recent 50 messages
- Override the default by asking for a different count, such as `总结最近 200 条`
- Review grouped conversation turns instead of a flat history list
- Edit a per-group default prompt that takes effect on the next question
- Let the backend send message context to the model in chronological order for better trend-aware answers

## LLM Proxy Configuration

To use the AI panel with your CLIProxyAPI deployment, set:

```bash
export TELEGRAM_KOL_LLM_BASE_URL="http://127.0.0.1:8317"
export TELEGRAM_KOL_LLM_API_KEY="your_proxy_api_key"
export TELEGRAM_KOL_LLM_MODEL="gpt-4.1-mini"
export TELEGRAM_KOL_LLM_TIMEOUT_SECONDS="60"
```

These values are used server-side only. Do not expose them to the browser.

## Telegram Auth

This project is designed to use your Telegram user account, not the Bot API.
Set these environment variables before running sync or listener commands:

```bash
export TELEGRAM_API_ID="your_api_id"
export TELEGRAM_API_HASH="your_api_hash"
export TELEGRAM_SESSION_PATH="data/telegram.session"
```

The session file is stored locally on your Mac.

You can also place the same values in a local `.env` file or
`config/telegram.env`. The app loads those files automatically when the shell
environment is missing Telegram credentials.

## Target Group Config

Copy the example config and fill in the archived strategy groups plus tracked
senders you want to study:

```bash
cp config/groups.example.yaml config/groups.yaml
```

## Operator Commands

Sync Telegram history, repair stale checkpoints, parse candidates, and merge trade ideas:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli sync
```

Report generation:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli report --output-path reports/leaderboard.json
```

Read-only KOL strategy PnL audit from a bounded JSON snapshot:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli audit-kol-pnl \
  --messages-json .audit-results/suozhang/messages.json \
  --decisions-json .audit-results/suozhang/decisions.json \
  --lifecycle-json .audit-results/suozhang/lifecycles.json \
  --chat-id=-1002368892075 \
  --symbol BTC --symbol ETH \
  --cutoff 2026-08-01T02:19:12Z \
  --output-dir .audit-results/suozhang
```

Use `--messages-json -` to stream a read-only server query through stdin. The
first run captures digest-verified public candles; repeat with `--offline` to
prove deterministic replay. Use `--reconstruction-only` while reviewing the
decision ledger. The command writes local ignored artifacts only: it does not
modify the project database, repair lifecycle rows, place orders, or calculate
actual Deepcoin account PnL. Any unreviewed candidate or unresolved event stops
the final report from being claimed.

Manual review queue:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli review --candidate-file data/candidates.json
```

Web workbench:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web
```

The `web` command will only enable Telegram realtime updates when the Telegram
auth environment variables are present. Otherwise the page still works in
local-snapshot mode and shows database freshness based on the latest stored
message.

## Context-aware strategy updates and cancellations

Cross-message updates, cancellations, replies, and text-plus-image evidence are
handled by the fail-closed contextual strategy resolver. It is disabled by
default and requires global auto trading, live management mode, the dedicated
boolean, and an explicit Telegram chat allowlist. See
[`docs/contextual-strategy-resolution.md`](docs/contextual-strategy-resolution.md)
for the data flow, safety gates, one-shot command, and troubleshooting.

Historical messages can be given the same immutable first-pass evidence before
contextual resolution is enabled. The backfill command is dry-run by default,
requires an explicit chat scope, and never applies the MiMo result:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 25
```

After reviewing the bounded plan, add `--apply --delay-seconds 2`. Re-running
the same command skips matching completed evidence and resumes from remaining
messages. When `next_scan_cursor` is present, pass that opaque value through
`--scan-cursor` for the next bounded page. See
[`docs/runbook.md`](docs/runbook.md) before using `--retry-failed`.

## Exact-position management liveness recovery

`execution_order_legs.pos_id` with authoritative attribution proves who owns a
position. `position_protection_ledger` separately proves which exact position
owns a stop or take-profit order. A strategy match never substitutes for either
proof. The `position_management_liveness_v2_mode` rollout is `disabled` by
default: `disabled` does no new liveness-v2 exchange write, `shadow` persists
bounded planning evidence only, and effective `live` additionally requires
global auto trading plus live management execution.

Review one exact position without changing exchange state:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli \
  recover-position-management-liveness \
  --database-path data/research.db --pos-id <exact-pos-id>
```

Apply only the unchanged reviewed output with `--apply
--expected-fingerprint <fingerprint>`. Unknown submission outcomes are frozen,
reconciled read-only, and never blindly retried. See
[`docs/runbook.md`](docs/runbook.md) for capability rules, audit SQL,
`recovery_disposition` meanings, and rollback.
