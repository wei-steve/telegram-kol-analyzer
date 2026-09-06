# Telegram KOL Win-Rate Research System Design

**Date:** 2026-04-07

**Goal**

Build a local-first system on macOS that uses the user's Telegram account to backfill archived strategy groups, continuously ingest new messages, extract trade signals from mixed text and image posts, and compute per-KOL win-rate and PnL quality metrics.

**Scope**

- Historical backfill plus continuous sync
- Focus on archived Telegram groups selected by the user
- Track multiple strategy posters inside the same group
- Prioritize research, ranking, and alerts
- Exclude automated order execution from v1

**Non-Goals**

- Full autonomous trading
- Voice/video signal extraction in v1
- Perfect zero-review parsing of all informal posts
- Production server deployment in v1

## Requirements

- Run locally on the user's Mac first
- Authenticate with the user's Telegram account, not a bot
- Discover and sync groups even when they live in `Archived Chats`
- Preserve every raw message before parsing so logic can be re-run later
- Support mixed text and image-based trade calls
- Compute KOL-level statistics, not only group-level statistics

## Recommended Approach

Use a balanced research architecture:

- `Telethon` for Telegram user-account sync
- `SQLite` for local persistence in v1
- Rule-based parsing for text-first extraction
- OCR for image-assisted extraction
- Confidence scoring plus manual review for ambiguous signals
- Message-to-trade normalization before any win-rate calculation

This approach is heavier than a one-off export script but much lighter than a full TDLib platform. It is the best fit for the user's stated goals: backfill, ongoing monitoring, and reliable KOL comparison.

## Architecture

### 1. Ingestion Layer

Use the Telegram user account to:

- Enumerate all dialogs
- Detect archived groups
- Apply a whitelist of target strategy groups
- Backfill message history in batches
- Subscribe to new messages and edits

### 2. Raw Message Layer

Store every inbound artifact before interpretation:

- Chat identity
- Sender identity
- Timestamp
- Text body
- Reply linkage
- Media metadata
- Local file path for downloaded images
- Original Telegram identifiers and payload fragments

This layer is the system of record. Parsing logic may change later without requiring a fresh Telegram sync.

### 3. Signal Candidate Layer

Create candidate trade events from:

- Native message text
- OCR text extracted from images
- Combined context from image caption plus OCR content

Each candidate should carry:

- Symbol
- Side
- Entry zone
- Stop loss
- Take-profit targets
- Leverage if present
- Parse source
- Confidence score
- Review status

### 4. Trade Normalization Layer

Merge multiple related updates into a single trade idea. This is required because KOLs often split one setup across several posts:

- Entry message
- Later stop update
- TP1 hit
- Remaining position guidance
- Final close

Normalization should prefer explicit reply chains and otherwise fall back to source, symbol, side, and time-window heuristics.

### 5. Analytics Layer

Compute metrics only from normalized, closed trade ideas:

- Win rate
- Sample size
- Average win and loss
- Profit factor
- Risk/reward
- Consecutive losses
- Holding time
- Data quality score

## Group and Source Modeling

The user's archive contains multiple strategy groups, and some groups have multiple admins posting signals. Therefore:

- A group cannot be treated as one KOL
- Admins must be discovered and stored explicitly
- The user must be able to map Telegram identities to custom KOL labels
- Statistics must run per source poster, with optional rollups by group

## Historical Backfill Strategy

Backfill in stages:

1. Pull the most recent 90 days for all selected groups
2. Extend high-value groups to 180 days or more
3. Continue older backfill only when a source appears promising

Store sync checkpoints per chat so historical backfill and continuous listening can coexist safely.

## Incremental Sync Strategy

After the initial backfill:

- Listen for new posts in target groups
- Capture edits because stop/TP values may be changed later
- Preserve reply graphs to improve trade merging
- Download only relevant images for OCR and audit

## Parsing Strategy

### Text

Apply normalization and rule-based extraction first:

- Symbol aliases
- Long/short intent
- Entry ranges
- TP and SL markers
- Leverage and risk descriptors

### Images

Use OCR only for images in v1, not for voice or video. Run the same parser on OCR output, then merge results with the image caption and surrounding message context.

### Confidence and Review

Confidence should be high when:

- Text is explicit
- Symbol and direction are unambiguous
- Entry and risk parameters are clear
- Reply relationships are intact

Low-confidence signals should remain reviewable and should not automatically affect the strict leaderboard.

## Performance Metrics

Produce at least two leaderboard views:

- `Strict`: high-confidence, closed, confirmed trades only
- `Expanded`: includes medium-confidence trades with visible quality warnings

Report metrics by:

- Source poster
- Time range: 30d, 90d, all-time
- Symbol where useful
- Group where useful

## First-Version Outputs

V1 should focus on:

- Local database
- CLI or static report generation
- KOL leaderboard export
- Drill-down into each KOL's reconstructed trades
- Simple alerting for new high-confidence signals

Do not begin with a heavy web dashboard. Trustworthy data should come first.

## Risks and Mitigations

### Parsing Drift

Risk: informal message styles reduce extraction quality.

Mitigation: confidence scoring, manual review queue, strict versus expanded leaderboards.

### Mixed Posters in One Group

Risk: statistics become polluted if multiple admins are treated as one source.

Mitigation: explicit source mapping and per-sender attribution.

### OCR Noise

Risk: screenshots introduce false positives.

Mitigation: keep image-derived signals lower confidence by default unless corroborated by text.

### Re-running History

Risk: parser logic changes over time.

Mitigation: keep complete raw messages and media references so parsing can be replayed offline.

## Technical Recommendation

For v1:

- Python 3.12+
- Telethon
- SQLite
- SQLAlchemy or equivalent lightweight ORM
- Tesseract OCR through Python bindings
- Typer or equivalent CLI
- Pytest for parsing, merge, and analytics tests

This stack is fast to validate on a Mac and can later be migrated to a server with minimal architectural change.
