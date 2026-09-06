# Server Production Safety Monitor Design

## Goal

Move routine Deepcoin production-safety monitoring from a local Codex heartbeat
to the production server. Normal checks must use no model tokens, survive the
Mac being offline, and notify the existing system-operator Telegram chat only
for actionable system abnormalities.

The monitor is observation-only. It must never submit, cancel, close, retry, or
compensate an exchange request; change trading settings; restart the trading
service; or write the production trading database.

## Chosen Architecture

Add a tested Python CLI command in this repository and invoke it from a separate
oneshot systemd service on a persistent 30-minute timer. This is preferred over
a shell/cron script because the checks, state transitions, sanitization, and
notification policy can be unit tested. An external cloud monitor is not used
because it cannot safely inspect the server-local database and attribution
evidence.

The deployment consists of:

- a pure result/evaluation layer;
- bounded read-only adapters for systemd, Git, local HTTP settings, journald,
  execution-event summaries, and the existing management audit;
- a CLI orchestration layer;
- an independent monitor-state file;
- `telegram-kol-monitor.service` and `telegram-kol-monitor.timer` units;
- an idempotent installation helper for the reviewed unit files.

## Safety Baseline

The monitor verifies these exact production expectations:

- `telegram-kol.service` is active;
- the server checkout HEAD matches the configured reviewed commit;
- `auto_trade_enabled=true`;
- `management_execution_mode=live`;
- `max_concurrent_positions=4`.

Expected gate values are explicit CLI arguments in the reviewed unit file rather
than hidden fallbacks. The expected commit cannot be embedded in the commit that
defines the unit without creating a circular SHA dependency. Instead, the
installation helper captures the currently deployed, reviewed HEAD into a
root-only `/etc/telegram-kol-monitor.env` file. A later reviewed deployment must
rerun the installer to advance that frozen commit baseline.

## Lightweight Check

Every 30 minutes the monitor performs bounded checks:

1. Prove the application is serving through its literal-loopback settings
   endpoint. The monitor has no system-bus socket access, so it cannot use
   systemd's control API.
2. Read the checkout HEAD with `git rev-parse HEAD`.
3. Read trading settings from the loopback HTTP API.
4. Count priority-error journal entries since the prior scheduled window.
5. Query only bounded abnormal execution-event summaries from a SQLite
   `mode=ro` connection. Never include request/response payloads or secrets.

Normal entry, take-profit, stop adjustment, and close events are not monitor
alerts because the application already owns business notifications. The
monitor alerts only on service/config/version drift, query failure, duplicate
or unknown execution state, and other system-safety conditions.

## Daily Deep Audit

The first successful timer run after 09:00 Asia/Shanghai each calendar day runs
the existing read-only `audit-management-batches` command. The state file
records only the last completed audit date.

An audit is healthy only when its exit code is zero, snapshot status is stable,
snapshot validation is `ok`, output is complete, legacy inspection is complete,
and `blocked`, `partial_failed`, `submit_unknown`, and `recovery_required` are
all zero. A `source_snapshots_differ` result is retried once immediately because
normal WAL activity can make one capture transiently unstable. A second failure
is an alert. No other audit failure is automatically retried.

## State And Deduplication

The monitor never writes `data/research.db`. Its state lives under
`/var/lib/telegram-kol-monitor/state.json`, written atomically with restrictive
permissions. The file contains only:

- the last scheduled-window timestamp;
- the last successful full-audit date;
- the current anomaly fingerprint;
- the last notification timestamp.

The anomaly fingerprint is a SHA-256 of a canonical, secret-free result summary.
The same unresolved anomaly is notified at most once every six hours. A changed
fingerprint is notified immediately. Returning to healthy clears the active
fingerprint silently; no recovery notification is required for the first
version.

If the state file is absent or malformed, the monitor safely rebuilds it. That
condition is logged and included in the current result, but state loss alone
does not authorize any trading or database action.

An absent file is a normal first run. For an existing unreadable or malformed
file, the repaired four-field state uses a non-null anomaly fingerprint with a
null notification timestamp as the pending state-integrity notification marker.
Missing configuration, delivery failure, and explicit no-notify diagnostics
retain that marker together with repaired cursor/audit progress. A later
notification-enabled invocation retries the fixed `state_invalid` alert; only a
successful delivery writes the notification timestamp, after which the next
healthy run clears the fingerprint without another message.

