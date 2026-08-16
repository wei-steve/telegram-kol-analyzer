# Bound Close Capture Diagnostic Observability Design

## Status

Approved on 2026-08-15. This work improves only the observability of the
stopped-service, read-only bound-close reservation capture window. It does not
change deployment-gate severity, classify additional evidence as safe, write
the production database, call an exchange writer, replay messages, deploy
code, or enable MiMo v2.

## Problem

The production read-only window can reach the exchange capture stage and then
exit because a capture is refused. The CLI writes the complete private dry-run
document to a temporary 0600 file, but `set -e` exits before the existing safe
projector runs. The EXIT trap then restores services and deletes the temporary
directory. The final result is truthful but not actionable: the operator sees
only failure and cannot distinguish active exchange work, incomplete history,
schema drift, response limits, or a timeout.

Repeated production windows without a redacted reason are unsafe and wasteful.
The solution must make the existing refusal evidence visible without retaining
raw authority or weakening the refusal.

## Goals

- Project a valid refused capture into closed classification and reason counts.
- Print the safe diagnostic only after the service-restoration attempt.
- Preserve the existing ready-first, two-independent-capture requirement.
- Keep all raw capture documents inside the existing 0700 temporary directory.
- Fail closed when the CLI result, parser result, exit status, or projection is
  malformed or inconsistent.

## Non-Goals

- Do not change `deployment_preflight.py` or any BLOCK/WARN rule.
- Do not infer terminal state from age, missing callbacks, or historical rows.
- Do not add another exchange request for diagnosis.
- Do not persist diagnostics in the database, journal, repository, or a durable
  server file.
- Do not expose reservation, position, order, message, provider, or database
  identifiers.
- Do not change apply, Batch 119, ordinary deployment, monitor activation, or
  MiMo v2 behavior.

## Considered Approaches

### 1. Extend the existing strict projector (selected)

The projector already parses the private dry-run document through the closed
authoritative parser. Add a dedicated diagnostic projection that aggregates
the validated observations by classification and closed reason code. This
reuses the existing schema, bounds, conservation checks, and redaction
boundary, and does not make another exchange request.

### 2. Add a new diagnostic capture command

A second CLI could repeat exchange reads and emit an aggregate. This adds an
unnecessary request path and could diagnose a different generation from the
failed capture. It is rejected.

### 3. Persist a root-only artifact or journal event

Durable diagnostics simplify later investigation but increase retention and
secret-handling obligations. The operator approved display-only output, so
this approach is rejected.

## Architecture

### Closed diagnostic projection

Add a dedicated projector mode for capture diagnostics. It must first call the
existing `_parse_bound_close_reservation_dry_run_document()` parser. Only a
successfully parsed document may contribute counts.

The safe projection contains only:

- `status` (`ready` or `refused` from the validated document);
- `action_count`;
- the existing conserved classification counts;
- `reason_counts`, keyed only by the closed reason registry;
- the three zero-write counters: database writes, exchange writes, and history
  replays.

It excludes capture timestamps and identity, confirmation token, observations,
reservation references, source/exchange/evidence fingerprints, provider rows,
errors, paths, and credentials. Reason keys are emitted in deterministic order;
counts are exact nonnegative integers and must sum to the observation total.

Malformed input, an unknown reason, an invalid count, a byte/tree bound
violation, or any parser/projection error produces only:

```json
{"status":"diagnostic_unavailable"}
```

and a nonzero exit status.

### Runbook control flow

For each capture attempt, temporarily disable shell errexit only around the
single CLI invocation and record its exit status. Immediately restore errexit.
Then:

- exit 0 is accepted only with a valid `ready` private document;
- exit 2 is accepted only with a valid `refused` private document;
- any other status, mismatch, or projector failure is fail-closed.

Always build a diagnostic projection from the same private document before it
can be deleted. A refused first capture stops the sequence; it never performs
the second capture. A ready first capture proceeds to the existing second fresh
capture. Only two ready documents reach the unchanged semantic comparator.

The validated diagnostic line is held only in bounded shell memory until EXIT.
The EXIT handler first attempts the existing service and worktree restoration,
then prints the diagnostic line, deletes the temporary directory as part of the
existing cleanup, and returns the original or cleanup failure status. The
diagnostic describes capture evidence only; it does not claim that restoration
succeeded.

### Error handling

- Missing, empty, oversized, malformed, or noncanonical CLI output: fixed
  `diagnostic_unavailable`, nonzero window result.
- CLI exit/document-status mismatch: fixed `diagnostic_unavailable`, nonzero
  window result.
- Unknown classification or reason: rejected by the closed parser.
- Projection failure: fixed diagnostic only; no raw stderr or exception text.
- Service restoration failure: retain a nonzero window result; never convert a
  refused capture into success.
- Signals: use the existing EXIT restoration path and never start another
  capture.

## Security and Privacy Invariants

- The private capture remains 0600 beneath the 0700 recovery directory.
- No private capture bytes are printed or copied outside that directory.
- The diagnostic schema has an exact allowlist and bounded size.
- The final diagnostic contains counts and closed reason names only.
- `database_writes`, `exchange_writes`, and `history_replays` must all be zero.
- A diagnostic has no apply capability and cannot be used as capture authority.
- The change grants no new production approval and cannot reuse an old window
  token.

## Verification

Tests must prove:

1. valid refused observations produce exact classification and reason counts;
2. identifiers, fingerprints, tokens, timestamps, paths, provider data, and
   observation objects never appear in diagnostic output;
3. duplicate keys, unknown fields/reasons, invalid types, malformed JSON, and
   oversized input produce only the fixed unavailable diagnostic;
4. exit 2 plus refused document restores services and prints the diagnostic
   after restoration without starting capture two;
5. exit 0 plus refused, exit 2 plus ready, and unexpected exit statuses fail
   closed;
6. two ready captures still use the existing strict comparator;
7. projector or cleanup failures never become success;
8. the temporary recovery directory and private documents are removed;
9. adjacent recovery, CLI, deployment-preflight, and runbook tests remain
   green; and
10. the full local suite and independent Critical/Important review pass before
    any push or production request.

## Rollback

Revert the observability commit. The underlying gate, capture classifier,
apply path, and production data remain unchanged. No data migration or server
cleanup is required because the feature creates no durable artifact.
