# Development Handoff

## Purpose

This repository ingests and analyzes Telegram KOL strategy material and provides research, review, reporting, and controlled execution components. The Mac mini is a development workstation only; production remains on the server.

## Project map

- `src/telegram_kol_research/`: Python application and CLI.
- `tests/`: automated checks.
- `config/*.example.yaml` and `config/development.env.example`: safe configuration templates.
- `docs/runbook.md`: local operating guide.
- `docs/server-deployment.md`: production topology and deployment guide.
- `AGENTS.md`: mandatory project workflow and deployment constraints.

## Boundary

Production Telegram sessions, databases, Deepcoin credentials, API secrets, and live-trading authority stay on the production server. Do not copy them to the Mac mini or into Git. Development-only values, if needed, are retrieved manually from a password manager.

## Deployment ownership

Use GitHub as the code-transfer channel. After a reviewed commit is pushed to `codex/deepcoin-auto-trading-v1`, use `./scripts/server_git_update.sh` on macOS/Linux or `scripts/server_git_update.ps1` on Windows from an approved workstation to update the server. Both helpers invoke the server update command, which reinstalls the editable package and restarts `telegram-kol.service`.

## Exact stop-protection handoff

The protection chain is `entry fill -> exact posId -> primary stop ownership ->
verified second stop -> staged take profit`. Persisted `ordId ↔ posId ↔ entry
leg` ownership is authoritative when a Deepcoin read-back omits a position ID;
an explicit different position ID is a conflict. The default second-stop offset
is 20 bps and it uses `closePosId` plus market execution.

Before any production repair, deploy reviewed code, run a server dry run, and
record its fingerprint, exact candidate evidence, deployed SHA, and service
state. A real apply is always one `posId` at a time with that exact fingerprint.
Known failed-primary positions are excluded from normal repair. Do not retry a
`NotEnoughMoneyToClose` failure automatically; preserve any verified second
stop and keep automatic management frozen.

### Delayed-entry exit and stop-rescue invariants

- A contextual full exit is authoritative only when the resolver selects one
  exact current-risk strategy. The low-confidence exception is closed: it
  requires the internal authority marker, exactly one target, confidence at
  least `0.60`, and no competing current or uncertain risk. Model-supplied
  fields cannot create that marker.
- A full exit owns the whole strategy: it closes every exact live position and
  cancels every remaining deferred entry leg. It must not leave a delayed leg
  able to reopen the strategy later.
- Trigger-entry TP/SL observed before the entry fill is unowned. It may not be
  adopted from symbol, side, price, or timing similarity; the post-fill exact
  `posId` evidence remains mandatory.
- A complete snapshot proving one live verified position with no verified or
  pending primary stop may enter the stop-rescue workflow. Rescue is stop-only,
  exact-`posId`, idempotent, and records bounded refusal/recovery state.
- `trigger_protection_stop_rescue_mode` defaults to `disabled`. Effective
  `live` additionally requires automatic trading enabled and management mode
  `live`; enabling it is a separate reviewed production approval.
- Close authority always wins over SL, backup-SL, and TP writes. An active
  close reservation, mutation, or management batch blocks protection writes.
- A critical unprotected position blocks only new automatic entries for the
  same Telegram `chat_id`. Exact close, cancel, reconciliation, and stop rescue
  remain available; other chats are unaffected.
- An absent exact position converges to terminal only with sufficient evidence.
  Unknown or conflicting history remains visible as `uncertain_risk` for manual
  review and is never silently removed from current-risk competition.

### 2026-08-03 delayed-entry rescue shadow rollout

- Reviewed/deployed commit: `fa1a1c85d113c59c567b9f66c97560afdad9bd7d`.
- Production service restarted at `2026-08-03 19:27:51 CST`; subsequent
  service state was `active`, the local HTTP health request returned `200`, and
  the post-restart warning journal was empty.
- The production focused suite passed: `603 passed` with deprecation warnings
  only.
- The pre-restart safe-window check found zero running recognition decisions,
  zero management submissions, zero active/unknown position mutations, zero
  active stop rescues, and zero new messages in the final two-minute window.
  Historical residue was listed separately: one old `partial_failed` and six
  old `recovery_required` management batches, 29 old submitted close
  reservations, three old pending instruction items, and one old unknown item.
  None of the live positions intersected an active close reservation or rescue.
- Stop rescue was absent from persisted settings before deployment, therefore
  `disabled` by default during restart. After passive health and test checks,
  only `trigger_protection_stop_rescue_mode` changed to `shadow`; every other
  trading setting compared equal before and after the update.
- Shadow baseline and immediate post-enable counts were both zero rescue rows
  and zero `stop_rescue_shadow_ready` incidents. No natural message or eligible
  trigger-fill sample arrived after deployment, so natural-sample shadow
  evidence remained pending at that checkpoint. A later explicit operator
  instruction enabled `live` without restarting the service. The setting is
  now effective only through the existing automatic-trading and live-management
  gates.

### 2026-08-03 production monitor history recovery

- Reviewed/deployed commit: `1401f0ff92a4e84c6de13e2eeba888807ec44e52`.
  The service is active, the loopback settings endpoint returns HTTP `200`, and
  the post-deployment warning journal is empty.
- Local verification passed the complete suite with the one documented legacy
  CLI monitor smoke case deselected. The final local recovery-focused suite
  passed `254 passed, 1 deselected`; post-deployment production verification
  passed `180 passed, 1 deselected` for the recovery, monitor, and CLI smoke
  coverage.
- The trading-settings fingerprint stayed
  `27c3be58cf009c0d041d4e4f3ef504b88f8a5cf7f82cd0fd5aab471ebfd31d29`
  across both deployments. Stop rescue remained effective `live`; no trading
  setting changed.
- The stable post-deployment management audit classifies 38 completed
  fail-closed rows as `terminal_blocked`, with zero actionable `blocked`, zero
  `partial_failed`, two `recovery_required`, and zero `submit_unknown`. The
  scheduled monitor therefore remains intentionally non-green with only
  `audit_abnormal`; it is reporting the two genuine unresolved rows rather than
  the 38 terminal historical rows.
- Exact dry runs converged batch 28 from restored-protection history after both
  exact positions were absent, batch 38 from one exact filled close order plus
  exact position absence, batch 86 from complete no-submission evidence, and
  batches 23 and 40 from exact closed-position history whose position ID, full
  closed size, timestamp, durable successful submission response, exchange
  order ID, and client order ID all matched. The workflow appended exactly five
  `management_history_recovery` events and zero other execution events after
  the first recovery.
