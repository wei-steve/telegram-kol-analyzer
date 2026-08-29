# Action-scoped deployment gates

The deployment workflow has five independently gated actions. One approved
phase may include several named actions; success never expands beyond that
approved phase or automatically starts an action outside it.

**A generated plan is not authorization.** It is a deterministic statement of
required evidence and prohibited effects. The operator grants one coherent
external phase whose named actions define its boundary.

Immutable staging and scoped activation are the only deployment path in the
local candidate. The planner does not authorize either action, and the
workstation helpers require an explicit action instead of selecting a default.

## Action matrix

| Action | Required evidence | Prohibited effects | Scope boundary |
| --- | --- | --- | --- |
| `local` | correct workspace; risk-scoped tests | SSH, service control, production settings/DB, Telegram, exchange writes | no production authorization |
| `push` | clean tree; reviewed diff; exact commit; fast-forward history | stage, activation, restart, production settings/DB, Telegram, exchange writes | push only |
| `stage` | exact commit; immutable inactive artifact; non-secret receipt | active checkout mutation, service control, production settings/DB, Telegram, exchange writes | server staging only |
| `activate` | verified stage receipt; approved activation scope; exact loaded-artifact identity; affected-service scope; ordinary-upgrade rollback or stopped-legacy maintenance boundary; scoped health | undeclared services, new-entry admission, activator-originated exchange writes, historical/frozen-message replay, bulk order actions | activation/restart only |
| `trading` | explicit trading approval; fresh runtime/exchange evidence; one canonical target; no unknown; fresh single-use confirmation; full local terminalization | bulk actions, historical/frozen-message replay, automatic retry after unknown | exactly one trading action |

Stage must not inspect live runtime or database state. This remains true for an L3 worker candidate: risk changes what later activation must prove, not whether inert candidate files may be prepared.

Trading enablement is never implied by activation. Enabling entry, performing a close/TPSL/rescue action, or writing to Deepcoin is a distinct `trading` action.

An activation does not disable already-authorized protection, management,
close, TPSL, or rescue merely to make deployment easier. Those capabilities
must be proven from fresh successful worker cycles and live task ownership. The
activator itself neither grants nor invokes them.

## Risk levels describe the change, not the action

- `L0`: documentation or static configuration with no runtime impact.
- `L1`: dormant, shadow, web, or observer behavior without authority takeover or exchange-write semantics.
- `L2`: runtime authority cutover, durable consumer, recovery, process separation, or worker/ingest ownership change.
- `L3`: schema change, production data mutation, or exchange-write semantics.

The manifest is fail-closed:

- all safety-relevant fields are mandatory;
- action, risk, and component names come from closed enums;
- unknown fields and duplicate components are rejected;
- schema/data/exchange-semantics impact below L3 is rejected;
- authority impact below L2 is rejected;
- activation without an explicit component scope and restart impact is rejected;
- a trading action below L3 is rejected.
- a trading action combined with deployment components or change-impact flags is rejected.

## Component-scoped activation

`web` and `monitor` activation proves the staged artifact, affected process
identity, source-mode failure boundary, and scoped health. Ordinary immutable
upgrades additionally prove rollback; `stopped_legacy` proves the persistently
stopped maintenance boundary instead. These services do not query active
exchange submissions or infer trading/protection authority because they do not
own it.

`ingest` and `worker` activation additionally requires:

- zero observed in-flight exchange submissions;
- exactly one directly proven global authority owner;
- absence of unknown authority state;
- directly observed protection authority.

These are action-level invariants, not settings-field inference. Checkout HEAD alone is not runtime identity; the started process must prove the exact immutable artifact it loaded.

Activation with schema or production-data mutation requires a scoped backup, `PRAGMA quick_check`, bounded before/after counts, and a concrete database rollback boundary. An L3 change limited to exchange-write semantics does not inherit database gates. Activation without schema/data mutation prohibits production database writes.

## Cross-action TOCTOU boundaries

