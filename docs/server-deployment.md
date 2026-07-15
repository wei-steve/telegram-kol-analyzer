# Server Deployment

This project is deployed on the Tencent Cloud server at `43.167.220.225`.

## Access

Open the web app from a computer or phone browser:

```text
http://43.167.220.225/
```

The site is protected by Nginx Basic Auth.

## Runtime Layout

Server project directory:

```bash
/opt/telegram-kol-analyzer
```

The server checkout is a Git clone of:

```bash
git@github.com:wei-steve/telegram-kol-analyzer.git
```

Default deployed branch:

```bash
codex/deepcoin-auto-trading-v1
```

The app runs through systemd:

```bash
systemctl status telegram-kol.service
systemctl restart telegram-kol.service
```

Nginx proxies public port `80` to the local app on `127.0.0.1:8000`.

## Update Flow

After making local changes, commit and push first. Then choose the helper for
your workstation:

```powershell
git add .
git commit -m "describe the change"
git push origin codex/deepcoin-auto-trading-v1
```

macOS / Linux:

```bash
./scripts/server_git_update.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Both helpers tell the server to:

1. Fetch and pull the latest GitHub code.
2. Reinstall the package in editable mode with the server virtualenv.
3. Restart `telegram-kol.service`.

The equivalent server-side command is:

```bash
BRANCH=codex/deepcoin-auto-trading-v1 /usr/local/bin/telegram-kol-update
```

After deploying the Deepcoin order-leg tracking update, run the one-time
history repair on the server so legacy `execution_bindings.payload_json`
submitted orders are copied into `execution_order_legs`:

```bash
cd /opt/telegram-kol-analyzer
. .venv/bin/activate
telegram-kol-research repair-execution-order-legs --database-path data/research.db
systemctl restart telegram-kol.service
```

The repair is idempotent. Re-running it updates the same `(binding, purpose,
leg_index)` rows instead of creating duplicates.

### Repair Deepcoin position attribution

Keep automation fail closed while repairing ownership. Back up SQLite before
reviewing or applying a plan:

```bash
cd /opt/telegram-kol-analyzer
. .venv/bin/activate
cp data/research.db "data/research.db.$(date +%Y%m%d-%H%M%S).bak"
telegram-kol-research repair-position-attribution --database-path data/research.db
```

The command is a read-only dry run unless `--apply` is supplied. Review every
old/new position owner, terminal leg transition, evidence summary, and
unresolved conflict. It never submits an exchange order or cancellation.

Dry-run output includes `historical_actions`. Before approving any of them:

- confirm no current `live_position_ids` appears in a historical action;
- verify the exact lifecycle, execution event, close reservation, exchange
  cancellation, or fully closed position-history row used as terminal evidence;
- confirm pending regular, trigger, and position-linked TPSL orders are absent
  from the cleanup component;
- review every affected leg, binding, and lifecycle old/new state;
- treat every `unresolved_conflicts` row as an apply blocker; and
- keep the global automatic-trading switch false.

A Deepcoin position-history row is fully closed evidence only when the exact
candidate `posId`, instrument, side, and split-position mode match, the original
`pos` is positive, and `closePos` equals the entire original size by exact
decimal comparison. Partial closure, missing or malformed sizes, mismatched
fields, conflicting duplicate rows, a history read failure, or any current
live/pending identity blocks cleanup. Never infer terminal state from a missing
live position or select one row from conflicting history.

When a stale competitor shares a duplicated historical `pos_id`, its exact
entry `order_id` may be queried as a historical position identifier. Treat that
order-derived identifier only as auditable closure evidence for the planned
historical clear/terminalize chain. It must not become a new live owner, a new
persisted `pos_id`, or a current repair in `actions`; verify it remains confined
to the affected `historical_actions` evidence.

If the plan contains `install_position_ownership_unique_index`, it must be the
last historical action and every planned redundant ownership clear must be
reviewed first. Index creation and all cleanup actions share one transaction;
an index or audit failure rolls the transaction back.

Only after the dry-run output matches the current Deepcoin positions and order
history, apply the exact freshly rebuilt plan:

```bash
telegram-kol-research repair-position-attribution \
  --database-path data/research.db \
  --apply \
  --expected-fingerprint <fingerprint-from-reviewed-dry-run>
systemctl restart telegram-kol.service
```

Nonempty apply requires the exact fingerprint copied from the reviewed dry run.
Apply refuses a stale database fingerprint, changed live position IDs, API
evidence errors, or unresolved attribution conflicts. After restart, run the
dry run again and repeat read-only reconciliation. Verify live positions,
entry-leg ownership, group labels, pending orders, and TPSL independently.
Re-enable automatic management only when every live position has one verified
entry leg and no attribution incident remains pending.

For historical cleanup, additionally require zero rows from the duplicate
`(venue, pos_id)` query and verify that
`uq_execution_order_legs_venue_pos` exists in `sqlite_master`. Do not apply a
nonzero historical plan as part of deployment review; deployment stops after a
fresh dry run until the operator separately approves the exact actions and
fingerprint.

## Data And Secrets

Do not commit runtime data or secrets to GitHub. Keep these on the server:

```text
data/
config/telegram.env
config/groups.yaml
config/ai_recognition.yaml
data/telegram.session
```

The `.gitignore` is expected to keep database files, Telegram sessions, media,
logs, and local config out of Git.

## Media Retention

Downloaded Telegram images are treated as a local cache. Message history, OCR
text, and AI recognition results stay in SQLite, but old non-critical image
files can be deleted to protect server disk space.

Preview cleanup:

```bash
cd /opt/telegram-kol-analyzer
. .venv/bin/activate
telegram-kol-research media-cleanup --dry-run
```

Apply cleanup:

```bash
cd /opt/telegram-kol-analyzer
. .venv/bin/activate
telegram-kol-research media-cleanup --apply
```

Default policy:

```text
retain_days: 14
max_media_dir_gb: 5
min_free_disk_gb: 10
```

The cleanup protects media linked to signal candidates or strategy lifecycle
records. When a file is removed, `media_assets.local_path` is cleared so the web
UI shows the media label and any OCR text instead of a broken image.
