# Action-scoped deployment gates

The deployment workflow has five independent actions. A successful action never authorizes or automatically starts the next one.

**A generated plan is not authorization.** It is a deterministic statement of required evidence and prohibited effects. The operator still grants each external action separately.

The existing conservative updater remains authoritative for production activation until immutable staging and scoped activation are implemented and independently reviewed. The local planner does not weaken or bypass that updater.

## Action matrix

| Action | Required evidence | Prohibited effects | Separate authorization |
| --- | --- | --- | --- |
| `local` | correct workspace; risk-scoped tests | SSH, service control, production settings/DB, Telegram, exchange writes | no production authorization |
| `push` | clean tree; reviewed diff; exact commit; fast-forward history | stage, activation, restart, production settings/DB, Telegram, exchange writes | push only |
| `stage` | exact commit; immutable inactive artifact; non-secret receipt | active checkout mutation, service control, production settings/DB, Telegram, exchange writes | server staging only |
| `activate` | verified stage receipt; explicit activation approval; exact loaded-artifact identity; affected-service scope; rollback; scoped health | undeclared services, trading enablement, exchange writes, historical/frozen-message replay, bulk order actions | activation/restart only |
| `trading` | explicit trading approval; fresh runtime/exchange evidence; one canonical target; no unknown; fresh single-use confirmation; full local terminalization | bulk actions, historical/frozen-message replay, automatic retry after unknown | exactly one trading action |

Stage must not inspect live runtime or database state. This remains true for an L3 worker candidate: risk changes what later activation must prove, not whether inert candidate files may be prepared.

Trading enablement is never implied by activation. Enabling entry, performing a close/TPSL/rescue action, or writing to Deepcoin is a distinct `trading` action.

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

`web` and `monitor` activation proves the staged artifact, affected process identity, rollback, and scoped health. It does not query active exchange submissions or infer trading/protection authority because those services do not own it.

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

## Removal sequence

The legacy one-command stage-and-activate path is removed only after these independent paths exist:

1. immutable stage-only command;
2. activation that consumes only a verified stage receipt;
3. affected-service-specific gates and rollback;
4. explicit workstation commands that never chain actions;
5. focused failure injection and a final full suite on the exact candidate.

Until then, the universal updater is retained as a conservative compatibility path. Adding a bypass flag, operator override, or settings-based authority inference is not part of this design.