- Batches 17 and 22 had remained `recovery_required` because their available
  closed-position history represents the whole position lifetime: batch 17
  followed a confirmed 10-of-20 partial close with a 10-of-10 close; batch 22
  followed confirmed 3-of-5 and 3-of-6 partial closes with exact 2-of-2 and
  3-of-3 closes. Commit `5002486` adds a fail-closed cumulative-chain proof for
  this Deepcoin history shape. It was deployed in server HEAD `665448f`; fresh
  dry runs returned `terminal_position_history_confirmed`, and two separately
  proven quiet windows applied batches 17 and 22 one at a time. Both batches
  are now `succeeded`, all three legs are `confirmed`, and exactly two
  `management_history_recovery` events were appended with no other execution
  event or exchange mutation.
- The apply for batch 28 occurred while eight new raw messages were inside the
  two-minute observation window because the first shell wrapper did not stop
  after its read-only gate returned nonzero. At that point execution ownership,
  management work, position mutations, rescues, and exchange execution events
  were all zero; the fingerprint compare-and-set passed and the operation made
  no exchange call. The later batch 38 and 86 applies used a corrected
  fail-fast wrapper and separately proven quiet windows.
- Live-WAL audit churn now falls back to SQLite's read-only online backup after
  the original no-atime component-copy path detects a transient change. The
  resulting private snapshot validates before audit; rollback-journal and other
  non-transient failures still fail closed.

## Handoff checklist

- Read `AGENTS.md`, `docs/runbook.md`, and `docs/server-deployment.md`.
- Confirm the branch and Git remote without recording private URLs or identities in this file.
- Use `scripts/preflight_mac_migration.ps1` before moving to a new workstation.
- Store only non-secret decisions and operational notes in repository documentation.
- If a credential is discovered in Git history, revoke or rotate it; deleting a working-tree file is insufficient.

## Server production safety monitor

Production safety monitoring runs independently through
`telegram-kol-monitor.service` and a persistent 30-minute
`telegram-kol-monitor.timer`. The dedicated unprivileged monitor identity has
no capabilities or system-bus access. Its root-owned monitor-only environment
contains the frozen expected HEAD and system-operator bot fields only; it must
never contain Deepcoin credentials or reuse the checkout's general environment.
Its independent writable state lives outside the trading database. Normal trade notifications are not
duplicated; the monitor alerts only on actionable system abnormalities and has
no authority to restart `telegram-kol.service`, change settings, write the
production database, or mutate Deepcoin state.
Production-monitor notifications are deterministic system messages, not AI
Agent output. Their fixed sections explain what happened, current impact, the
required operator action, notification source, and bounded diagnostic data.
Severity is `🔴 立即处理`, `🟡 稍后核查`, or `🔵 状态提醒`; critical unchanged
problems remind after six hours, while stable historical audit residue sends
once. Audit details show at most 10 exact actionable batch IDs and never raw
message, exchange, exception, or credential data. Recovery from an audit cause
is announced only after a new, complete, healthy management audit.

Version drift is deployment context, not a production-safety failure by itself.
Current and expected HEADs may remain internal diagnostic facts, but unrelated
version hashes are omitted from the human-facing alert. 版本号不参与
`audit_abnormal` fingerprinting. Frequent reviewed deployments therefore do not
retrigger the same historical management notification.

### Human-readable monitor rollout evidence (2026-08-04)

- Reviewed commit `087f992` was deployed from
  `codex/deepcoin-auto-trading-v1`. Local verification reported 3,241 passed,
  one skipped, and one pre-existing obsolete compact-summary test deselected;
  the deployed focused monitor suite reported 228 passed with that same test
  deselected.
- The pre-restart quiet-window gate kept the latest raw message and completed
  decision at 9286 for a bounded 30-second recheck. Recognition, evidence,
  context, management, position-mutation, stop-rescue, runtime-claim, recovery,
  and recent execution-event work in flight were all zero.
- The installed no-notify diagnostic completed its bounded result with
  `monitor_error=null`; its only reason was the known `audit_abnormal`
  historical baseline. A separate pure formatter simulation rendered the
  approved yellow message for management batches 17 and 22, included the
  system/non-AI source statement, and contained no Git hashes. No Telegram
  delivery was forced during rollout.
- After installation, `telegram-kol-monitor.timer` was enabled and active,
  `telegram-kol.service` was active, and the loopback root route returned HTTP
  200. The five-field monitor state remained owned by
  `telegram-kol-monitor:telegram-kol-monitor` at mode `0600`.
- The Runtime Agent and scanner remained enabled/active with their existing
  `management_partial_failed` Agent/Telegram selectors. Agent action authority
  remained false and both shadow and action playbook allowlists remained empty.
- Immediate monitor rollback is
  `systemctl disable --now telegram-kol-monitor.timer`. A code rollback still
  requires reviewed Git reverts followed by the normal server update helper;
  do not edit the production checkout or monitor environment by hand.

On each reviewed server deployment, first run
`systemctl disable --now telegram-kol-monitor.timer`, then require both
`systemctl is-enabled --quiet telegram-kol-monitor.timer` and
`systemctl is-active --quiet telegram-kol-monitor.timer` to be false. Run
`./scripts/install_server_monitor.sh --expected-entry-preamble-mode <approved-mode>`
only from `/opt/telegram-kol-analyzer`.
Its default is install-only and fails closed if an old timer remains enabled or
active. Complete the runbook's never-enabled static no-notify diagnostic unit
and its single instruction for the installed, never-enabled static labelled-
notification oneshot before running
`./scripts/install_server_monitor.sh --enable --expected-entry-preamble-mode
<approved-mode>`. Status and monitor-only rollback
commands, including removal of the never-enabled diagnostic and test-
notification units and
persistent timer-state cleanup with
`systemctl clean --what=state telegram-kol-monitor.timer`, are maintained in
`docs/runbook.md` and `docs/server-deployment.md`. The initial root-only
credential source is `/etc/telegram-kol-monitor.credentials`.

## Mobile-first web workbench

The Web console is now a mobile-first strategy record center. Its five primary
destinations, in order, are `策略`, `持仓`, `动态`, `群组`, and `更多`.
`策略` is the phone landing destination and defaults to all groups plus
`需要处理` (`needs_attention`), so recognition, execution, position-management,
and attribution exceptions are visible without first finding a Telegram group.

