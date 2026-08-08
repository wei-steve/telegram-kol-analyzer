# Runtime Incident AI Agent Rollout Runbook

## Purpose

Provide a fixed per-conversation and per-deployment procedure that prevents the
runtime incident agent rollout from interrupting normal Telegram intake,
strategy handling, management, exits, or reconciliation.

## Start of Every Implementation Conversation

1. Read `AGENTS.md`.
2. Read:
   - `docs/plans/2026-07-28-runtime-incident-agent-design.md`
   - `docs/plans/2026-07-28-runtime-incident-agent.md`
   - `docs/runtime-incident-agent-status.md`
   - this runbook
3. Run:

```bash
git status --short
git branch --show-current
git log -5 --oneline --decorate
```

4. Confirm the branch is `codex/deepcoin-auto-trading-v1`.
5. Preserve all unrelated worktree changes.
6. Set `phase_status: in_progress` when implementation begins.
7. Implement or resume only `current_phase`.

## Development Rules

- Add new behavior dormant by default.
- Prefer additive database changes.
- Do not make the normal listener or trading path wait for incident recording,
  the agent provider, Telegram notification, or recovery execution.
- Write focused failing tests before runtime code.
- Run contextual resolution and management regressions even though the agent
  must not own those flows.
- Never use live trading as a test fixture.

## Pre-Deployment Continuity Gate

Do not deploy merely because local work is finished.

Before any runtime restart:

1. Confirm the pushed commit is reviewed and matches the intended phase.
2. Confirm all new agent-related feature flags default off.
3. Use existing read-only production tools to check:
   - service and listener health;
   - latest Telegram checkpoint/freshness;
   - reconcile backlog;
   - active recognition ownership;
   - active strategy-management batches;
   - `submit_unknown`, `partial_failed`, and `recovery_required`;
   - protection incidents;
   - current production safety monitor result.
4. If a time-sensitive recognition, entry, management action, exit, or
   reconciliation is in flight, defer deployment.
5. If the read-only snapshot is incomplete, defer deployment.
6. Record the deferral in the status file and leave the phase `in_progress`.

The requirement is a proven safe window, not a guessed quiet period.

## Deployment

Use the project helper after push:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Documentation-only phases do not pull or restart production.

For runtime phases, the helper must pull the reviewed branch, reinstall the
editable package, and restart `telegram-kol.service`. Do not enable a new agent
flag in the same step as first deploying its code.

## Immediate Post-Deployment Verification

With all new flags off:

1. Confirm the server commit.
2. Confirm `telegram-kol.service` is active.
3. Confirm the listener resumes and the Telegram checkpoint advances.
4. Run or inspect reconciliation to prove no message gap.
5. Confirm existing first-pass recognition still runs.
6. Confirm contextual strategy resolution behavior is unchanged.
7. Confirm management/reconciliation workers are healthy.
8. Run the production safety monitor.
9. Confirm no unexpected incident-agent worker or notification is active.

Only then enable the phase's canary flag.

## Canary Rules

- Enable one incident type or playbook at a time.
- Start with capture, then deterministic notification, then read-only Agent,
  then shadow playbook, then low-risk action.
- Compare source records with incident records before widening scope.
- Any duplicate, unexpected business mutation, verification mismatch, or
  notification storm triggers immediate disablement.
- Disabling the incident agent must not disable normal production components.

### Phase 2 capture and notification flags

Phase 2 uses two independent, dormant-by-default settings:

- `TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES`: comma-separated exact
  incident types. Empty disables capture. Wildcards are not supported.
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED`: only `1`, `true`, `yes`,
  or `on` enables deterministic incident delivery. Empty or any other value
  disables delivery.
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`: optional comma-separated
  exact incident-type delivery allowlist. When absent, legacy global delivery
  behavior is preserved. When present but empty, no runtime incident is
  claimed for Telegram delivery.
- `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID`: optional exclusive
  runtime-incident ID watermark for Telegram delivery. When absent, legacy
  oldest-first eligibility is preserved. A valid non-negative integer allows
  only rows whose `runtime_incidents.id` is greater than the watermark. A
  present malformed, negative, or SQLite-overflow value fails closed by using
  the maximum SQLite integer, so no existing row is claimable.
- `TELEGRAM_KOL_RUNTIME_AGENT_TYPES`: optional comma-separated exact
  incident-type diagnosis allowlist. When absent, legacy Agent behavior is
  preserved. When present but empty, the Agent remains supervised but claims
  no runtime incident for AI diagnosis.

