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
