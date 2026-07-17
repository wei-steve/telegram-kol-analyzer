# Mobile Strategy Record Center Design

## Goal

Redesign the existing Web workbench around a mobile-first strategy record center. The primary operator task is to verify, from one place, whether AI recognition was correct, how a strategy progressed, which real Deepcoin position it belongs to, and what every position-management action did.

The design keeps the existing FastAPI/Jinja application, dark trading-console visual language, backend business rules, and live-trading safety boundaries. Desktop uses the same information architecture with higher information density; it is not a separate product.

## Confirmed Product Decisions

- Phone browsers are the primary usage environment.
- The primary object is a strategy record, not a Telegram group, raw event, or exchange position.
- The default landing view aggregates all groups and prioritizes strategies that need attention.
- Selecting a strategy opens a dedicated detail page instead of expanding a long card in place.
- Event history and exchange-position views remain available but support the strategy record rather than competing with it.
- Dangerous mutations remain inside a concrete detail and confirmation flow.

## Primary Information Architecture

The phone bottom navigation contains five destinations:

1. `策略`: the default landing destination, with global needs-attention records first.
2. `持仓`: real positions, open orders, attribution exceptions, and protection state.
3. `动态`: recent messages, recognition decisions, fills, management actions, and exceptions.
4. `群组`: per-KOL recognition controls, auto-trading state, and strategy counts.
5. `更多`: trading settings, AI configuration, prompts, profiles, and logs.

The existing standalone home destination is removed. Its service health, unrealized PnL, risk count, and pending count become a compact summary at the top of the strategy destination. `消息` becomes `动态` because the destination contains more than Telegram messages. Management batches move into strategy details and remain reachable from `更多` for cross-strategy auditing.

Desktop uses the same destinations. It may render the strategy list and selected detail side by side, but it must not maintain a second navigation state model.

## Strategy Landing View

The strategy landing view defaults to all groups and the `需要处理` filter. The header contains:

- needs-attention count;
- executing count;
- pending-entry count;
- unattributed or attribution-conflict count;
- independent Telegram, database, and Deepcoin health;
- total unrealized PnL when current exchange data is available.

Primary filters are `需要处理 / 全部 / 执行中 / 待入场 / 已结束`. A searchable group filter defaults to all groups. Within the default view, risk severity sorts first and the most recent meaningful change sorts second.

List refreshes preserve the active filter, group, scroll position, and selected record. New data does not replace visible content immediately; a persistent new-changes control lets the operator reveal it deliberately.

## Strategy Cards

Cards show only the fields needed for fast mobile assessment:

- KOL or source group;
- instrument and long/short direction;
- current lifecycle phase;
- entry range, stop loss, take profits, and real position size where applicable;
- AI recognition state;
- execution state;
- attribution state;
- latest meaningful change time;
- one concrete attention reason when intervention is needed.

Recognition states distinguish confirmed authority, manual review, semantic disagreement, and recognition failure. Execution states distinguish not submitted, open order, partial fill, filled, position management, and finished. Attribution states distinguish uniquely bound, awaiting fill, unmatched, ambiguous, and conflicting.

Normal cards remain compact. Attention cards use persistent text labels and a concise reason; meaning must never depend on color alone. No live trading mutation appears on a list card.

## Dedicated Strategy Detail

Selecting a card opens a dedicated strategy detail route. A sticky identity header shows the KOL, symbol, direction, lifecycle state, real-position state, and the current operator conclusion.

The detail contains four sections:

1. `概览`: strategy parameters, authoritative AI result, current real position, attention reason, and attribution state.
2. `运行链`: the chronological chain from original Telegram message through recognition, strategy creation, order actions, fills, management, reconciliation, and exit.
3. `真实执行`: orders, fills, position identity, TPSL state, management batches, client/exchange identifiers, and exchange evidence.
4. `原始证据`: message text, media, OCR, authoritative MiMo result, auxiliary reviews, and disagreement reasons.

Cross-navigation is bidirectional. A message links to its strategy record, a real position links back to its owning strategy, and a management batch identifies the exact strategy, binding, target position, and execution legs.

Dangerous actions such as close, bind, or protection changes remain in a detail view. Confirmation content includes the concrete instrument, side, quantity, KOL or strategy attribution, intended exchange effect, and current exchange identity. Existing backend validation, reservation, and idempotency remain authoritative.