The first canary must set exactly one capture type and leave Telegram delivery
off. Compare source rows with `runtime_incidents` fingerprints and repeat
counts before adding another type. Telegram delivery may be enabled only after
the capture comparison is correct. Immediate Phase 2 rollback is to clear both
settings and restart only in a newly proven safe window.

Telegram delivery has at-least-once crash semantics because Telegram does not
accept an idempotency key. A crash after Telegram accepts a report but before
the local `delivered` commit may repeat it after lease expiry. The fixed
`事件ID` is the operator deduplication marker; committed successes are never
reclaimed.

The watermark must first be deployed dormant with the setting absent and the
existing narrow Telegram type selector unchanged. For a later separately
approved activation, stop in a proven safe window, record the current maximum
`runtime_incidents.id`, set that value as the watermark, add only the approved
new incident type to the selector, restart, and prove that no row at or below
the watermark was claimed. Rollback must restore the narrow selector before removing the watermark;
otherwise removing the cutoff while the selector is still widened makes the
historical backlog immediately eligible. Never bulk-mark historical rows as
delivered or not-needed to manufacture a clean activation state.

### Historical protection incident convergence

`audit-protection-incidents` is read-only. It may classify a legacy or
transient incident as `resolved_by_current_exchange_evidence` without creating
a synthetic replacement revision, but only from one stable database snapshot
and one complete current exchange snapshot.

The exact current-evidence path requires the incident's live `posId`, binding,
and execution leg; a complete pending-TPSL observation for the target
instrument; conflict-free account-wide order ownership; one visible active
specialized backup stop; a separate visible primary stop after excluding that
exact backup ID from legacy `stop_loss` rows; at least one visible verified
take profit; matching persisted instrument, side, price, and size; and no
unowned native TPSL order that can affect the position. Deepcoin pending TPSL
rows may omit `posId`; a globally unique order ID plus the canonical protection
ledger supplies ownership. Symbol, side, price, size, or time similarity never
does.

Missing pagination evidence, exchange errors, wrong local scope, duplicate
primary/backup identity, missing roles, mismatched readback, or ownership
conflicts fail closed as `evidence_insufficient` or `current_risk`. The audit
does not update incidents, revisions, ledgers, claims, or notifications and
does not call a Deepcoin mutation method. Historical source rows remain
immutable even when their returned classification converges.

### Context resolution target-contract repair

`context-resolution-v2` makes the provider prompt match the existing strict
parser: `new_thread`, `hold`, and `unresolved` must use an empty target list,
while revise, manage, cancel, and exit decisions must use only supplied
candidate thread IDs. Mentioning an existing strategy does not by itself make
commentary executable.

The resolver remains limited to two provider calls. After a first
`target_not_allowed` response, only the second call receives a deterministic
target-cardinality correction. The parser remains authoritative: do not normalize invalid target lists,
do not clear IDs on the model's behalf, and continue to fail closed if the
second response is invalid. The durable rejected-response diagnostic contains
only the closed decision, closed error code, and target count; it never stores
target IDs or the raw provider response.

Deployment and verification apply only to future natural messages. Historical
evidence remains immutable: never replay raw message 9758, never reclassify it,
and never allow this repair to create a management batch, mutation intent,
execution event, notification, or exchange write for it.

### Phase 3 read-only Agent sidecar

Phase 3 adds a separately supervised worker and keeps it dormant by default:

- `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`: only `1`, `true`, `yes`, or `on`
  enables diagnosis. Empty or any other value leaves the worker disabled.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_STEPS`: bounded to 1–4.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_WALL_SECONDS`: bounded to 5–120 seconds.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_PROMPT_BYTES`: bounded to 4096–32768 bytes.
- `TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_OUTPUT_BYTES`: bounded to 512–32768
  bytes.
- `TELEGRAM_KOL_RUNTIME_AGENT_CLAIM_LEASE_SECONDS`: bounded to 5–3600 seconds.

The reviewed unit is
`deploy/systemd/telegram-kol-runtime-agent.service`. Install it only with:

```bash
sudo /opt/telegram-kol-analyzer/scripts/install_runtime_agent_sidecar.sh
```

The installer refuses an active or enabled unit and leaves the installed unit
disabled and inactive. Deployment of Phase 3 code and unit installation must
therefore create no Agent process. The worker may be started for a synthetic
canary only after the normal service passes the post-deployment continuity
checks and the environment file explicitly enables the Agent.

Each incident generation permits at most three model attempts. Failures use a
durable 5-second, then 10-second retry schedule; the third failure escalates
without another automatic model call. Tool transcripts, individual tool
outputs, model turns, wall time, and repeated calls are independently bounded.

Immediate Phase 3 rollback is:

1. stop and disable `telegram-kol-runtime-agent.service`;
2. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
3. leave capture and deterministic Phase 2 notification settings unchanged;
4. confirm `telegram-kol.service`, listener, recognition, contextual
   resolution, management, and reconciliation remain active.

### Phase 4 evidence projections and offline evaluation

Phase 4 remains read-only and adds no playbook execution or business-action
authority. Its evidence policy version is `runtime-agent-tools-v2`.

The reviewed corpus is stored under
`tests/fixtures/runtime_incidents/`. It contains bounded, redacted cases for
provider retry exhaustion, contextual-worker technical exhaustion,
`submit_unknown`, `partial_failed`, `recovery_required`, severe protection
incidents, and notification delivery failure. `unresolved`, `hold`, strategy
selection, and contextual-resolution replacement are prohibited corpus
outcomes.

Run the deterministic offline gate with:

```bash
PYTHONPATH=src .venv/bin/python -m telegram_kol_research.cli \
  runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents
```

The gate must report `all_passed: true` and full scores for classification,
evidence-tool selection, unsafe-recommendation refusal, supported certainty,
budget compliance, and contextual-targeting refusal.

The Phase 4 projections remain bounded and redacted:

- local versus durable last-observed exchange state reports explicit match,
  mismatch, and unknown counts;
- worker history is limited to ten related records and excludes prompts,
  provider bodies, and raw errors;
- prior same-fingerprint attempts are limited to ten generations;
- protection summaries expose presence/count facts but never order IDs or raw
  evidence payloads.

Server validation must first deploy with the Agent sidecar disabled. Run the
offline gate from the deployed checkout, then execute only the read-only tools
against reviewed incident IDs. Do not start the Agent sidecar or enable action
authority for Phase 4. If a projection is incomplete, records unbounded data,
or disagrees with its durable source row, leave Phase 4 `in_progress` and
disable the Agent flag.

### Phase 5 versioned recovery playbooks in shadow mode

Phase 5 adds the closed `runtime-playbooks-v1` catalog and deterministic
`runtime-shadow-policy-v1`. It adds no executor and authorizes no runtime
action. The model may nominate one catalog playbook; policy independently
records `shadow_accepted`, `shadow_refused`, or `not_requested`. Every
diagnosis report and Codex handoff must continue to state
`action_executed: false`.

`TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS` is a comma-separated exact
allowlist. Empty disables every shadow playbook. Wildcards and unknown names
never pass policy. The first deployment must keep both the Agent sidecar and
this allowlist disabled. After the normal service passes the continuity gate,
enable the Agent only in a separately proven safe window and canary exactly one
read-only playbook:

```text
refresh_read_only_exchange_snapshot
```

The Phase 5 catalog also contains `rerun_production_audit`,
`recover_stale_side_effect_free_claim`, `reschedule_non_writing_ai_job`,
`fetch_missing_telegram_evidence`, and
`build_read_only_reconciliation_plan`. Operational nominations require exact
durable proof that no business write is owned; absent proof is a deterministic
refusal. No Phase 5 canary may execute any of these functions or change a
source business row.

Before widening a shadow allowlist, compare the durable nomination, policy
version, refusal reasons, recovery status, notification text, and Codex
handoff with the reviewed corpus. Require zero accepted unknown playbooks,
zero accepted operational playbooks without exact prerequisite proof, and
zero `action_executed: true` values.

Immediate Phase 5 rollback is:

1. clear `TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS`;
2. stop and disable `telegram-kol-runtime-agent.service`;
3. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
4. leave Phase 2 capture and deterministic notification settings unchanged;
5. confirm the normal service and all business workers remain active.

### Phase 6 low-risk automatic recovery

Phase 6 adds a closed executor, but all action authority remains dormant until
both of these exact flags are enabled:

- `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`: only `1`, `true`, `yes`, or
  `on` enables action policy evaluation.
- `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS`: comma-separated exact
  playbook allowlist. Empty disables every action. Wildcards and unknown names
  are refused.
- `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_CIRCUIT_THRESHOLD`: bounded to 1–5 and
  defaults to 3.

The Runtime Agent provider is isolated from every shared or business-model
credential:

- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL` must be an HTTPS
  OpenAI-compatible endpoint;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY` is required and never falls back to
  `TELEGRAM_KOL_LLM_API_KEY`;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL` is required;
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS` is bounded to 5–120
  seconds.

Production uses the direct MiMo endpoint and `mimo-v2.5`. The dedicated key
exists only in `config/runtime_incident_agent.env`, which must be root-owned
mode `0600`; never print, commit, persist, or include it in a handoff. Token
accounting is intentionally performed only in the MiMo console through this
dedicated key. The application does not store usage metadata.

Missing or invalid dedicated provider configuration must fail before an
incident claim or model-attempt increment. It must never fall back to the Web
workbench provider, authoritative recognition credentials, or another model.
The first provider verification must keep the sidecar and all action flags
disabled and use a non-business prompt with no incident, Telegram, strategy,
position, or exchange data. Report only configuration completeness, endpoint
host, model, HTTP status, and response-shape validity.

Enabling a name is not sufficient to execute it. Each playbook also requires a
reviewed, explicitly injected deterministic action handler. A missing handler
is a refusal, never a simulated success. The handler must perform the named
operation and return only a boolean completion signal; bounded read-only
verification independently proves the postcondition. Passive durable
projections cannot verify a live snapshot refresh, audit rerun, evidence
fetch, or recorded reconciliation plan.

Every execution requires the current incident fingerprint, the live worker
claim token and unexpired lease, one durable idempotency reservation, the
playbook attempt budget, and exact post-action evidence. Only one action may be
reserved globally at a time. An active duplicate reports `action_in_progress`;
an expired reservation is treated as an unknown outcome and freezes the
incident without replay. Verification mismatch, unexpected handler exception,
or repeated failures freeze further action and open the bounded circuit.

The first Phase 6 deployment must keep the Agent sidecar, action authority, and
action allowlist disabled. A canary is prohibited until:

1. a reviewed production model provider is available;
2. the exact playbook handler is wired and tested;
3. its verification projection returns playbook-specific positive proof;
4. a new safe window is proven;
5. the action flag and allowlist contain exactly one reversible playbook.

The canary report must show the incident ID, exact playbook, whether the action
executed, exact verification status, bounded evidence references, and remaining
risk. Never canary an order, position, protection, strategy, recognition,
contextual-resolution, or unknown-write mutation.

Immediate Phase 6 rollback is:

1. clear `TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS`;
2. clear `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`;
3. stop and disable `telegram-kol-runtime-agent.service`;
4. clear `TELEGRAM_KOL_RUNTIME_AGENT_ENABLED`;
5. leave Phase 2 capture and deterministic notification settings unchanged;
6. confirm the normal service and all business workers remain active.

### Phase 8R proactive read-only incident detection

Phase 7 bounded business recovery is deferred, unauthorized, and non-blocking.
Phase 8R extends only proactive discovery, bounded read-only diagnosis,
Telegram reporting, Codex handoff, and continued read-only verification.

Phase 8R never enables `TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED`.
Phase 8R never populates either the shadow or action playbook allowlist.
`TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS` and
`TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS` must remain empty throughout all
Phase 8R tasks.

Execute Phase 8R using these separately reviewed runtime stages:

1. repair independent monitoring observability and add health evidence;
2. widen existing technical incident capture in capture-only mode;
3. deploy the invariant scanner dormant, then run it shadow-only;
4. canary one deterministic notification rule at a time;
5. enable bounded AI diagnosis and Codex handoff only for a proven rule;
6. expand the rule catalog and continuous-quality metrics.

Task 8R.2 uses the closed `READ_ONLY_CAPTURE_PROFILE`: provider retry
exhaustion, contextual worker exhaustion, the three terminal management
failure states, severe protection incidents, monitor adapter or incomplete
audit failures, and notification delivery failures. It excludes unresolved or
held business outcomes, ambiguous strategy targeting, and ordinary audit
abnormalities. During capture comparison, every newly enabled type must be
absent from both the Telegram and Agent type allowlists. Three identical source
scans must retain one fingerprint generation, and the authoritative source
row plus its `updated_at` must remain unchanged.

Management and protection sources are repeatable durable scans. Provider,
context-worker, and notification failures are one-shot callbacks emitted only
after their authoritative terminal-state commit; their parity check replays
the same reviewed callback projection three times and verifies that only the
runtime incident repeat counter changes. Monitor failures have no business
source row: parity is three identical monitor evaluations with zero management
or protection rows created or changed.

When Telegram delivery or the Runtime Agent is already enabled, migrate both
selectors before widening capture. In one proven safe window, keep the capture
list unchanged, set `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` and
`TELEGRAM_KOL_RUNTIME_AGENT_TYPES` to the exact currently approved legacy
types, reload the main service and sidecar, and verify that no unapproved type
is claimable. Only then add capture types one at a time while leaving both
selectors unchanged. An absent selector means legacy-all and is not a safe
capture-only production setting.

The independent monitor receives the same exact capture allowlist through its
root-owned `/etc/telegram-kol-monitor.env`, but no Agent/provider setting or
credential. `scripts/install_server_monitor.sh` extracts only
`TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES` from the root-owned runtime
policy file. Therefore each safe-window capture change must regenerate the
monitor environment with the timer stopped, verify the installed allowlist,
then restart/reload the main service and monitor schedule as one staged policy
change. The Telegram and Agent selectors remain confined to their existing
service configuration.

The monitor never writes the production SQLite database. It submits a closed
projection to
`http://127.0.0.1:8000/api/runtime-incidents/monitor-capture`; the trusted main
service applies its own capture policy and performs only the reviewed incident
append and durable-source scan. The endpoint requires the dedicated
`TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN`, rejects non-loopback and forwarded
requests, strictly bounds the body and schema, and accepts no incident type,
business identifier, fingerprint, or arbitrary summary. The installer copies
only this token and the exact capture allowlist into the monitor environment;
it does not copy provider, exchange, Agent, or business credentials.

