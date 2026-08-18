# Runtime Serialization Remediation Design

**Goal:** Remove the structural causes that let a single slow network call stall
the whole trading system, and let messages from different Telegram groups be
recognized and executed independently, without rewriting the existing
recognition, contextual resolution, or Deepcoin execution logic.

All line anchors in this document and in the phase files were verified against
commit `2fc0ad2`. Where a line number has since drifted, the symbol named beside
it is authoritative.

## Problem statement

The system intermittently fails to execute trades for some groups' messages, and
intermittently stalls entirely. Both symptoms trace to three structural defects
in the runtime layer, not to defects in the trading logic.

### Defect 1 — Synchronous blocking calls run directly on the asyncio event loop

`run_strategy_management_worker_loop` calls `run_strategy_management_worker_tick`
directly inside an `async def` with no `asyncio.to_thread`
(`src/telegram_kol_research/strategy_management_worker.py:923`). The same pattern
exists in `run_break_even_convergence_worker_loop`
(`src/telegram_kol_research/break_even_convergence_worker.py:344`).

Those ticks reach `load_deepcoin_execution_reconciliation_snapshot`, which calls
`list_positions` and `list_open_orders` through `DeepcoinClient`. That client uses
`httpx.Client` — a synchronous blocking client with a 15 second default timeout
(`src/telegram_kol_research/deepcoin_client.py:301`). A tick processes up to 10
batches.

While such a tick runs, the event loop is blocked. Telethon cannot deliver new
messages, SSE cannot push, HTTP handlers cannot answer, and every other worker
is frozen. This is the mechanical cause of "stuck at some stage".

`src/telegram_kol_research/web_app.py:279` already contains the comment
"outside the saturated shared executor", which is prior evidence of the same
class of problem.

### Defect 2 — One global lock serializes every group

`handle_new_message` holds a single process-wide `asyncio.Lock`
(`src/telegram_kol_research/web_app.py:4592`) across the entire processing chain:
persist, media download, OCR, MiMo recognition (LLM, up to 120s timeout),
contextual resolution, `auto_trade_executor`, and Deepcoin order submission
(`src/telegram_kol_research/telegram_live_listener.py:624`).

`run_periodic_reconcile` takes the same lock
(`src/telegram_kol_research/telegram_live_listener.py:1091`) and, while holding
it, replays up to 50 messages through the full recognition and execution chain.
The manual refresh endpoints take it too
(`src/telegram_kol_research/web_app.py:6783`, `:7457`).

Consequently a slow message in group A delays every message in group B by the
full duration of A's LLM and exchange round trips. This is the mechanical cause
of "messages from certain groups do not produce trades" — the instruction is not
misrecognized, it is never reached in time.

The lock granularity is wrong. Cross-group and cross-symbol work shares no
mutable state that requires mutual exclusion. The state that genuinely requires
exclusion is per-position, and it already has its own boundary in
`src/telegram_kol_research/position_authority_lock.py`.

### Defect 3 — Processing is inline in the event callback, with a hard expiry

There is no durable job queue. The full chain runs inside the Telethon callback.
A restart, an exception, or a stalled loop loses the in-flight message.

The only compensator is the reconcile pass, which selects raw messages with no
`RecognitionDecision` (`src/telegram_kol_research/telegram_live_listener.py:804`).
It runs every 300 seconds, and `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE` is 15 minutes
(`src/telegram_kol_research/telegram_live_listener.py:64`). A stall longer than
15 minutes marks the backlog expired and it is never executed. The compensator
also only covers messages with no decision at all; a message that was recognized
but failed during order submission falls to a different repair path.

## Second-order evidence

The instability has been treated by adding modules rather than by fixing the
runtime layer. Measured on 2026-08-18:

| Metric | Value |
|---|---|
| Modules in `src/` / lines | 206 / 158,980 |
| Modules whose name is repair/recovery/reconcile/remediation/cleanup/rescue/backfill/convergence | 39 (19%) |
| Database tables | 80 |
| Plan and design documents in `docs/plans/` | 308 |
| Commits total / since 2026-06-01 | 1500 / 1497 |

No repair module can compensate for a blocked event loop, because every repair
module runs on the same blocked loop.

## Target architecture

Receive, decide, and execute must be decoupled, and execution must never block
reception.

```text
Telethon listener      responsibility: persist + enqueue only, returns in ms
      |                never calls an LLM, never calls the exchange
      v
raw_messages + message_processing_jobs   (durable, status state machine)
      |
      v
recognition workers    sharded by chat_id, concurrent across chats
      |
      v
instruction rows       (durable, attempt_count + next_attempt_at)
      |
      v
execution worker       serialized per position/symbol, parallel across symbols
      |                idempotent submission keyed by client order id
      v
reconciliation worker  independent cadence, read exchange -> correct DB,
                       never blocks any path above it
```

Four properties the current system lacks and the target requires:

1. The listener performs no network work beyond persisting the message.
2. Locks are keyed by position or chat, never process-global.
3. Job state and retry scheduling live in the database, so a restart resumes.
4. Expiry is a business decision made against current market state, not a
   side effect of the system having been stalled.

## Phase decomposition

The remediation is split so that exactly one phase is executed per session, and
each phase file is self-contained. Order is by risk-adjusted benefit: phases 1
and 2 alone are expected to remove the majority of the observed symptoms.

| Phase | Name | Nature | Session-sized |
|---|---|---|---|
| 0 | Loop health observability | Additive, read-only | Yes |
| 1 | Unblock the event loop | Behavior-preserving | Yes |
| 2 | Per-chat lock sharding | Concurrency change | Yes |
| 3 | Compensation window repair | Recovery semantics | Yes |
| 4 | Durable job table, shadow enqueue | Additive, dormant | Yes |
| 5 | Queue consumer takeover | Flagged cutover | Yes |
| 6 | Process separation | Deployment change | Yes |

Phase 0 must run first because it produces the measurement that proves phase 1
worked. Phases 1 and 2 are independent of 4 through 6 and deliver value alone.
Phase 5 must not start before phase 4 has run in shadow long enough to prove
enqueue parity.

## Safety rules inherited from the project workflow

These are not new rules; they are restated here because each phase file is read
cold and must not lose them.

- Every phase is introduced dormant or shadow-only where it changes behavior,
  preserves the current production path, and has a tested disable path before it
  may be enabled.
- Never deploy or restart during an active time-sensitive strategy operation. If
  a safe window cannot be proven, finish local work, leave the phase
  `in_progress`, and record the exact server verification still outstanding.
- Never implement more than one phase in one user turn.
- The existing first-pass recognition and contextual multi-information strategy
  resolution remain authoritative. This remediation changes when and on which
  thread they run, never what they decide.
- Real verification runs on the server, because the Telegram session, the
  Deepcoin IP allowlist, and production keys only work there.

## Non-goals

This remediation does not change recognition prompts, instruction semantics,
position attribution, protection ledger logic, or any trading decision. It does
not delete existing repair modules; once the runtime layer is sound, retiring
them is separate work with its own evidence requirement.
