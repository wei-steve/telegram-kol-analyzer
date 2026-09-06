# Runtime Agent Read-Only Exchange Refresh Design

## Scope

Phase 6 gains exactly one additional production handler:
`refresh_read_only_exchange_snapshot`. The normal listener, recognition,
context resolution, management, reconciliation, and mutation paths remain
authoritative and unchanged. The Agent, shadow, action, and action-allowlist
settings remain empty or disabled during deployment.

## Chosen design

The main web service already owns the Deepcoin credentials and client. It will
expose one loopback-only GET endpoint that rejects forwarded requests before
client construction and performs only
`list_positions()` and `list_open_orders()`, validates bounded list responses,
projects a small stable set of state fields, and returns counts plus a SHA-256
fingerprint. It never returns order IDs, position IDs, raw rows, credentials,
or provider errors.

The sidecar will not receive Deepcoin credentials. A process-local refresh
coordinator calls the endpoint once from the deterministic action handler and
retains only the bounded fingerprint/count proof. The existing
`compare_local_exchange` verification tool calls the endpoint independently a
second time. Verification succeeds only when both reads are complete and their
fingerprints and counts match. Natural exchange drift, an incomplete source,
an invalid payload, a missing first proof, or an HTTP error fails closed.

This two-read design is preferred over:

- giving the sidecar Deepcoin credentials, which broadens credential reach;
- treating the durable last-observed database projection as verification,
  which the Phase 6 runbook explicitly forbids;
- persisting raw exchange snapshots, which adds sensitive durable state and a
  new cleanup obligation.

## Safety and rollback

- The endpoint is read-only and bounded to 200 rows per source.
- Only stable scalar fields participate in the fingerprint.
- The action handler returns only `True`; the executor retains the existing
  claim, fingerprint, idempotency, circuit, and one-attempt fences.
- No business table or exchange state is written.
- With action authority or its exact allowlist disabled, the new handler is
  unreachable.
- Rollback is the existing Phase 6 rollback: clear action authority and
  allowlists, keep the sidecar disabled, and deploy a reviewed forward fix if
  code rollback is needed.

## Verification

Tests must prove endpoint redaction and bounds, two independent reads, coherent
success, drift refusal, invalid/incomplete response refusal, dormant CLI
wiring, and no regression in the passive durable comparison used before an
action. Production canarying requires a fresh safe window and exactly this
playbook in the one-shot allowlist.