A strategy record is a read-only projection for observation. It is not an
operational source of truth and must never write inferred state back to the
recognition, lifecycle, binding, execution, management, or reconciliation
tables. The evidence/authority chain is:

`message -> candidate -> lifecycle -> binding -> exchange state`

The detail view may also display execution events, management batches,
reconciliation evidence, and AI audit records, but those source records retain
their existing authority. MiMo remains authoritative for recognition decisions.
Current Deepcoin reads and exact reconciliation/binding evidence remain
authoritative for real-position state. Deepcoin unavailable, stale, or failed
is `unknown`; it is never displayed as confirmed zero or as proof that no
position exists.

Mobile is the primary layout. Desktop enhances the same hierarchy with a left
navigation rail and wider content area. Do not create a separate `/mobile`
application or duplicate backend actions. The release gate includes a real
browser check at 390x844 and 1440x900 using server-served, current data. Verify
44px touch targets, safe-area padding, long evidence wrapping, no horizontal
overflow, and unobstructed navigation/content.

Low-risk actions such as refresh, filtering, navigation, and opening details can happen directly. Position close, live-position binding, and trading-setting changes remain detail-only actions with explicit confirmation, pending-state feedback, and duplicate-submit protection.

Strategy list/detail cross-links and links from `动态` messages or `持仓` rows
must use unique authoritative identifiers only. A message-to-strategy link uses
the selected `SignalCandidate` ID and must fail closed when it resolves to more
than one lifecycle. A position-to-strategy link uses the unique
`execution_binding_id` relation. Never reconstruct ownership from a matching
chat, message, symbol, side, label, or card text.

The workbench shell uses destination-level lazy loading. `GET /` must not call
Deepcoin or embed group strategy/config/profile/trading-form data. `策略` loads
after first paint; `持仓`, `动态`, `群组`, and `更多` load when opened. Restoring
filters or group selection must not replace a newer request or steal list scroll
position. Focus/visibility recovery requests remain coalesced.

The `群组` destination remains the desktop three-column observation surface:
group list, original per-group strategy list, and the companion message/detail
panel. Its root shell must keep a scoped `data-detail-panel` host inside the
`groups` workbench panel so `/groups/{chat_id}/detail` can render next to the
strategy list. Client code must resolve detail panels by active workbench view
instead of writing to the first global `[data-detail-panel]`, because `activity`
also owns a detail panel.

Standalone strategy-record routes (`/strategy-records` and
`/strategy-records/{lifecycle_id}`) stay read-only and outside the workbench
shell, but they must expose a visible `完整工作台` navigation block linking to
`/?view=strategies`, `/?view=positions`, `/?view=activity`, `/?view=groups`, and
`/?view=more` so desktop and phone users can recover the same five primary
destinations without guessing the root route. Desktop renders that block as a
left rail so the sidebar does not appear to disappear when moving between the
workbench shell and standalone record/detail routes.

Dangerous-operation boundaries are unchanged. Strategy summary cards contain no
close, bind, TPSL, order, or trading-setting mutation. Existing detail-only
confirmation, backend validation, reservation, idempotency, and fail-closed
ownership checks remain authoritative.

Local implementation verification does not prove live behavior. Production
verification requires a reviewed commit pushed through GitHub, server pull and
service restart, route/asset HTTP checks, then read-only inspection of at least
one real record across MiMo recognition, source message, lifecycle, binding,
exchange position/TPSL, execution events, and management batches. Do not submit
a trade mutation merely to verify the redesign. As of 2026-07-17 this production
and live-browser verification is intentionally pending: the local sandbox
forbids Git metadata writes and local port binding, and the in-app browser blocks
local `file:` URLs.

Design and implementation references:

- `docs/plans/2026-07-12-mobile-first-web-workbench-design.md`
- `docs/plans/2026-07-12-mobile-first-web-workbench.md`
- `docs/plans/2026-07-12-shared-group-context-design.md`
- `docs/plans/2026-07-12-shared-group-context.md`
- `docs/plans/2026-07-13-lazy-workbench-loading-design.md`
- `docs/plans/2026-07-13-lazy-workbench-loading.md`
- `docs/plans/2026-07-16-mobile-strategy-record-center-design.md`
- `docs/plans/2026-07-16-mobile-strategy-record-center.md`

## MiMo-authoritative recognition and exit safety

All production recognition paths use MiMo as the authority for text and media. A successful MiMo decision is first persisted as unclaimable `execution_pending`. Immediately before the existing lifecycle safety and automation path, that exact generation must atomically claim durable ownership with `execution_pending -> execution_running`. A newer recognition may replace an unclaimed pending generation, but cannot overwrite a running generation; if an older generation loses the claim, it performs no lifecycle mutation or auto-trade. Only the exact running generation may persist its automation outcome and transition to claimable `pending` (or back to `completed` for an unchanged completed comparison). A post-submit outcome-write failure intentionally remains `execution_running`, blocking duplicate execution until recovery. DeepSeek semantic comparison runs later in the Web service worker and never sits on the execution critical path. MiMo failure blocks automatic mutation, is notified independently, and does not create a semantic-review job; its terminal audit write uses the same durable status/token guard and is rejected before apply/auto/audit mutation when another generation owns `execution_running`.

Executable current-message evidence is limited to the Telegram raw text plus
MiMo's authoritative `input_reading.observed_text` for the current image. Model
`reason` fields are explanatory and never become instruction text. An exact
structured `exit_full` action cannot be downgraded merely because its rationale
mentions `成本价`. Projection and execution still require the existing exact
lifecycle target, verified Deepcoin position ownership, fresh position
preflight, and idempotent execution claim; symbol/side fallback is forbidden.

MiMo authoritative recognition makes one immediate retry for transient provider
failures and invalid/schema-incomplete model responses before writing a
terminal `authoritative_failed` decision. Local deterministic failures such as
missing model configuration, empty input, or declared-but-unreadable image media
still fail closed without treating DeepSeek as execution authority. For
high-risk failed messages that mention crypto symbols, active positions,
take-profit, stop-loss, exit, protection, or position-management terms, the
operator alert is preserved and a single delayed background authoritative retry
is scheduled. That delayed retry uses the same MiMo authority, lifecycle,
automation, idempotency, and ownership gates; it must not replay through
DeepSeek or bypass execution safeguards. Clearly external-stock-only failures,
such as a standalone 美光/MU idea with no crypto or position-management context,
are persisted as `notification_status=suppressed_low_value` and do not send the
system-operator bot message.

The unified MiMo prompt incorporates the established DeepSeek strategy and lifecycle rules and adds explicit image-reading instructions. Decisions and model comparisons are persisted in `recognition_decisions`, including automation and notification outcomes.

