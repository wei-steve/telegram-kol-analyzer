# Deployment Procedure (shared by every phase)

> Every phase file references this document for its deploy task. It is the one
> place the deployment mechanics are written down, so they cannot drift between
> phases. Read this together with your phase file; do not read other phase files.

## The actual pipeline

Work is edited locally, pushed to GitHub, and pulled onto the server by a gated
updater. Nobody logs in and runs `git pull` by hand, and no phase file should
ever instruct that.

```text
[local]  edit -> test -> commit -> push to origin/<deploy branch>
[local]  EXPECTED_COMMIT=<40-hex branch tip> ./scripts/server_git_update.sh
            |
            v  ssh
[server] deploy/telegram-kol-update, run as /usr/local/bin/telegram-kol-update
         1. fetch origin/<branch>, assert FETCH_HEAD == ExpectedCommit
         2. assert the updater's own SHA256 matches the one in that commit
         3. assert telegram-kol.service is currently active
         4. take /run/telegram-kol-update.lock (one deployment at a time)
         5. auto-detected schema change only: SQLite backup + migration dry run
         6. deployment_active_write_check  <- gate 1, refuses on an active exchange write
         7. systemctl stop telegram-kol.service
         8. deployment_active_write_check again  <- gate 2
         9. git checkout <branch> && git merge --ff-only <ExpectedCommit>
        10. pip install -e .
        11. systemctl start telegram-kol.service, assert active
        12. verify_http_health polls /api/trading-settings, then reinstall the
            updater from the deployed commit
```

Three consequences that phase files depend on:

- **The safe window is enforced, not asserted.** Step 6 is what stops a
  deployment from interrupting a time-sensitive strategy operation. A phase that
  says "prove a safe window" means: expect the preflight to pass, and if it
  returns `BLOCK`, do not retry blindly — read the reason, wait, and record it.
- **The server fast-forwards.** `git merge --ff-only` means the pushed commit
  must be a descendant of what the server has. Never rewrite pushed history on
  the deploy branch.
- **The updater deploys itself.** Step 11 installs the updater from the commit
  being deployed. Changing `deploy/telegram-kol-update` is therefore
  self-applying and needs care — this matters in Phase 6.

## The deploy branch

The updater's default branch is `codex/deepcoin-auto-trading-v1`
(`deploy/telegram-kol-update:5`, and the `-Branch` default in
`scripts/server_git_update.ps1`).

This remediation is committed to that same branch, recorded as `deploy_branch` in
`docs/runtime-serialization-remediation-status.md`. Because it matches the
updater default, `-Branch` does not need to be passed. Confirm the value in the
status file before the first deployment rather than assuming it.

The server fast-forwards onto that branch, so every phase commits and pushes
there. Do not deploy from a different branch without passing `-Branch`, and never
rewrite pushed history on it.

## There is no change class — corrected 2026-08-19 (Phase 1b)

Earlier versions of this document, and every phase file, specified a
`-ChangeClass` per phase and a `-PreviousLiveSnapshotPath` for writer-sensitive
phases. **None of those arguments exist.**

- `deploy/telegram-kol-update` reads only `EXPECTED_COMMIT` and `BRANCH`.
- `scripts/server_git_update.sh` exports only `SERVER`, `KEY_PATH`, `BRANCH`,
  `EXPECTED_COMMIT`.
- `scripts/server_git_update.ps1` has no `-ChangeClass` parameter.

Phase 0's recorded `CHANGE_CLASS=code` was an inert environment variable, not a
gate selection. Setting these does nothing; it does not weaken a gate, but it
also does not select one, and believing otherwise gives false assurance.

What the updater actually enforces, on every deployment regardless of what the
change contains:

- **Safe window** — `telegram_kol_research.deployment_active_write_check`, run
  immediately before *and* immediately after the service stop. Exit 3 means
  "refused: active exchange write". This is automatic; there is nothing for a
  phase to pass in, and nothing a phase can skip.
- **Schema** — auto-detected by diffing `src/telegram_kol_research/models.py`,
  `src/telegram_kol_research/db.py` and `migrations/` between the current and
  candidate commits. A detected change triggers the SQLite backup and migration
  dry run by itself.
- **Health** — `verify_http_health` polls `/api/trading-settings` up to 20 times
  after start; the cleanup trap rolls production back to the previous commit if
  any step fails.

Capturing a live position snapshot before a writer-sensitive deployment is still
worth doing as evidence. The updater simply has no argument to receive it.

## Standard deployment command

Run from the local machine, **from a checkout of the commit being deployed** —
the updater compares the SHA256 of the local `deploy/telegram-kol-update`
against the copy inside that commit and exits silently on a mismatch. This cost
one confusing failure in Phase 0.

```bash
EXPECTED_COMMIT=<40-hex> ./scripts/server_git_update.sh
```

There is a PowerShell wrapper, `scripts/server_git_update.ps1`, but this
workstation has no PowerShell. Use the bash path.

**`EXPECTED_COMMIT` must be the current tip of the deploy branch, not merely the
commit whose change you care about.** Step 1 asserts
`FETCH_HEAD == EXPECTED_COMMIT`, so if any further commit — even a docs-only one
— has been pushed since, the deployment fails at the bootstrap with exit 1 and
production is untouched. This cost one failed attempt in Phase 1b.

Get the tip, and confirm it is actually on the remote before deploying:

```bash
git rev-parse HEAD
git branch -r --contains HEAD
```

**Capture the updater's exit code without a pipe.** `cmd | tail` reports
`tail`'s status, not the updater's — Phase 1 cited an exit code that was
actually `tail`'s. Redirect to a file and read `$?`, then verify HEAD, service
state and the endpoint over ssh regardless.

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
   good 40-hex SHA — no class argument exists. Note the SHA must be reachable
   as the branch tip the server fetches, so a rollback may need a revert commit
   pushed on top rather than an older SHA passed directly. The updater's own
   failure path already restores the previous commit if a deployment fails
   midway (`previous_commit` and its cleanup trap).
3. **Revert and redeploy.** Commit a revert locally, push, deploy it. Needed only
   when the previous commit is not a valid target.

Phases 0, 1, 1b, and 3 have no settings flag, so their rollback is level 2 or 3.
That is stated in each of those phase files.

## Phase 6 note

Phase 6 splits `telegram-kol.service` into three units. The updater hardcodes
that service name in its precondition (`systemctl is-active --quiet
telegram-kol.service`, step 3), in its stop and start steps, and in
`verify_http_health`. It also reinstalls itself from the deployed commit, and
the bash wrappers (`scripts/server_git_update.sh`,
`scripts/bootstrap_server_updater.sh`) must be taught alongside it — those are
the paths actually in use.

So the updater must be taught about the new topology **in a deployment that
happens while the old topology is still running**, and the split itself is a
manual, out-of-band maintenance step, not a `server_git_update.sh` run. Phase 6
covers this; do not attempt to shortcut it.
