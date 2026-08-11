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

## Exact backup-stop repair

Second stops are conditional market-close orders bound to one split-position
`closePosId`. The default distance is 20 bps beyond the verified primary stop,
rounded conservatively to the contract tick. A primary-stop `203` / insufficient
margin failure is never retried automatically; an active exact second stop stays
in place and strategy management remains frozen for operator recovery.

Always start with the read-only plan:

```bash
telegram-kol-research repair-backup-stops --database-path data/research.db
```

Review every proposed `pos_id`, primary order, liquidation boundary, second-stop
price, and the full fingerprint. Apply only one reviewed action:

```bash
telegram-kol-research repair-backup-stops \
  --database-path data/research.db \
  --pos-id <reviewed-pos-id> \
  --apply \
  --expected-fingerprint <fingerprint-from-dry-run>
```

The fingerprint becomes invalid when the local ownership evidence, live position,
pending orders, contract specification, or candidate set changes. `--apply`
without both `--pos-id` and `--expected-fingerprint` exits before any exchange
write. Do not apply all positions in one invocation. An unknown exchange result,
conflicting position ID, failed primary stop, or similar unowned conditional
order is an operator stop condition, not permission to retry or infer ownership.

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

### Trigger-entry protection recovery rollout

The strategy detail's `触发单保护恢复` section is an audit projection only. It
shows the parent trigger order ID, exact verified `pos_id`, recovery state and
attempt count, adopted TPSL order IDs, a bounded refusal code, and stop-rescue
state. It intentionally never renders Telegram message text, credentials, or
raw request/response payloads.

Use the rollout in this strict order:

The following invariants are mandatory throughout the rollout:

- contextual full exit requires one exact current-risk target; the internal
  low-confidence exception is valid only at confidence `>= 0.60` and cannot be
  forged by model output;
- full exit covers both live positions and every deferred entry leg;
- protection created before entry fill remains unowned and is never adopted by
  a symbol/side/time fallback;
- complete live-position/no-stop evidence may advance only to an exact stop-only
  rescue plan;
- rescue defaults disabled, and live mode requires separate approval plus live
  automatic-trading and management gates;
- close/cancel ownership wins over primary SL, backup SL, and TP writes;
- unresolved critical unprotected exposure blocks only new entries in that
  exact chat, never close, cancel, rescue, or another chat's entries.

1. Perform a read-only audit first. Inspect the exact trigger-entry leg,
   parent trigger order ID, and exact `pos_id` with a fresh exchange snapshot.
   A missing, ambiguous, or stale observation is a refusal; do not turn it
   into a write by guessing from symbol, side, price, group, or time.
2. Enable strict TPSL adoption only after the audit result identifies exactly
   one post-baseline TPSL order for that exact verified position and its
   displayed fingerprint has been reviewed. Confirm the adopted TPSL ID in the
   strategy detail before considering the lifecycle recovered.
3. Enable stop rescue only with one separate, tiny guarded live probe: one
   newly created trigger entry, one exact position, and one stop-only rescue
   candidate. Review the planned rescue and its refusal code first; stop after
   that single probe and verify the resulting ledger/order ID before widening
   scope.

Before enabling that probe, verify the strategy dashboard exposes the exact
binding, entry leg, `pos_id`, planned stop, immutable exposure start, and rescue
state. The runtime scanner rule `active_position_missing_protection_v1` is
shadow-only and must be explicitly allowlisted. A stale or terminal observation
must not block entries; the live gate derives its decision from current exact
binding/leg/protection state.

Never automatically cancel a legacy opaque take-profit order. An opaque TP is
a blocking condition for stop rescue, not evidence that it belongs to the
current strategy. Do not cancel, replace, or infer ownership of it from a
symbol/side/time match. Keep the case read-only until an operator has exact
ownership evidence and has separately approved any manual action.

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

Monitor alerts use deterministic Chinese templates; they are produced by the
system timer, not an AI model and not the Runtime Incident AI Agent. Every
message states what happened, the known or unknown trading impact, what the
operator should do or avoid, the notification source, and a final bounded
diagnostic section. Internal reason codes are diagnostic data, not the title.
The source line is fixed as `系统定时安全检查，不是 AI Agent`.

Operator-facing severity is fixed:

- `🔴 立即处理` covers service/settings drift, unknown or recovery-required
  exchange events, duplicate exact closes, adapter failures, malformed
  snapshots, and incomplete audit evidence. An unchanged critical fingerprint
  repeats after six hours.
- `🟡 稍后核查` covers actionable historical management residue, recent journal
  errors while the service remains active, and monitor-state integrity. Stable
  audit residue sends once and remains log-only until a meaningful fact changes.
- `🔵 状态提醒` is sent once after every previously delivered active cause has
  been rechecked and cleared. An audit cause can recover only after a new,
  complete, healthy management audit; a run that skips the audit cannot claim
  recovery.

The management-audit explanation includes the actionable state counts and at
most 10 exact management batch IDs. If more exist, the message states both the
total and that only the first ten are displayed. It never includes raw errors,
messages, order requests/responses, position identifiers, or credentials.
也就是说：紧急问题最多每六小时提醒一次；审计通知最多 10 个批次；
只有新的完整、健康的管理审计才能宣布审计问题已恢复。

Valid HEAD drift remains deployment context, not a standalone safety failure.
The monitor may record current and expected versions internally, but unrelated
version numbers are omitted from the operator message and the version number
does not participate in an `audit_abnormal` fingerprint. A normal code deployment
therefore cannot retrigger an unchanged historical audit notification.

If the monitor reports `authoritative_processor_required`, raw Telegram intake
may remain running when its health checks are otherwise normal, but automatic
message interpretation has stopped. Do not enable a legacy recognizer. Inspect
the production application construction and deployed commit, repair the
authoritative processor wiring, and redeploy only in a proven safe window.
Never automatically replay messages older than the observed gap; preserve them
as raw evidence for an explicit, separately reviewed recovery decision.

The management audit reports informational keep-holding history separately as
`counts.informational_noop`. It also reports completed fail-closed history as
`counts.terminal_blocked` when the batch has `completed_at` and no leg retains
`reserved`, `submitted`, `submit_unknown`, `partial`, `inconsistent`,
`partial_failed`, or `recovery_required`. Both categories remain visible but do
not contribute to `audit_abnormal_count`. `counts.blocked` therefore means an
incomplete or actionable blocked batch. `partial_failed`, `recovery_required`,
and `submit_unknown` remain abnormal regardless of age. New `hold_update`
recognition is skipped before a Deepcoin client or management batch is created.

### Recover paused management history

Handle exactly one reviewed batch at a time. The default invocation loads a
complete read-only Deepcoin reconciliation snapshot and writes neither the
exchange nor the database:

```bash
.venv/bin/telegram-kol-research recover-management-history \
  --database-path data/research.db \
  --batch-id <exact-batch-id>
```