Use the authenticated GET endpoint
`/api/runtime-incidents/monitor-capture-health` for deployment validation. It
performs no source scan and no database write. Never use an empty POST as a
health probe: every accepted capture POST intentionally runs the bounded
durable-source scan after processing its monitor projection.

Failure of this loopback writer is fail-open for monitoring: the independent
monitor result and operator alert remain authoritative, while the missing
runtime-incident append is logged without sensitive detail. Roll back by
clearing newly added capture types first; rotate or remove the dedicated token
only while the monitor timer is stopped. The database, WAL, and SHM mounts must
remain read-only in every monitor unit.

The monitor-only loopback client has a 45-second upper bound because the
production web loop has observed pre-existing synchronous maintenance windows
longer than the SQLite 30-second busy timeout. This wait occurs only in the
isolated monitor oneshot and never in the listener or trading process. During
deployment, keep the timer stopped, update policy, restart and verify the main
service, and enable the timer last. The timer is `Persistent=true` and may run
an immediate catch-up when enabled; enabling it before the main restart can
create a real transient `monitor_adapter_failure` and operator alert.

Task 8R.3 adds an independently supervised scanner with these scanner-only
settings:

- `TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED` defaults to `false`;
- `TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY` must remain `true`;
- `TELEGRAM_KOL_RUNTIME_SCANNER_RULES` is an exact allowlist with no wildcard;
- `TELEGRAM_KOL_RUNTIME_SCANNER_INTERVAL_SECONDS` is bounded to 10–3600
  seconds and defaults to 60.

