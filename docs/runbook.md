# Telegram KOL Research Runbook

## Purpose

This runbook describes the local operator flow for researching archived
Telegram strategy groups with a Telegram user account on macOS.

## 1. Local Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

If needed, use the already-verified environment:

```bash
source .venv313b/bin/activate
```

## 2. Telegram User Auth

Export your Telegram API credentials:

```bash
export TELEGRAM_API_ID="your_api_id"
export TELEGRAM_API_HASH="your_api_hash"
export TELEGRAM_SESSION_PATH="data/telegram.session"
```

This project uses your Telegram user session and targets archived chats. It
does not use the Bot API.

If you do not want to export them in every shell, save the same values in a
repo-local `.env` file or `config/telegram.env`. The CLI and web workbench
load those files automatically.

## 3. Configure Target Groups

Copy the example config and edit the target groups:

```bash
cp config/groups.example.yaml config/groups.yaml
```

For each strategy group:
- Set `chat_title`
- Leave `enabled: true` only for groups you want to track
- Fill `tracked_senders` for each admin or signal poster you want scored
- Optionally set sync date bounds for backfill research windows

## 4. Backfill and Sync

The main sync entry command is:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli sync
```

Current implementation status:
- Archived target dialogs are discovered from your Telegram user account
- Recent history is fetched and normalized into the local SQLite database
- Downloaded image media is stored under `data/media`
- Parsed signal candidates and trade ideas are refreshed during sync
- Stale history checkpoints are repaired before sync continues

## 5. Incremental Listening

Start the web workbench with Telegram credentials loaded to enable realtime
listening:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web --host 127.0.0.1 --port 8000
```

Realtime delivery uses two paths:
- Push path: Telethon live listener publishes browser updates through SSE
- Recovery path: a periodic reconcile pass replays a small recent window so
  missed messages after reconnects can still land in SQLite safely

The message header also includes an `立即刷新` button for a one-shot reconcile.
If credentials are missing or invalid, the failure reason is returned directly
in the page instead of failing silently.

If Telegram credentials are missing, the workbench still opens but stays in
local-snapshot mode and shows how stale the current database is.

## 6. Review Ambiguous Candidates

List pending review candidates from a local JSON file:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli review --candidate-file data/candidates.json
```

Apply a decision:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli review \
  --candidate-file data/candidates.json \
  --candidate-id 101 \
  --decision confirmed \
  --note "Chart text and caption agree"
```

## 7. Generate Reports

Write a leaderboard report locally:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli report --output-path reports/leaderboard.json
```

This writes JSON output to the path you specify.

## 8. Launch the Web Workbench

Start the local browser UI:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web --host 127.0.0.1 --port 8000
```

Before using the AI panel, configure the LLM proxy environment:

```bash
export TELEGRAM_KOL_LLM_BASE_URL="http://127.0.0.1:8317"
export TELEGRAM_KOL_LLM_API_KEY="your_proxy_api_key"
export TELEGRAM_KOL_LLM_MODEL="gpt-4.1-mini"
export TELEGRAM_KOL_LLM_TIMEOUT_SECONDS="60"
```

Current web workbench behavior:

- Phone navigation is `策略 / 持仓 / 动态 / 群组 / 更多`; `策略` opens first.
- The strategy list starts with all groups plus `需要处理` and offers lifecycle
  and group filters without disruptive refresh or scroll loss.
- Strategy details combine source, MiMo recognition, lifecycle, binding,
  execution, management, and current exchange evidence in a read-only chain.
- `动态` retains chronological messages, downloaded media, search/sender
  filtering, bounded load-more behavior, and recognition detail access.
- `群组` retains configured aliases and per-group AI/prompt context.
- `/api/events` supplies non-disruptive new-change notification through SSE;
  reconcile windows still replay a small safety overlap.
- Telegram, database, and Deepcoin freshness/failure remain independent; a
  failed Deepcoin read preserves an explicit unknown state.

Recommended browser flow:

1. Open the workbench; `策略` is the default phone destination.
2. Start with all groups plus `需要处理`, then use `全部 / 执行中 / 待入场 /
   已结束` or the group selector only when narrowing is useful.
3. Open a strategy record to inspect recognition, source evidence, lifecycle,
   binding, execution events, real position/TPSL, and management batches.