1. Review to push: the reviewed exact commit is the only push candidate.
2. Push to stage: staging resolves that exact commit into an immutable inactive artifact and records a receipt.
3. Stage to activate: activation independently verifies the receipt and artifact immediately before switching the declared services.
4. Activate to health: success is based on loaded-artifact process identity and affected-service health, not repository HEAD.
5. Evidence to trading: runtime and exchange evidence are refreshed after planning; a new single-use confirmation binds exactly one canonical target and one attempt.

Any changed artifact, stale evidence, missing proof, active global authority conflict, or target-related unknown invalidates the next action. No later action falls back to a prior plan or silently retries.

## Read-only planner

The first implementation batch is a local pure planner:

```bash
python -m telegram_kol_research.deployment_action_plan \
  --manifest action-manifest.json \
  --format json
```

The manifest schema is:

```json
{
  "action": "stage",
  "risk_level": "L3",
  "components": ["worker"],
  "requires_restart": true,
  "schema_changed": true,
  "production_data_mutation": false,
  "exchange_write_semantics_changed": false,
  "authority_changed": true
}
```

The CLI reads one local JSON file and prints a deterministic, non-secret plan. It does not run Git, SSH, systemd, SQLite, Telegram, or Deepcoin commands.

## Immutable stage-only command

`deploy/telegram-kol-stage` implements only candidate staging. It requires:

- `EXPECTED_COMMIT`: the reviewed full 40-character commit;
- `BRANCH`: the reviewed remote branch;
- `ACTION_MANIFEST`: a regular JSON file accepted by the action planner with `action=stage`;
- `SOURCE_REPO`: a read-only source for the origin URL, defaulting to `/opt/telegram-kol-analyzer`;
- `RELEASE_ROOT`: the root-owned, non-writable-by-group/others release directory, defaulting to `/opt/telegram-kol-releases`;
- `STAGER_LOCK_PATH`: the exclusive staging lock, defaulting to `/run/telegram-kol-stage.lock`.

The command reads only the origin URL from `SOURCE_REPO`. It creates a temporary bare repository under `RELEASE_ROOT`, fetches the declared branch there, and requires its head to equal `EXPECTED_COMMIT`. It never fetches, checks out, merges, or installs into the active source directory.

The published directory is named by the exact commit and contains source files plus:

- `.telegram-kol-release.json`: canonical commit, tree, content digest, branch, action manifest, and action-plan digest;
- `.telegram-kol-stage-receipt.json`: canonical non-secret receipt bound to the release manifest.

All published directories are mode `0555`; regular files are `0444` or `0555` according to their executable bit. Publication is same-filesystem and no-replace. Re-running the same commit with the same action manifest validates and returns the existing receipt. Any content, metadata, branch, or action-manifest mismatch fails closed and is not repaired automatically.

Successful staging does not authorize activation. It does not inspect the production database, does not control any service, does not change settings, does not send Telegram messages, and does not access an exchange. A separate future `activate` command must independently verify the receipt and obtain activation authorization.

## Scoped immutable activation command

`deploy/telegram-kol-activate` is the separate activation entry point. It
accepts `EXPECTED_COMMIT`, an ordinary-upgrade `ROLLBACK_COMMIT`, `ACTION_MANIFEST`,
`ACTIVATION_AUTHORIZATION`, `ACTIVATION_AUTHORIZATION_CONSUMED`,
`RELEASE_ROOT`, `SERVICE_DROPIN_ROOT`, and `DATABASE_PATH`. The activation
manifest must describe the same risk, components, restart, schema/data,
exchange-semantics, and authority impact as the staged manifest; only its
action changes from `stage` to `activate`.

The authorization is a root-owned mode-`0400` canonical JSON document bound to
the candidate commit, ordered component scope, activation-plan digest, a
64-hex nonce, and an at-most-15-minute issue/expiry window. Activation validates
it only after validating the candidate and fresh source-mode evidence. Ordinary
immutable-to-immutable activation also validates the rollback release;
`stopped_legacy` does not require one. It then consumes the token once with a
same-directory no-replace hard link plus unlink before controlling a service.
Failure after consumption never restores the token.
Ordinary upgrades retain a separately validated `ROLLBACK_COMMIT`.

