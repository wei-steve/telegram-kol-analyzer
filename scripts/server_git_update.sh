#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 {plan|push|stage|activate}" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

ACTION="$1"
case "$ACTION" in
  plan|push|stage|activate) ;;
  *) usage; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLANNER_PYTHON="${PLANNER_PYTHON:-$ROOT/.venv/bin/python}"
ACTION_MANIFEST="${ACTION_MANIFEST:?set ACTION_MANIFEST to the local action manifest}"

[ -x "$PLANNER_PYTHON" ] || { echo "Planner Python is unavailable." >&2; exit 2; }
[ -f "$ACTION_MANIFEST" ] || { echo "ACTION_MANIFEST is unavailable." >&2; exit 2; }

PLAN_JSON="$($PLANNER_PYTHON -m telegram_kol_research.deployment_action_plan \
  --manifest "$ACTION_MANIFEST" --format json)"

if [ "$ACTION" = "plan" ]; then
  printf '%s\n' "$PLAN_JSON"
  exit 0
fi

MANIFEST_ACTION="$(printf '%s' "$PLAN_JSON" | "$PLANNER_PYTHON" -c \
  'import json,sys; print(json.load(sys.stdin)["action"])')"
if [ "$MANIFEST_ACTION" != "$ACTION" ]; then
  echo "Action manifest does not match requested action." >&2
  exit 2
fi

EXPECTED_COMMIT="${EXPECTED_COMMIT:?set EXPECTED_COMMIT to the reviewed 40-character commit}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "EXPECTED_COMMIT must be a full 40-character hexadecimal commit." >&2
  exit 2
fi
EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "$BRANCH" == *..* ]]; then
  echo "BRANCH contains unsupported characters." >&2
  exit 2
fi

if [ "$ACTION" = "push" ]; then
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
    echo "Push requires a clean worktree." >&2
    exit 3
  fi
  if [ "$(git -C "$ROOT" rev-parse HEAD)" != "$EXPECTED_COMMIT" ]; then
    echo "Push requires EXPECTED_COMMIT to equal the checked-out HEAD." >&2
    exit 3
  fi
  remote_commit="$(git -C "$ROOT" ls-remote origin "refs/heads/$BRANCH" | awk 'NR == 1 {print $1}')"
  if [ -n "$remote_commit" ] && ! git -C "$ROOT" merge-base --is-ancestor "$remote_commit" "$EXPECTED_COMMIT"; then
    echo "Push would not be a fast-forward." >&2
    exit 3
  fi
  git -C "$ROOT" push origin "$EXPECTED_COMMIT:refs/heads/$BRANCH"
  pushed_commit="$(git -C "$ROOT" ls-remote origin "refs/heads/$BRANCH" | awk 'NR == 1 {print $1}')"
  if [ "$pushed_commit" != "$EXPECTED_COMMIT" ]; then
    echo "Remote branch identity verification failed." >&2
    exit 3
  fi
  printf '{"action":"push","commit":"%s","status":"complete"}\n' "$EXPECTED_COMMIT"
  exit 0
fi

export DEPLOYMENT_ACTION="$ACTION" ACTION_MANIFEST EXPECTED_COMMIT BRANCH
exec "$ROOT/scripts/bootstrap_server_updater.sh"