4. Use `持仓` for exchange-first inspection, `动态` for message/event-first
   inspection, `群组` for per-KOL context, and `更多` for configuration/logs.
5. Treat `无法确认` or Deepcoin `unknown` as missing current evidence. Never read
   it as zero position size or proof that no position exists.

### Strategy record authority and safe verification

Strategy records are read-only views. The operational authority chain is:

`message -> candidate -> lifecycle -> binding -> exchange state`

For split positions, expand `binding` through active verified `entry` legs and
check every exact `pos_id`; never query Deepcoin with the comma-joined binding
compatibility value. For TPSL evidence, prefer `closePosId` and quarantine an
exact ID that is absent from the current position snapshot. It must not enter
the symbol/side/time fallback matcher.

List/detail and cross-destination links must preserve unique IDs. Message links
use the selected candidate ID and fail closed if it owns multiple lifecycles.
Position links use a unique execution binding ID. Do not infer a strategy owner
from chat/message, symbol, side, group name, or rendered labels.

The strategy list must not expose close, bind, order, TPSL, or settings
mutations. Existing detail confirmation and server-side validation remain in
force. During UI verification, use read-only navigation and refresh only.

Before production approval, deploy a reviewed commit through the documented
GitHub/server workflow and verify both 390x844 and 1440x900 against server-served
current data. Check:

- the default is `策略`, all groups, and `需要处理`;
- list filters and group selection preserve current state and scroll;
- a real record traces source message, authoritative MiMo decision, lifecycle,
  binding, execution events, current position/TPSL, and management batches;
- unassigned, ambiguous, conflicting, stale, and unavailable exchange evidence
  remains visibly unconfirmed;
- a management message whose lifecycle stop differs from exact current
  Deepcoin protection appears as `management_execution_drift` in both list and
  detail unless a confirmed batch for that same message and stop explains it;
- detail renders each verified live leg's position ID, size, protection state,
  stop, and take-profit evidence separately;
- Deepcoin failure does not render confirmed zero;
- long evidence has no horizontal overflow, navigation does not cover content,
  phone touch targets are at least 44px, and desktop uses the same data model;
- no trade or configuration mutation is submitted.

Record deployed commit, service state, route/asset HTTP results, phone and
desktop screenshots, and deferred limitations in `docs/migration-handoff.md`.
Local screenshots or deterministic tests are not substitutes for this server
gate. As of 2026-07-17 the gate is pending because the managed local environment
blocks port binding and local `file:` navigation; do not describe it as passed.

## Per-group automatic-entry position cap

`max_concurrent_positions` is the cap for one exact Telegram `chat_id`, not an
account-wide limit, and its code default is `4`. The effective-position count
includes only distinct Deepcoin entry `posId`s whose entry legs are both
`active` and `attribution_status=verified` through the exact group binding.
Pending regular or trigger orders do not count.

At or above the cap, a new automatic entry is skipped with
`group_position_limit_reached`. The cap never blocks management actions,
including partial take profit, full exit, stop changes, or temporary exit. The
count and entry submission are intentionally not one transaction: two
concurrent entry messages for the same group can both observe a count below the
limit and briefly exceed it. This small concurrent-entry race is an accepted
operational boundary; reconciliation and later management still use exact
verified `posId` ownership.

## Operate the server production safety monitor

The production safety monitor is a separate read-only oneshot service under the
dedicated unprivileged `telegram-kol-monitor` identity. It
checks the deployed commit, `telegram-kol.service` state, the approved live
gates, bounded journal errors, bounded abnormal execution summaries, and the
daily management audit. It writes only its monitor-identity-owned mode-`0600`
runtime state file under
`/var/lib/telegram-kol-monitor`; it has an empty capability set, no system-bus
socket access, and no access to the checkout's `.env`, `config/`, or other
`data/` content. It does not change trading settings, write the
production database, call a Deepcoin mutation, or restart the trading service.
Normal trade notifications are not duplicated: only actionable system
abnormalities are eligible for monitor alerts.

The management audit reports informational keep-holding history separately as
`counts.informational_noop`. Only exact historical zero-leg
`hold_update`/`blocked` rows with reason
`management_intent_not_supported` qualify; they do not contribute to the
monitor's `audit_abnormal_count`. All other blocked or leg-bearing rows remain
abnormal. New `hold_update` recognition is skipped before a Deepcoin client or
management batch is created.