For a live-bound Deepcoin strategy, recognizing or submitting an exit is not proof that the position is closed. The lifecycle records `exit_requested` and remains active until exchange reconciliation confirms the exact bound position and any live entry order are absent. This rule applies to realtime, manual-recognition, and missed-message recovery paths.

Design and implementation plan:

- `docs/superpowers/specs/2026-07-13-mimo-authoritative-recognition-design.md`
- `docs/superpowers/plans/2026-07-13-mimo-authoritative-recognition.md`

## Background semantic disagreement review

MiMo remains the only execution authority. A successful authoritative generation moves through `execution_pending -> execution_running`; only the exact owner token may perform lifecycle/automatic-execution work and persist its real automation outcome. Finalization then exposes a reviewable row as `pending`. The Web service claims that work as `running` and invokes DeepSeek in the background, after the MiMo path has returned. DeepSeek can classify audit severity and notification eligibility only; timeout, invalid output, or any other DeepSeek failure cannot authorize, block, modify, retry, compensate, or roll back a trade.

Operator interpretation of `recognition_decisions.comparison_status` is:

- `execution_pending`: a MiMo generation is persisted but has not claimed execution ownership;
- `execution_running`: the exact MiMo generation owns execution and must be recovered cautiously if it is stranded; do not manually queue semantic review or replay a trade from this state;
- `pending`: authoritative automation has finished and semantic review is waiting or delayed until `comparison_next_attempt_at`;
- `running`: one worker owns semantic review; a claim older than five minutes is recoverable after restart;
- `completed`: semantic review is terminal; inspect `disagreement_severity` and notification state;
- `failed`: three review attempts were exhausted; the persisted MiMo/automation outcome remains authoritative and unchanged.

Completed results use `none`, `normal`, or `critical`. Only `critical` schedules a system-operator notification. `none` and `normal` remain database-audited and Web-visible; normal details are collapsed by default. Pre-migration completed rows have no semantic severity and the Web labels them `待重新复核` (`unclassified`) instead of falsely reporting agreement.

The strategy-record center treats missing `recognition_decisions` as actionable
only for candidates produced by the MiMo-authoritative path. Lifecycles with no
candidate, and legacy `SignalCandidate` rows from older `text`, `text+ocr`, or
`llm` parse sources, remain visible as `AI legacy`, but they do not fill the
default `待处理` view solely because post-migration audit evidence does not
exist.

Review retries are bounded to three attempts with increasing delay. Stale `running` work is reclaimed through a new claim token, so an old worker cannot complete over a newer owner. Critical notification delivery separately freezes an immutable payload and fingerprint and commits `scheduled` before the network call. `scheduled`, `sent`, and `failed` are never automatically claimed again, providing at-most-once sending across retries and restarts; a crash after `scheduled` can therefore require manual delivery investigation rather than an automatic resend.

Design and implementation references:

- `docs/plans/2026-07-13-semantic-ai-disagreement-review-design.md`
- `docs/plans/2026-07-13-semantic-ai-disagreement-review.md`

Production deployment and controlled server verification for this change remain pending. Use the read-only audit in `docs/runbook.md`; never place a real order merely to test semantic-review notification.

Sequential position-management remediation is dry-run-first and chain-scoped.
Missed instructions are ordered by source time, raw-message ID, instruction
sequence, and candidate ID within each exact strategy. Only one chain head can
be approved at a time. Reconcile that action and rebuild the plan before
considering the next step. A confirmed full exit terminates later instructions
for the same old lifecycle. Conflicts freeze their own chain and do not grant
permission to guess ownership or fan out by symbol/side. Production rollout
must begin in management shadow mode and prove zero exchange writes during
historical replay.

## Web-managed AI prompts

All AI business prompts now belong to the versioned database registry. The shared trading template A covers new-strategy judgment and the full strategy lifecycle; MiMo alone adds image template B. Runtime context C remains dynamically generated. Therefore DeepSeek uses `A + C`, while authoritative MiMo uses `A + B + C`.

Use the Web prompt center for browsing and editing. Saving produces a non-live draft. Publication requires validation, and trading prompts also require a side-effect-free historical comparison. Every live AI invocation records exact version IDs. Per-group research prompts are server-scoped; an old `telegram-workbench:prompt:<chatId>` browser value may be imported as a draft but is never sent directly to the model or automatically published.

The old YAML prompt fields are compatibility seed inputs only. Database versions take precedence and runtime call sites must not compose prompts from YAML. Model/API configuration remains in YAML, while API keys must never appear in prompt APIs or rendered HTML.

Operational details, table names, rollback boundaries, and production checks are documented in `docs/context/ai-prompt-registry.md`.

## Per-group automatic-entry position cap

`max_concurrent_positions` is defined per exact Telegram `chat_id` and defaults
to `4`; it is not an account-wide cap. A group's effective count is the number
of distinct Deepcoin entry `posId`s on `active` entry legs with
`attribution_status=verified` through that group's exact binding. Pending
regular and trigger orders are intentionally excluded.

Only new automatic entries are gated. At the limit they record
`group_position_limit_reached`, while partial take profit, full exit, stop
changes, temporary exit, and all other management actions remain available.
The count and entry submission are intentionally non-transactional, so two
concurrent entry messages for the same group can both observe spare capacity
and briefly exceed the cap. This small race is accepted; it does not relax the
exact verified-ownership requirements used for reconciliation or management.

## Per-symbol fixed range-entry thresholds

Range-entry pricing is authoritative through
`symbol_entry_thresholds`, not the legacy shared range percentage. Each symbol
has three non-negative decimal price distances:

- `market_leg_threshold`: maximum absolute distance from the side-aware anchor
  at which the first 50% leg may become a market order;
- `first_limit_offset`: fixed offset for the first limit leg;
- `second_limit_offset`: fixed offset for the second limit leg.

For a normalized range `low <= high`, a long uses `high` as the market anchor
and a short uses `low`. The hybrid test is
`market_leg_threshold > 0` and
`abs(current_price - anchor) <= market_leg_threshold`. If it matches, the
first leg is market and the second limit is `low + second_limit_offset` for a
long or `high - second_limit_offset` for a short. Otherwise, long limits are
`high + first_limit_offset` and `low + second_limit_offset`; short limits are
`low - first_limit_offset` and `high - second_limit_offset`.