Task 8R.5 adds a deterministic message-operation projector with no provider,
notification, incident, Agent, planner, or exchange dependency. It is not
imported by the web service, listener, recognition, contextual resolution,
management, execution, protection, or reconciliation paths. Its only caller is
the explicit CLI command `message-operation-supervisor --shadow --once`.

The Phase 8R.5 settings are:

- `TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED` defaults to false;
- `TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY` defaults to true and
  the CLI refuses false;
- `TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID` is a
  required future-only watermark when enabled; absent, malformed, negative, or
  overflowing values fail closed at the maximum SQLite integer;
- `TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_BATCH_LIMIT` is bounded to 1–100
  and defaults to 50.

The CLI also refuses an enabled invocation unless both `--shadow` and `--once`
are explicit and the production database already exists. It reads only
terminal authoritative recognition rows above the watermark and writes only
idempotent rows in `message_operation_contracts` and
`message_operation_items`. Ordinary chat creates no contract. A successful
projection reports `model_calls: 0`; any nonzero value, RuntimeIncident,
notification/Agent claim, business-row change, or exchange request fails the
canary.

Deploy Phase 8R.5 with the supervisor disabled. After post-deployment
continuity checks, record the current maximum `raw_messages.id`, configure that
exact watermark with enabled=true and shadow-only=true, and run one bounded
CLI cycle manually. Never replay or backfill a historical message. This phase
does not install a timer or long-running supervisor unit. Immediate rollback
is to set the enabled flag false; because no normal service imports the
projector, rollback requires no listener, trading, Agent, or scanner shutdown.
The two additive tables may remain for audit and idempotent retry evidence.
For a later separately reviewed one-shot cycle, advance the configured
watermark only to the prior cycle's returned `last_scanned_raw_message_id`;
this prevents ordinary chat (which correctly has no contract) from occupying
the same bounded batch forever. The cycle scans raw-message IDs contiguously
and stops before a missing or nonterminal recognition decision, so an
out-of-order completion cannot be skipped. Both `completed` and exhausted
`failed` comparison states are terminal for this inspection cursor.

