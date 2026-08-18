# Deployment Procedure (shared by every phase)

> Every phase file references this document for its deploy task. It is the one
> place the deployment mechanics are written down, so they cannot drift between
> phases. Read this together with your phase file; do not read other phase files.
>
> Verified against `deploy/telegram-kol-update` (448 lines) and
> `scripts/server_git_update.ps1` as they exist on this branch. An earlier draft
> of this document described a different, older updater that had a
> `CHANGE_CLASS` parameter and a `deployment-preflight` gate. **Neither exists
> here.** If you find `-ChangeClass` anywhere in these plans, it is stale — the
> only required argument is the commit.

## The actual pipeline

Work is edited locally, pushed to GitHub, and pulled onto the server by a gated
updater. Nobody logs in and runs `git pull` by hand, and no phase file should
ever instruct that.

```text
[local]  edit -> test -> commit -> push to origin/<deploy branch>
[local]  scripts/server_git_update.ps1 -ExpectedCommit <40-hex>
            |
            v  ssh
[server] deploy/telegram-kol-update, run as /usr/local/bin/telegram-kol-update
         1. assert EXPECTED_COMMIT is 40 hex chars; fetch origin/<branch>
         2. assert the updater's own SHA256 matches the one in that commit
         3. assert telegram-kol.service is currently active
         4. take /run/telegram-kol-update.lock (one deployment at a time)
         5. stage the candidate commit in a scratch worktree
         6. auto-detect schema change by diffing models.py, db.py, migrations
            between the deployed commit and the candidate; if changed, take a
            SQLite backup and run a migration dry run with PRAGMA quick_check
            and a watermark comparison
         7. active-write check          <- gate 1, refuses to interrupt live work
         8. systemctl stop telegram-kol.service (bounded by STOP_TIMEOUT_SECONDS)
         9. active-write check again    <- gate 2, now that no writer can start
        10. git checkout <branch> && git merge --ff-only <EXPECTED_COMMIT>
        11. pip install -e .
        12. install the updater from the deployed commit
        13. systemctl start telegram-kol.service, assert active
        14. verify HTTP health: GET /api/trading-settings, up to 20 attempts
```

Any failure after step 8 triggers an automatic rollback that restores the
previously deployed commit, resets the branch ref, restores the previous updater
binary, and restarts the service. If that rollback itself fails, the updater
prints `ROLLBACK FAILED; telegram-kol.service may remain stopped.` — treat that
as an incident, not a retry.

## What the safe-window gate actually checks

Steps 7 and 9 run `telegram_kol_research.deployment_active_write_check` against
the production database. It counts rows in in-flight states, including:

- `position_backup_stop_orders` with status `submitting`
- `execution_order_legs` with status `submitting` or `cancel_submitting`
- `instruction_execution_contracts` with state `submitting`
- `strategy_management_components` with status `submitting` or `cancel_submitting`
- `strategy_management_batches` with status `executing`
- `strategy_revision_batches` with status `submitting_replacements`

The deployment proceeds only on `active_write_count=0`. Exit code 3 means
`Deployment refused: active exchange write.`

So when a phase file says "prove a safe window", it means: expect this check to
pass. If it exits 3, an order is genuinely in flight — wait and retry later.
Do not work around it.

## No change classes

There is no `CHANGE_CLASS`, no `-ChangeClass`, no `-PreviousLiveSnapshotPath`,
no `-ReviewedShadowEvidencePath`, and no `-AuthorizeLivePromotion` on this
branch. Every phase in this remediation deploys the same way.

Schema changes are **detected automatically** in step 6 by diffing
`src/telegram_kol_research/models.py`, `src/telegram_kol_research/db.py`, and
`migrations`. Phase 4 adds a table and therefore trips that detection on its own;
it needs no flag and no special argument. Keep the backup and dry-run evidence
the updater produces — Phase 4 depends on it.

## The deploy branch

`deploy/telegram-kol-update:5` defaults `BRANCH` to
`codex/deepcoin-auto-trading-v1`, and `scripts/server_git_update.ps1` has the
same default.

This remediation lives on its own branch, recorded as `deploy_branch` in
`docs/runtime-serialization-remediation-status.md`, because the local
`codex/deepcoin-auto-trading-v1` had diverged from the pushed one. **Pass
`-Branch` explicitly on every deployment in every phase**, and confirm the value
in the status file first rather than assuming the default is right.

The server fast-forwards (step 10), so never rewrite pushed history on the
deploy branch.

## Standard deployment command

Run from the local machine, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 -ExpectedCommit <40-hex-sha> -Branch <deploy_branch from the status file>
```

Get the SHA from the pushed commit:

```bash
git rev-parse HEAD
```

Confirm it is actually on the remote before deploying — the server fetches from
origin, so an unpushed commit fails at step 1:

```bash
git branch -r --contains HEAD
```

## Where each kind of command runs

Phase files mark commands `[local]` or `[server]`. The distinction is not
cosmetic:

- `[local]` — tests, linting, commits, pushes, and the deployment script itself.
  Local tests use the project venv: `.venv/bin/python -m pytest`.

  Note: `README.md` and `AGENTS.md` still point at `.venv313b`. As of 2026-08-18
  that environment has no `bin/python` and cannot run the suite; `.venv`
  (Python 3.12.12) works and imports the package from `src/`. Verify before
  trusting either.

- `[server]` — anything that touches the real Telegram session, the Deepcoin API
  (the key is IP-allowlisted to the server), production data, or the live HTTP
  endpoints. Reach it over ssh:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 '<command>'
```

An HTTP verification in a phase file such as `curl -s http://127.0.0.1:8000/...`
is always a `[server]` command. `127.0.0.1` means the server's loopback, and the
service does not listen anywhere else.

## Rollback

Three independent levels, in increasing cost:

1. **Trading settings flip.** Phases 2, 4, and 5 ship behind a settings flag.
   Reverting is a settings change: it takes effect immediately, with no deploy
   and no restart. This is always the first rollback to reach for.
2. **Deploy the previous commit.** Run the same script with the previous known
   good 40-hex SHA. The updater also rolls back on its own if a deployment fails
   after the service stops.
3. **Revert and redeploy.** Commit a revert locally, push, deploy it. Needed only
   when the previous commit is not a valid target.

Phases 0, 1, and 3 have no settings flag, so their rollback is level 2 or 3.
That is stated in each of those phase files.

## Phase 6 note

Phase 6 splits `telegram-kol.service` into three units. The updater hardcodes
that service name in nine places, including its precondition
(`deploy/telegram-kol-update:31`), the stop path (`:347`), the start and active
assertions (`:395`, `:399`), and the rollback path (`:103`, `:132`, `:135`). It
also reinstalls itself from the deployed commit (`:419`, `:424`).

So the updater must be taught about the new topology **in a deployment that
happens while the old topology is still running**, and the split itself is a
manual, out-of-band maintenance step, not a `server_git_update.ps1` run. Phase 6
covers this; do not attempt to shortcut it.