`market_leg_threshold=0` disables hybrid market conversion even when the
current price equals the anchor. A zero limit offset uses the original range
endpoint. BTC migration and fresh-install defaults are `200 / 90 / 90`; ETH
defaults are `4 / 2 / 2`; missing and newly added symbols default to
`0 / 0 / 0`. Decimal values remain strings until calculation, then prices are
normalized to the verified Deepcoin tick. Invalid, negative, non-finite,
out-of-float-range, non-positive, or tick-normalized-to-zero prices fail
closed.

Explicit market signals remain one 100% market leg. The separate
single-point “附近” conversion continues to use
`nearby_entry_market_deviation_pct`; it is not governed by these range
distances. Strategy-revision replacement drafts resolve thresholds from the
authoritative execution binding symbol, not a possibly stale symbol on the
revision message, and use the same values for preflight and submission.

The old range percentage and range-style fields remain persisted and are
submitted as hidden settings-form values solely so an older release can read
them after rollback. New range drafts do not use them. This change affects
only newly built entry orders and never rewrites existing orders or
positions. Production verification is settings/API/UI read-back plus passive
evidence only: never create a real test order, position, Telegram signal, or
management action to verify these formulas.

## Deepcoin position-attribution authority

`execution_order_legs` is the only persisted authority that may connect a live
Deepcoin `posId` to a KOL strategy. A live position is manageable only when one
nonterminal entry leg uniquely owns that exact `posId` with
`attribution_status=verified`. `execution_bindings.pos_id`, lifecycle state,
symbol/side similarity, entry-price proximity, and the fact that only one
position remains are not ownership proof.

For legacy compatibility, `execution_bindings.pos_id` may be empty, a single
posId, or a comma-separated summary of split-position posIds. Management
planning must treat this field as a summary only. The authoritative ownership
set remains the verified nonterminal `execution_order_legs` entry rows; a
binding summary may only narrow that set, never expand it or prove ownership by
itself.

Reconciliation loads one coherent read-only exchange snapshot, refreshes exact
entry-order states, runs global one-to-one matching, then derives binding and
lifecycle state. The permitted ownership states are `verified`, `unassigned`,
`attribution_conflict`, and `evidence_unavailable`. Every conflict or evidence
failure freezes automatic close and TPSL mutation. API failure is never treated
as position/order absence.

Exact Deepcoin `position_history` can terminalize already-exited historical
entry legs whose persisted `posId` is no longer live only when the queried row
matches the requested `posId`, instrument, and side, and proves `closePos ==
pos`. This cleanup removes stale alert noise from terminal strategies; it does
not convert conflicted evidence into verified live-position ownership and never
authorizes close or TPSL mutation.

Strategy-management source isolation is end to end. The management raw
message, authoritative candidate, and recognition decision must identify the
same raw row and chat; the target lifecycle, strategy instance, and binding
must preserve that chat and exact lifecycle-to-binding chain. The planner
checks this initially and again inside the atomic batch-create transaction.
Cross-chat or stale pointers create no executable work and no Deepcoin write.

Every close entry point performs a fresh exchange position read at the shared
executor boundary after the durable claim and before any leg reservation. The
exact owned `posId`/instrument/side/current-size set must equal the frozen
preflight. Snapshot errors, manual closes, partial-size drift, or an unowned
extra/missing leg durably freeze the batch as `recovery_required` with zero
close submissions. Positions uniquely verified as another strategy's remain
excluded only through the strict ownership allowlist.

An exact order/client ID proves which fill belongs to a leg, but does not by
itself prove which later position that fill opened. Regular fills require either
an explicit `fill.posId` or Deepcoin's direct `order_id == posId` identity.
Successful trigger history requires nonzero `triggerTime`, `errorCode=0`, an
exact size match, a position created within five seconds, and a global
mutual-unique result. Time/size/price proximity alone never authorizes ownership.

Manual exchange close and cancellation are terminal facts. A manually closed
position or cancelled entry leg cannot be revived by an old Telegram lifecycle,
a later reconcile pass, or a same-symbol live position. All real close, partial
close, and TPSL changes pass the same verified-ownership gate. Manual Web
binding is an explicit operator decision, writes a `manual_bind` verified leg,
and cannot overwrite conflict or unavailable evidence.

The terminal-entry cleanup invariant extends that rule: an `exited`, `expired`,
`cancelled`, or `invalidated` lifecycle may not retain an unfilled exact entry
leg. Manual full close cancels and reads back deferred entry legs before
submitting the position close. Exchange reconciliation does the same before it
terminalizes a missing primary position, and its bounded historical backstop
repairs old terminal rows. Exact binding plus order/client ID is mandatory;
ambiguous identity performs zero exchange writes.

Cleanup outcomes are durable `execution_events` outbox rows. The KOL
event-processing bot leases and delivers them with a stable fingerprint,
bounded error type, delayed retry, and a maximum of five attempts. Delivery
after process restart does not rerun cleanup and cannot call Deepcoin. This
invariant is always on, with no shadow behavior and no feature flag.

Production verification uses the read-only SQL in `docs/runbook.md` and must
show zero terminal lifecycle/nonterminal entry-leg anomalies. Rollback is a
reviewed Git revert followed by the normal server deployment. Keep schema and
outbox history, and never recreate an order already confirmed cancelled or
absent.

Position protection is attributed independently from strategy ownership.
`position_protection_ledger` is the sole ownership authority: a pending TPSL
row is owned only when its exact `ordId` has one verified ledger `posId`.
Exchange `posId` is validation evidence and a disagreement is a conflict; it
does not create ownership without the ledger. Price, quantity, instrument,
side, creation time, `sz=0`, and candidate uniqueness never assign an order.
Unowned or conflicting rows freeze mutation and expose no cancellable order
IDs. Read failure remains `止损证据暂不可用`.
Initial TP/SL protection created immediately after a market entry must also be
persisted into `position_protection_ledger`, not merely recorded as an
`execution_events` row. When Deepcoin returns one protection order ID but the
pending TPSL snapshot shows distinct TP and SL order IDs, record only rows that
are tied to the exact `posId`/`closePosId` and uniquely match instrument, side,
purpose, and trigger price. Price-only matches are never verified protection
evidence. The Web
current-order view must display TPSL ownership from the protection ledger first;
TPSL rows without ledger evidence are `保护归属未验证` and must not be grouped
under a strategy by symbol, side, price, message text, or group label alone.

Within the single `telegram-kol.service` process, reconciliation and management
mutations share one authority lock from evidence read through exchange request,
closing the validation/request race. The card-level exact-position close also
uses a durable database reservation and revalidates verified ownership inside a
SQLite immediate transaction before any exchange request. Reconcile preserves a
currently reserved exact owner until that request has a recorded outcome.

