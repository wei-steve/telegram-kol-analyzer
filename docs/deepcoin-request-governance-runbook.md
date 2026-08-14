# Deepcoin Request Governance and Protected Entry Rollout Runbook

Last reviewed: 2026-08-14.

This runbook deploys the request governor and protected-entry operation model
in separately approved stages. It is not an activation approval. Task 16 only
publishes the reviewed candidate; it does not update the server, restart a
service, change a production setting, or send an exchange request.

## Non-negotiable boundaries

- Run production commands only from `/opt/telegram-kol-analyzer` in a separately
  approved quiet window.
- Record the exact reviewed Git SHA. Never deploy an uncommitted checkout or a
  moving branch name without resolving it to that SHA first.
- Back up `data/research.db` and its live SQLite state before installation.
  Validate the backup before changing code or configuration.
- Stop `telegram-kol.service` before the final active-work query, backup, code
  installation, or environment change.
- A failed or incomplete Deepcoin read is `unknown`, never an empty account.
  Every promotion requires a complete, same-generation account snapshot and an
  exact protection proof for every live position.
- A version-1 protected-entry operation that has attempted any writer remains
  owned by version 1. Disabling the creation gate permits readback-only
  reconciliation; it does not permit legacy execution, replay, or another POST.
- Do not restore a pre-writer database backup after any exchange writer may
  have been submitted. Reconcile from exchange truth and durable idempotency
  evidence instead.
- Telemetry observes real calls. It creates no shadow or simulated exchange
  orders and imposes no delay.
- The known frozen two-leg incident is not repaired, migrated, replayed, or
  generalized by this rollout. An exception to the active-work gate requires
  its separately reviewed exact identity and SHA-256 evidence fingerprint,
  unchanged durable rows, a complete current-position snapshot, and exact
  protection proof. The exception grants no recovery or writer authority.
- Batch 119 remains a separate, dedicated recovery. Never run its planner,
  apply command, or runbook in the same deployment operation or quiet window.

## Batch 119 exact-read isolation

Batch 119 is not a request-governance stage and grants no exception to a Stage
1 active-work gate. Its allowlisted CLI dry-run uses a dedicated exact scope:
current positions, open orders, and pending TPSL; one position-history GET with
the durable `posId`; and one trigger-history GET with `limit=100` for each of
the two verified owned stop `ordId` values. With no durable regular-close
reference this is exactly six GETs and zero instrument-wide history reads. An
exact response at 100 rows without independent completion evidence remains
`snapshot_page_limit_ambiguous`; exact identity is not pagination authority.

Only one verified `stop_loss` or `backup_stop` may prove a natural stop. The
position history must prove the same position closed after that trigger. A
manual or unowned trigger, two stops claiming the close, any identity, owner,
purpose, or time mismatch, a remaining current position, or any durable close
request, response, client/exchange ID, mutation, or event stops the recovery.
The plan emits only bounded states and hashed references, never raw position or
order IDs, provider text, exchange JSON, or credentials.

The loader issues a process-local, non-serializable capture capability. It is
valid only for the exact snapshot object issued in the same CLI invocation; it
is not written to JSON, the database, or an operator record and cannot be
carried from dry-run into a later process. Every later invocation captures
again. Before any database mutation or idempotent return, the locked
transaction rebuilds source, exact scope, durable-close, exchange, and CAS
evidence. All dispositions require MiMo contract v1. Any path that could need a
writer also requires effective live settings in that same locked session and
an exact three-way match between capture, planning read-client expected, and
writer UID scope.
`position_absent` builds no writer and has zero POST, cancel, close, or TPSL
reachability.

The currently authorized stop point is two read-only dry-runs, each against a
new private consistent database copy. Follow the complete candidate worktree,
SQLite backup, copy-only bootstrap, and cleanup block in `docs/runbook.md`.
That block must:

- resolve the separately approved SHA after fetching the reviewed branch,
  require the approved remote ref to equal that full SHA, and record that
  non-sensitive SHA before creating the worktree;
- create a detached candidate worktree inside a mode-0700 temporary directory;
- create each database copy with SQLite `.backup`, set mode 0600, and pass
  `PRAGMA quick_check`;
