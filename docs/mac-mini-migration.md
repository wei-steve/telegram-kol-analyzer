# Mac mini Development Migration

## What moves and what does not

Move source code and non-secret project knowledge through the private Git repository. Do not move production Telegram sessions, databases, Deepcoin credentials, API secrets, or live-trading configuration. The Mac mini is for development and non-production verification.

## Windows preparation

1. Review `AGENTS.md`, this guide, and `docs/migration-handoff.md`.
2. Run `powershell -ExecutionPolicy Bypass -File .\scripts\preflight_mac_migration.ps1`.
3. Review every redacted `path:line` warning. Remove accidental tracked secrets and rotate any exposed credential before continuing.
4. Commit and push only reviewed source changes to the private repository. Do not bundle the working tree, `.venv`, `data/`, or local configuration into an archive.

## Mac mini setup

1. Create a dedicated macOS development account, enable FileVault, and disable automatic login.
2. Install and sign into Tailscale before enabling remote access; use MFA for its identity provider.
3. Install Xcode Command Line Tools, Git, Python 3.12 or newer, and uv.
4. Clone the private repository, then run `bash scripts/bootstrap_mac_dev.sh` from its root.
5. If a local feature genuinely needs credentials, copy `config/development.env.example` to `config/development.env` and enter development-only values from a password manager. Keep session path empty unless you intentionally create a separate non-production session.
6. Run `uv sync` and the documented local tests only after reviewing the configuration. Do not start authenticated Telegram syncs or trading commands by default.

## Production deployment

Mac changes reach production only after review and a Git push. Continue to update the server through the existing `scripts/server_git_update.ps1` helper. Production verification must run on the server because its session, IP allowlist, and production keys are intentionally not on the Mac.

## Recovery

- Lost Mac: remove the device from Tailscale, revoke its GitHub session, and rotate any development credential it held.
- Suspected secret in Git: revoke or rotate it immediately, then remove it from files and assess history exposure.
- Broken Mac checkout: delete only the checkout and reclone; do not restore production files from backups.
- Remote access problem: use the local display/keyboard first, then check Tailscale device approval before changing any network setting.
