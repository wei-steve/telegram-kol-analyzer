# Simple Deployment Safety Gate Design

> Superseded by 2026-08-17-minimal-deployment-gate-design.md.
> Retained only as historical context; do not execute this runbook.

**Date:** 2026-08-16

**Status:** Approved for implementation planning

**Production baseline:** `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`

**Implementation branch:** `codex/deployment-gate-simplification`

## Context

The previous evidence-based gate had the right safety goal but accumulated too
much policy machinery. It classified rows mainly from state names and age,
maintained a detailed change-surface graph, and coupled the gate rollout to a
terminal-entry writer repair. A server shadow of candidate
`54491733bd40907311663a6b8b28c6efd262afbb` correctly blocked deployment but
also exposed systematic false positives:

- `mimo_recognition_runs` and `mimo_recognition_attempts` are recognition audit
  tables, but the generic state-table detector treated them as unregistered
  execution work;
- `position_backup_stop_orders.status = 'missing'` and
  `source_message_deletion_exits.state = 'unbound'` are valid production
  states, but the adapters classified them as malformed;
- several paused, read-only-reconciled, or cross-table-terminal records were
  grouped with true unknown exchange outcomes solely because their state names
  contained `unknown`, `recovery_required`, or `partial_submission_failed`;
- `created_at` older than one hour was used as a proxy for historical safety,
  even though age does not prove whether a startup worker can claim a row;
- the combined candidate changed terminal-entry writer behavior, so real
  pending entry and reconciling management work made the whole candidate
  undeployable even though the gate itself could be isolated from that change.

The gate must remain fail-closed at an exchange mutation boundary, but ordinary
historical or operational irregularities must not turn every deployment into a
new recovery project.

## Goals

1. Keep only a small set of direct, explainable deployment blockers.
2. Distinguish a real or possibly repeated exchange write from paused,
   historical, terminal, and non-execution evidence.
3. Detect writer changes automatically with one stable fingerprint.
4. Allow an unchanged writer to restart around normal queued work with an
   explicit warning.
5. Preserve two-phase collection, fail-safe rollback, schema backup, and
   migration dry-run behavior.
6. Produce bounded, sanitized artifacts and never expose message text, order
   payloads, position identifiers, credentials, or database row identifiers.
7. Deploy the gate separately from terminal-entry cleanup and every other
   exchange-writer change.

## Non-Goals

- No addition, removal, or modification of MiMo v2 runtime, prompt, activation,
  replay, or schema code. The dormant production code remains byte-for-byte
  unchanged in this gate-only candidate, while MiMo v1 remains authoritative.
- No Batch-specific, reason-code-specific, row-ID-specific, or time-limited
  deployment exception.
- No database history edit or automatic remediation of existing records.
- No global writer lease in this phase.
- No detailed per-row code-owner graph.
- No automatic deployment, service restart, or push without its separate
  approval.

## Chosen Architecture

The gate has three small inputs:

1. one automatically calculated writer-surface fingerprint;
2. one bounded database evidence summary;
3. automatic schema-diff detection.

The result is collected twice:

```text
online Phase A
    -> allowed to stop service?
service stopped
    -> final Phase B from the same candidate SHA and writer fingerprint
    -> allowed to checkout/install/start?
```

The gate-only candidate is rebuilt from production baseline `2274d90`. It must
not contain the terminal-entry cleanup changes or any other runtime writer
change from candidate `5449173`.

## Single Writer-Surface Fingerprint

A single reviewed manifest lists files that define:

- Deepcoin mutation calls, including submit and cancel;
- durable pre-submit ownership and state transitions;
- automatic startup or worker claims of queued/recovery work;
- read-only reconciliation of a possibly completed exchange write.

The candidate and production versions of those files are hashed in stable path
order. The gate exposes only whether the fingerprints are equal and their
digests. The operator does not declare a change class.

The manifest is deliberately one flat safety boundary, not a per-table or
per-owner graph. Tests enforce that every public Deepcoin mutation call site is
inside the manifest. A newly introduced call site outside the manifest fails
the build. Gate code, documentation, Web display code, and recognition-only
code do not affect the writer fingerprint unless they contain or import a
registered mutation boundary.

