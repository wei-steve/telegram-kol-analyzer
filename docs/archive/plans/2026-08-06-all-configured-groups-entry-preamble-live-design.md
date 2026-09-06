# All Configured Groups Entry-Preamble Live Design

## Goal

Enable explicit entry-sizing preambles for every Telegram trading group already
configured in the application. A matching preamble must immediately change the
risk budget of the following real entry order; no per-chat allowlist is used.

## User-visible rule

For example, when one configured group posts:

1. `BTC 多单半仓操作`
2. a complete BTC long entry strategy

the second message is sized from `configured BTC maximum-loss budget × 50%`.
If BTC is configured for 20 USDT maximum loss, the real order is sized from 10
USDT. An entry without a compatible preamble continues to use 20 USDT.

## Scope

“All groups” means every group present in the application's configured trading
group set. Messages from unconfigured Telegram groups are not brought into the
trading path. Once `entry_preamble_mode=live`, no additional chat allowlist is
required or consulted.

The application-level fallback remains `disabled` so a missing or reset
production setting cannot silently enable trading behavior. Production is
explicitly set to `live` after deployment and verification. The same setting is
the immediate rollback switch.

## Matching and safety

The existing deterministic rules remain authoritative:

- the preamble and strategy must be in the same chat;
- symbol and side must match;
- Telegram source order, not worker completion order, determines precedence;
- only `半仓` and explicit percentages are supported;
- `半仓` always means maximum-loss budget multiplied by `0.5`;
- vague language such as `轻仓` or `重仓` does not change sizing;
- an intervening entry, cancellation, replacement, edit, or deletion forms a
  hard boundary;
- incomplete recognition, multiple eligible preambles, invalid evidence, or a
  rollout downgrade blocks the affected entry before any exchange request;
- a strategy with no eligible preamble uses its configured full risk budget.

Durable evidence continues to record the configured budget, multiplier,
effective budget, both Telegram message IDs, and assembly fingerprint.

## Configuration and UI

`entry_preamble_mode` retains `disabled`, `shadow`, and `live` for operational
compatibility. `live` now means all configured trading groups. The obsolete
`entry_preamble_live_chat_ids` field is ignored by execution and removed from
the operator form. Existing stored values may remain in SQLite for backward
compatibility but have no effect.

Operator wording must avoid “shadow” as the primary explanation:

- disabled: do not use an earlier sizing message;
- test only: recognize and record, but do not change real orders;
- live for all configured groups: change the next matching real order.

## Prompt publication

The production `trading.analysis.shared` prompt must be updated through the
existing draft, validation, historical-test, and publication workflow. Startup
seeding never overwrites an active production prompt. Historical tests must
cover both MiMo and DeepSeek with current active prompt versions before
publication.

The live switch is not enabled until the published prompt contains the reviewed
`entry_context` contract and historical cases confirm standalone preambles,
complete strategies, management messages, vague sizing language, and malformed
output behavior.

## Deployment and verification

Before restart or activation, verify there is no active strategy-management
batch, submitted position mutation, recovery claim, or time-sensitive incoming
strategy operation. Deploy code first with the feature disabled. Then publish
the prompt and set `entry_preamble_mode=live`.

Verification uses controlled persisted fixtures and read-only checks rather
than waiting for a specific KOL to repeat the wording. The final production
checks must prove:

- all configured chats enter the same live path without an allowlist;
- `20 × 50% = 10 USDT` is applied before contract quantities are calculated;
- no-preamble entries retain 20 USDT;
- ambiguous or unresolved preceding context blocks before exchange writes;
- monitoring reports no entry-preamble invariant violations;
- Web evidence contains only bounded identifiers and risk values.

## Rollback

Set `entry_preamble_mode=disabled`. Any unfinished persisted assembly is blocked
rather than resumed under different sizing. Existing orders and positions are
managed through their normal attributed-position workflow; historical evidence
is retained and no Telegram entry message is replayed.

## Testing

Add coverage for all configured chats applying live multipliers without an
allowlist, disabled rollback, explicit percentages, ordinary entries, mismatch,
ambiguity, source-order recognition gaps, prompt-shaped null strategies,
cross-chat identities, UI wording, safe persistence, and the exchange-write
boundary. Run focused tests locally and on the server, followed by the full
local suite with unrelated baseline failures reported separately.
