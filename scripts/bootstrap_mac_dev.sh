#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }
check() { printf '[OK] %s\n' "$1"; }

[[ "$(uname -s)" == "Darwin" ]] || fail 'This bootstrap script must run on macOS.'
xcode-select -p >/dev/null 2>&1 || fail 'Install Xcode Command Line Tools first: xcode-select --install'
command -v git >/dev/null 2>&1 || fail 'Install Git first.'
command -v python3 >/dev/null 2>&1 || fail 'Install Python 3.12 or newer first.'
command -v uv >/dev/null 2>&1 || fail 'Install uv first.'

python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || fail "Python 3.12+ required; found $python_version."
git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail 'Run this from a Git checkout.'
git -C "$repo_root" remote get-url origin >/dev/null 2>&1 || fail 'Git remote origin is required.'
if git -C "$repo_root" ls-files --error-unmatch config/development.env >/dev/null 2>&1; then
    fail 'config/development.env is tracked. Remove it from Git and rotate any values before continuing.'
fi

check "Repository: $repo_root"
check "Python: $python_version"
check "Branch: $(git -C "$repo_root" branch --show-current)"
git -C "$repo_root" status --short
cat <<'NEXT_STEPS'

Next manual steps:
1. Read docs/mac-mini-migration.md and docs/migration-handoff.md.
2. If local development credentials are needed, copy config/development.env.example to config/development.env and fill only development values from your password manager.
3. Do not copy production Telegram sessions, databases, or trading credentials to this Mac.
4. Run uv sync and project checks only after you have reviewed the documentation.
NEXT_STEPS