## Database Evidence Model

Registered execution evidence maps to four meanings:

| Evidence | Meaning |
| --- | --- |
| `active_write` | A durable claim or lease has crossed the pre-submit boundary and the exchange mutation has not been terminally persisted. |
| `unknown_outcome` | A mutation may have reached the exchange and no complete durable terminal proof exists. |
| `queued_work` | The row can be processed after restart, but no exchange write is currently in flight and no prior mutation result is unknown. |
| `inactive` | The row is terminal, permanently paused, historical with no automatic claim path, or a non-execution audit fact. |

`invalid_evidence` is an input error, not a fifth business state. It means a
registered execution table or required durable field cannot be interpreted.

Classification order is fixed:

1. invalid registered execution evidence;
2. active write boundary;
3. attempted mutation without terminal proof;
4. automatically claimable queued work;
5. inactive evidence.

State text alone is insufficient for `unknown_outcome`. The adapter must prove
that a mutation was attempted and then fail to find a terminal proof. Conversely,
a cross-table verified binding/leg projection may close a stale summary state.

Timestamps may validate a lease or artifact expiry. `created_at`, `updated_at`,
heartbeat recency, and an arbitrary age threshold do not decide exchange safety.

## Known Production Shapes

The generic rules must classify the audited production shapes as follows:

- `mimo_recognition_runs` and `mimo_recognition_attempts`: non-execution audit,
  therefore inactive;
- backup stop `missing`: terminal local knowledge of an absent backup order;
  live-position protection remains a separate snapshot check;
- source deletion `unbound` with no target and no claim: inactive;
- execution leg `unknown` under a stale or unknown, non-active binding with no
  automatic mutation claim: inactive;
- instruction item `unknown` without an execution contract or claim path:
  inactive;
- management `recovery_required` excluded from worker claim queries: inactive;
- protection recovery whose parent binding and entry leg are closed: inactive;
- `partial_submission_failed` whose complete verified legs project to active or
  terminal outcomes: inactive for submission-outcome purposes;
- a durable `submitting`, `cancel_submitting`, or equivalent pre-submit claim:
  active write;
- `submit_unknown`, `unknown_exchange_outcome`, or an attempted mutation with
  incomplete leg projection: unknown outcome;
- pending entry, ready management, or another normal automatic work item:
  queued work.

These are general predicates. They must not mention production IDs, Batch 119,
specific timestamps, or current row counts.

## Decision Rules

Only four conditions block deployment:

| Condition | Decision |
| --- | --- |
| `active_write > 0` | BLOCK |
| `unknown_outcome > 0` | BLOCK |
| `invalid_evidence > 0` | BLOCK |
| writer fingerprint changed and `queued_work > 0` | BLOCK |

All other evidence is either PASS or WARN:

- unchanged writer plus queued work: WARN;
- inactive historical or paused evidence: PASS;
- protected live positions: WARN;
- incomplete exchange snapshot with unchanged writer: WARN;
- changed writer with an incomplete required exchange snapshot: BLOCK;
- schema change without a verified backup and migration dry-run: BLOCK.

There is no operator override for BLOCK. There is also no deployment blocker for
an unrelated HTTP timeout, notification backlog, recognition audit status, or
other operational health signal outside the registered execution boundary.

## Automatic Schema Detection

The gate detects migrations and model/schema files from the Git diff. A schema
change automatically requires:

1. a SQLite online backup;
2. `quick_check` on the backup;
3. candidate migration against a disposable copy;
4. schema and watermark validation after the dry-run.

The operator cannot downgrade a schema change to code-only. Schema handling is
orthogonal to the writer fingerprint: a candidate may change neither, either,
or both.

## Two-Phase Deployment

### Phase A: Online

The detached candidate opens production SQLite with `mode=ro` and
`PRAGMA query_only=ON`, computes the writer fingerprint, detects schema changes,
and writes one sanitized mode-0600 preliminary artifact.

If Phase A is BLOCK or invalid, the updater exits without stopping the service.