## Data Authority And Aggregation

The Web layer builds a read-oriented record from the existing authority chain:

`message -> candidate -> lifecycle -> binding -> execution event -> reconciliation -> exchange state`

The record is a presentation model, not a second source of truth. It does not write inferred lifecycle or attribution state back to operational tables.

Real position state must come from the current Deepcoin read and reconciliation evidence. A local lifecycle label alone cannot prove that a position opened or closed. When attribution is not unique, exchange data is unavailable, or evidence is insufficient, the UI states `无法确认` and exposes the reason instead of guessing.

MiMo remains authoritative for trading-state recognition. Auxiliary semantic review can add an attention state and explanation but cannot silently replace or block the authoritative result.

## Attention Rules

The needs-attention view includes at least:

- AI field disagreement requiring source review;
- recognition failure or missing required fields;
- a lifecycle marked as entered without a unique real position;
- a real position without strategy attribution;
- ambiguous or conflicting attribution;
- missing stop protection;
- strategy protection that disagrees with live TPSL;
- a recognized management action without confirmed exchange outcome;
- stale or failed exchange verification;
- failed order submission or reconciliation exception.

Each state uses a concrete human-readable reason and a direct link to supporting evidence. The summary does not expose an unsafe one-tap corrective action.

## Loading, Refresh, And Failure States

- Initial loads use layout-preserving skeletons.
- A refresh failure preserves the last successful data and its timestamp.
- Telegram, database, and Deepcoin health are shown independently.
- Loading or failed exchange data never renders as a confirmed zero.
- Empty states distinguish a legitimate absence of records from a synchronization failure.
- New events use a non-disruptive indicator and never steal scroll position.
- Browser state restores filters, group selection, selected record, and scroll position when still valid.
- Stale asynchronous responses cannot overwrite a newer filter, group, or strategy selection.

## Visual And Accessibility Direction

- Retain the current dark trading-console identity.
- Reduce border density and simultaneous accent colors.
- Use strong numeric hierarchy for decision-critical values and lower contrast for supporting metadata.
- Pair every status color with a visible text label.
- Keep primary phone touch targets at least 44px.
- Support long Chinese text, message media, multiple take-profits, multiple orders, and multiple positions without horizontal overflow.
- Preserve visible keyboard focus and logical reading order on desktop.
- Use semantic headings, navigation labels, tabs, live regions, and explicit error text; screenshots alone cannot prove full accessibility compliance.

## Implementation Boundaries

The implementation continues to use server-rendered Jinja templates, the existing CSS, and vanilla JavaScript. It may add read-only aggregation queries, view models, and partial/detail routes. It does not introduce a new frontend framework or separate mobile application.

The change must not alter:

- MiMo authority or recognition semantics;
- strategy and lifecycle mutation rules;
- order construction or Deepcoin submission behavior;
- position-management intent or batch semantics;
- binding or reconciliation safety rules;
- live-action confirmation, validation, reservation, or idempotency.

## Validation

Automated validation covers:

- global needs-attention ordering and filters;
- group filtering and browser-state restoration;
- strategy card fields and attention reasons;
- detail aggregation across message, lifecycle, binding, execution, management batch, and exchange evidence;
- bidirectional navigation between related objects;
- stale, empty, failed, and partial-data states;
- no confirmed zero when exchange data is unknown;
- stale-request guards;
- absence of dangerous mutations on summary cards;
- preservation of existing close, bind, protection, and trading-setting safety tests;
- common phone widths including 390x844, 44px targets, safe-area padding, long content, and no horizontal overflow;
- desktop list/detail layout using the same navigation and source data.

Local verification covers deterministic queries, rendering, JavaScript, CSS, and unit behavior. Verification that depends on the Telegram session, production database freshness, Deepcoin IP allowlist, live positions, or production credentials runs on the server after a reviewed commit is pushed and deployed through the established GitHub update workflow.

## Out Of Scope

- A separate `/mobile` application.
- A new client framework.
- Changes to strategy, risk sizing, leverage, or execution semantics.
- Automatic correction of ambiguous attribution.
- One-tap close, bind, or protection mutations from the strategy list.
- Treating the Web presentation model as an operational source of truth.