Attribution transitions are immutable in `position_attribution_audits`.
Abnormal notifications use a canonical fingerprint and persist delivery status
separately, so repeated identical reconciliation does not spam the system bot
and notification failure never changes ownership. Claiming is a conditional
atomic update. Delivery is intentionally at-most-once: `delivering` or `failed`
incidents require manual investigation instead of an automatic resend whose
exchange/network outcome could be ambiguous.

Historical repair is dry-run-first:

```bash
telegram-kol-research repair-position-attribution --database-path data/research.db
telegram-kol-research repair-position-attribution --database-path data/research.db \
  --apply --expected-fingerprint <fingerprint-from-reviewed-dry-run>
```

Back up the database and review the dry run before `--apply`. Apply is explicit,
transactional, audited, idempotent, refuses stale database/live-position
evidence and unresolved conflicts, and never submits an exchange mutation.
If a legacy database already contains duplicate `(venue,pos_id)` owners,
bootstrap leaves the unique index pending so this read-only/dry-run repair path
can start; runtime ownership gates still reject the duplicate. After repair,
restart once more to create the unique index.

Unbound holding cleanup is separate from manual close and attribution repair.
Use it only after a current Deepcoin snapshot shows zero live positions and zero
current orders, and only for reviewed `entered` lifecycles that have no
`execution_binding_id`, no matching Deepcoin execution binding, and no
management batch. The command is dry-run-first and refuses any row with
exchange ownership evidence:

```bash
telegram-kol-research archive-unbound-holdings --database-path data/research.db \
  --lifecycle-id <id> --lifecycle-id <id>
telegram-kol-research archive-unbound-holdings --database-path data/research.db \
  --lifecycle-id <id> --lifecycle-id <id> \
  --expected-count <reviewed-id-count> --apply
```

It marks eligible local lifecycle rows `invalidated` with
`exit_reason=context_invalidated` and records
`management_action=operator_archived_unbound_holding`. It must not be used for
`position_attribution_conflict`, `bound_non_live`, `active`, pending order, or
management-batch residue. Those require their own evidence review so the
operator does not convert ambiguous exchange history into a clean local archive.

Entry construction and historical attribution follow an additional economic
identity invariant. A strategy with one normalized entry price produces one
100% entry leg. If distinct draft range prices collapse to the same exchange
price after tick normalization, equivalent legs are coalesced before live
submission; a queued legacy draft is coalesced again at the submission boundary.
TP/SL values may filter incompatible attribution candidates only when direct,
current, and unmutated. They are auxiliary evidence, never standalone ownership
proof. When historical legs and positions are strictly equivalent and the
binding owner is otherwise proven, the repair planner uses a stable sorted
canonical mapping rather than a random or input-order-dependent choice. Only a
reviewed repair may persist this versioned
`equivalent_permutation_assignment` evidence. Ordinary reconciliation must not
originate it and therefore leaves a new equivalent permutation unresolved.

On 2026-07-15, before this code was pushed or deployed, the operator manually
closed the two then-unattributed live positions suspected to belong to Miya.
That exchange-side state change invalidates every earlier repair plan and
fingerprint, including any plan that expected two Miya assignment actions.
After deployment, fetch a fresh coherent Deepcoin snapshot of positions, open
TPSL orders, pending triggers, and relevant entry-order history before building
a new mandatory dry run. A `posId` absent from the fresh live snapshot must
never receive verified ownership and must never be sent a close request. Audit
remaining protection/trigger orders, stale legs, bindings, and lifecycles in
read-only mode, then generate the fresh dry run. Any terminal/manual transition
or stale-record cleanup must be an explicit fingerprinted planner action and
receive separate review before `--apply`. Zero actions authorizes no change.
Never bypass the planner/fingerprint by editing the production database directly.

Production remains fail closed until real positions, entry legs, pending orders,
TPSL, audit incidents, service health, and server tests all agree.

## Durable strategy-management batches

Telegram position management now uses one exact, group-isolated identity chain:
source chat/message -> candidate -> lifecycle -> strategy instance -> execution
binding -> verified entry legs -> immutable management batch legs -> Deepcoin
`posId`s. A lifecycle, symbol, side, price, or lone remaining position is never
enough to cross a missing link. Any ambiguity, stale ownership, missing leg, or
exchange-evidence failure blocks the whole preflight before an exchange write.

An unqualified first partial take-profit closes 50% of the total verified
strategy position, allocated proportionally over every split position. An
explicit close fraction overrides that default. Retained-position wording is
the inverse: `保留 40%` / `剩余 40%` means close 60%. If independently
explicit close and retain percentages disagree, recognition fails closed and
does not create an executable management instruction. The second distinct partial request
closes all remaining size, so repeated partial messages cannot leave an
indefinite tail. Full exit also covers every split position. An explicit stop
price is applied to every position; breakeven uses each position's own average
entry price. Existing TP/SL protection is preserved or compensated per
position, and a composite partial-then-breakeven action waits for exchange
confirmation of the close phase before replacing protection.

The batch and leg rows are the durable execution journal. `ready` is planned
but unclaimed, `executing` is the compare-and-set owner, `reserved` is durable
pre-submit intent, `submitted` awaits exchange truth, and `reconciling` checks
one coherent exchange snapshot. `protection_ready` means a confirmed composite
close may enter its protection phase. `succeeded`, `blocked`, and `resolved`
are safe terminal states. `partial_failed` means some legs succeeded while a
known failure remains; `submit_unknown` means an exchange outcome is unknown
and must never be automatically retried; `recovery_required` freezes the batch
for operator investigation. `restored` means failed protection replacement was
compensated with the complete prior protection for that position. Earlier
confirmed successes are never rolled back or hidden.

Abnormal transitions persist an immutable notification/outbox identity in the
same database transaction. Sending is separately claimed and bounded; a bot
failure cannot alter the batch or authorize a retry. Exchange reconciliation,
not an HTTP success response, confirms close completion. Manual close/cancel is
a first-class terminal fact: refresh exact exchange evidence and reconcile the
bound legs rather than attaching a new same-symbol position or editing rows.

