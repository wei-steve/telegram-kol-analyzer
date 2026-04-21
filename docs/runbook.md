# Telegram KOL Research Runbook

## Purpose

This runbook describes the local operator flow for researching archived
Telegram strategy groups with a Telegram user account on macOS.

## 1. Local Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

If needed, use the already-verified environment:

```bash
source .venv313b/bin/activate
```

## 2. Telegram User Auth

Export your Telegram API credentials:

```bash
export TELEGRAM_API_ID="your_api_id"
export TELEGRAM_API_HASH="your_api_hash"
export TELEGRAM_SESSION_PATH="data/telegram.session"
```

This project uses your Telegram user session and targets archived chats. It
does not use the Bot API.

If you do not want to export them in every shell, save the same values in a
repo-local `.env` file or `config/telegram.env`. The CLI and web workbench
load those files automatically.

## 3. Configure Target Groups

Copy the example config and edit the target groups:

```bash
cp config/groups.example.yaml config/groups.yaml
```

For each strategy group:
- Set `chat_title`
- Leave `enabled: true` only for groups you want to track
- Fill `tracked_senders` for each admin or signal poster you want scored
- Optionally set sync date bounds for backfill research windows

## 4. Backfill and Sync

The main sync entry command is:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli sync
```

Current implementation status:
- Archived target dialogs are discovered from your Telegram user account
- Recent history is fetched and normalized into the local SQLite database
- Downloaded image media is stored under `data/media`
- Parsed signal candidates and trade ideas are refreshed during sync
- Stale history checkpoints are repaired before sync continues

## 5. Incremental Listening

Start the web workbench with Telegram credentials loaded to enable realtime
listening:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web --host 127.0.0.1 --port 8000
```

Realtime delivery uses two paths:
- Push path: Telethon live listener publishes browser updates through SSE
- Recovery path: a periodic reconcile pass replays a small recent window so
  missed messages after reconnects can still land in SQLite safely

The message header also includes an `立即刷新` button for a one-shot reconcile.
If credentials are missing or invalid, the failure reason is returned directly
in the page instead of failing silently.

If Telegram credentials are missing, the workbench still opens but stays in
local-snapshot mode and shows how stale the current database is.

## 6. Review Ambiguous Candidates

List pending review candidates from a local JSON file:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli review --candidate-file data/candidates.json
```

Apply a decision:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli review \
  --candidate-file data/candidates.json \
  --candidate-id 101 \
  --decision confirmed \
  --note "Chart text and caption agree"
```

## 7. Generate Reports

Write a leaderboard report locally:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli report --output-path reports/leaderboard.json
```

This writes JSON output to the path you specify.

## 8. Launch the Web Workbench

Start the local browser UI:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web --host 127.0.0.1 --port 8000
```

Before using the AI panel, configure the LLM proxy environment:

```bash
export TELEGRAM_KOL_LLM_BASE_URL="http://127.0.0.1:8317"
export TELEGRAM_KOL_LLM_API_KEY="your_proxy_api_key"
export TELEGRAM_KOL_LLM_MODEL="gpt-4.1-mini"
export TELEGRAM_KOL_LLM_TIMEOUT_SECONDS="60"
```

Current web workbench behavior:

- Group list is ordered by latest message time and prefers configured aliases
- Message timeline is newest-first
- Clicking a group refreshes only the message panel instead of the whole page
- Message panel supports free-text search within the current group
- Message panel supports sender-name filtering
- Load more appends older messages while preserving the active filters
- Downloaded image media is served locally through the app
- Message header shows database freshness plus the current refresh mode
- AI panel defaults to grounded context from the current group's latest 50 messages
- The user can override the default recent-message count in natural language, such as `最近 100 条` or `最近 200 条`
- AI panel now uses a simplified single-input workflow without manual scope or date controls
- Each group has its own editable default prompt in the AI panel, and prompt changes affect the next question immediately
- The backend orders the scoped message context chronologically before sending it to the model so recent discussion evolution is clearer
- `/api/events` provides SSE notifications for new messages, and the browser consumes them with `EventSource`
- Reconcile windows replay a small safety overlap to reduce missed-message risk

Recommended browser flow:

1. Open the target group from the left-hand list.
2. Use the Search field to narrow messages by keyword.
3. Use the Sender field when you want to inspect a single poster.
4. Click `Apply filters` to refresh the message panel in place.
5. Click `Load more` to append older matching messages without losing the current filters.
6. Ask a question in the AI panel; if you do not specify a range, it analyzes the current group's latest 50 messages.
7. If needed, ask for a different bounded range in natural language, for example `总结最近 200 条消息`.
8. Adjust the group-specific default prompt at the top of the AI panel when you want a different standing analysis style for that group.

## 9. Test the Project

Run the current automated test suite:

```bash
python3 -m pytest tests -v
```
