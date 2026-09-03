# Split-runtime per-role rollback activation design

## Status and authorization boundary

Status: approved for design, implementation, tests, and independent review.

This phase must not execute the new activator against production, create or consume a
production activation authorization, restart a production service, change schema or business
data, or perform an exchange write. The production schema tables remain empty and the 29
legacy `execution_running` rows remain observation-only.

## Problem

Scoped activation can intentionally leave Web, monitor, ingest, and worker on different
immutable releases. Authority activation still models rollback as one commit and therefore
requires Web, ingest, and worker to already match that one release. Once a Web-only activation
has created a split state, the authority path cannot move ingest or worker even though every
individual release is valid.

The fix is not to relax identity or authority scope. It is to make the rollback contract express
the real pre-state: one pre-authorized immutable release per affected component.

## Goals

- Keep authority activation atomic in scope: any ingest or worker activation still declares
  exactly `web`, `monitor`, `ingest`, and `worker`.
- Bind each component's rollback commit and manifest digest before service control.
- Prove that every rollback target is both immutable-valid and the target actually serving or
  configured for that component.
- Restore each component to its own target on failure.
- Retain the legacy single-rollback protocol without semantic change.
- Provide a dry-run that executes all read-only gates without consuming authorization, writing
  drop-ins, reloading systemd, or controlling a service.
- Bootstrap the new control protocol without depending on an older runtime release to understand
  it.

## Non-goals

- No relaxation of the full authority component scope.
- No dynamically discovered or unsigned rollback target.
- No stopped-legacy fallback or service inhibition workflow.
- No runtime, schema, business-data, recognition-state, or exchange change in this phase.
- No repair of the 29 existing `execution_running` rows.

## Considered approaches

### A. Activation-manifest rollback map with authorization v3 — selected

The activation manifest carries the complete rollback map. A v3 authorization binds the same
canonical map, controller identity, controller bundle digest, candidate, component list, source
mode, and action-plan digest. This keeps one inspectable signed contract and makes ambiguity a
validation error.

### B. Separately signed rollback sidecar — rejected

A sidecar could avoid changing the manifest model, but it introduces another file, hash, and
canonicalization boundary and makes it easier for the action plan and rollback declaration to
diverge.

### C. Discover the rollback map from live identities during activation — rejected

Discovery is useful as evidence but cannot substitute for authorization. It would allow the
machine's current state to select rollback targets after the owner authorized the action.

## Manifest contract

An activation manifest may add exactly one activation-only field:

```json
{
  "action": "activate",
  "risk_level": "L3",
  "components": ["web", "monitor", "ingest", "worker"],
  "requires_restart": true,
  "schema_changed": false,
  "production_data_mutation": false,
  "exchange_write_semantics_changed": true,
  "authority_changed": false,
  "rollback_releases": {
    "web": {
      "commit": "<40 lowercase hex>",
      "manifest_sha256": "<64 lowercase hex>"
    },
    "monitor": {
      "commit": "<40 lowercase hex>",
      "manifest_sha256": "<64 lowercase hex>"
    },
    "ingest": {
      "commit": "<40 lowercase hex>",
      "manifest_sha256": "<64 lowercase hex>"
    },
    "worker": {
      "commit": "<40 lowercase hex>",
      "manifest_sha256": "<64 lowercase hex>"
    }
  }
}
```

Rules:

- `rollback_releases` is prohibited for `stage`, `local`, `push`, and `trading` actions.
- Its keys must exactly equal the activation component list. Authority activation therefore has
  exactly four entries. A scoped Web activation may use a one-entry map.
- Each value contains only `commit` and `manifest_sha256`; unknown keys, mixed case, malformed
  lengths, duplicates after normalization, and missing components are rejected.
- A candidate commit must differ from every rollback commit.
- Every distinct rollback release must pass `validate_release()`, and its computed manifest digest
  must equal the manifest-bound digest.
- Candidate runtime-support digest must match every distinct rollback release's runtime-support
  digest, retaining the existing config/dependency/unit compatibility gate.
- Stage and activate still compare every existing deployment-change field and `components`
  exactly. `_same_declared_change()` ignores only `action` and the new activation-only
  `rollback_releases` field. No other difference is accepted.

Legacy activation manifests omit `rollback_releases`, continue to supply one
`ROLLBACK_COMMIT`, use authorization v2, and execute the existing path unchanged.
Supplying both the new map and a legacy rollback commit is rejected to prevent competing sources
of truth.

## Canonical authorization v3

The v3 canonical JSON contains exactly:

- the existing `contract`, `schema_version`, `commit`, `components`, `source_mode`,
  `action_plan_sha256`, `nonce`, `issued_at`, and `expires_at` fields;
- `rollback_releases`, byte-for-byte equivalent after canonical parsing to the activation
  manifest map;
- `controller_commit`, equal to the exact reviewed commit used to build the control bundle;
- `controller_bundle_sha256`, equal to the received control bundle's SHA-256.

The contract is `scoped-activation-authorization-v3`, schema version is 3, and the existing
15-minute lifetime and root-owned mode-0400 requirements remain. V3 is accepted only with a
per-role manifest. V2 is accepted only with the legacy single rollback form. Authorization is
validated before and immediately before consumption. Dry-run validates it but never consumes it.

## Control-bundle bootstrap

An old rollback release cannot parse the new protocol. The standard shell and PowerShell clients
therefore build an exact-commit activation control archive containing only the activator,
deployment action-plan parser, package initializer, and activation launcher required for the
control process. They compute its SHA-256, send it with the activation manifest, and the remote
wrapper:

