# Cross-Platform Server Update Design

## Goal

Allow a Mac or Linux development workstation to trigger the existing production GitHub pull, editable reinstall, and service restart without requiring PowerShell.

## Decision

Keep `scripts/server_git_update.ps1` for Windows and add `scripts/server_git_update.sh` for macOS and Linux. Both scripts use the same defaults: production server `root@43.167.220.225`, branch `codex/deepcoin-auto-trading-v1`, and the user’s Tencent SSH private key.

## Behavior

The shell helper accepts environment overrides for `SERVER`, `KEY_PATH`, and `BRANCH`. It verifies that `ssh` exists and that the selected key file is readable before connecting. It invokes only the server’s existing `/usr/local/bin/telegram-kol-update` helper with a safely quoted `BRANCH` assignment, so the server remains responsible for pulling, reinstalling, and restarting `telegram-kol.service`.

The helper does not copy credentials, read runtime data, or print secret material.

## Documentation and Tests

Deployment documentation will show the macOS/Linux command first and retain the Windows PowerShell command. A lightweight local test will assert the shell script’s defaults, preflight checks, environment overrides, and remote command construction without opening an SSH connection.
