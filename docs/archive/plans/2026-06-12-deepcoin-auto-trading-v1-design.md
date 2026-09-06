# Deepcoin Auto-Trading V1 Design

## Goal

Build the first conservative automation layer for Telegram KOL trading signals: parse more Chinese strategy text, classify whether a signal can be considered for automated trading, and keep all KOLs notify-only by default.

## Scope

V1 does not place Deepcoin orders. It prepares the local decision layer that future browser automation can consume after explicit user enablement.

## Approach

The existing ingestion flow remains unchanged: Telethon stores raw messages, `persist_text_signal_candidates` parses text into `SignalCandidate`, and merge logic groups candidates into trade ideas. V1 improves the parser and adds an execution-decision module beside the parser instead of changing realtime ingestion semantics.

## Components

- `parsing/text_parser.py` recognizes common Chinese BTC/ETH formats, including `做多`, `做空`, `方向：多`, `建仓`, `现价`, `市价`, `止损`, `止盈`, and close/update phrases.
- `group_config.py` supports per-group and per-sender trading mode fields. Defaults remain conservative: every group and sender is `notify_only` unless configured otherwise.
- A new decision module converts parsed signal/candidate-like data into one of `notify_only`, `manual_review`, or `eligible_for_auto_trade`, with machine-readable reasons.

## Safety Rules

- Only BTC and ETH are eligible by default.
- Missing stop loss always requires manual review.
- Image/vision-derived signals always require manual review in V1.
- Reverse, duplicate same-KOL same-symbol same-side, or unmatched temporary exit conditions require manual review.
- Notify-only KOL configuration always blocks auto trading.

## Testing

All behavior is test-first. Parser tests cover observed Chinese examples. Config tests verify safe defaults and explicit opt-in. Decision tests verify whitelist, stop-loss, provenance, duplicate-position, and notify-only gates.

## Restart Recovery

On service restart, the system scans the previous 48 hours of `auto_trade` KOL signals. V1 recovery only produces decisions; it does not place orders directly.

Recovery is conservative:

- Notify-only KOLs are skipped.
- If price touched the entry range after the signal was posted, the trade is treated as missed and requires manual review.
- If current price is inside the entry range, the trade requires manual review.
- If an existing order or same-KOL active position is found, the trade requires manual review.
- Only signals whose entry range was never touched can become `eligible_for_recovery_limit_order`.

The database candidate builder joins `SignalCandidate`, `RawMessage`, and `Source`, then applies `GroupConfig` runtime settings. Group-level auto trading requires a configured `chat_id`; sender-level auto trading requires a matching tracked sender by Telegram sender id or display name. This keeps unmatched groups notify-only by default.

Market data is injected through a thin provider interface with `load_candles(symbol, start_at, end_at)` and `get_current_price(symbol)`. Recovery evaluation asks for candles from the signal timestamp through the restart scan `now`, then applies the same manual-review gates. V1 keeps the provider abstract; real Deepcoin or exchange adapters can be added behind this interface later.

Deepcoin read-only account state is also injected. V1 maps raw read-only position/order payloads into normalized `ActivePosition` and `OpenOrder` objects only when a local binding exists for `pos_id` or `order_id`; unbound exchange state is not attributed to a KOL signal. A later safety pass should treat unbound live exchange state as a global manual-review blocker before any browser execution is enabled.

Execution bindings are persisted locally in `execution_bindings` with a unique key of `venue + chat_id + message_id + symbol + side`. The table stores `kol_id`, `order_id`, `pos_id`, and `status`; only `open` and `active` Deepcoin bindings are loaded for recovery account-state checks.

The `recovery-dry-run` CLI command is a safe orchestration entrypoint. It loads config and the local database, but V1 intentionally does not create a live market-data provider inside the CLI; without an injected provider it exits with a clear unavailable message. Tests cover the full flow with fake providers.

The preferred concrete market-data provider is Gate public USDT futures data, enabled explicitly with `--market-provider gate`. It uses public futures candlestick and ticker endpoints for BTC/ETH USDT checks and remains read-only. Binance public spot data remains available with `--market-provider binance` as a fallback screening source; a Deepcoin-native market-data provider can replace both later if Deepcoin exposes stable public candle/ticker endpoints.

Dry-run decisions can be persisted with `recovery-dry-run --persist`. Results are upserted into `recovery_decisions` by `chat_id + message_id + symbol + side`, including the KOL id, action, reason codes JSON, entry range text, risk amount, and run timestamp. This gives the web workbench a stable review source without creating orders.