Every planned close quantity is derived from the verified Deepcoin contract
`quantity_step` and `min_quantity`. The executor repeats those checks at the
final live-position write boundary and rejects a missing/mismatched contract
specification, an off-step quantity such as `2.4` BTC contracts when the step is
`1`, a below-minimum quantity, or a quantity above the current position before
calling `place_order`. Recognition stores requested management values on the
candidate only. It must not overwrite the lifecycle's confirmed stop, take
profit, management action, or entered/exited state. Partial-close lifecycle
metadata is promoted only after every close leg is confirmed by a coherent
exchange snapshot; full exit remains owned by full-close reconciliation.
Protection metadata is promoted only after every replacement request has a
durable successful Deepcoin response.

Web message cards show AI recognition separately from real execution. The
execution badge distinguishes not executed, shadow planned, waiting, submitted
and awaiting exchange confirmation, operator recovery, Deepcoin-accepted
protection replacement, and exchange-confirmed close. A recognition result or
submission response alone must never render as an exchange-confirmed exit.
Historical management messages must not be replayed after deployment; reconcile
current exchange state and create actions only from new messages.

Deepcoin TPSL readback validates a ledger owner against position identity
carried by the order. In current API responses this is normally `closePosId`;
legacy aliases (`posId`, `pos_id`, and `positionId`) remain accepted. A
disagreement with the canonical ledger is a conflict. An exchange position ID
without the ledger remains unowned and must never fall through to
symbol/side/time/size inference.

For split-entry strategies, `execution_order_legs` is the position-identity
authority. Strategy list and detail views derive the current `pos_ids` from
non-terminal, `verified`, `entry` legs and compare every ID independently with
one captured Deepcoin snapshot. The comma-joined compatibility value on
`execution_bindings.pos_id` is not a Deepcoin position ID and must never be
matched as one. A single-position compatibility fallback remains only for
legacy bindings without verified entry legs.

The read-only strategy record projection reports
`management_execution_drift` as critical only when a persisted management
message exists, exact bound exchange positions are current, and concrete
Deepcoin protection evidence disagrees with the lifecycle's expected stop.
Ambiguous or unavailable protection evidence remains unknown instead of being
promoted to a mismatch. This alert is diagnostic only: it never replays an old
message, changes a lifecycle, or writes to the exchange.

The settings have three meanings: `disabled` creates no management plan and
performs no management exchange write; `shadow` may persist reviewed plans but
does not execute them; `live` is effective only together with
`auto_trade_enabled=true`. This rollout must remain
`auto_trade_enabled=false` and `management_execution_mode=disabled`. Enabling
shadow or live requires a later, explicit approval; live additionally requires
reviewed shadow evidence.

Deepcoin triggered-limit lineage (trigger -> generated regular order -> fill ->
position) is deliberately a separate task and branch. Do not mix its historical
attribution migration or compatibility repair into this batch rollout.

The management audit preserves a zero-write source boundary: it never gives
SQLite the production path. Linux requires a successful `O_NOATIME` source
open with no fallback; macOS/APFS requires atomic `clonefile(2)` into the
private temporary volume; unsupported capability fails before reading as
`snapshot_unavailable`. Two captures of main/WAL/SHM must match in component
set, access-inclusive metadata, incremental hash, and size; both private copies
must pass `quick_check` and schema inspection. Components are streamed in fixed
chunks and never retained as whole byte arrays. A rollback journal or any
instability fails closed as `snapshot_unstable`. Historical JSON, IDs, and
decimals have parser/output resource limits and only fixed malformed flags may
escape. The same pre-parse character, UTF-8 byte, and nesting-depth validator
covers legacy payloads, batch target snapshots, and leg errors. Temporary
directory, private write/sync, and cleanup failures become fixed safe reasons
without exception text. All identities in JSON and text output are hashed
references. Counts
are exact where labelled complete; any batch, leg, or returned-item truncation,
or any false completeness flag, prohibits a "no residue" conclusion.

`hold_update` is an informational MiMo result meaning that the KOL said to keep
holding. It is not an exchange-management instruction. Automatic processing
records it as `management_intent_informational`, does not construct a Deepcoin
client, and does not create a management batch. Historical zero-leg rows whose
exact tuple is `hold_update` + `blocked` +
`management_intent_not_supported` remain immutable audit history but are
counted as `informational_noop`, not as actionable `blocked` residue. Any row
with a leg, a different intent, status, or reason remains alerting.

Web shutdown explicitly disconnects the shared Telethon client before
cancelling the live-listener task. Both disconnect and listener cleanup are
bounded to five seconds so a shielded or failed Telegram cleanup cannot hold
FastAPI lifespan open until systemd sends `SIGKILL`. Production verification
still requires one controlled service restart and journal confirmation that
the old PID exits before `TimeoutStopSec`.

Historical entry-protection TPSL repair is allowed only through
`repair-entry-protection-ledger`. The command is dry-run first and apply is
guarded by the displayed fingerprint. A repair action must start from a local
`execution_events` row whose action/reason is `set_position_tpsl` /
`entry_protection`, whose request carries the exact `posId`, instrument, side,
TP, and SL, and whose response returns at least one Deepcoin TPSL order id.
The returned id must still exist in current pending TPSL rows and match the
same instrument/side/requested trigger price within the event-time window.
Sibling TPSL rows may be written only when they share the returned order's
exchange timestamp group, match the remaining requested TP/SL exactly, and are
unique. Price-only, symbol/side-only, or non-unique historical rows stay
unrepaired and must continue rendering as unverified rather than owned.

## Composite-management v2 handoff

The v2 contract preserves every clause of one MiMo-authoritative management
message and executes ordered durable components. DeepSeek remains advisory and
cannot replace the contract, choose a target, or authorize an exchange write.
Completion requires confirmed evidence for TP consumption, exact partial-close
convergence, and create-before-cancel main/backup protection replacement.

Admission disable and recovery are different: `disabled` prevents new planning
and writes, while an already-submitted unknown outcome remains durably frozen
for read-only exchange reconciliation. There is no blind retry, ownership
fallback, or automatic historical replay. Miya and Sanjie history must never be
fed back through the live listener after migration.

Missing v2 tables during a version transition are deployment context rather
than a safety incident. Once present, missing completion evidence, duplicate
close submissions, oversized retained TP, missing verified stops, and stalled
components are fixed critical invariants. Live enablement requires a later
reviewed shadow report and explicit approval.

## Position-management liveness v2 handoff

Position ownership and protection-order ownership remain separate authorities.
An account-wide authoritative `execution_order_legs.pos_id` assignment permits
an exact-position operation; `position_protection_ledger` permits cancellation
or replacement of a particular order. Symbol/side similarity and an unowned
pending TPSL authorize neither operation.