Task 8R.6B adds a separate durable Stage 1 outbox for every affected source
message of a `message_operation_failure`. The main-service system-operator
dispatcher materializes rows idempotently from the existing bounded
`runtime_incident_affected_messages` relation. The unique identity is
`(runtime_incident_id, raw_message_id, notification_kind)`, so fingerprint
coalescing may reuse one RuntimeIncident/Agent investigation without
suppressing any source-message alert. Stage 1 never claims the Runtime Agent,
does not invoke a model, and does not depend on the legacy incident-type
Telegram selector.

The Phase 8R.6B settings are:

- `TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_ENABLED` defaults to false;
- `TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_AFTER_CONTRACT_ID` is a required
  exclusive future-only message-operation contract watermark; absent, malformed,
  negative, or overflowing values fail closed at the maximum SQLite integer;
- `TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_MAX_ATTEMPTS` is bounded to 1–20 and
  defaults to 5;
- the existing notification lease setting controls durable stale-claim
  recovery and retry spacing.

Each outbox row stores only bounded status, claim token/time, attempt count,
next attempt, Telegram message ID, delivered time, and error type. Formatting
loads the exact incident, raw message, and violated contract, then includes a
bounded redacted original message, authoritative intent, violation checkpoint,
known impact, and the fixed notice that read-only AI investigation is in
progress. Missing or conflicting durable identity fails the delivery closed
and remains retryable; exception text is never persisted.

Deploy 8R.6B with the flag absent/false. After the normal post-deployment
continuity checks, stop only in a newly proven safe window, record the current
maximum message-operation contract ID, set that exact value as the watermark, and then
enable Stage 1. Prove no contract at or below the watermark materializes or is
claimed. Because coalesced incidents may predate a
new affected message, eligibility is always based on the per-message contract
identity and never on the shared RuntimeIncident ID. Immediate rollback is to clear only the Stage 1 enabled flag and
restart the main service in a safe window. Existing outbox rows and affected
message relations remain as audit evidence; do not delete, rewrite, or
bulk-complete them. Agent eligibility, legacy notification selectors, the
message-operation supervisor mode, and every business-mutation authority stay
unchanged.

Task 8R.7 adds an audited declarative investigation broker. It is a library
boundary only in this phase and is not added to the Agent tool registry or any
incident selector. The closed evidence categories are message/reply evidence,
database projections, processing timelines, bounded journal summaries,
deployed code/Git state, non-secret configuration/audit state, exchange
snapshots, Telegram evidence, and prior incidents. Requests bind to one
existing incident, at most 32 bounded object IDs, an optional reviewed query
name (never SQL or shell text), a maximum 31-day time window, and 256-32768
result bytes.

Every request first appends a bounded `started` audit row before validation or
provider execution, then appends an `allowed`, `denied`, or `error` terminal
row. A crash or provider hang therefore remains visible as a started request.
Rows contain only the incident ID, evidence category, arguments fingerprint,
status, first evidence reference, byte/duration measures, and a closed denial code. Exception text, query
contents, credentials, raw provider payloads, and model responses are never
persisted. Database evidence uses SQLite URI `mode=ro` plus
`PRAGMA query_only=ON` and an authorizer that refuses DML, DDL, transaction,
ATTACH/DETACH, and PRAGMA operations. It intentionally does not use
`immutable=1` against the live WAL database because that would omit committed
WAL facts.

Opaque `sk-` credentials, JWTs, bearer values, private-key material, and
sensitive evidence references are rejected before model output or audit
persistence. File evidence is limited to reviewed read-only roots, rejects traversal and
credential-like paths, and exposes no create/modify/delete API. Network
evidence authorizes HTTPS GET only, rejects forwarded/proxy headers, limits
Deepcoin to an exact read-endpoint allowlist, and denies unapproved hosts and
exchange mutation paths. Exchange, Telegram, and production-audit adapters
project bounded proofs before the broker accepts their result.

The sidecar remains the dedicated unprivileged `telegram-kol-agent` identity.
Its checkout stays read-only under `ProtectSystem=strict`; the installer fails
if that identity can write source or configuration. A mode-0700
`/var/lib/telegram-kol-runtime-agent` state directory is its only private
analysis workspace. The root-owned Runtime Agent environment file is
inaccessible inside the service mount namespace after systemd loads it.
The production data directory has no Agent default ACL. Its service mount is
writable only because SQLite must be able to recreate absent WAL/SHM files
after a clean close or reboot. DAC grants the Agent write-plus-traverse but not
directory enumeration, the directory is sticky, and the installer removes
legacy Agent grants by applying an explicit deny ACL to every non-database
top-level file, denies group/other access on future inherited file ACLs, and
refuses deployment if any such file remains readable or writable. Known Telegram session paths are
also inaccessible in the service mount namespace. A root-owned pre-start
helper reapplies Agent access only to the exact database/WAL/SHM/journal
allowlist before every sidecar start, covering files recreated first by the
root main service after a clean close or reboot. Existing database write access remains limited to the reviewed incident/claim
lifecycle and the new audit ledger; production fact reads made by the broker
are query-only.