- run additive bootstrap only on each copy through candidate `PYTHONPATH`;
- keep the production checkout, database, service, and settings unchanged; and
- remove the detached worktree, results, and database copies through the
  validated temporary-directory cleanup trap.

The dry-run itself must remain bound to that candidate source and use the
current CLI signature:

```bash
PYTHONPATH="$CANDIDATE_ROOT/src" "$RUNTIME_PYTHON" -m \
  telegram_kol_research.cli recover-composite-management-batch \
  --database-path "$RECOVERY_DB_COPY" \
  --generation-database-path "$PRODUCTION_DB" \
  --batch-id 119 \
  --deepcoin-contract-specs-path "$DEEPCOIN_CONTRACT_SPECS"
```

The generation database is opened OS read-only and is used only to read the
live account-write generation before and after the six GET capture. Additive
bootstrap remains restricted to the private copy. Both results must be
`ready`, report `production_writes=0` and
`exchange_calls=0`, and retain identical source population, exact scope,
collection digests, natural-stop ownership, source fingerprint, and evidence
fingerprint. Fresh capture timestamps are required but are not semantic
fingerprint input. Stop after the second result. Do not add `--apply`, deploy,
restart a service, bootstrap the production database, or change a setting.
Apply would require a separate approval and all of `--apply`,
`--expected-fingerprint`, and `--authorization`. Batch 119 recovery and Stage 1
must never share a deployment operation or quiet window.

## Stage record

Create one private operator record for each stage. Do not put credentials,
Telegram text, raw exchange payloads, order IDs, or position IDs in it.

```bash
cd /opt/telegram-kol-analyzer
APPROVED_SHA='<exact-reviewed-sha>'
DEPLOY_SHA="$(git rev-parse HEAD)"
test "$DEPLOY_SHA" = "$APPROVED_SHA"
test -z "$(git status --porcelain)"
systemctl show telegram-kol.service -p MainPID -p ActiveState -p SubState
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > "/tmp/trading-settings.${DEPLOY_SHA}.before.json"
```

Record only bounded counts, state names, timestamps, hashes, the reviewed SHA,
the approved stage, and the backup path. Do not copy the service environment or
governor files into the record.

## Common preflight and postflight

Every stage below repeats this gate. A stage stops at the first failure.

### Preflight

1. Confirm a separate approval for exactly one stage and a quiet window.
2. Confirm the checkout SHA and clean worktree.
3. Save the complete current trading-settings response for comparison only.
   Do not later POST this stale snapshot as a rollback payload.
4. Stop the service, then query active, uncertain, or writer-owned operations.
   After Stage 1 has bootstrapped the additive schema, use:

```bash
sudo systemctl stop telegram-kol.service
systemctl is-active --quiet telegram-kol.service && exit 1 || true

sqlite3 -readonly data/research.db <<'SQL'
.headers on
.mode column
SELECT state, outcome_certainty, COUNT(*) AS rows
FROM deepcoin_execution_operations
WHERE state IN (
  'entry_submitting', 'entry_pending_readback', 'entry_unknown',
  'protection_pending_readback', 'protection_unknown',
  'recovery_required'
)
   OR outcome_certainty = 'unknown'
GROUP BY state, outcome_certainty
ORDER BY state, outcome_certainty;

SELECT COUNT(*) AS nonterminal_writer_owned
FROM deepcoin_execution_operations
WHERE writer_attempted_at IS NOT NULL
  AND state NOT IN (
    'completed', 'protected', 'entry_rejected',
    'submission_failed_no_exposure'
  );
SQL
```

Any returned active/unknown row blocks the stage unless it is resolved by its
existing versioned GET-only reconciler. The frozen-incident exception cannot be
expressed as a broad state, age, symbol, or side exclusion. Attach the exact
separately reviewed fingerprint proof or abort.

Before the first Stage 1 installation, the new table is expected not to exist.
That one absence is not a successful active-work proof. Use the existing global
deployment queries instead and require zero current execution ownership:

```bash
sqlite3 -readonly data/research.db <<'SQL'
.headers on
.mode column
SELECT status, COUNT(*) AS rows FROM trade_signals
WHERE status IN ('processing', 'executing', 'entry_submitting',
                 'entry_pending_readback', 'entry_unknown',
                 'active_protection_pending', 'recovery_required')
GROUP BY status;
SELECT status, COUNT(*) AS rows FROM strategy_management_batches
WHERE status IN ('ready', 'executing', 'reserved', 'submitted',
                 'submit_unknown', 'reconciling', 'partial_failed',
                 'protection_ready', 'recovery_required')
GROUP BY status;
SELECT status, COUNT(*) AS rows FROM position_mutation_intents
WHERE status IN ('reserved', 'submitting', 'submitted',
                 'recovery_required')
GROUP BY status;
SELECT status, COUNT(*) AS rows FROM execution_order_legs
WHERE purpose = 'entry' AND status IN ('submitting', 'cancel_submitting')
GROUP BY status;
SQL
```

Any row blocks Stage 1. After the new binary creates the additive tables, stop
the service again and run the canonical `deepcoin_execution_operations` query
before considering Stage 1 complete.

5. Make and validate a private backup while the service is stopped:

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="data/research.db.${STAMP}.${DEPLOY_SHA}.bak"
sqlite3 data/research.db ".backup '$BACKUP'"
chmod 600 "$BACKUP"
sqlite3 -readonly "$BACKUP" "PRAGMA quick_check;" | grep -Fx OK
sha256sum "$BACKUP" > "${BACKUP}.sha256"
chmod 600 "${BACKUP}.sha256"
```

6. With the service still stopped, perform the separately approved governed
   read-only account capture. It must prove availability, valid schemas,
   affirmative pagination completion, unchanged even account-write generation,
   and exact current positions/open orders/pending TPSL/history. Compare its
   canonical collection fingerprints with the final pre-stop capture. Do not
   substitute a Web card, one list endpoint, or a row count for this proof.
7. For every live position, prove that each required stop is present with exact
   `posId`, instrument, side, size/economics, ledger ownership, and current
   pending-order identity. Missing, duplicate, unowned, stale, or conflicting
   protection aborts the stage.
8. Record the known frozen incident's exact reviewed fingerprint separately.
   Recompute it from unchanged durable identity plus the complete current
   position/protection evidence. A mismatch aborts; a match only removes that
   one incident from the deployment hold and grants zero write authority.

### Postflight

After applying only the approved stage change:

```bash
sudo systemctl start telegram-kol.service
systemctl is-active --quiet telegram-kol.service
systemctl show telegram-kol.service -p MainPID -p ActiveEnterTimestamp
curl -fsS http://127.0.0.1:8000/ >/dev/null
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > "/tmp/trading-settings.${DEPLOY_SHA}.after.json"
curl -fsS \
  'http://127.0.0.1:8000/api/execution/deepcoin-request-health?window_minutes=15' \
  | jq -e '.complete == true'
journalctl -u telegram-kol.service --since '-10 minutes' \
  --no-pager -p warning