After deploying a graceful-shutdown change, perform one controlled restart and
inspect the old process rather than relying only on the new service's active
state:

```bash
old_pid=$(systemctl show telegram-kol.service -p MainPID --value)
systemctl restart telegram-kol.service
systemctl is-active telegram-kol.service
new_pid=$(systemctl show telegram-kol.service -p MainPID --value)
test "$old_pid" -gt 0
test "$new_pid" -gt 0
test "$new_pid" != "$old_pid"
echo "old_pid=$old_pid new_pid=$new_pid"
journalctl -u telegram-kol.service --since '-2 minutes' --no-pager
```

The journal must not contain `stop-sigterm timed out`, `SIGKILL`, or a stop
timeout for that restart, and the new `MainPID` must differ from `old_pid`.

If an existing state file is unreadable or malformed, the monitor repairs the
four-field file but keeps a pending state-integrity notification as a fingerprint
with no notification timestamp until one delivery succeeds. A `--notify` run
retries that fixed `state_invalid` alert after missing configuration or delivery
failure. A no-notify diagnostic run still advances successfully read window and
audit progress, but it does not mark it delivered; the pending alert remains for
a later notification-enabled invocation. If `state_invalid` is delivered together
with a continuing real anomaly, the message reports both, while the durable
six-hour dedupe fingerprint excludes the acknowledged one-shot `state_invalid`
reason. The continuing anomaly is therefore not sent again merely because the
state file was repaired; a genuine change to that anomaly still notifies
immediately.

Provision the monitor-only root credential file once. It may contain only the
system-operator bot token, chat ID, and optional timeout; never put a Deepcoin,
Telegram-session, database, or application credential in this file:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/telegram-kol-monitor.credentials
sudoedit /etc/telegram-kol-monitor.credentials
```

```text
TELEGRAM_KOL_SYSTEM_BOT_TOKEN=<operator-bot-token>
TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID=<operator-chat-id>
```

For every install or upgrade, explicitly stop and disable the monitor first.
The install-only helper fails before changing users, files, or systemd state if
the timer is active or enabled:

```bash
sudo systemctl disable --now telegram-kol-monitor.timer
if systemctl is-enabled --quiet telegram-kol-monitor.timer || \
   systemctl is-active --quiet telegram-kol-monitor.timer; then
  echo "monitor timer must be disabled and inactive" >&2
  exit 1
fi
```

After deploying and reviewing the fixed production checkout, install the files
without enabling or starting the timer:

```bash
cd /opt/telegram-kol-analyzer
sudo ./scripts/install_server_monitor.sh
if systemctl is-enabled --quiet telegram-kol-monitor.timer || \
   systemctl is-active --quiet telegram-kol-monitor.timer; then
  echo "unexpected enabled or active monitor timer" >&2
  exit 1