Deploy 8R.7 with Agent eligibility and tool registration unchanged. Stop and
disable only the Runtime Agent sidecar before its installer, then restore the
existing sidecar policy after the main-service safe-window gate. Run isolated
read and mutation-refusal canaries with temporary data; do not send Telegram,
call a live exchange mutation, replay a historical message, or make a
message-operation incident eligible. Completion additionally requires a
trade-disabled Deepcoin evidence credential or an equivalent trusted
read-only loopback projection, plus enforced egress evidence for the deployed
identity. Until those controls are proven, keep 8R.7 `in_progress` and do not
begin 8R.8. Immediate rollback is to stop the Runtime Agent sidecar; normal
intake, execution, reconciliation, Stage 1 delivery, scanner, and monitor stay
independent.

The operator approved a controlled Phase 8R.3 completion canary on 2026-08-08
so that a rare natural multi-target failure cannot block later read-only Agent
work indefinitely. This alternative changes only the verification method; it
does not widen production capture, Telegram, Agent, scanner, or action policy.
It may replace the first-natural-failure wait only when all of these are true:

1. the local and deployed-code canaries use automatically removed temporary
   databases and synthetic IDs only;
2. one exact multi-target partial-take-profit projection admits an eligible
   sibling and refuses an ineligible sibling;
3. the refusal creates exactly one `management_target_refused` runtime
   incident linked back to the refused target;
4. the existing dispatcher claims that exact type, renders the unified AI
   Agent notification, and commits `delivered` through an injected in-memory
   receiver;
5. the real production system Bot identity and target chat pass the existing
   read-only `getMe`/`getChat` evidence probe;
6. previously delivered production runtime notifications continue to prove
   the unchanged real `sendMessage` transport;
7. production envelope, target, incident, business, and exchange state are
   unchanged before and after the canary;
8. no historical message is replayed, no live test trading message is sent,
   and no additional Telegram test notification is emitted; and
9. focused capture, dispatcher, Agent-selector, and architecture regressions
   pass.

This composition is intentionally non-writing with respect to production. The
in-memory receiver proves capture/claim/format/commit composition without
violating the project rule that an implementation turn sends only its final
stop notification. Record all evidence in the canonical status before marking
8R.3 complete.

The deployable shadow projections are
`cancel_outcome_stale_unknown_v1` and
`active_position_missing_protection_v1`. The
`management_safety_gate_divergence_v1` projection is also deployable but
defaults disabled: it compares only a zero-write management refusal whose sole
reason is `protection_recovery_required` with later healthy observations for
the same exact binding, entry-leg, position, and exchange-snapshot generation.
It writes a bounded `runtime_incident_observations` row only. It does not create
a runtime incident, notify Telegram, clear or bypass a management gate, grant
Agent authority, or perform an exchange write. All other reviewed pure rules remain
non-deployable until their coherent snapshot builders exist; configuration
silently cannot activate them. The cancel rule observes at most 100 exact
cancel mutation intents and treats only `reserved`, `submitting`, `submitted`,
or `recovery_required` beyond the ten-minute window as unknown. `confirmed`,
`rejected`, and `blocked` are known terminal outcomes.

The reviewed position-compliance catalog also contains
`terminal_high_risk_management_without_instruction_v1` and
`verified_replacement_role_gap_v1`. Both are pure-rule contracts only. They
remain absent from `RUNTIME_SCANNER_DEPLOYABLE_RULE_IDS`, have no production
projection, and cannot be activated through scanner configuration. The first
detects a terminal high-risk management recognition with no executable
instruction; the second detects a verified replacement that lacks either the
exact primary-stop or backup-stop role. Adding either production projection,
notification selector, or Agent selector requires a later separately approved
runtime stage.

The unprotected-position rule is operator-visible but remains disabled unless
explicitly allowlisted. It evaluates only exact live verified entry legs and
requires no verified/pending primary stop and no close, mutation, or management
write in progress. Its bounded evidence contains only chat/strategy/binding/
leg/position identifiers, planned stop, immutable exposure start, and rescue
state; it excludes raw exchange payloads and credentials. New risks reserve
scanner capacity so unresolved manual-review observations cannot starve them.
Recovery of an earlier observation is decided by an exact per-position lookup:
terminal or currently protected state resolves it, while missing or ambiguous
evidence remains insufficient rather than proving closure.

This scanner observation is not the automatic-entry gate. The live gate reads
current exact business state and blocks only new entries for the affected
`chat_id`; it does not block exact close, cancel, stop rescue, or another chat.