```

Then rerun the complete exchange snapshot and exact live-position protection
proof. Confirm that database state, exchange state, configured stage, PID/HTTP,
and bounded logs match the approved expectation. An unknown writer, incomplete
metrics scan, protection gap, authentication error, schema incompatibility,
unexpected setting drift, or fingerprint drift stops the rollout.

### Rollback boundary

- Before any versioned writer attempt, restore the prior code/config only after
  stopping the service and repeating the preflight. Restore the database backup
  only when exchange evidence proves that no request could have been submitted.
- After any versioned writer attempt, never restore the database backup and
  never route that operation to legacy code. Disable creation of new protected
  operations, keep the current version-1 reconciler installed, and resume only
  GET-only readback/supervision from durable evidence.
- A governor mode rollback changes only the environment mode after a new quiet
  window; it does not delete governor state, attempts, snapshots, operations,
  ledger rows, or exchange history.
- A protected-entry rollback fetches current settings immediately before the
  write, changes only `protected_entry_execution_mode` to `disabled`, and
  verifies the response. It never lowers the watermark.

## Stage 1: dormant foundation (`disabled`)

Purpose: install additive schema, persistent client, evidence repositories,
governor code, read-only reconciliation, Web projection, and metrics while both
new gates remain dormant.

Required configuration:

```text
DEEPCOIN_REQUEST_GOVERNOR_MODE=disabled
protected_entry_execution_mode=disabled
```

Use the common preflight: exact SHA, stopped-service active/unknown query,
validated backup, complete exchange/protection proof, and exact frozen-incident
fingerprint with no recovery authority. Install the reviewed SHA and editable
package, but do not promote a setting.

Postflight must additionally prove:

- the governor environment resolves to `disabled`;
- protected-entry mode is `disabled` and its watermark is unchanged;
- schema bootstrap is additive and historical/frozen rows are unchanged;
- background reconciliation is GET-only for protected operations; and
- no new `deepcoin_execution_operations` writer-owned row appeared during the
  restart.

Rollback is permitted only under the common pre-writer rule. If any versioned
writer evidence exists, retain the version-1 reconciler and use readback-only
supervision; no legacy handoff is allowed.

## Stage 2: governor telemetry

Purpose: measure real request pressure without sleeping, reserving enforced
capacity, or creating simulated orders.

Before the common preflight, prepare one service-owned absolute state directory
on a protected parent filesystem. It must be owned by the service UID, mode
`0700`, not a symlink, and must never contain an API key in its path or files.
Set only:

```text
DEEPCOIN_REQUEST_GOVERNOR_MODE=telemetry
DEEPCOIN_GOVERNOR_STATE_DIR=/var/lib/telegram-kol/deepcoin-governor
```

Repeat the exact SHA/backup/active-work/exchange/protection/frozen-incident
preflight. Keep `protected_entry_execution_mode=disabled`. After restart,
verify PID/HTTP/log/database/exchange state plus:

- observed governor delay is recorded, enforced wait remains zero;
- no synthetic or shadow order was created;
- no credentials or raw payloads appear in state files, metrics, logs, or Web;
- request-health scans are complete and bounded; and
- critical/background endpoint counts match durable attempts.

Rollback changes the environment mode to `disabled` in a new quiet window. Do
not delete telemetry files or durable evidence. The common no-legacy-handoff
rule applies after any versioned writer attempt.

## Stage 3: enforce reads in two approvals

Purpose: enforce the documented UID-shared budgets for GET traffic while
writers remain on their existing transport behavior.

`enforce_reads` is intentionally one transport mode for all GET priorities; it
is not a hidden priority-specific feature flag. Therefore Stage 3A is a
controlled read-only server verification with the normal service stopped and
must return the installed service to `telemetry`. Stage 3B is the later,
separately approved service-wide promotion. Do not claim that a running service
is enforcing only Web/background GETs.

### Stage 3A: background and Web reads

With the normal service stopped, run the separately approved read-only
background/Web capture under
`DEEPCOIN_REQUEST_GOVERNOR_MODE=enforce_reads`. The verification process must
have no exchange writer method or mutation worker. Use the common exact SHA,
backup, stopped-service active-work query, complete exchange/protection proof,
and frozen-incident fingerprint gate. Return the installed service environment
to `telemetry` before its postflight restart.

Postflight must prove bounded background waits/defer behavior, event-loop and
critical-lane liveness, complete metrics, no false-empty snapshot, and zero
writer retries. A background deadline may defer a cycle; it may not create an
unknown writer fact when the typed outcome is `NOT_SENT`.

### Stage 3B: critical reads

Only after Stage 3A has separately reviewed evidence, approve the running
service's background, Web, critical entry, protection, and reconciliation GETs
under `enforce_reads`. Repeat the full preflight and postflight. Verify critical reserve availability,
`Retry-After`/deadline bounds, timestamp/signing after governor wait, row-100
pagination refusal, and exact same-generation snapshot authority.

Rollback either approval to `telemetry` or `disabled` only after a new common
preflight. Preserve state and evidence. Any attempted versioned writer stays on
version-1 readback; it is never handed to legacy.

## Stage 4: future-only protected entry

Purpose: create version-1 protected-entry operations only for TradeSignals
strictly above an immutable activation watermark. Governor remains
`enforce_reads`; writer governance is not promoted in this stage.

During the stopped-service common preflight, record the database high-water
mark. Restart in the still-disabled mode and prove dormant health. Immediately
before activation, fetch the current settings and use the API's latest
TradeSignal watermark (the locked save validator rejects any race with a newer
signal):

```bash
STOPPED_WATERMARK="$(sqlite3 -readonly data/research.db \
  'SELECT COALESCE(MAX(id),0) FROM trade_signals;')"
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > /tmp/trading-settings.protected-entry.current.json
WATERMARK="$(jq -er '.protected_entry_latest_trade_signal_id' \
  /tmp/trading-settings.protected-entry.current.json)"