1. creates a root-only temporary directory under `/run`;
2. verifies the received manifest and bundle hashes before extraction;
3. rejects unsafe archive paths and links;
4. executes the controller with `python -B` and `PYTHONDONTWRITEBYTECODE=1`;
5. passes the exact controller commit and bundle digest for v3 authorization validation;
6. removes the temporary directory on exit.

The legacy single-rollback path continues to dispatch through the immutable rollback release.
The new bundle is a deployment controller, not a runtime release, and cannot bypass candidate or
rollback release validation.

## Rollback-target proof

### Web, ingest, and worker

For each runtime role independently:

1. validate the map's commit with the existing full-tree immutable validator;
2. require the validated manifest SHA-256 to equal the map value;
3. read `/api/runtime/deployment-identity` through `SystemRuntimeAdapter`;
4. retain the existing systemd MainPID, `/proc` start-ticks, cwd, command-role, event-loop health,
   entry-freeze, release commit, manifest digest, and loaded-artifact checks;
5. compare that role only with its own mapped release.

No arbitrary valid commit is accepted: the immutable evidence and the live role identity must
agree.

### Monitor

Monitor is an independent rollback component. Its read-only proof combines configured identity
and real execution evidence:

- inspect effective systemd `Environment`, `FragmentPath`, and `DropInPaths` for every monitor
  service unit plus `telegram-kol-monitor.timer`;
- require all release path, commit, and manifest variables to agree with the mapped monitor
  release wherever those variables apply;
- `lstat` every reported unit and drop-in path, require root ownership, regular files, no symlink,
  and compute the greatest nanosecond mtime across all monitor unit and drop-in files;
- read the latest successful `monitor-deployment-diagnostic-v1` payload without starting a unit;
- require its release commit and manifest digest to match the map, and require
  `loaded_artifact_verified=true`, `result_complete=true`, and `sources_complete=true`;
- require the diagnostic completion timestamp to be strictly later than the maximum unit/drop-in
  mtime.

Missing, malformed, inconsistent, or stale configuration/diagnostic evidence fails closed.
ExecStartPre success alone is never sufficient.

## Activation flow

The per-role path performs, in order:

1. parse and canonicalize the action manifest;
2. enforce full authority scope when ingest or worker is present;
3. validate candidate and every distinct rollback release full tree;
4. validate all manifest-bound rollback digests and runtime-support digests;
5. validate canonical v3 authorization and controller identity;
6. prove each live runtime role against its own rollback target;
7. prove monitor configuration plus fresh matching diagnostic;
8. prove active exchange-write count is zero and retain entry-freeze semantics;
9. revalidate authorization;
10. in live mode only, consume authorization and begin the existing stop/publish/start sequence;
11. validate the candidate tree again after drop-in publication and prove all restarted roles;
12. run the existing real monitor diagnostic against the candidate.

All current authority, active-write, entry-freeze, process identity, manifest, full-tree, restart,
monitor, and undeclared-process checks remain.

## Per-component rollback

After mutation starts, any failure stops every declared unit. Drop-ins are rendered per component
from its mapped release. Publication is all-or-stop: if any target cannot be written, no unit is
started. After daemon reload, services start in the existing safe order and each runtime role is
proved against its own target; monitor is proved against its mapped release with a real diagnostic.

If publication, start, identity proof, or monitor proof fails, the activator best-effort stops all
declared units and raises `activation failed; rollback_failed`. It never reports rollback complete,
never tries the candidate again, and never substitutes a different release. Only complete
per-component restoration returns `activation failed; rollback_complete`.

## Dry-run

`ACTIVATION_DRY_RUN=1` uses the same v2 or v3 parser and all pre-mutation gates, including
authorization validation, full-tree validation, runtime-support comparison, live role proof,
monitor configuration/fresh-diagnostic proof, zero active writes, and entry-freeze evaluation.
It then returns a canonical `validated` result containing candidate and rollback evidence.

Dry-run never consumes or links authorization, writes a drop-in, reloads systemd, starts/stops a
unit, or runs a new monitor diagnostic. Live activation repeats all time-sensitive checks.

## Testing

- RED regression: an authority activation whose current Web and worker releases differ is rejected
  by the legacy single rollback contract.
- GREEN: the identical split state passes with a fully bound per-role map.
- V2 single rollback behavior remains byte-for-byte compatible at the API/result boundary.
- Missing, extra, malformed, wrong-manifest, arbitrary-but-valid, or live-identity-mismatched targets
  fail before authorization consumption or service control.
- Monitor proof rejects missing diagnostic, mismatched commit/manifest, incomplete evidence, unsafe
  unit paths, and a diagnostic timestamp equal to or older than the greatest config mtime.
- Partial rollback publication/start/proof failure leaves all declared units stopped and reports
  `rollback_failed`.
- Dry-run proves all gates and records zero mutating adapter calls.
- Existing authority scope, active-write, entry-freeze, full-tree, runtime-support, restart,
  monitor, authorization-consumption, and rollback tests remain green.
- Shell and PowerShell tests prove exact-commit bundle construction, hash checking, safe extraction,
  `python -B`, and legacy dispatcher compatibility.

## Production rollout boundary

This phase ends after code, focused/full tests, documentation, and independent review. A later
explicit authorization is required to create a production v3 authorization, run production
dry-run, stage a new runtime release, or activate any component.
