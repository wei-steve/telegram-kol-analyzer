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

All production recognition paths use MiMo as the authority for text and media. Text-only messages additionally run DeepSeek as a non-authoritative comparison. A disagreement sends an operator alert after the MiMo result has continued into automation; it must never block an urgent stop-loss, take-profit, or full-exit instruction. MiMo failure blocks automatic mutation and is itself notified.

The unified MiMo prompt incorporates the established DeepSeek strategy and lifecycle rules and adds explicit image-reading instructions. Decisions and model comparisons are persisted in `recognition_decisions`, including automation and notification outcomes.

For a live-bound Deepcoin strategy, recognizing or submitting an exit is not proof that the position is closed. The lifecycle records `exit_requested` and remains active until exchange reconciliation confirms the exact bound position and any live entry order are absent. This rule applies to realtime, manual-recognition, and missed-message recovery paths.

Design and implementation plan:

- `docs/superpowers/specs/2026-07-13-mimo-authoritative-recognition-design.md`
- `docs/superpowers/plans/2026-07-13-mimo-authoritative-recognition.md`

## Web-managed AI prompts

All AI business prompts now belong to the versioned database registry. The shared trading template A covers new-strategy judgment and the full strategy lifecycle; MiMo alone adds image template B. Runtime context C remains dynamically generated. Therefore DeepSeek uses `A + C`, while authoritative MiMo uses `A + B + C`.

Use the Web prompt center for browsing and editing. Saving produces a non-live draft. Publication requires validation, and trading prompts also require a side-effect-free historical comparison. Every live AI invocation records exact version IDs. Per-group research prompts are server-scoped; an old `telegram-workbench:prompt:<chatId>` browser value may be imported as a draft but is never sent directly to the model or automatically published.

The old YAML prompt fields are compatibility seed inputs only. Database versions take precedence and runtime call sites must not compose prompts from YAML. Model/API configuration remains in YAML, while API keys must never appear in prompt APIs or rendered HTML.

Operational details, table names, rollback boundaries, and production checks are documented in `docs/context/ai-prompt-registry.md`.
