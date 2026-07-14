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

## Handoff checklist

- Read `AGENTS.md`, `docs/runbook.md`, and `docs/server-deployment.md`.
- Confirm the branch and Git remote without recording private URLs or identities in this file.
- Use `scripts/preflight_mac_migration.ps1` before moving to a new workstation.
- Store only non-secret decisions and operational notes in repository documentation.
- If a credential is discovered in Git history, revoke or rotate it; deleting a working-tree file is insufficient.

## Mobile-first web workbench

The main web console now follows one shared mobile-first information architecture rather than adapting the old three-column desktop shell independently. Its five primary destinations are `首页`, `持仓`, `策略`, `消息`, and `更多`.

The home destination leads with account risk and service health, then combines recent Telegram messages, strategy lifecycle changes, and Deepcoin execution records into a normalized read-only event feed. Source tables remain authoritative; the feed does not copy trading state.

Mobile is the primary layout. Desktop enhances the same hierarchy with a left navigation rail and wider content area. Do not create a separate `/mobile` application or duplicate backend actions.

Low-risk actions such as refresh, filtering, navigation, and opening details can happen directly. Position close, live-position binding, and trading-setting changes remain detail-only actions with explicit confirmation, pending-state feedback, and duplicate-submit protection.

`策略` and `消息` share one persisted current-group context. Their canonical selector is the sticky context bar above the shared workbench. Mobile opens a searchable bottom sheet; desktop opens the same picker as a compact overlay. `首页`, `持仓`, and `更多` remain global and must not be implicitly filtered by this selected group.

The workbench shell uses destination-level lazy loading. `GET /` must not call Deepcoin or embed the selected group's message timeline, strategy cards, or exchange-position panel. The home dashboard loads asynchronously after first paint; `持仓`, `策略`, and `消息` load when first opened. Restoring a persisted group changes selection state only and must not simulate a group click while the user is still on `首页`. Focus/visibility recovery requests are coalesced to avoid duplicate refresh bursts.

Design and implementation references:

- `docs/plans/2026-07-12-mobile-first-web-workbench-design.md`
- `docs/plans/2026-07-12-mobile-first-web-workbench.md`
- `docs/plans/2026-07-12-shared-group-context-design.md`
- `docs/plans/2026-07-12-shared-group-context.md`
- `docs/plans/2026-07-13-lazy-workbench-loading-design.md`
- `docs/plans/2026-07-13-lazy-workbench-loading.md`

## MiMo-authoritative recognition and exit safety

All production recognition paths use MiMo as the authority for text and media. A successful MiMo decision is first persisted as unclaimable `execution_pending`. Immediately before the existing lifecycle safety and automation path, that exact generation must atomically claim durable ownership with `execution_pending -> execution_running`. A newer recognition may replace an unclaimed pending generation, but cannot overwrite a running generation; if an older generation loses the claim, it performs no lifecycle mutation or auto-trade. Only the exact running generation may persist its automation outcome and transition to claimable `pending` (or back to `completed` for an unchanged completed comparison). A post-submit outcome-write failure intentionally remains `execution_running`, blocking duplicate execution until recovery. DeepSeek semantic comparison runs later in the Web service worker and never sits on the execution critical path. MiMo failure blocks automatic mutation, is notified independently, and does not create a semantic-review job; its terminal audit write uses the same durable status/token guard and is rejected before apply/auto/audit mutation when another generation owns `execution_running`.

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

Review retries are bounded to three attempts with increasing delay. Stale `running` work is reclaimed through a new claim token, so an old worker cannot complete over a newer owner. Critical notification delivery separately freezes an immutable payload and fingerprint and commits `scheduled` before the network call. `scheduled`, `sent`, and `failed` are never automatically claimed again, providing at-most-once sending across retries and restarts; a crash after `scheduled` can therefore require manual delivery investigation rather than an automatic resend.

Design and implementation references:

- `docs/plans/2026-07-13-semantic-ai-disagreement-review-design.md`
- `docs/plans/2026-07-13-semantic-ai-disagreement-review.md`

Production deployment and controlled server verification for this change remain pending. Use the read-only audit in `docs/runbook.md`; never place a real order merely to test semantic-review notification.

## Web-managed AI prompts

All AI business prompts now belong to the versioned database registry. The shared trading template A covers new-strategy judgment and the full strategy lifecycle; MiMo alone adds image template B. Runtime context C remains dynamically generated. Therefore DeepSeek uses `A + C`, while authoritative MiMo uses `A + B + C`.

Use the Web prompt center for browsing and editing. Saving produces a non-live draft. Publication requires validation, and trading prompts also require a side-effect-free historical comparison. Every live AI invocation records exact version IDs. Per-group research prompts are server-scoped; an old `telegram-workbench:prompt:<chatId>` browser value may be imported as a draft but is never sent directly to the model or automatically published.

The old YAML prompt fields are compatibility seed inputs only. Database versions take precedence and runtime call sites must not compose prompts from YAML. Model/API configuration remains in YAML, while API keys must never appear in prompt APIs or rendered HTML.

Operational details, table names, rollback boundaries, and production checks are documented in `docs/context/ai-prompt-registry.md`.

## Deepcoin position-attribution authority

`execution_order_legs` is the only persisted authority that may connect a live
Deepcoin `posId` to a KOL strategy. A live position is manageable only when one
nonterminal entry leg uniquely owns that exact `posId` with
`attribution_status=verified`. `execution_bindings.pos_id`, lifecycle state,
symbol/side similarity, entry-price proximity, and the fact that only one
position remains are not ownership proof.

Reconciliation loads one coherent read-only exchange snapshot, refreshes exact
entry-order states, runs global one-to-one matching, then derives binding and
lifecycle state. The permitted ownership states are `verified`, `unassigned`,
`attribution_conflict`, and `evidence_unavailable`. Every conflict or evidence
failure freezes automatic close and TPSL mutation. API failure is never treated
as position/order absence.

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
telegram-kol-research repair-position-attribution --database-path data/research.db --apply
```

Back up the database and review the dry run before `--apply`. Apply is explicit,
transactional, audited, idempotent, refuses stale database/live-position
evidence and unresolved conflicts, and never submits an exchange mutation.
If a legacy database already contains duplicate `(venue,pos_id)` owners,
bootstrap leaves the unique index pending so this read-only/dry-run repair path
can start; runtime ownership gates still reject the duplicate. After repair,
restart once more to create the unique index.
Production remains fail closed until real positions, entry legs, pending orders,
TPSL, audit incidents, service health, and server tests all agree.