Review `decision`, `reason_code`, the redacted exact-position references, and
`evidence_fingerprint`. A refusal is terminal for that attempt: do not infer
identity from symbol, side, price, size, or timing. Apply only in a newly proven
safe window and pass the unchanged fingerprint from the dry run:

```bash
.venv/bin/telegram-kol-research recover-management-history \
  --database-path data/research.db \
  --batch-id <exact-batch-id> \
  --apply \
  --evidence-fingerprint <reviewed-fingerprint>
```

The apply recomputes the current read-only exchange decision and compare-and-
sets the durable source row. Any exchange or database evidence change refuses
the write. A successful apply updates only the selected management batch and
legs and appends one `management_history_recovery` execution event. It never
submits, cancels, or adjusts an exchange order. Immediately rerun the management
audit, confirm there is no new exchange submission event, and verify all live
trading settings are unchanged.

For a position that was first partially closed and later fully closed, Deepcoin
position history reports the lifetime original and closed sizes rather than the
last batch delta. Recovery accepts that history only when every management close
leg for the exact binding, entry leg, and `posId` forms one gap-free size chain:
the first preflight equals the lifetime position size, each following preflight
equals the prior remainder, every prior leg is confirmed/succeeded, the reviewed
current leg has its exact successful order response, and the chain ends at zero.
Missing, duplicate, non-terminal, non-finite, or discontinuous evidence remains
a refusal.

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
notification-bot token, chat ID, and optional timeout; never put a Deepcoin,
Telegram-session, database, or application credential in this file:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/telegram-kol-monitor.credentials
sudoedit /etc/telegram-kol-monitor.credentials
```

```text
TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN=<notification-bot-token>
TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID=<notification-chat-id>
TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS=10
```

The application uses two separate Bot roles. Keep
`TELEGRAM_KOL_SYSTEM_BOT_TOKEN` and `TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID` for the
existing decision Bot: it alone receives pending-entry expiry reviews and their
interactive **continue waiting / cancel / keep** buttons. Configure
`TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN` with the informational third Bot and set
`TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID` to the same chat ID when both Bots
deliver to one operator group. AI-recognition, position-attribution,
strategy-management, instruction-summary, and production-monitor notices use
the notification Bot. Never commit either token.

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
approved_entry_preamble_mode=disabled # replace with the currently approved value
sudo ./scripts/install_server_monitor.sh \
  --expected-entry-preamble-mode "$approved_entry_preamble_mode"
if systemctl is-enabled --quiet telegram-kol-monitor.timer || \
   systemctl is-active --quiet telegram-kol-monitor.timer; then
  echo "unexpected enabled or active monitor timer" >&2
  exit 1
fi
```

The installer refuses any path other than the validated
`/opt/telegram-kol-analyzer` Git root. It freezes that checkout's current
`git rev-parse HEAD` with only the allowlisted operator-bot fields in the
root-owned `0600` `/etc/telegram-kol-monitor.env`. The mode argument is required
so a routine reinstall cannot silently reset an approved live mode. Rerun the
installer after each later reviewed deployment to advance that expected-commit
baseline. Before enabling,
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
sudo ./scripts/install_server_monitor.sh --enable \
  --expected-entry-preamble-mode "$approved_entry_preamble_mode"
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

## Deepcoin dynamic contract specifications

In `live` contract-spec mode, Deepcoin new-entry eligibility is this
intersection:

```text
global allowed_symbols
∩ Deepcoin live *-USDT-SWAP instruments
∩ fresh, fully validated contract specifications
```

The global list is venue-independent. Saving `ABC` globally is valid even if
Deepcoin does not support `ABC-USDT-SWAP`; it remains saved and visible. In
`live` mode it is not traded on Deepcoin. A later validated Deepcoin listing can
make it eligible without a code change. Conversely, a suspended/delisted
instrument immediately stops live-mode new entries. Static and shadow are
deliberate rollout stages: their reviewed YAML remains execution authority, so
dynamic capability is observation rather than an enforcement gate until live
promotion. Existing-position risk reduction uses its separately proven frozen
specification and is not blocked solely by a later listing change.