test "$WATERMARK" -ge "$STOPPED_WATERMARK"
jq --argjson watermark "$WATERMARK" \
  '.protected_entry_execution_mode="live"
   | .protected_entry_execution_after_trade_signal_id=$watermark' \
  /tmp/trading-settings.protected-entry.current.json \
  > /tmp/trading-settings.protected-entry.live.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.protected-entry.live.json \
  http://127.0.0.1:8000/api/trading-settings
```

The settings write occurs only after the service is restarted and its dormant
health is proven. Enabling is a separate approval from deployment. Read back
the setting and prove the stored watermark is exactly `WATERMARK`. Existing
`id <= watermark` signals and all historical incidents must stay on their
existing authority. If a new signal wins the race, the settings API must reject
the stale watermark; fetch again and restart the approval check rather than
lowering or guessing the boundary.

For the first future signal, review its pinned `contract_version=1`, exact
operation/child identities, ten-second deadline, protection-before-next-leg
gate, attempt facts, complete snapshots, and lifecycle projection. Unknown
writers must have one logical POST and GET-only recovery. A deferred later leg
must remain `pre_submit_deferred` with no timer resubmission.

Rollback fetches current settings, changes only mode to `disabled`, preserves
the nondecreasing watermark, and reads it back. If no versioned writer was
attempted, the operation stops. If a writer was attempted, its v1 reconciler
remains installed and owns readback only; there is no legacy handoff or database
restore.

## Stage 5: enforce all new-version writers

Purpose: govern writer transport only after Stages 1-4 have separately reviewed
evidence and all active version-1 operations are understood.

Set `DEEPCOIN_REQUEST_GOVERNOR_MODE=enforce_all` in a new approved quiet window.
Repeat the exact SHA, stopped-service active/unknown query, validated backup,
complete exchange/protection proof, and frozen-incident fingerprint check.

Postflight must prove:

- each new-version writer is charged exactly once to the UID/path budget;
- a governor/deadline refusal before send is durable `not_sent`, never
  `unknown`, and may be re-armed only through exact identity CAS;
- an accepted, rejected, or unknown POST is never resent;
- signing timestamp is generated after governor wait and the deadline is
  rechecked immediately before HTTP send;
- protection is completely read back before a later-leg POST; and
- disabled-after-writer operations remain GET-only under their pinned version.

Rollback the environment mode only after a new common preflight. Do not delete
governor windows or attempt evidence. Disabling protected-entry creation is
allowed, but a writer-owned operation remains version-1 readback-only.

## Stage 6: legacy retirement

Purpose: remove old transition/string paths only in a later reviewed code
change. This stage is not authorized by this runbook alone.

Before proposing retirement, repeat the common exact SHA/backup/active-work/
exchange/protection/frozen-incident gate and prove every legacy and versioned
operation is terminal or has an explicitly retained versioned readback owner.
There must be zero unknown writer, recovery-required writer, pending readback,
deferred ownership ambiguity, or unclassified historical row. Batch 119 and the
known frozen incident remain separate and unchanged.

Retirement requires its own tests, independent review, commit, deployment
approval, post-restart PID/HTTP/log/database/exchange verification, and rollback
plan. If any writer-owned state is found after retirement starts, stop; do not
restore a stale database or hand the operation to legacy. Reinstall the last
version containing its pinned reconciler and reconcile from durable exchange
evidence.

## Stage completion evidence

A stage is complete only when its operator record contains:

- exact reviewed and deployed SHAs;
- validated private backup path and SHA-256;
- stopped-service active/unknown query output;
- complete exchange collection fingerprints and unchanged generation;
- exact per-position protection proof;
- the frozen-incident reviewed fingerprint result and explicit zero authority;
- before/after setting or environment diff limited to the approved field;
- post-restart PID, HTTP, bounded warning log, database, metrics, and exchange
  verification;
- the applicable rollback boundary; and
- an explicit statement that batch 119 was not invoked.

Do not advance automatically. Each following stage requires a new approval.
