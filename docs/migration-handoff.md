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

Design and implementation references:

- `docs/plans/2026-07-12-mobile-first-web-workbench-design.md`
- `docs/plans/2026-07-12-mobile-first-web-workbench.md`
- `docs/plans/2026-07-12-shared-group-context-design.md`
- `docs/plans/2026-07-12-shared-group-context.md`
