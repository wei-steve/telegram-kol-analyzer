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
[local]  scripts/server_git_update.ps1 -ExpectedCommit <40-hex> -ChangeClass <class>
            |
            v  ssh
[server] deploy/telegram-kol-update, run as /usr/local/bin/telegram-kol-update
         1. fetch origin/<branch>, assert FETCH_HEAD == ExpectedCommit
         2. assert the updater's own SHA256 matches the one in that commit
         3. assert telegram-kol.service is currently active
         4. take /run/telegram-kol-update.lock (one deployment at a time)
         5. schema_compatible only: SQLite backup + migration dry run
         6. deployment-preflight  <- gate 1, refuses to interrupt live work
         7. systemctl stop telegram-kol.service
         8. deployment-preflight + verify-deployment-preflight  <- gate 2
         9. git checkout <branch> && git merge --ff-only <ExpectedCommit>
        10. pip install -e .
        11. reinstall the updater from the deployed commit
        12. systemctl start telegram-kol.service, assert active
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

## Change class per phase

`CHANGE_CLASS` selects which gates run. Passing too weak a class skips a gate
that exists for a reason.

| Phase | Change class | Why |
|---|---|---|
| 0 | `code` | Additive observability only; no writer path, no schema |
| 1 | `execution_writer` | Changes the thread the management and break-even writers run on |
| 2 | `execution_writer` | Changes concurrency on the path that reaches order submission |
| 3 | `execution_writer` | Recovery loop invokes `authoritative_processor`, which can execute |
| 4 | `schema_compatible` | Adds the `message_processing_jobs` table and its index |
| 5 | `execution_writer` | Adds a worker that submits orders |
| 6 | `code` plus special handling | See the Phase 6 note below |

Rules attached to these classes, from `deploy/telegram-kol-update:28-42`:

- `execution_writer` and `live_promotion` **require** `-PreviousLiveSnapshotPath`
  pointing at a prior independent live position snapshot. Capture it before
  starting the deployment, not during.
- `live_promotion` additionally requires `-ReviewedShadowEvidencePath` and
  `-AuthorizeLivePromotion`. No phase in this remediation deploys as
  `live_promotion`: the mode changes in phases 2, 4, and 5 are trading-settings
  flips, not deployments.
- `schema_compatible` triggers a SQLite backup and a migration dry run before and
  after the service stops. Phase 4 depends on that evidence.

## Standard deployment command

Run from the local machine, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 -ExpectedCommit <40-hex-sha> -ChangeClass <class>
```

For a writer-sensitive class, add the snapshot argument:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 -ExpectedCommit <40-hex-sha> -ChangeClass execution_writer -PreviousLiveSnapshotPath /opt/telegram-kol-analyzer/data/web_cache/deepcoin_live_positions.json
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
   good 40-hex SHA and the same change class. The updater's own failure path
   already restores the previous commit if a deployment fails midway
   (`previous_commit` and its cleanup trap).
3. **Revert and redeploy.** Commit a revert locally, push, deploy it. Needed only
   when the previous commit is not a valid target.

Phases 0, 1, and 3 have no settings flag, so their rollback is level 2 or 3.
That is stated in each of those phase files.

## Phase 6 note

Phase 6 splits `telegram-kol.service` into three units. The updater hardcodes
that service name in its precondition (`systemctl is-active --quiet
telegram-kol.service`, step 3) and in its stop and start steps. It also
reinstalls itself from the deployed commit.

So the updater must be taught about the new topology **in a deployment that
happens while the old topology is still running**, and the split itself is a
manual, out-of-band maintenance step, not a `server_git_update.ps1` run. Phase 6
covers this; do not attempt to shortcut it.