Runtime services load code with a component-specific systemd drop-in whose
`PYTHONPATH` names the exact immutable release. Success requires the loopback
runtime endpoint to prove all of the following at once:

- the imported module resides under that release's `src` directory;
- the immutable manifest bytes match the expected manifest digest and commit;
- the endpoint PID/start ticks equal systemd `MainPID` and `/proc` start ticks;
- each declared restarted runtime has a new PID/start-ticks identity;
- for worker authority, exactly the worker directly reports live management,
  protection, close, TPSL, and rescue task authority.

For ordinary immutable-to-immutable activation, the activation control program
is imported from the separately verified immutable rollback release, not from
the mutable active checkout. Candidate and rollback may differ in application
source, tests, documentation, and activation control code, but their runtime
configuration, dependency metadata, and installed systemd unit inputs must
match. `stopped_legacy` instead dispatches the candidate's own validated
activator after proving the full legacy/split scope inactive, persistently
inhibited, `MainPID=0`, cgroup-empty, process-empty, and free of active exchange
writes.

Web-only activation does not query the database or trading authority. Worker
activation performs a read-only active-write check both before and after the
old worker is stopped. Any worker or ingest authority activation must declare
and restart `web`, `ingest`, and `worker` together. This is necessary because
all three can admit entry work through HTTP, live message processing, or the
durable worker. Partial authority scope is rejected before release or
authorization handling.
The maintenance interval starts when the first declared service is stopped and
ends only after the new identities and authority have passed. This is an
explicit short interruption, not a zero-interruption claim.

Every authority-cutover candidate and rollback drop-in sets the same root-owned
`TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1` on `web`, `ingest`, and `worker`.
While it is set, the message-processing consumer never starts, Web rejects new
entry-command admission, and the durable worker-command consumer skips only
`recovery_live_submit` and `process_next_trade_signal`. The recovery submission
gate and entry-revision replacement boundary independently recheck the same
process environment immediately before an entry exchange write; pending entry
revision work is not claimed. Pending-entry cancel/recreate management is
blocked before its cancel so it cannot strand an order halfway through a
maintenance switch. Close and sync commands, together with independently
proven protection, management, TPSL, and rescue authority, remain available.
Runtime success requires all three runtime endpoints to prove this entry freeze
and the absence of a message consumer; a settings or API write cannot clear it.

Activation deliberately leaves all runtime entry paths frozen after success and
rollback. Removing that drop-in is a later, separately authorized `trading`
action. Before removal, that action must establish a fresh bounded watermark
and terminalize or explicitly review work accumulated during the maintenance
window; it must never automatically replay frozen-period messages or queued
entry commands.

A later Web-only or monitor-adjacent activation preserves any freeze reported
by the affected runtime before restart. No `activate` action is allowed to
transition entry freeze from true to false; only the future `trading` thaw
action may do so.

Monitor activation stops only its timer and oneshot units. Every monitor
oneshot receives the immutable `PYTHONPATH` plus a self-identity `ExecStartPre`;
the activator separately runs only the same read-only loaded-artifact
self-identity module from the candidate release. It does not execute the normal,
diagnostic, or notification monitor command because those paths can update
monitor state or capture an incident. If the timer was active before the switch,
it is restored only after the identity probe passes.

For ordinary immutable-to-immutable activation, any failure after service
control rewrites the same scoped drop-ins to the separately validated
`ROLLBACK_COMMIT`, starts only the declared services, and proves the rollback
identities, authority, and runtime-wide entry freeze. For `stopped_legacy`,
post-start proof is single-attempt: any failure persistently inhibits and stops
the full scope, proves `MainPID=0` and empty cgroups, and ends as
`maintenance_stopped`. It never starts the legacy runtime or automatically
retries. Neither path changes settings, enables trading, or can replay messages,
invokes a Telegram send, or calls Deepcoin. Unknown evidence fails closed and
is not converted to zero.

Three deliberate activation blockers remain:

1. A `stopped_legacy` first switch requires the complete authority scope to be
   inactive, persistently inhibited, `MainPID=0`, cgroup-empty and process-empty;
   checkout HEAD is not accepted as a substitute for this stopped boundary or
   the candidate's post-start loaded-artifact identity.
2. Schema or production-data mutation is refused until a separate L3 executor
   can create and verify the scoped backup, `PRAGMA quick_check`, bounded counts,
   and database rollback boundary. The activator does not accept a bypass or an
   operator assertion in place of those checks.
3. Worker/ingest activation requires exactly one durable
   `entry_revision_exchange_authority` document in the canonical idle shape.
   A production database that predates this authority row fails closed. Seeding
   or repairing it is a separate production database write with its own review,
   backup, and authorization; activation never creates it.

`deploy/telegram-kol-update` is now only an explicit activation dispatcher. It
accepts `DEPLOYMENT_ACTION=activate`. Ordinary immutable-to-immutable activation
executes the activator from the separately named immutable `ROLLBACK_COMMIT`;
the first `stopped_legacy` switch executes the activator from the validated
`EXPECTED_COMMIT` candidate and does not accept a rollback requirement. It has
no checkout-mutating, stage-and-activate, settings, database, or service-control
compatibility path.
No local implementation or successful test authorizes installation, SSH,
activation, restart, or any production write.

## Explicit workstation commands

The Bash entry point exposes four separate commands:

```bash
ACTION_MANIFEST=/path/to/manifest.json ./scripts/server_git_update.sh plan
ACTION_MANIFEST=/path/to/push.json EXPECTED_COMMIT=<reviewed-sha> \
  ./scripts/server_git_update.sh push
ACTION_MANIFEST=/path/to/stage.json EXPECTED_COMMIT=<reviewed-sha> \
  ./scripts/server_git_update.sh stage
ACTION_MANIFEST=/path/to/activate.json EXPECTED_COMMIT=<candidate-sha> \
  ROLLBACK_COMMIT=<control-release-sha> \
  ACTIVATION_AUTHORIZATION=/run/path/to/authorization.json \
  ACTIVATION_AUTHORIZATION_CONSUMED=/run/path/to/authorization.consumed \
  ./scripts/server_git_update.sh activate
ACTION_MANIFEST=/path/to/activate.json EXPECTED_COMMIT=<candidate-sha> \
  ACTIVATION_SOURCE_MODE=stopped_legacy \
  ACTIVATION_AUTHORIZATION=/run/path/to/authorization.json \
  ACTIVATION_AUTHORIZATION_CONSUMED=/run/path/to/authorization.consumed \
  ./scripts/server_git_update.sh activate
```

PowerShell exposes the same `plan`, `push`, `stage`, and `activate` action names
through its mandatory `-Action` parameter. Every non-plan command requires a
manifest declaring that exact action. `push` requires a clean exact-HEAD
worktree and refuses a non-fast-forward update. `stage` transports only an
ephemeral exact-commit control bundle and creates the inactive immutable
candidate. Ordinary `activate` invokes the rollback-release dispatcher;
`-ActivationSourceMode stopped_legacy` invokes the candidate dispatcher without
`-RollbackCommit`. Both require server-side authorization paths:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 `
  -Action activate -ActionManifest .\activate-action.json `
  -ExpectedCommit <candidate-40-character-sha> `
  -ActivationSourceMode stopped_legacy `
  -ActivationAuthorization /run/path/to/authorization.json `
  -ActivationAuthorizationConsumed /run/path/to/authorization.consumed
```

No helper command invokes the next action. In particular, push never stages,
stage never activates, activation never changes trading state, and no trading
command exists in these helpers. The legacy one-command updater has been removed.

## Completed removal sequence

The legacy one-command stage-and-activate path is removed only after these independent paths exist:

1. immutable stage-only command;
2. activation that consumes only a verified stage receipt;
3. affected-service-specific gates and rollback;
4. explicit workstation commands that never chain actions;
5. focused failure injection and a final full suite on the exact candidate.

The separate stage and activate paths passed focused failure-injection coverage
before the compatibility implementation was deleted. A bypass flag, operator
override, or settings-based authority inference is not part of this design.