fi
```

The installer refuses any path other than the validated
`/opt/telegram-kol-analyzer` Git root. It freezes that checkout's current
`git rev-parse HEAD` with only the allowlisted operator-bot fields in the
root-owned `0600` `/etc/telegram-kol-monitor.env`. Rerun the installer after each later
reviewed deployment to advance that expected-commit baseline. Before enabling,
start the installed static diagnostic unit. It runs the full audit without
notification delivery under the same dedicated identity, read-only mounts,
credentials isolation, and sandbox as the scheduled monitor:

```bash
sudo systemctl start telegram-kol-monitor-diagnostic.service
journalctl -u telegram-kol-monitor-diagnostic.service -n 20 --no-pager
```

The diagnostic unit is static and never enabled. Confirm its compact result is
healthy. Then start the installed static oneshot
unit to send exactly one clearly labelled notification-chain test. The unit
loads `/etc/telegram-kol-monitor.env`, runs as the dedicated identity with the
same sandbox as the scheduled monitor, is never enabled, and does not call the
settings, database, audit, or exchange adapters:

```bash
sudo systemctl start telegram-kol-monitor-test-notification.service
```

Confirm exactly one message beginning `【监控测试】` arrived, then enable only
the monitor timer and inspect its schedule and latest result:

```bash
sudo ./scripts/install_server_monitor.sh --enable
systemctl is-active telegram-kol-monitor.timer
systemctl list-timers telegram-kol-monitor.timer --no-pager
journalctl -u telegram-kol-monitor.service -n 50 --no-pager
systemctl is-active telegram-kol.service
```

Rollback removes only the monitor timer, service, expected-commit file, and
optional independent state. It must leave `telegram-kol.service`, trading
settings, the production database, and exchange state untouched:

```bash
sudo systemctl disable --now telegram-kol-monitor.timer
sudo systemctl stop telegram-kol-monitor.service telegram-kol-monitor-diagnostic.service telegram-kol-monitor-test-notification.service
sudo systemctl clean --what=state telegram-kol-monitor.timer
sudo rm -f /etc/systemd/system/telegram-kol-monitor.timer
sudo rm -f /etc/systemd/system/telegram-kol-monitor.service
sudo rm -f /etc/systemd/system/telegram-kol-monitor-test-notification.service
sudo rm -f /etc/systemd/system/telegram-kol-monitor-diagnostic.service
sudo rm -f /etc/telegram-kol-monitor.env
sudo rm -f /etc/telegram-kol-monitor.credentials
sudo rm -rf /var/lib/telegram-kol-monitor
sudo systemctl daemon-reload
```

## Audit strategy-management batches

Keep both production gates off for this rollout:

```text
auto_trade_enabled=false
management_execution_mode=disabled
```

`disabled` neither plans nor executes position management. `shadow` may save a
plan for review but cannot send it to Deepcoin. `live` can execute only when
the global automatic-trading gate is also true. Do not enable shadow or live as
part of this rollout. Live requires a new explicit approval after reviewed
shadow evidence.

Percentage and contract-size rules for live management:

- `止盈/减仓/平仓 60%` means close 60%; `保留/剩余 40%` also means
  close 60%. Conflicting explicit close/retain values are blocked.
- The planner apportions the aggregate target across every verified `posId`
  using the instrument's `quantity_step` and `min_quantity`. The executor
  validates the persisted step, minimum, current live size, and exact step
  alignment again immediately before any Deepcoin close request.
- An off-step or stale quantity is an operator-recovery event, not a reason to
  round ad hoc or retry. Inspect the immutable batch, live positions, and the
  verified contract specification; do not edit the quantity in SQLite.
- AI recognition is intent only. Treat Web `已提交，等待交易所确认` as
  still open until reconciliation reports `交易所已确认执行`. Protection
  updates use the separate `Deepcoin 已接受保护单更新` label.
- Never replay old Telegram management messages to repair a missed action.
  Reconcile the current exchange snapshot first and wait for a new instruction
  or an explicitly reviewed operator action.

Run the bounded read-only audit from the server checkout:

```bash
telegram-kol-research audit-management-batches \
  --database-path data/research.db \
  --limit 20 \
  --output-format text

telegram-kol-research audit-management-batches \
  --database-path data/research.db \
  --limit 20 \
  --output-format json