The main service alone owns schema creation. The scanner opens an existing
database without bootstrap or migrations and writes only
`runtime_incident_observations`. Its systemd unit reads only the dedicated
scanner environment, cannot read checkout configuration/secrets, and has no
provider, Telegram, exchange, system-bus, or service-control credential. The
installer refuses an active or enabled scanner and always installs it disabled
and inactive. In Phase 8R.3 shadow mode the scanner creates no runtime
incident, Agent claim, or Telegram notification. Immediate rollback is to stop
and disable only `telegram-kol-runtime-scanner.service` and set its enabled
flag false; the main service and Runtime Agent are untouched.

To stage the safety-gate divergence rule, first deploy with the rule absent
from `TELEGRAM_KOL_RUNTIME_SCANNER_RULES` and keep
`TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY=true`. After a separately approved
safe-window check, add exactly `management_safety_gate_divergence_v1` to the
scanner rule allowlist and restart only the scanner sidecar. Verify that source
management batches, health observations, runtime incidents, Agent claims,
notifications, and exchange writes are unchanged except for the additive
scanner observation. To disable the rule, remove only that exact ID and restart
the scanner sidecar. To roll back immediately, stop and disable
`telegram-kol-runtime-scanner.service`; no main-service restart is required.

At most one runtime stage may be implemented per user turn. New code and
configuration default off. A first deployment never enables its feature. A
later enablement requires another complete safe-window check and the exact
rule or capture-type allowlist; wildcards are refused.

The proactive scanner must remain outside the listener, recognition,
contextual-resolution, execution, management, exit, protection, and
reconciliation critical paths. Scanner or Agent failure must not block or
change those paths. The scanner may write only additive observation and
incident metadata; it may not write a source business row.

Before every Phase 8R deployment or enablement:

1. prove the reviewed commit and dormant defaults;
2. prove no time-sensitive recognition, execution, management, exit,
   protection, reconciliation, or recovery work is in flight;
3. prove the read-only production snapshot is complete;
4. preserve durable Telegram intake and checkpoint recovery;
5. keep the scanner or new rule disabled during the first deployment;
6. verify the main service, listener, latest recognition, contextual resolver,
   management, protection, reconciliation, and independent monitor immediately
   after deployment;
7. compare source rows before and after any shadow scan and require zero source
   mutation.

If a safe window or complete snapshot cannot be proven, finish local work,
leave the current Phase 8R task `in_progress`, and record the exact server
verification still required. Do not restart or enable anything.

Immediate Phase 8R rollback is layered and independent:

1. disable the newest scanner rule or capture type;
2. disable proactive Telegram delivery while retaining durable evidence;
3. disable AI diagnosis while retaining deterministic incident capture;
4. stop and disable the scanner sidecar without touching the main service;
5. if necessary, stop the Runtime Agent sidecar while leaving normal Telegram
   intake and every business worker active;
6. confirm action authority is false and both playbook allowlists are empty;
7. run the independent monitor and verify message continuity.

## Rollback

1. Disable the newest phase feature flag.
2. Do not delete incident records or rewrite source business rows.
3. Confirm normal listener, recognition, contextual resolution, execution, and
   reconciliation remain active.
4. If a code rollback is required, use a reviewed forward fix or the project's
   safe deployment procedure; do not use destructive Git commands.
5. Run the production safety monitor and reconcile message continuity.
6. Leave the phase `in_progress` and record the exact failure evidence.

## Telegram Reports

During early phases, Telegram reports diagnosis only. Later reports must state:

- incident ID;
- evidence-based impact;
- Agent diagnosis as a hypothesis;
- exact playbook selected;
- whether any action executed;
- verification result;
- remaining risk;
- whether Codex repair is required.

No report may include credentials, raw provider responses, unbounded logs, or
direct personal/payment identifiers.

## Codex Repair Handoff

When an incident requires a code fix, use:

```text
请调查运行异常 incident_id=<ID>。
先读取 AGENTS.md 和该事件的 Codex 交接包。
独立验证 Agent 的诊断，不要把诊断当作事实。
不要扩大交易权限或绕过现有安全网关。
本地修复和测试后，按项目流程推送并在服务器安全窗口验证。
```

## End of Every Implementation Conversation

Before returning control:

1. Record local tests and results.
2. Record commit and push status.
3. Record deployment or the reason deployment was safely deferred.
4. Record server verification.
5. Update phase status and exact remaining work.
6. If complete, advance exactly one phase and set it to `planned`.
7. Preserve the exact next-session prompt:
   `请执行自定义ai agent的下一步实施`.
8. Send the single required project stop notification.