### Phase B: Service Stopped

After Phase A is allowed, the updater records that a stop was attempted, stops
the sole writer service, verifies it is inactive, and recollects the same facts.
The final artifact binds:

- production and candidate SHA;
- writer fingerprints;
- automatic schema classification;
- Phase A artifact fingerprint;
- database watermarks and aggregate evidence counts.

Phase B does not recursively re-verify an arbitrary artifact chain. It verifies
one direct parent fingerprint and recomputes the final decision from final
facts. Checkout, install, and startup occur only after the final decision is
PASS or WARN.

## Exit Codes And Artifacts

The CLI uses four stable exit codes:

```text
0  PASS
2  WARN (deployment may continue)
3  BLOCK
4  invalid input or evidence
```

Artifacts contain only aggregate counts, reason codes, SHA values, fingerprints,
watermarks, and timestamps. They expire after five minutes and are written
atomically with mode 0600.

## Failure And Rollback

- Phase A failure never stops the service.
- Any attempted service stop is tracked before invoking `systemctl`.
- A stop failure, timeout, TERM, or INT must restore and verify the original
  service as active before exit.
- A Phase B failure restarts the unchanged production service.
- A checkout, install, or startup failure restores the previous SHA, reinstalls
  the previous package, and restores the previous service.
- A rollback failure remains a hard non-zero result and is reported explicitly;
  the updater never continues with an unverified candidate.
- Candidate updater code is not installed before final authorization.

Rollback never restores an older database, deletes business history, replays a
message, retries an exchange mutation, or enables MiMo v2.

## Gate-Only Bootstrap

The gate-only branch starts from production and contains only the gate, updater,
tests, and documentation. It does not also carry the separately reviewed MiMo
v2 retirement diff. A boundary test proves that its writer fingerprint is
identical to production. Existing queued work therefore produces WARN rather
than BLOCK, while active writes, true unknown outcomes, invalid registered
evidence, or schema verification failures still block.

This is not a SHA-specific bypass. The same unchanged-writer rule remains the
normal deployment rule after bootstrap.

The terminal-entry cleanup writer repair remains a separate candidate. Its
writer fingerprint changes, so it must wait for `queued_work = 0` and all other
hard blockers to clear before a separate deployment approval.

## Testing Strategy

Implementation is test-driven. Required RED cases include:

- recognition audit tables are not execution work;
- backup `missing` and source deletion `unbound` are valid inactive states;
- operator-paused recovery is inactive;
- a cross-table verified partial submission is inactive;
- a genuine submit-unknown or active lease is still BLOCK;
- normal queued work is WARN with an unchanged writer fingerprint;
- the same queued work is BLOCK with a changed writer fingerprint;
- a newly added unregistered Deepcoin mutation call site fails the manifest
  boundary test;
- a schema diff cannot avoid backup and migration dry-run;
- every Phase A/Phase B artifact field is recomputed rather than trusted;
- stop timeout, deferred stop completion, TERM/INT, Phase B failure, checkout
  failure, install failure, startup failure, and rollback failure preserve or
  restore the old service correctly;
- collection produces zero database writes, notifications, and exchange
  mutation calls.

Run focused gate, updater, writer-boundary, reconciliation, MiMo v1,
configuration, and retirement-boundary tests, followed by compile checks,
`git diff --check`, and the full suite. Independent review must report zero
Critical and zero Important findings before push approval.

## Server Acceptance

After explicit push approval, stage the exact reviewed SHA in a mode-0700
detached candidate and run the same focused tests with the production virtual
environment. A read-only shadow must prove:

```text
writer fingerprint changed = false
active_write = 0
unknown_outcome = 0
invalid_evidence = 0
queued_work >= 0 (WARN is allowed)
database writes = 0
notifications = 0
exchange mutations = 0
```

Production SHA, settings, service state, tracked files, MiMo v1 authority, and
database watermarks must remain unchanged by shadow validation.

Shadow acceptance is not deployment approval. Deployment requires a separate
explicit approval and must use the reviewed two-phase updater. No step enables
MiMo v2 or deploys the separate terminal-entry writer candidate.