```

The command never opens the source database with SQLite. It reads the main DB
and any WAL/SHM components as ordinary files twice, compares file sets,
metadata, hashes, and bytes, then writes two private temporary snapshots. Both
private copies must pass SQLite `quick_check` and schema inspection before one
is queried. A rollback journal, active-file change, inconsistent copies, or
failed validation returns `snapshot_unstable` and no audit data. SQLite may
create/checkpoint sidecars only inside the temporary directory; the source DB
and its directory remain untouched even when they are read-only.

Source copying is platform-gated before reading. On Linux the source descriptor
must open successfully with `O_NOATIME`; permission failure has no ordinary-read
fallback and returns `snapshot_unavailable`. On macOS/APFS the command requires
the atomic copy-on-write `clonefile(2)` capability, then hashes the private
clone. Other platforms, unsupported volumes, or cross-volume clone failure are
refused before a source stream is read. Access time is part of the compared
source metadata. Main DB, WAL, and SHM evidence are processed in fixed-size
chunks with incremental hashes; component bytes are never accumulated in
memory. SHM remains stability evidence only, while main DB and WAL form each
private SQLite snapshot.

The command does not initialize or migrate the schema, claim or convert legacy
signals, build a Deepcoin client, make a network call, retry work, or change
database state. Output is capped at 100 batches and 100 returned legs per
batch. Every identity is a one-way reference, including signal, message, batch,
raw-message, lifecycle, binding, leg, chat, strategy, and position IDs. Only
counts, validated sizes, states, modes, leg indexes, completeness flags, and
malformed-field flags remain clear. Pending legacy management candidates are
streamed completely for exact counts while only bounded redacted items are
returned.

Legacy `payload_json`, batch `target_snapshot_json`, and leg `last_error` share
one bounded validator: 65,536 characters, 262,144 UTF-8 bytes, and nesting
depth 64 are checked before object construction. Historical JSON and decimal
text are resource-bounded before parsing or fixed formatting. Oversized/deep
payloads, non-canonical or overlong batch IDs, huge decimal exponents,
malformed old columns, and parser resource errors are reported only through
fixed malformed counters/flags. Raw values, exception text, and tracebacks are
never audit output. Temporary-directory creation, private writes, sync, and
cleanup failures similarly return a fixed `snapshot_unavailable` reason in
both JSON and text mode.

The audit includes abnormal counts for `blocked`, `submit_unknown`,
`partial_failed`, and `recovery_required`. Never conclude that there is no
legacy or abnormal residue if `output_complete=false`, `complete=false`,
`scan_truncated=true`, `batches_truncated=true`, any `legs_truncated=true`, or
the legacy item list is truncated. `management_schema_missing` or
`schema_unavailable` is an audit result, not permission to run a compatibility
migration.

Interpret and handle abnormal states as follows:

- `blocked`: correct the identity/evidence/configuration defect, then allow a
  new source message to form a new batch. Do not edit the blocked row.
- `submit_unknown`: freeze. Compare the exact client/order identity against a
  fresh coherent exchange snapshot. Never automatically retry an unknown
  request, because the first request may have succeeded.
- `partial_failed`: retain and report earlier confirmed leg successes. Review
  each failed leg and its exchange evidence independently; do not repeat the
  entire strategy action.
- `recovery_required`: keep both gates off, preserve the batch/outbox, and
  collect exact position, order, fill, and TPSL evidence. Resume only through a
  separately reviewed recovery procedure.
- Manual close/cancel: verify the exact `posId` or order is absent/terminal,
  reconcile the verified entry leg and lifecycle, then audit again. Never bind
  a new same-symbol position by proximity.
- Stale or conflicting ownership: leave it unassigned/conflicted, audit the
  candidate/lifecycle/binding/entry legs separately, and use the fingerprinted
  attribution-repair dry run if appropriate. Never auto-repair ambiguity.

Pending legacy management signals are audit-only. Do not claim, execute, or
convert them during deployment. Deepcoin triggered-limit lineage is a separate
branch and must not be repaired or migrated by this rollout.

## 9. Test the Project

Run the current automated test suite:

```bash
python3 -m pytest tests -v
```

## 10. Audit Semantic Disagreement Review

The Web service runs DeepSeek semantic review in the background after MiMo has persisted the real automation outcome. `execution_pending` and `execution_running` belong to MiMo execution ownership; `pending`, `running`, `completed`, and `failed` belong to later semantic review. Do not change a row, replay recognition, or place an order merely to clear an audit state.

Only `critical` review results are eligible for a system-operator bot message. `normal` and `none` remain Web-visible/database-only. A completed historical row with no severity appears as `待重新复核` (`unclassified`); it is not evidence that the models agreed. DeepSeek errors retry at most three times, then remain `failed` without changing the MiMo decision or automation result. A `running` claim older than five minutes can be recovered by the worker. Notification `scheduled`, `sent`, and `failed` are at-most-once terminal claims and must not be reset for an automatic resend.

Run the following on the production server only, from `/opt/telegram-kol-analyzer`. `sqlite3 -readonly` prevents accidental writes, and these grouped queries return counts/timestamps only: they do not select Telegram message text, model payloads, exception text, or credentials.

```bash
sqlite3 -readonly data/research.db <<'SQL'
.headers on
.mode column

SELECT comparison_status, COUNT(*) AS row_count
FROM recognition_decisions
GROUP BY comparison_status
ORDER BY comparison_status;

SELECT COALESCE(disagreement_severity, 'unclassified') AS severity,
       COUNT(*) AS row_count
