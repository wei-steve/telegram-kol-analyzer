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
