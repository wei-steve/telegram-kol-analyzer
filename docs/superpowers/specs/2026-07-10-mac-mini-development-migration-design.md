# Mac mini Development Migration Design

## Goal

Make a Mac mini a reproducible development workstation for this repository without moving production credentials, Telegram sessions, production databases, or live-trading authority off the server.

## Scope

This work adds repository-owned migration guidance, a non-secret development environment template, and two safety-focused helper scripts:

- A Windows preflight script that reports migration readiness without copying, printing, or uploading secrets.
- A macOS bootstrap script that validates local developer prerequisites and repository state without creating credentials or contacting production services.

It also documents the supported remote-access topology: Tailscale as the private network, RustDesk for graphical access, and SSH limited to the private network for administrative access.

## Non-goals

- Migrating production Telegram sessions, Deepcoin credentials, databases, or service state to the Mac mini.
- Automating password-manager access, credential transfer, API key rotation, GitHub authentication, or server deployment.
- Opening inbound router ports or exposing VNC, SSH, RustDesk, or application services directly to the public internet.
- Changing application trading behavior or production deployment logic.

## Architecture

GitHub is the only code-transfer channel. The repository holds all non-secret operational knowledge through a migration guide and a concise handoff index. Secrets remain outside Git: production secrets and Telegram sessions remain on the production server, while any development-only credentials are manually retrieved from an approved password manager.

The Mac mini is a developer workstation. It receives reviewed code by cloning or pulling the private repository, runs local non-production checks, and sends commits to GitHub. Production updates retain the existing path: the server pulls from GitHub, reinstalls the editable package, and restarts `telegram-kol.service` via the repository helper.

```text
Windows developer workstation ---- GitHub private repository ---- Mac mini developer workstation
                                          |
                                          v
                                Production server (only production sessions,
                                credentials, database, and live service)
```

## Deliverables

### `docs/mac-mini-migration.md`

An ordered human-run migration guide covering: Windows preparation; GitHub verification; Mac prerequisites; clone and dependency installation; local configuration creation from a non-secret template; safe local validation; operating boundaries; and rollback/recovery.

### `docs/migration-handoff.md`

The durable project-memory entry point. It identifies the system purpose, source layout, service/deployment ownership, authoritative documentation, development/production boundary, and a short non-secret handoff checklist. It must not contain hostnames, usernames, tokens, IP addresses, chat IDs, or credential values.

### `config/development.env.example`

A commented, non-secret example describing only variables that are appropriate for local development. Values must be blank or explicitly safe placeholders. The file must warn that it cannot be used for production and that any actual values come from a password manager, never Git.

### `scripts/preflight_mac_migration.ps1`

A read-only Windows PowerShell script that:

1. Locates the repository root from its own path.
2. Checks that `git` is available and reports branch/status summary.
3. Checks that `.gitignore` contains rules for `.env`, Python virtual environments, Telegram session files, and common local data/log artifacts.
4. Lists candidate untracked sensitive paths by file name only; it never reads their contents.
5. Scans tracked text files for credential-like assignments using redacted findings (path and line number only, never matching text).
6. Exits nonzero when required tools or ignore rules are missing, or when a tracked file has a suspected credential assignment.

The scan intentionally reports possible false positives for manual review. It must exclude `.git`, `.venv`, caches, videos, and binary files.

### `scripts/bootstrap_mac_dev.sh`

A `bash` script that:

1. Resolves its repository root and confirms macOS.
2. Checks for Xcode Command Line Tools, Git, Python 3.11 or later, and `uv`.
3. Confirms the checkout has a Git remote and reports the current branch/status.
4. Refuses to continue if a local `.env` file is already tracked by Git.
5. Creates no credentials, does not run a service, and does not contact production.
6. Explains the next manual steps: copy the development template only if needed, use development-only secrets from a password manager, then run the project’s documented local checks.

The script exits nonzero when a required precondition fails.

### `docs/remote-access.md`

An operational guide for configuring Tailscale, RustDesk, and SSH on the Mac mini, Windows computer, and phone. It includes device login protection, FileVault, non-public SSH configuration, Mac power/sleep considerations, permission requirements for unattended remote desktop, and an emergency-only mobile workflow.

## Data and Secret Handling

No real secrets are created, copied, printed, committed, or logged. The scripts use filenames and redacted locations only. Production secrets and sessions remain on the production server. Git history must be checked before first push; if a secret is found in history, it must be revoked/rotated rather than merely removed from the working tree.

## Error Handling

Both scripts fail closed: unmet prerequisite or detected risky condition returns a nonzero exit code and explains the corrective action. Informational warnings do not modify state. Neither script attempts to repair ignore rules, delete files, change Git remotes, or alter network settings.

## Verification

- Parse the PowerShell script with `Parser::ParseFile` and run it in the repository with safe sample conditions.
- Run `bash -n scripts/bootstrap_mac_dev.sh` where Bash is available; run its prerequisite/reporting path without installing anything.
- Validate the template has no non-placeholder values and is ignored if copied to a local environment filename.
- Run the preflight script and confirm it redacts all suspected secret findings.
- Review `git diff --check` and inspect all created files for accidental credentials.

## Acceptance Criteria

- A new developer can follow repository documentation to prepare a Mac mini without access to production credentials.
- The repository contains a durable non-secret handoff entry point and an environment template.
- The Windows preflight detects common accidental secret-tracking risks without exposing values.
- The Mac bootstrap stops on missing prerequisites and does not change production state.
- Remote-access guidance requires private-network access and prohibits public port forwarding.