FROM recognition_decisions
WHERE comparison_status = 'completed'
GROUP BY COALESCE(disagreement_severity, 'unclassified')
ORDER BY severity;

SELECT COUNT(*) AS pending_count,
       MIN(updated_at) AS oldest_pending_updated_at,
       ROUND((julianday('now') - julianday(MIN(updated_at))) * 1440, 1)
         AS oldest_pending_age_minutes,
       SUM(CASE
             WHEN comparison_next_attempt_at IS NOT NULL
              AND comparison_next_attempt_at > CURRENT_TIMESTAMP
             THEN 1 ELSE 0
           END) AS retry_delayed_count
FROM recognition_decisions
WHERE comparison_status = 'pending';

SELECT comparison_attempts,
       COUNT(*) AS failed_count,
       SUM(CASE WHEN comparison_error IS NOT NULL THEN 1 ELSE 0 END)
         AS rows_with_error
FROM recognition_decisions
WHERE comparison_status = 'failed'
GROUP BY comparison_attempts
ORDER BY comparison_attempts;

SELECT COALESCE(notification_status, 'not_scheduled') AS notification_status,
       COUNT(*) AS critical_count
FROM recognition_decisions
WHERE comparison_status = 'completed'
  AND disagreement_severity = 'critical'
GROUP BY COALESCE(notification_status, 'not_scheduled')
ORDER BY notification_status;
SQL
```

Interpret a growing pending age, repeated `failed` rows, or critical `scheduled`/`failed` delivery as an operational investigation signal. Keep investigation read-only until service logs, provider health, and the exact ownership/notification state are understood. Production rollout and controlled latency/notification verification must follow `docs/server-deployment.md` and the semantic-review plan; do not use live trading as a test fixture.

## 11. Operate Equivalent Entry-Leg Attribution Repair

Entry submission must preserve economic identity:

- One normalized entry price creates one entry leg with the full allocation.
- If two range legs collapse to the same exchange-normalized price and otherwise
  have the same execution identity, coalesce them before submission. The live
  submission boundary repeats this check for queued legacy drafts.
- TP/SL is auxiliary candidate-filtering evidence only. Equal, missing, stale,
  ambiguous, or post-entry-mutated protection cannot prove ownership.
- A strictly equivalent historical permutation may be mapped only by the repair
  planner's versioned, stable sorted canonicalization. Never choose randomly or
  depend on API/database input order.
- Ordinary reconciliation cannot create
  `equivalent_permutation_assignment`; without previously reviewed evidence it
  must leave the component unresolved and fail closed.

Every repair begins with a new dry run. Back up the production database, fetch
one fresh coherent exchange snapshot, and run without `--apply`:

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-position-attribution \
  --database-path data/research.db
```

Exact position-history evidence is intentionally paced at no more than one
request per 1.05 seconds, following the stricter Deepcoin endpoint limit. A
history-heavy dry run can therefore take roughly one second per candidate; do
not interrupt it merely because output is delayed. HTTP or schema errors still
block every action and must not be treated as empty history or retried with
`--apply`.

Review the snapshot fingerprint, every proposed action, every conflict, and the
absence of unrelated clears before separately authorizing `--apply`. Rebuild and
fingerprint the live/database evidence at apply time; drift must refuse the
operation. Never reuse a saved plan after a manual exchange action. Never edit
the production database directly or bypass the planner/fingerprint checks to
force an attribution, terminal state, manual state, or stale-record cleanup.

The dry-run JSON separates current attribution repairs in `actions` from
evidence-backed legacy cleanup in `historical_actions`. Historical cleanup may
clear a redundant old `pos_id`, terminalize an entry leg, close its exact
binding, exit its execution-backed lifecycle, or install the partial unique
ownership index. Review every old/new value and its terminal evidence. Confirm
that none of `live_position_ids` appears in `historical_actions` and that no
pending regular, trigger, or position-linked TPSL order is being treated as
history.

An exact Deepcoin position-history row may prove historical closure only when
its `posId`, instrument, side, and split-position mode match the candidate, its
original `pos` is positive, and `closePos` equals that full original size using
exact decimal comparison. Partial closure, missing or malformed sizes,
mismatched identity fields, conflicting duplicate rows, unavailable history,
and any current live or pending identity remain blocking. They must appear in
`unresolved_conflicts`; do not choose a convenient row or infer closure from
position absence.

