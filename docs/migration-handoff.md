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
Version drift is deployment context, not a production-safety failure by itself:
when current and expected HEADs are both well-formed but differ, the monitor
records the two versions in details and includes them with any real alert, but
does not mark the snapshot unhealthy for that reason alone. Notification repeats
remain reason-aware: unchanged high-priority monitor fingerprints still remind
every six hours, while unchanged low-priority `audit_abnormal` residue is
delivered once and then kept log-only until the fingerprint changes.

On each reviewed server deployment, first run
`systemctl disable --now telegram-kol-monitor.timer`, then require both
`systemctl is-enabled --quiet telegram-kol-monitor.timer` and
`systemctl is-active --quiet telegram-kol-monitor.timer` to be false. Run
`./scripts/install_server_monitor.sh` only from `/opt/telegram-kol-analyzer`.
Its default is install-only and fails closed if an old timer remains enabled or
active. Complete the runbook's never-enabled static no-notify diagnostic unit
and its single instruction for the installed, never-enabled static labelled-
notification oneshot before running
`./scripts/install_server_monitor.sh --enable`. Status and monitor-only rollback
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

Position protection is attributed independently from strategy ownership.
Exact TPSL `posId` wins; otherwise the matcher uses a small time window,
instrument, side, full-position `sz=0`, and aggregated partial TP sizes. A
unique result may be displayed and mutated. Ambiguous protection displays
`止损存在，归属待确认`; read failure displays `止损证据暂不可用`; neither state
exposes cancellable order IDs. Missing timestamps, incompatible sizes, or two
protection groups competing for one position are ambiguous and fail closed.
Unscoped TPSL matching is global and mutual-unique across all live positions.
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

Deepcoin TPSL attribution must first use the position identity carried by the
order itself. In current API responses this is normally `closePosId`; legacy
aliases (`posId`, `pos_id`, and `positionId`) remain accepted. An exact
`closePosId` must never fall through to symbol/side/time heuristics or be
borrowed by a nearby same-symbol position.

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