The authoritative source is Deepcoin's public product-information request
`GET https://api.deepcoin.com/deepcoin/market/instruments?instType=SWAP`
([official documentation](https://www.deepcoin.com/docs/zh/DeepCoinMarket/getBaseInfo)).
The candidate snapshot must contain valid `instType`, `instId`, `ctVal`,
`lotSz`, `minSz`, `tickSz`, and `state` values for every accepted row. The
application accepts only exact USDT perpetual IDs and only `state=live` for new
entries; malformed, duplicate, ambiguous, non-positive, or incompatible values
reject the whole refresh.

The process defaults are:

```text
cache: data/deepcoin_contract_specs_cache.json
TTL: 24 hours
mode: static
```

The cache is generated data. Never hand-edit it. A refresh writes a restrictive
same-directory temporary file, reloads and validates it, then atomically
replaces the cache. Failed fetching, validation, writing, or reloading returns
nonzero and does not replace a valid cache. The static YAML at
`config/deepcoin_contract_specs.yaml` is reviewed bootstrap/comparison data; it
is not an unlimited fallback that may authorize a missing or stale symbol in
live mode.

### Read-only status and explicit refresh

Run from the checkout with the same explicit path and TTL passed to the Web
process:

```bash
.venv/bin/telegram-kol-research deepcoin-contract-specs status \
  --cache-path data/deepcoin_contract_specs_cache.json

.venv/bin/telegram-kol-research deepcoin-contract-specs refresh \
  --cache-path data/deepcoin_contract_specs_cache.json \
  --ttl-hours 24
```

`status` performs no network request and does not create the cache or its parent
directory. Its bounded JSON state means:

- `fresh`: digest, timestamps, and counts passed complete validation and have
  not expired.
- `missing`: the configured cache file does not exist.
- `stale`: the file validated but its expiry boundary has been reached.
- `invalid`: JSON, schema, timing, digest, or instrument validation failed.
- `unreadable`: the file exists but the process cannot read it.

`refresh` is the explicit operator command that fetches and publishes. The Web
process also uses the same safe refresh mechanism at startup, at a bounded
half-TTL cadence, and before symbol-settings reads/saves. Those automatic
refreshes run even in static/shadow for observation, but do not change execution
authority. Explicit refresh success prints only the SHA-256 digest, UTC
timestamps, counts, cache path, and state; it never prints raw product rows,
credentials, signed headers, or exception bodies. Failure prints a small
categorical result and exits nonzero. If `cache_preserved=true`, the prior fresh
validated cache remains authoritative for its remaining TTL; a failed refresh
never extends that TTL.

The Web symbol endpoint exposes dynamic per-symbol `reason_code` values:
`tradable`, `global_not_allowed`, `venue_instrument_unsupported`,
`venue_instrument_not_live`, `contract_spec_stale`, `contract_spec_invalid`, or
`contract_spec_sync_unavailable`. It separately exposes
`execution_reason_code` and `execution_tradable` for the current rollout mode.
In static/shadow, dynamic `reason_code` is observational and the static YAML may
still produce `execution_reason_code=tradable`; in live, the fields converge and
only dynamic `tradable` can admit a new Deepcoin entry. Unsupported global
symbols remain saved; they are not traded once live dynamic authority is active.

### Static, shadow, live, and rollback

Keep `deepcoin_contract_specs_mode=static` for dormant deployment. In `static`,
the reviewed YAML stays authoritative. After an explicit successful server
refresh and a proven safe window, change only the mode to `shadow`; shadow
refreshes and compares the authoritative cache while execution still uses the
static YAML. Review at least two fresh independent observations, all BTC/ETH
differences, all globally allowed symbols, and zero unknown states before
requesting separate approval for live.

Preserve the full settings payload when changing the mode:

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > /tmp/trading-settings.contract-specs.before.json
jq '.deepcoin_contract_specs_mode="shadow"' \
  /tmp/trading-settings.contract-specs.before.json \
  > /tmp/trading-settings.contract-specs.shadow.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.contract-specs.shadow.json \
  http://127.0.0.1:8000/api/trading-settings
```

`live` requires a separate explicit approval after reviewed shadow evidence.
Repeat the same full-payload procedure with `"live"`. In live mode, only a
fresh authoritative cached specification may authorize a future new entry;
there is no static fallback, no allowlist expansion, and no historical replay.

Rollback is a settings-only authority change. The pre-activation payload is an
audit/diff artifact, not a safe write payload: other settings may have changed
since it was captured. Fetch current settings immediately, alter only the mode
to `static`, POST the full current payload, read it back, and confirm `static`.
Do not delete the cache, edit the YAML, replay a Telegram message, or modify an
existing binding as part of rollback:

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > /tmp/trading-settings.contract-specs.current.json
jq '.deepcoin_contract_specs_mode="static"' \
  /tmp/trading-settings.contract-specs.current.json \
  > /tmp/trading-settings.contract-specs.rollback.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.contract-specs.rollback.json \
  http://127.0.0.1:8000/api/trading-settings
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  | jq -e '.deepcoin_contract_specs_mode == "static"'
```

## Entry preamble sizing context

`entry_preamble_mode` defaults to `disabled`. In this mode an earlier message
such as `BTC 多单半仓操作` is neither persisted nor applied. `shadow` persists
authoritative normalized evidence and reports the proposed multiplier, but the
effective multiplier remains `1`. `live` applies the multiplier to every group
already configured for automatic trading; there is no separate chat allowlist.
The production safety monitor independently checks this value against
`TELEGRAM_KOL_MONITOR_EXPECTED_ENTRY_PREAMBLE_MODE`. Install it with the same
approved mode via `--expected-entry-preamble-mode`; any mismatch alerts as
`entry_preamble_mode_drift`.

`半仓操作` means `configured risk budget × 50%`; it does not mean half of
the account balance, half of the order quantity, or half leverage. Sizing must
show both the configured and effective risk budgets and the two Telegram
message IDs. Raw prompts, provider responses, and credentials must not appear
in operator output.

Before live activation, update `trading.analysis.shared` through the Web
prompt center using the normal `draft -> validate -> historical test ->
publish` workflow. The application seed only creates a missing definition and
never overwrites the current published production prompt. Both MiMo and
DeepSeek historical comparisons must cover the current active prompt versions
before publication is allowed.

Use the read-only replay with database row IDs, not Telegram message IDs:

```bash
python scripts/replay_entry_preamble_shadow.py \
  --database-path /path/to/research.db \
  --preamble-raw-message-id 9334 \
  --strategy-raw-message-id 9335 \
  --configured-risk-usdt 20
```

The script opens SQLite with `mode=ro&immutable=1` plus `query_only` and cannot
submit exchange requests. Promotion and rollback criteria are in
`docs/entry-preamble-live-verification.md`.
  Reconcile the current exchange snapshot first and wait for a new instruction
  or an explicitly reviewed operator action.

For a protection-recovery full-exit audit, use this read-only query. A
`protection_recovery_bypass` marker proves only that the exact full-exit was
admitted; `succeeded` still requires the later exchange reconciliation result.

```bash
sqlite3 -readonly data/research.db <<'SQL'
.headers on
.mode column
SELECT b.id AS batch_id,
       b.status AS batch_status,
       b.reason_code,
       l.pos_id,
       l.status AS leg_status,
       l.exchange_order_id,
       sl.lifecycle_status,
       sl.exit_reason
FROM strategy_management_batches AS b
JOIN strategy_management_legs AS l ON l.management_batch_id = b.id
JOIN strategy_lifecycles AS sl ON sl.id = b.target_lifecycle_id
WHERE json_extract(b.target_snapshot_json, '$.protection_recovery_bypass.version') = 1
ORDER BY b.id DESC, l.leg_index ASC
LIMIT 100;
SQL
```

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

For image-only position management, treat only the source message text and the
current MiMo `input_reading.observed_text` as executable wording. Never use a
model `reason` field to infer a close, partial close, or stop change. Structured
`exit_full` remains authoritative even when the explanation mentions `成本价`,
but it must still pass the exact lifecycle, verified `posId`, fresh Deepcoin
snapshot, and execution-idempotency gates. If any ownership evidence is missing
or conflicting, stop at the existing safe refusal; never select another
position by symbol and side.

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

## 11a. Audit Trigger-Entry Protection-Ledger Adoption

Use this server-only, read-only audit to inspect the historical TPSL protection
ownership for one reviewed trigger-entry binding and exact position. It reads
the current Deepcoin pending-TPSL snapshot and prints a dry-run plan; it does
not submit, cancel, modify, or otherwise mutate an exchange order.

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-entry-protection-ledger \
  --database-path data/research.db \
  --binding-id 158 \
  --pos-id 1001124227079271 \
  --include-trigger-entries
```

Treat the printed plan as an audit artifact. A safe outcome is either zero
actions, or only the precisely reviewed ledger action(s) that map the verified
trigger-entry leg's exact `posId` to an exact current TPSL `ordId`; explicit
refusals for missing, conflicting, partial-size, or non-unique evidence are
safe fail-closed outcomes. Do not infer ownership from symbol, side, price,
group, message, or proximity, and do not treat a refusal as proof that no
protection exists.

An application is permitted only after the immediately preceding dry run shows
precisely the action set the operator reviewed. Copy that dry run's fingerprint
verbatim and use it explicitly:

```bash
.venv/bin/telegram-kol-research repair-entry-protection-ledger \
  --database-path data/research.db \
  --binding-id 158 \
  --pos-id 1001124227079271 \
  --include-trigger-entries \
  --apply \
  --expected-fingerprint <fingerprint-from-reviewed-dry-run>
```

Never apply a historical entry-protection repair after an operator manually
changes TP/SL, cancels protection, or otherwise changes protection on the
exchange without first taking a fresh snapshot and obtaining a new operator
review. The old dry-run output and fingerprint are then void. If the rebuilt
plan differs, contains an unexpected action, or contains an unresolved refusal,
stop and investigate read-only; do not edit the production database directly.

## 11b. Audit Historical Protection-Incident Convergence

The first production run of this audit is observation-only. It sends no
notification, writes no business row, and has no `--apply` mode:

```bash
.venv/bin/telegram-kol-research audit-protection-incidents \
  --database-path data/research.db --limit 100 --output-format json
```

The command reads a private coherent SQLite snapshot and one coherent read-only
Deepcoin snapshot. It returns counts plus at most 100 redacted incident
references. Treat `output_complete=false` as an explicit refusal: truncation,
unstable database evidence, and incomplete exchange reads can never prove
resolution.

Review every `current_risk` item before considering any later incident selector
or notification change. `resolved_by_current_exchange_evidence` requires a
newer exact complete replacement whose primary stop, backup stop, and take
profit are all visible for the same binding, entry leg, and `posId`.
`historical_terminal` is informational history; `evidence_insufficient` remains
unresolved. Never convert this audit output directly into an exchange write,
database edit, Telegram delivery, or replay of the original management message.

## 12. Record External Manual Position Closure

### Terminal lifecycle / entry-order invariant

A lifecycle must not become terminal while its exact execution binding still
has an unfilled entry leg. Before a full close, and when reconciliation proves
the primary position is gone, the service cancels each exact bound pending
regular or trigger entry and confirms its absence on a fresh Deepcoin
read-back. `resolved` means cancellation and absence were confirmed;
`already_absent` means the local leg existed but the exact exchange order was
already absent. `blocked` or `unknown` preserves the lifecycle as nonterminal
with `management_action=terminal_cleanup_required`; it never guesses that the
order disappeared.

The same reconciliation cycle repairs bounded historical
`terminal lifecycle + nonterminal entry leg` anomalies. It uses the exact
binding and order/client IDs only. It must never select an order by symbol,
side, price, time, or proximity. Cleanup results are stored as
`execution_events.action=terminal_entry_cleanup_outcome` and delivered through
the KOL event-processing bot. Notification retry reads only this durable
outbox; it never repeats the exchange cancellation.

This invariant is always active. It has no shadow path and no runtime feature
switch. A failure must be fixed at the identity/read-back boundary rather than
hidden by disabling the invariant.

On the production server, this query is read-only and should return zero rows:

```bash
sqlite3 -readonly data/research.db <<'SQL'
SELECT
  l.id AS lifecycle_id,
  l.lifecycle_status,
  b.id AS binding_id,
  e.id AS entry_leg_id,
  e.order_id,
  e.client_order_id,
  e.status AS entry_status
FROM strategy_lifecycles AS l
JOIN execution_bindings AS b
  ON b.id = l.execution_binding_id
JOIN execution_order_legs AS e
  ON e.execution_binding_id = b.id
WHERE l.lifecycle_status IN ('exited','expired','cancelled','invalidated')
  AND e.purpose = 'entry'
  AND e.pos_id IS NULL
  AND lower(COALESCE(e.status, '')) IN (
    'pending','open','submitted','partially_filled','partial'
  )
ORDER BY l.id, e.id;
SQL
```

For notification health, inspect only bounded status metadata:

```bash
sqlite3 -readonly data/research.db <<'SQL'
SELECT id, status, notification_status, notification_attempts,
       notification_next_attempt_at, notified_at
FROM execution_events
WHERE action = 'terminal_entry_cleanup_outcome'
ORDER BY id DESC
LIMIT 20;
SQL
```

`delivered` is final. `failed` is eligible for a bounded delayed retry;
`exhausted` means five delivery attempts failed and needs operator
investigation. Neither state authorizes another Deepcoin write.

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

A missing-position snapshot alone does not authorize this transition. Automatic
reconciliation may proceed only through the lifecycle's exact execution binding
and only after deferred-entry cleanup is confirmed. If strategy identity is
uncertain, keep the investigation read-only and route the case through the
reviewed repair/fingerprint workflow.

To roll back the code, create and review a Git revert of the terminal-entry
cleanup commits, push it, and deploy through the normal server update helper.
Do not drop the new nullable event columns or delete outbox history. Most
importantly, never recreate an entry order that the repaired version already
confirmed cancelled or absent; exchange cancellation is a terminal fact.

## 13. Contextual strategy resolution

Keep contextual resolution disabled unless a reviewed rollout explicitly
enables one chat:

```json
{
  "context_resolution_enabled": false,
  "context_resolution_live_chat_ids": []
}
```

This path never uses shadow mode. Before enabling it, run the redacted replay
tests and inspect the message/strategy evidence view:

```bash
uv run --frozen pytest tests/test_context_resolution_replay.py -q
uv run --frozen telegram-kol-research resolve-context-once \
  --database-path data/research.db
```

The one-shot command does not configure an exchange writer. For unresolved
items, inspect the strategy thread root, reply chain, evidence version,
decision confidence, supporting/opposing message IDs, and next trigger. Never
copy raw model responses, credentials, private media, or full exchange JSON
into an incident report. Full behavior and failure handling are documented in
[`contextual-strategy-resolution.md`](contextual-strategy-resolution.md).

## 14. Backfill historical MiMo evidence

Keep contextual live resolution disabled throughout this procedure. The
backfill persists first-pass text/image evidence only; it must not apply a
recognition result, run DeepSeek, create strategy instructions, or call
Deepcoin.

Start with a bounded dry run for an explicit chat:

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 25
```

Review `considered`, `planned`, all skip counts, and the bounded message IDs.
Then run the same immutable scope with a conservative rate:

```bash
.venv/bin/telegram-kol-research backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 25 \
  --delay-seconds 2 \
  --apply
```

Both model calls and source scanning are bounded. `--limit` caps MiMo calls;
`--scan-limit` (default `1000`) caps source rows and media fingerprints examined
by one invocation. If output includes a non-null `next_scan_cursor`, continue
the next page with that exact opaque value:

```bash
.venv/bin/telegram-kol-research backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 25 \
  --scan-limit 1000 \
  --scan-cursor <next_scan_cursor>
```

Use the same cursor when adding `--apply`; do not mix output from a different
chat/time scope.

The cursor is a chronological `(posted_at, message_id, database id)` keyset,
not a row offset. A historical row inserted behind the current cursor is picked
up by the next full sweep starting without `--scan-cursor`; it cannot shift the
remaining page or make an already-existing later row disappear.

After every batch:

1. Confirm `telegram-kol.service` is still active.
2. Confirm matching `message_evidence_versions` contain separate
   `text_evidence_json` and `image_evidence_json`.
3. Confirm no new strategy thread, management instruction, execution binding,
   order leg, or exchange mutation was caused by the batch.
4. Review failed/image-unavailable rows before retrying.
5. Re-run the command; matching completed fingerprints must become
   `skip_completed`.

Use `--start-at` and `--end-at` with ISO-8601 timestamps to fix a historical
window. `--use-configured-context-chats` reads the saved chat list even while
the live boolean is disabled, but an empty list still fails closed. Never use a
whole-database implicit scope.

Failures are durable and skipped on ordinary resume. Use `--retry-failed` only
when the cause was reviewed as transient (for example a temporary model API
failure or repaired media path). One command provides only one batch-level
retry opportunity per failed message; MiMo's internal request retry remains
bounded. Active evidence claims suppress duplicate MiMo calls across live and
backfill workers. If the source message changes during inference, the batch
returns `message_input_changed` and deliberately saves no stale evidence.
Operator output contains stable error codes only; inspect protected service
logs for details.

## 15. Audit account-wide TPSL ownership

Keep automatic trading frozen. This server-only command reads current
positions and pending TPSL rows from Deepcoin and opens the local database in
SQLite read-only mode:

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research audit-tpsl-ownership \
  --database-path data/research.db \
  --output-json
```

Review `live_position_count`, `pending_tpsl_count`,
`owned_pending_count`, `unowned_pending_order_ids`, `conflicts`, and
`stale_ledger_order_ids`. Every pending TPSL must be classified exactly once
as owned, unowned, or conflicting. Price, size, direction, and creation time
do not establish ownership.

The runtime read and mutation sequence is fixed:

1. Read the complete account position and pending-TPSL snapshots.
2. Load all verified `position_protection_ledger` rows for the account.
3. Join pending rows by exact `ordId → posId`; validate, but never derive,
   ownership from an exchange `posId`.
4. Display, audit, plan, cancel, or replace only canonical owned rows.
5. Refuse stale, missing, unowned, conflicting, or incomplete evidence before
   producing any exchange write.

`position_backup_stop_orders` and `position_take_profit_orders` describe
workflow state only. They are not ownership authorities. Stable refusal codes
include `no_existing_position_tpsl_to_adjust`,
`protection_missing_cancellable_order_id`,
`protection_price_or_size_mismatch`,
`protection_ambiguous_global_assignment`, and
`target_protection_evidence_unavailable`.

`exchange_write_count` must be `0`. Any nonzero value invalidates the audit
and requires immediate investigation. This command never submits, adjusts, or
cancels an order and never creates or updates the database.

### Canonical TPSL ledger backfill

After reviewing the ownership audit, generate a database-only dry run:

```bash
.venv/bin/telegram-kol-research backfill-canonical-tpsl-ledger \
  --database-path data/research.db
```

Review every exact `order_id`, `pos_id`, source row, refusal, and the complete
fingerprint. Price, size, direction, and time never select a position. Apply
only when the immediately preceding dry run has precisely the reviewed action
set and zero refusals:

```bash
.venv/bin/telegram-kol-research backfill-canonical-tpsl-ledger \
  --database-path data/research.db \
  --apply \
  --expected-fingerprint <reviewed-fingerprint> \
  --confirmation-token <single-use-token>
```

The apply writes only `position_protection_ledger` and the single-use
confirmation record in one database transaction. It never calls a Deepcoin
write API. Rerun `audit-tpsl-ownership` immediately afterward and require
`exchange_write_count=0`.

Historical repair is supervised and dry-run first. Symbol, side, price,
quantity, creation time, or `sz=0` may be shown to an operator as supporting
context, but an apply is allowed only for the separately reviewed exact
`ordId → posId` actions in the fingerprinted plan. Runtime code must never
reuse those review fields as automatic attribution rules.
## 第一止盈后自动成本保护

自动成本保护由 `strategy_break_even_convergences` 和
`strategy_break_even_convergence_legs` 持久化。只有交易所精确订单终态或完整仓位差额证据能证明
TP1 成交；新的反向策略消息本身不能结束旧策略，也不能授权平仓。

排查顺序：

1. 查询收敛任务的 `trigger_evidence_json`、`target_snapshot_json`、`status` 和
   `reason_code`，确认触发订单及策略身份唯一。
2. 查询逐腿的 `pos_id`、`preflight_size`、`avg_entry_price` 和 `decision_json`。
3. 核对冻结目标里的未成交入场腿已经撤销并取得终态回读；结果未知时只能继续只读回查。
4. 核对已成交 TP1 没有重新出现，剩余止盈总量不超过当前持仓量。
5. 对 `set_break_even` 核对新止损已按相同 `pos_id` 和实际均价出现在待执行 TPSL；
   对 `full_exit` 核对精确 `closePosId` 已经归零；对 `keep_tighter_stop` 核对系统没有写单。
6. 查询对应 `position_mutation_intents`。`submitting`、`recovery_required` 或未知结果禁止人工
   重跑提交；先用交易所仓位、待执行 TPSL、订单历史和成交记录完成只读对账。

关闭或回滚：把交易设置中的 `move_stop_to_breakeven_after_tp1` 关闭，或把管理执行模式切到
`disabled`。上线前先使用 `shadow`：它会保存行情和逐腿决策，但不会撤单、改止损或平仓。
遇到 `automatic_break_even_*` 保护异常告警时，按告警中的任务 ID 和 `posId` 核对；在所有未知
mutation 得到确定终态前不得切回 live。

## 复合仓位管理 v2

`止盈50% + 止损移到开仓价`是一份不可丢失子句的合同，按顺序持久化为
`consume_take_profit_stage` → `converge_partial_close` →
`replace_remaining_protection`。只有所有必需组件都为 `confirmed` 且均有交易所回读证据，才能称为完成。

只读排查顺序：先核对原消息、MiMo 权威 payload 和不可变合同；再按 `sequence`
查询组件状态、期望值、证据和最后进展时间；然后依次核对第一止盈终态、精确平仓差额、剩余仓位、保留止盈总量、新主止损和备用止损。

```sql
SELECT b.id, b.status, b.reason_code, c.id, c.sequence,
       c.component_kind, c.status, c.reason_code,
       c.last_progress_at, c.evidence_json
FROM strategy_management_batches b
JOIN strategy_management_components c ON c.management_batch_id = b.id
WHERE b.id = :batch_id ORDER BY c.sequence, c.id;

SELECT id, operation, pos_id, status, idempotency_key, updated_at
FROM position_mutation_intents
WHERE idempotency_key LIKE :component_id || ':%' ORDER BY id;
```

稳定告警码为 `completed_batch_missing_component_evidence`、
`duplicate_composite_close_submission`、`live_position_retained_tp_oversized`、
`composite_position_without_verified_stop` 和 `stalled_composite_component`。
`submitting`、`submitted`、`recovery_required` 或未知结果一律禁止重新发单。
将模式切为 `disabled` 只会阻止新批次，不会把已发出的未知结果当作回滚。
米娅和三姐的历史消息永不自动重放；历史补操作必须单独只读规划、人工批准和交易所回读。

## 精确仓位管理活性 v2

`execution_order_legs.pos_id` 只有在账户级唯一、`verified`、具有权威持久化证据且未终态时，
才证明“这个精确仓位归谁管”。`position_protection_ledger` 另外证明“这个止损/止盈订单
归哪个 `posId`”。策略、币种、方向或价格相似不能替代任一证明。

| 操作 | 必需证据 | 不得放宽的条件 |
| --- | --- | --- |
| 撤销/替换保护单 | 权威精确仓位 + 该 order ID 已验证归属 | 未归属或归属冲突不可撤 |
| 新增精确备用止损 | 权威仓位、完整快照、已保存 SL、清算安全 | 已有/未知止损或 mutation 不可写 |
| 新增分段止盈 | 权威仓位、精确 owned stop、完整 TPSL 快照 | 未知/未归属 TP 或总量超仓不可写 |
| 精确减仓 | 权威仓位、完整快照、无 active/unknown mutation | 保留 TP 不安全时不可部分减仓 |
| 精确全平 | 权威仓位、完整快照、无 active/unknown mutation | 某条腿的止损归属待恢复不应全局阻断其他仓位 |

`position_management_liveness_v2_mode=disabled` 不产生新的 v2 交易所写；`shadow` 只计算并持久化
有界证据；`live` 还必须同时满足全局自动交易和 live 管理开关。先做只读规划：

```bash
.venv/bin/telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db --pos-id <exact-pos-id>
```

只有快照、账本、intent、合约规格和预计 payload 都未变时，才可用指纹执行：

```bash
.venv/bin/telegram-kol-research recover-position-management-liveness \
  --database-path data/research.db --pos-id <exact-pos-id> \
  --apply --expected-fingerprint <reviewed-fingerprint>
```

`recovery_disposition=retry` 只允许重做只读证据收集，`exact_backup` 只允许通过全部预检的
精确 SL fallback，`manual_review`/`terminal` 禁止自动操作。任何 `submit_unknown` 或
`recovery_required` 都必须冻结：查精确 client/order/posId、仓位、待执行 TPSL、历史与成交，
不得重发。在策略详情中查 `protection_states`、`backup_stops`、`protection_incidents`、
`trigger_protection_recovery` 和 `take_profit_orders.convergence`。

回滚时只把 v2 模式设为 `disabled`并停止新任务。不删除已确认的交易所订单、账本、intent、
convergence、incident 或 unknown-outcome 记录；它们是对账和避免重发的安全边界。

### 只读审计 SQL

```sql
-- 1. 已验证仓位不应同时缺少所有精确 owned stop。
SELECT l.id AS leg_id, l.pos_id FROM execution_order_legs l
WHERE l.venue='deepcoin' AND l.purpose='entry'
  AND l.attribution_status='verified' AND l.status IN ('active','partially_filled')
  AND NOT EXISTS (SELECT 1 FROM position_protection_ledger p
    WHERE p.execution_order_leg_id=l.id AND p.pos_id=l.pos_id
      AND p.purpose IN ('stop_loss','combined') AND p.status IN ('verified','active'))
  AND NOT EXISTS (SELECT 1 FROM position_backup_stop_orders b
    WHERE b.execution_order_leg_id=l.id AND b.pos_id=l.pos_id
      AND b.status='active' AND b.order_id IS NOT NULL);

-- 2. active TP 总量不得超过最新的精确仓位快照。
SELECT c.id, c.pos_id, SUM(CAST(t.size_text AS REAL)) AS tp_size,
       CAST(o.size_text AS REAL) AS position_size
FROM trigger_take_profit_convergences c
JOIN position_take_profit_orders t ON t.execution_order_leg_id=c.execution_order_leg_id
JOIN position_reconciliation_observations o ON o.pos_id=c.pos_id
WHERE c.status IN ('ready','reserved','submitted') AND t.status='active'
  AND o.id=(SELECT MAX(o2.id) FROM position_reconciliation_observations o2 WHERE o2.pos_id=c.pos_id)
GROUP BY c.id, c.pos_id HAVING tp_size > position_size;

-- 3. 一个 order ID 不得有多个 verified protection owner。
SELECT venue, order_id, COUNT(DISTINCT pos_id) AS owner_count
FROM position_protection_ledger WHERE status IN ('verified','active')
GROUP BY venue, order_id HAVING owner_count > 1;

-- 4. 超过 escalation deadline 的 recovery_required 必须已进入 runtime incident。
SELECT i.id, i.execution_order_leg_id, i.next_attempt_at
FROM trigger_protection_intents i
WHERE i.recovery_state='recovery_required' AND i.next_attempt_at <= CURRENT_TIMESTAMP
  AND NOT EXISTS (SELECT 1 FROM runtime_incidents r
    WHERE r.source_kind='position_protection_incident'
      AND r.source_record_id LIKE 'trigger-intent-' || i.id || '-%');

-- 5. 终态仓位不得保留 live convergence work。
SELECT c.id, c.pos_id, l.status AS leg_status, b.status AS binding_status
FROM trigger_take_profit_convergences c
JOIN execution_order_legs l ON l.id=c.execution_order_leg_id
JOIN execution_bindings b ON b.id=c.execution_binding_id
WHERE c.status IN ('ready','reserved')
  AND (l.status IN ('closed','cancelled','failed','rejected','expired')
       OR b.status IN ('closed','cancelled','failed','rejected','expired'));
```

## 相邻入场消息组装 v2

两个独立开关必须按顺序推进：`entry_message_assembly_v2_mode` 先于
`entry_revision_v2_mode`，且修订开关不得超过组装开关的阶段。发布顺序是：

1. 两者均为 `disabled` 时发布；
2. 运行服务器聚焦测试和只读历史回放；
3. 仅将组装设为 `shadow`，检查自然消息方案和安全不变式；
4. 组装设为 `live`，修订仍为 `disabled`；
5. 修订设为 `shadow`；
6. 评审自然影子证据后，仅开放未成交旧单的 live 修订；当前生产调用保持 `allow_partial_fill_live=false` 和 `allow_supplemental_live=false`；
7. 只有精确归属、连续止损保护和恢复路径均有自然影子证据，并且两个细分门禁经单独代码评审和发布后，才可开放部分成交与补仓动作。

历史回放仅读 SQLite，不导入交易所适配器：

```bash
.venv/bin/python scripts/replay_adjacent_entry_assembly.py \
  --database-path data/research.db --configured-risk-usdt 20 \
  --message-id 4154 --message-id 9902 --message-id 9936 \
  --message-id 558 --message-id 538
```

重启或切换模式前，必须用只读 SQL 证明没有正在持有 claim、正在写交易所或会在 worker 重启后自动继续的操作：

```sql
SELECT comparison_status, COUNT(*) FROM recognition_decisions
 WHERE comparison_status IN ('running','execution_running')
    OR comparison_status IS NULL
    OR comparison_status NOT IN
       ('completed','pending','running','execution_pending','execution_running')
 GROUP BY comparison_status;
SELECT extraction_status, COUNT(*) FROM message_evidence_versions
 WHERE superseded_at IS NULL
   AND extraction_status IN ('pending','processing','running')
 GROUP BY extraction_status;
SELECT extraction_status, COUNT(*) FROM message_evidence_versions
 WHERE superseded_at IS NULL AND (
   extraction_status IS NULL OR extraction_status NOT IN
   ('completed','failed','expired','pending','processing','running'))
 GROUP BY extraction_status;
SELECT status, COUNT(*) FROM execution_order_legs
 WHERE purpose='entry' AND status IN
 ('submitting','cancel_submitting') GROUP BY status;
SELECT status, COUNT(*) FROM strategy_revision_batches
 WHERE status IN ('planned','cancelling_old_entries','old_entries_terminal',
                  'submitting_replacements','rebuilding','reconciling',
                  'recovery_required') GROUP BY status;
SELECT status, COUNT(*) FROM strategy_management_batches
 WHERE status IN ('ready','executing','reserved','submitted','submit_unknown',
                  'reconciling','partial_failed','protection_ready',
                  'recovery_required')
 GROUP BY status;
SELECT status, COUNT(*) FROM position_mutation_intents
 WHERE status IN ('reserved','submitting','submitted','recovery_required')
 GROUP BY status;
```

任一查询返回行，或监听、对账、保护健康检查不正常，都延后重启和模式切换。
`execution_pending` 上的 `comparison_claim_token` 是同步执行代际标识，不是后台
worker claim；只有 `execution_running` 表示正在执行交易状态变更。
`pending`/`open` 入场单是可跨重启的稳定交易所状态。任何 `submit_unknown`、
`recovery_required` 或 `partial_failed` 默认阻断发布；只有同时证明该行属于不会被
新旧 worker 接纳的旧类型、对应开关发布前后均为 `disabled`，并完成交易所只读
读回且保留全部证据时，才允许仅重启发布。该例外绝不允许模式切换，也不得自动
重试、删除证据或把未知结果改写为成功。
通过窗口检查后，每次只改一个字段，保留完整设置 payload：

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings > /tmp/trading-settings.before.json
jq '.entry_message_assembly_v2_mode="shadow"' \
  /tmp/trading-settings.before.json > /tmp/trading-settings.next.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.next.json \
  http://127.0.0.1:8000/api/trading-settings
```

后续 `live`/`shadow` 切换使用同一流程，但每个阶段都要重做窗口和证据评审。
回滚时立即把两个模式设为 `disabled`，这会停止新接纳和自动 revision worker。现有 batch 改由运维使用只读查询与交易所读回对账至确定终态；不得删除证据、重发未知结果请求或宣称订单已改变。

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  | jq '.entry_message_assembly_v2_mode="disabled" | .entry_revision_v2_mode="disabled"' \
  > /tmp/trading-settings.rollback.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.rollback.json \
  http://127.0.0.1:8000/api/trading-settings
```

## 修复相邻入场组装指纹历史不一致

恢复触发入场路径已保证：在任何交易所提交之前，已持久化的
trade signal 顶层和订单草稿内层必须同时使用组装的最终指纹。
历史上已提交的绑定和订单快照不得改写；本命令只能为一个能够从
最终组装证据机械推导出的旧指纹追加一条审计事件。它不得重放
Telegram 消息、重发交易信号、调用 Deepcoin 变更接口，也不得修改已提交
的 binding、trade signal、订单或成交记录。

首先在服务器上仅执行 dry-run：

```bash
telegram-kol repair-entry-assembly-fingerprint \
  --database-path <production-db> \
  --assembly-id 2 \
  --execution-binding-id 266
```

输出只包含可审核的 ID、指纹和冲突码，不包含原始消息或完整 payload。
操作员必须确认：

- `action` 非空，且 `conflicts` 为空；
- assembly ID、execution binding ID 和 trade signal ID 与事故证据一致；
- `old_fingerprint` 是可从最终证据严格推导的最终化前指纹；
- `final_fingerprint` 等于当前组装指纹；
- `repair_fingerprint` 和顶层的 plan `fingerprint` 均为 64 位且在重复
  dry-run 中保持不变。

dry-run 之后必须向用户展示这一份脱敏计划，并获得一次新的、明确的
生产写入批准。之前对修复方案或部署的同意不等于这次 apply 批准。
批准后重做安全窗口检查和 dry-run，只有计划未变时才能复制其精确指纹：

```bash
telegram-kol repair-entry-assembly-fingerprint \
  --database-path <production-db> \
  --assembly-id 2 \
  --execution-binding-id 266 \
  --apply \
  --expected-plan-fingerprint <exact-fingerprint-from-approved-dry-run>
```

缺少指纹、指纹改变、不是恰好一个 action，或任何 conflict 都必须在写入前
失败关闭。成功后记录输出的 `event_id`，重复 dry-run 并按现有生产安全
监控命令运行一次不带 `--notify` 的诊断，确认
`live_entry_preamble_binding_evidence_missing` 已消失且没有新异常。

代码回滚时可恢复上一版服务，但已追加的审计事件不删除、不改写。
如后续证明该证据有误，立即按新事故处理，保留原事件并通过单独评审的
追加证据纠正；不得手工删行或直接改写生产数据。

## 同消息多指令识别灰度发布

`multi_instruction_mode` 默认是 `disabled`。三个模式只影响权威负载中
`instructions` 列表向候选项和执行项的投影：

- `disabled`：保留原有单动作兼容路径，不接纳列表中的额外动作；
- `shadow`：把规范化列表留在识别审计负载中，但不创建额外候选项或执行项；
- `live`：仅对 `raw_message_id` 严格大于
  `multi_instruction_activation_after_raw_message_id` 的未来消息投影全部动作。

同一消息中的取消挂单/仓位管理必须排在新策略之前，但每个执行项独立记录
`submitted`、`skipped`、`failed` 或 `unknown`。一个动作成功不得掩盖兄弟动作
失败；未知交易所结果不得自动重试。精确策略修订使用
`revision_target_min_confidence`（默认 `0.70`），只有生命周期与 execution
binding 归属完全一致且替换 entry/SL/TP 完整时才能使用；普通新开仓继续使用
`min_ai_confidence`（默认 `0.75`）。

撤销未成交触发入场时，历史记录缺少 `state` 只能在同一次调用收到明确成功
响应、精确订单身份和经济字段一致、订单已从 pending 消失、没有成交证据、
也没有精确活仓证据时确认终态。任一证据缺失或冲突都保持失败关闭。

发布必须先完成本节上方的安全窗口查询，并保存当前最大 terminal raw message
ID 作为未来水位线。首次只切到 `shadow`：

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  | jq --argjson watermark '<latest-terminal-raw-id>' \
    '.multi_instruction_mode="shadow" |
     .multi_instruction_activation_after_raw_message_id=$watermark' \
  > /tmp/trading-settings.multi-shadow.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/trading-settings.multi-shadow.json \
  http://127.0.0.1:8000/api/trading-settings
```

只评审自然到达的未来消息；禁止把 `#4206`、`#4210`、`#4212` 或其他历史
消息送回生产 listener。确认声明指令数、候选项数和执行项数一致，且没有
`missing_instruction_projection`、`unevaluated_sibling_instruction` 或
`hidden_instruction_failure` 后，重新完成安全窗口检查，再把同一完整 payload
中的模式改为 `live`。切换本身不得产生候选项、执行项或 Deepcoin 写入。

回滚只把 `multi_instruction_mode` 改为 `disabled`，保留水位线、识别负载、
候选项、执行项、交易所事件及异常证据。不得删除审计行，也不得为验证回滚
而重放历史消息。

## 确定性部署预检

新的部署路径不再依赖操作员临时拼 SQL 判断。本地必须先推送已评审的
40 位 commit，再显式声明变更类型：

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" \
CHANGE_CLASS=code \
./scripts/server_git_update.sh
```

PowerShell 使用 `-ExpectedCommit <sha>` 和 `-ChangeClass <class>`。可用类型仅为
`code`、`schema_compatible`、`execution_writer`、`live_promotion`。
缺少参数、分支尖端与预期 commit 不一致或预检证据不完整，都在
checkout、安装和重启前失败关闭。

预检仅读取本地 SQLite 和已持久化的 Deepcoin 只读快照，检查：

- TradeSignal 和 instruction execution contract；
- management batch/component；
- position mutation、精确平仓保留、保护与 rescue；
- source-message deletion job；
- 历史 unknown 残留、活仓数、快照完整性和数据库水位。

任意新鲜在途操作或未证明止损保护的活仓都是 `BLOCK`。旧 unknown 和已保护活仓是显式
`WARN`，不能被隐藏为 PASS。对 `execution_writer` 和 `live_promotion`，
止损只能由仓位自身的 SL 字段，或同时精确匹配 `posId` 与 durable ledger `ordId` 的 TPSL 证明；
同品种同方向的其他订单不构成 ownership。
交易所快照必须是两个不同 version/抓取时间的独立采集，且规范化事实一致；
同一缓存文件连读两次不构成稳定证据。其他类型的快照缺失仍会以 WARN 显示。
`schema_compatible` 会生成 SQLite backup，再用候选 commit 对可丢弃副本执行迁移演练和
`quick_check`，因此允许已知旧 schema 在真实迁移前进入预检。

输出文件位于 `/run/telegram-kol/deployment-preflight-<sha>.json`，权限为
`0600`，五分钟后过期。更新器先做一次预检，通过后停止唯一交易写服务，
再在无新 writer 可启动的状态下重新采集和校验，并保持到安装完成。中途失败
会由 trap 恢复原服务。退出码为：

```text
0 PASS
2 WARN（允许发布，但必须保留原因）
3 BLOCK
4 输入或证据不完整
```

`live_promotion` 还必须提供已评审 shadow 证据 artifact 的服务器路径和
`I_AUTHORIZE_LIVE_PROMOTION` 显式授权。一般代码发布、之前的“认可”或
沉默都不等于 live 授权。预检会重算 artifact 指纹，并校验 commit、水位、
观察窗口、覆盖面、有效样本数和零未解释差异，不接受单独输入的 64 位字符串。

## 统一执行真相故障与回放门禁

任何 instruction execution contract、entry writer、management writer、相邻消息
组装或历史迁移改动，在提交前必须运行以下门禁：

```bash
uv run pytest -q tests/test_instruction_execution_fault_injection.py
uv run pytest -q tests/test_instruction_execution_replay.py
uv run pytest -q tests/test_historical_state_repair.py
uv run pytest -q
```

故障注入覆盖 contract transition 前、进入 `submitting` 后但 HTTP 前、HTTP
发送后响应丢失、交易所接受后本地提交前、两条 entry leg 之间、建仓后保护
提交前、wakeup 丢失和重启对账中断。只要存在交易所写入可能性，重启路径就
必须保持 `submitting`/`submit_unknown` 并只允许只读对账，不能再次进入 writer。
所有入场腿的 `client_order_id` 必须唯一且重启后不变。

脱敏回放语料固定在
`tests/fixtures/instruction_execution/replay_corpus.json`。语料只能追加或经过
独立评审后更新，必须固定校验草稿指纹、总风险、目标 identity、contract 终态
和交易所写入次数；不能用生产消息正文、交易所 ID、账户 ID 或密钥。历史快照
迁移测试必须连续 bootstrap 两次，并证明不会为旧 lifecycle 自动创建
TradeSignal、ExecutionBinding、ExecutionEvent、订单草稿或 execution contract。

上述任一门禁失败都阻止发布，不允许把 execution、unknown outcome 或 migration
失败标记为无关基线。调查失败时保留数据库和 fixture 证据，不重放旧 Telegram
消息，也不调用任何交易所变更接口。

## 2026-08-11 统一执行真相休眠发布记录

生产已通过确定性更新器部署 commit
`eb05d4380331d5d330af3b939d98997003126adf`。本次只安装 additive schema、
兼容镜像和只读监控能力，没有启用新执行权：

- execution contract 模式仍为 `disabled`；
- entry/management 水位均为 `0`；
- contract/transition 表生产行数均为 `0`；
- 未回放旧消息，未重跑历史订单，也未切换 shadow/live。

发布前用监督修复命令收敛了一个 TP 逻辑腿分裂真相。dry-run 必须只有一个
action，并以 fingerprint 和一次性 confirmation token 应用。该操作只把已经由
durable order、ledger、活仓和 Deepcoin pending readback 共同证明的现有 TP 订单
绑定到逻辑腿；不调用交易所 writer。生产核验显示 signal、binding、entry leg、
TP order、ledger 和 execution event 的数量与最大 ID 均无变化，待修复的活跃
TP 逻辑腿由一条降为零，verified TP 增加一条，confirmation token 增加一条。

两次正式预检均无 fresh active work，五个活仓都有止损保护，未保护仓位为零，
backup 和 migration dry-run 均有效。`schema_compatible` 的 WARN 仅保留历史 residue、
历史 unknown、已保护活仓和非稳定 display snapshot 事实，不构成执行权放开。

部署后验证：服务为 active，settings API 为 HTTP 200，包路径指向生产 checkout；
服务器聚焦测试先通过 643 项，最终 monitor 补丁再通过 285 项。最后一次不带
`--notify` 的 full audit 为 `healthy=true`、零 reason code、无 monitor error，
notification status 为 `not_needed`。

若回滚，先保持 contract 模式 `disabled`，通过新评审的 revert commit 和同一预检
流程发布；不得删除监督修复 token、解绑已验证 TP、清理历史 unknown 或重放旧消息。
任何 shadow/live 灰度必须另取未来水位、生成并评审证据 artifact，再取得明确授权。