For a stale leg competing for a duplicated historical `pos_id`, the planner may
look up that leg's exact entry `order_id` as a historical position identifier.
Such an order-derived ID is audit-only terminal evidence: it may support the
reviewed `clear_redundant_historical_position` and terminalization actions, but
must not create a current ownership assignment, populate a new live `pos_id`,
or appear in `actions`. Review the evidence's historical identifier against the
leg before accepting the cleanup.

`unresolved_conflicts` is an apply blocker. A missing live position is not
terminal evidence; an `entered` lifecycle remains unchanged unless an exact
completed close reservation, successful close event, terminal lifecycle, or
matching exchange cancellation proves the transition. Research-only
lifecycles without an execution binding are outside this repair workflow.

A nonempty plan of either action type requires the exact
`--expected-fingerprint` from the reviewed dry run. Zero actions authorizes no
database mutation. After an approved apply, verify that no duplicate ownership
groups remain and that the database constraint exists:

```bash
sqlite3 -readonly data/research.db <<'SQL'
SELECT venue, pos_id, COUNT(*) AS owner_count
FROM execution_order_legs
WHERE pos_id IS NOT NULL AND pos_id != ''
GROUP BY venue, pos_id
HAVING COUNT(*) > 1;

SELECT name, sql
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_execution_order_legs_venue_pos';
SQL
```

The first query must return no rows and the second must return exactly the
partial unique index. Also recheck live positions, Web holding counts, service
health, and the global automatic-trading switch independently.

The operator manually closed the two unattributed positions suspected to belong
to Miya on 2026-07-15, before this change was pushed or deployed. Therefore all
older Miya repair output and fingerprints are void. After deployment:

1. Pull current Deepcoin positions, open TPSL orders, pending triggers, and
   relevant regular/trigger entry-order history.
2. Confirm the manually closed `posId` values are absent. An absent position
   must not receive verified ownership and must not be closed again.
3. Keeping the process read-only, audit residual TP/SL and trigger orders plus
   the associated execution legs, binding, and lifecycle. Do not infer
   ownership from protection, and do not mutate exchange or database state.
4. Generate a fresh dry run from the same coherent snapshot. Any proposed
   terminal/manual transition or stale leg/binding/lifecycle cleanup must appear
   as an explicit planner action covered by that fingerprint.
5. Review every nonzero action separately before `--apply`. Copy the fingerprint
   from that exact dry run and pass
   `--expected-fingerprint <fingerprint-from-reviewed-dry-run>`; the rebuilt
   current plan must match it exactly. Zero actions is a valid result and
   authorizes no modification. If the planner cannot express a needed state
   transition, stop and change/review the planner rather than editing the
   database around it.

These are deployment-time audit requirements, not a claim that production has
already been checked or repaired.

## 12. Record External Manual Position Closure

Closing a position directly in Deepcoin does not, by itself, authorize the
service to guess which strategy ended. Exchange position absence is not
position-attribution evidence.

After an operator has independently verified the exact strategy affected by an
external close, use that strategy's Web `manual-close` action (or another
reviewed entry point that calls the same audited transition). The transition
must atomically mark the lifecycle `exited/manual`, the binding
`closed/manual_closed_by_user`, and every entry leg `manually_closed` with a
manual terminal reason. Later reconciliation must preserve those terminal
states and must not attach an old filled order to a same-symbol position.

Every accepted manual close requires exactly one Deepcoin execution binding
for the lifecycle's chat, message, symbol, and side. This requirement also
applies to a lifecycle currently marked `entered`; never exit the lifecycle by
itself when the binding is absent or ambiguous.

The same action accepts a legacy lifecycle that reconciliation previously
demoted from `entered` to `pending_entry` only when `entered_at` is still set
and the lifecycle resolves to an execution binding. This is compatibility for
an already-entered damaged record, not permission to close a never-entered
pending strategy. A `pending_entry` row without `entered_at` or without a
resolvable binding must be rejected without changing lifecycle, binding, or
entry-leg state.

Do not use a missing-position snapshot to perform this transition
automatically. If strategy identity is uncertain, keep the investigation
read-only and route the case through the reviewed repair/fingerprint workflow.