When state corruption and a continuing operational anomaly are delivered in one
message, the post-delivery fingerprint omits only the synthetic `state_invalid`
reason and represents the continuing result. This preserves its ordinary six-hour
suppression across the repaired-state boundary. Before successful delivery, the
pending fingerprint still covers the complete current result so failures retry;
after delivery, a changed continuing result remains immediately eligible.

## Notification Policy

Reuse `load_system_operator_bot_config` in explicit environment-only mode and
`send_system_operator_bot_message`. Messages contain only bounded fields:

- check time;
- failed check names and fixed reason codes;
- observed/expected service, commit, and gate values;
- bounded abnormal state counts;
- whether the full audit was incomplete or abnormal.

No bot token, HTTP body, exchange payload, raw exception, header, database row,
or message text is emitted. Missing bot configuration or delivery failure makes
the monitor service fail and remains visible in journald; it never changes the
underlying trading state and is not retried inside the same invocation.

## systemd Operation

`telegram-kol-monitor.service` is `Type=oneshot` and runs as the dedicated
unprivileged `telegram-kol-monitor` user/group with an empty capability set.
AF_UNIX remains available for Python/asyncio runtime-local socket pairs, while
`/run/dbus/system_bus_socket` and `/run/systemd/private` are explicitly
inaccessible so no system-bus/service-control connection is possible. A read-only mount allowlist exposes only the
virtualenv/source, Git metadata, database components, and journal; the general
checkout `.env`, `config/`, and unrelated data remain hidden. It reads a
root-owned environment file containing only the frozen expected HEAD and
system-operator bot fields, and invokes the virtualenv CLI with fixed bounded
arguments. It is
separate from `telegram-kol.service` and has no restart or mutation relationship
with it.

`telegram-kol-monitor.timer` uses a true 30-minute `OnCalendar=` schedule,
randomized delay to avoid synchronized load, and `Persistent=true` so systemd
records and catches up a missed calendar run. The unit writes only to journald
and the independent state directory.

The installation helper accepts only the fixed validated
`/opt/telegram-kol-analyzer` production Git root, copies reviewed unit files,
captures that checkout HEAD into the root-only allowlisted environment file,
creates the dedicated identity and its state directory with restrictive
ownership/mode, and reloads systemd. Install-only fails before changes when an
existing timer is active or enabled. An explicit `--enable` option enables the timer only
after the dry health run and labelled notification test have passed. It never
starts the trading service, edits trading settings, or modifies database files.
Re-running it is idempotent and intentionally advances the monitored SHA only
when the operator reruns it from the reviewed server checkout.

## Testing

Use test-driven development for:

- exact healthy and drift evaluation;
- abnormal-event classification without normal-trade duplication;
- daily-audit scheduling in Asia/Shanghai;
- one retry only for `source_snapshots_differ`;
- strict audit-completeness requirements;
- canonical anomaly fingerprinting and six-hour suppression;
- atomic state recovery and restrictive state contents;
- bounded/redacted Telegram formatting;
- CLI exit codes and no-notification healthy behavior;
- static unit-file and installer safety assertions.
- dedicated identity, empty capabilities, system-bus denial, checkout-secret
  isolation, persistent calendar scheduling, disabled-upgrade ordering, fixed
  checkout validation, and rollback timer-state cleanup.

Adapters are injected or subprocess boundaries are faked in unit tests. No test
uses production credentials, sends Telegram messages, touches the production
database, or calls Deepcoin.

## Rollout And Rollback

Develop and review locally, push `codex/deepcoin-auto-trading-v1`, and deploy
through the existing GitHub-to-server update helper. On the server, run focused
tests, install the reviewed timer units, and execute one manual monitor run with
notification delivery disabled to verify a healthy result.

Then perform exactly one clearly labelled monitor test notification through the
existing system-operator bot. It must not contain trading controls and must not
invoke any exchange or database mutation. After confirming delivery, enable the
timer and verify its last result, next trigger, service journal, production
service state, code SHA, trading gates, and management audit.

Only after the server timer is proven active and the test notification is
confirmed should the temporary Codex automation be deleted.

Rollback disables the timer, cleans its persistent systemd timer state while
the unit is loaded, and removes only `telegram-kol-monitor.timer` and
`telegram-kol-monitor.service`, reloads systemd, and leaves
`telegram-kol.service`, trading settings, database, and exchange state
untouched.
