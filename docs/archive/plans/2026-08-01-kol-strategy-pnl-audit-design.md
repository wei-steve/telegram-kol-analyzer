# KOL Strategy PnL Audit Design

## Goal

Build a read-only, evidence-backed audit of the BTC and ETH strategies posted
by `币圈所长会员群-11分组`. The audit must reconstruct strategies from
raw Telegram messages, replay public market data under explicit rules, and
produce a durable report without changing production strategy records.

## Scope

The first audit covers all available raw messages for Telegram chat
`-1002368892075` through the audit cutoff. It evaluates BTC and ETH strategy
instructions only. Promotional posts, retrospective profit claims, general
market commentary, and strategies for other instruments are excluded.

This phase does not repair `strategy_lifecycles`, change recognition results,
place orders, restart the service, or deploy code during strategy activity.

## Alternatives Considered

### Read-only independent audit

Reconstruct a separate ledger from `raw_messages`, replay market data, and
write report artifacts. This is the selected approach because it provides an
independent result and cannot corrupt production state.

### Direct production-record repair

Correct `strategy_lifecycles` while auditing. This would make existing views
look complete sooner, but mixes research judgments with operational state and
creates unnecessary repair risk.

### Shadow audit tables

Persist a second ledger beside operational lifecycle tables. This is a useful
future extension, but requires schema, migration, and UI work beyond the first
audit.

## Architecture

The audit has four pure stages:

1. Load a stable snapshot of the target chat's raw messages.
2. Reconstruct logical BTC and ETH strategies plus their management events.
3. Replay immutable public candles against each reconstructed strategy.
4. Render machine-readable JSON and a human-readable Markdown report.

Production access is read-only. Market-data adapters may read Binance public
candles and cache the returned payload under a local ignored audit directory so
the same evidence can be replayed without making later results dependent on a
new request.

## Strategy Reconstruction

Each logical strategy receives a stable audit identity derived from the chat,
the first strategy message, symbol, side, and strategy ordinal within that
message. One message may create more than one strategy, including two
strategies for the same symbol when it contains distinct entry/stop/target
instructions.

The reconstruction rules are:

- A message must contain an actionable entry instruction and enough fields to
  evaluate entry and risk. Ambiguous commentary remains excluded or unresolved.
- Reposts and explicit continuation messages extend the original strategy and
  do not create another trade.
- A materially different entry, side, stop, or target plan creates a new
  revision or replacement according to the message wording and context.
- Explicit cancel, replace, partial-close, protection, and full-exit messages
  attach only when the target strategy is unambiguous.
- Profit screenshots and claims are evidence for comparison, never the source
  of the independently calculated result.
- Every inclusion, exclusion, merge, split, and event link records its source
  message IDs and a reason.

Pending entry instructions stay valid until the KOL explicitly cancels,
replaces, or invalidates them, or until the audit cutoff. A repeated instruction
extends the same logical strategy. The audit does not impose the operational
system's fixed expiry window.

## Entry and Position Sizing

A single entry price receives 100% of the strategy allocation. A true range or
explicit head-position/add-position instruction uses the project's current
deterministic entry-leg allocation. If the KOL gives a percentage, the explicit
percentage wins.

An entry leg fills only after its publication time and only when the public
candle trades through its price. The weighted entry price uses filled legs
only. Unfilled legs remain pending until cancelled or the strategy terminates.
The audit must not infer an entry from a KOL profit claim.

## Take-Profit and Stop Rules

Explicit KOL allocations take precedence. Otherwise staged take profits use:

- two targets: `50/50`;
- three targets: `40/30/30`;
- four targets: `40/20/20/20`;
- five targets: `40/15/15/15/15`.

Targets execute in favorable-price order. The remaining position moves to
break-even only after an explicit protection or break-even instruction from
the KOL.

A price stop triggers on touch. A five-minute, fifteen-minute, hourly, or other
close-qualified stop triggers only when the matching completed candle closes
beyond the stop condition. An explicit KOL exit uses the first public candle
available after the exit message as the auditable exit price.

When entry, stop, and target ordering cannot be resolved inside the same source
candle, the audit chooses the result unfavorable to the KOL and records
`intrabar_order_uncertain`.

## PnL Metrics

The primary result is unleveraged net `R`, before exchange fees and funding:

`R = signed realized price movement / initial stop-risk distance`.

Partial exits are allocation weighted. Reports also include unleveraged return
percentage, entry status, targets reached, exit reason, realized versus open
allocation, and result confidence. Fees, funding, slippage, and actual account
PnL are not invented; those require a separate Deepcoin-fill audit.

Summary metrics include strategy count, entered count, unresolved count,
profitable count, loss count, break-even count, win rate, cumulative `R`,
average `R`, profit factor, maximum drawdown in `R`, and maximum loss streak.
BTC and ETH are shown separately and combined.

## Evidence and Confidence

Every audit row contains:

- original and management message IDs and timestamps;
- normalized strategy fields and reconstruction decisions;
- candle source, interval, and relevant timestamps/prices;
- each fill and exit allocation;
- final status, calculated PnL, and reason codes;
- comparison with current lifecycle records and KOL claims.

`high` confidence requires explicit strategy fields and unambiguous event and
candle ordering. `medium` allows a documented normalization such as an obvious
typographical correction. `low` or `unresolved` results are excluded from the
strict headline metrics and listed separately.

## Outputs

The audit writes timestamped files beneath an ignored local audit directory:

- normalized strategy ledger JSON;
- candle evidence/cache metadata;
- full machine-readable result JSON;
- Markdown report containing BTC/ETH summaries and per-strategy details;
- recognition gaps, duplicates, corrupt fields, and lifecycle disagreements.

No report contains credentials, Telegram session material, or private API
values.

## Failure Handling

Missing messages, incomplete price history, ambiguous attribution, malformed
prices, and inconsistent timestamps fail closed into an unresolved result.
They do not become assumed wins or losses. Network fetch failure preserves any
existing immutable cache and prevents publication of a newly claimed final
result.

## Verification

Pure unit tests cover reconstruction, duplicate merging, composite-message
splitting, entry-leg fills, staged take profits, explicit protection, timeframe
stops, manual exits, intrabar ambiguity, and summary metrics. Fixture-based
integration tests build an audit from messages and candles without server or
network access. The final live verification reads the production database and
public market data without writes, then checks deterministic reruns against the
cached evidence.