`position_management_liveness_v2_mode` defaults to `disabled`. `shadow` may
compute mutual-unique assignment, capability, backup-stop, and staged-TP plans,
but writes no exchange mutation. `live` is effective only with
`auto_trade_enabled=true` and `management_execution_mode=live`. Recovery is
dry-run first through `recover-position-management-liveness`; apply requires
the reviewed fingerprint and reruns authoritative ownership plus coherent
snapshot preflight under the account lock.

`recovery_disposition=retry` permits read-only evidence collection;
`exact_backup` permits only a fully preflighted exact SL fallback;
`manual_review` and `terminal` prohibit automatic action. `submit_unknown` and
`recovery_required` are frozen states, not retry queues. Rollback sets the v2
mode to `disabled` and leaves confirmed exchange orders, ledger rows, recovery
records, and incident history intact. Never delete them to make rollback appear
clean.

## Multi-instruction recognition handoff

Authoritative recognition now has a bounded per-action `instructions` contract.
It preserves a cancellation or position-management instruction alongside an
independent strategy in the same Telegram message and orders management before
entry while retaining independent terminal outcomes. Context resolution may
attach an exact target to management, but must not erase or retarget a sibling
entry.

The rollout is dormant by default. `multi_instruction_mode=shadow` retains the
normalized comparison in recognition evidence without creating extra durable
candidates/items. `live` is future-only and applies only above
`multi_instruction_activation_after_raw_message_id`. Rollback sets the mode to
`disabled`; it never deletes evidence or replays historical messages.

Exact revisions use `revision_target_min_confidence=0.70` only with complete
entry/SL/TP replacement fields and exact lifecycle-to-binding ownership. New
entries still use `min_ai_confidence`. State-less Deepcoin cancel history is
accepted only by the strict same-invocation combined proof documented in the
runbook; ambiguity, fill evidence, exact live-position evidence, or an unknown
cancel response remains fail-closed.

## MiMo v2 analysis observability handoff

The canonical MiMo v2 result preserves an ordered, multi-dimensional intent
list and independently attributed evidence for every image. The Web projection
shows that first-pass result before contextual resolution, system acceptance,
execution truth, and the collapsed DeepSeek review. Historical rows are never
reinterpreted: existing reliable v1 fields remain labelled as historical v1,
and missing run/attempt or per-image detail remains explicitly missing.

Production stays on `mimo_contract_mode=v1` until the isolated server replay
described in `docs/runbook.md` passes with zero executable mismatch and both
P95 gates satisfied. The replay operates on a temporary SQLite copy and emits
redacted JSON/CSV artifacts only; it has no Deepcoin writer, notifier, or live
listener path.

Activation is future-only. Capture the current terminal maximum raw-message ID
after deployment preflight proves no in-flight or time-sensitive operation,
then set `v2_live_adapter` and that exact value as
`mimo_v2_activation_after_raw_message_id`. The settings API rejects an unsafe
initial watermark and exposes the durable circuit state. Verification observes
only naturally arriving later messages. Rollback sets the mode back to `v1`
and preserves the watermark, v2 runs and attempts, image evidence, instruction
state, lifecycle state, and all exchange audit records.

### 2026-08-11 server verification result

Commit `21e8bee968dc4d1637bfe4539e08f2f2f783289b` was deployed with the
managed `code` preflight. Both preflight passes reported no fresh active work
and no unprotected open position; the accepted warnings were incomplete/stale
exchange snapshot metadata, historical active/unknown residue, and five
protected open positions. The service returned healthy after the restart.

The isolated replay smoke gate then failed on raw message `10505`, before any
production activation:

- classification: `unsafe_evidence_mismatch`;
- difference: `text_field_attribution_changed`;
- v1 duration: `17912.640 ms`;
- v2 duration: `22615.577 ms`;
- adapter duration: `0.124 ms`;
- response size: `7896 bytes`;
- performance failure: `v2_p95_above_115_percent_of_v1`;
- production writes and notifications: both zero.

The redacted server artifacts are
`/run/telegram-kol/mimo-v2-replay-smoke/summary.json` (SHA-256
`a1f6649b3a87d88d1c42f28bf6ac10c16decb51488bc0f8333098351d8786e09`)
and `comparisons.csv` (SHA-256
`738fa7cf2b94482494a1882355417de6caa3eaca561a590203523bb3097cb720`).
The larger 17-message replay was stopped after this hard gate failure to avoid
unnecessary provider calls; its terminated temporary database copy was
deleted. Production remains `mimo_contract_mode=v1` with activation watermark
`0`. No historical message was replayed into the execution path, and no v2
activation was attempted.

### 2026-08-11 additive-evidence fix verification

Commit `b39a5e98e90ad7ad43af37016ad79fa8676cbd22` changed the replay
comparison so a v2-only structured text-evidence field is treated as additive
enrichment, while a v1 field that is lost or changed by v2 remains an unsafe
mismatch. Local verification passed 365 MiMo-focused tests, 602
execution-critical tests, and the full suite with 5551 passing and one skipped.

The commit was deployed with the managed `code` preflight. Both preflight
passes found no fresh active work and no unprotected open position. The
accepted warnings were stale/incomplete exchange snapshot metadata,
historical active/unknown residue, and five protected open positions. The
service returned active after restart and production stayed on
`mimo_contract_mode=v1` with activation watermark `0`.

The repeated isolated replay of raw message `10505` still failed the semantic
evidence gate:

- classification: `unsafe_evidence_mismatch`;
- difference: `text_field_attribution_changed` (therefore not merely a
  v2-only additive text field under the new comparator);
- v1 duration: `25761.729 ms`;
- v2 duration: `20301.090 ms`;
- adapter duration: `0.197 ms`;
- response size: `7608 bytes`;
- all performance gates passed;
- production writes and notifications: both zero.

The redacted artifacts are
`/run/telegram-kol/mimo-v2-replay-smoke-b39a5e9/summary.json` (SHA-256
`c8f84226c6b49be10d2cccfb014ad9ea2f5445f0589952ea139d64b87defcdec`)
and `comparisons.csv` (SHA-256
`7bb00a77bda57201e704b49a1ae3c71c18238de6020ba34fbe864880af84e5d8`).
A subsequent isolated diagnostic call also returned a v2 contract-validation
failure for the same message. The 17-message corpus was therefore not run, and
v2 was not activated. This is a model-contract/evidence stability gate, not a
Web rendering or automatic-trading regression; production remains safely on
v1 until a later fix passes the same isolated gate.
