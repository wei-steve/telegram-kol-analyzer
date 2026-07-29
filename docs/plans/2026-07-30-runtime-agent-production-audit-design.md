# Runtime Agent Production Audit Rerun Design

## Status

Approved by the existing Phase 6 rollout boundary and the canonical status
checkpoint. This turn implements only `rerun_production_audit`; every Agent,
shadow, and action flag remains off in persistent production configuration.

## Goal

Let the Runtime Incident Agent execute one bounded, read-only management audit
and verify that the audit produced a complete result. The handler must not
start system services, send notifications, alter business rows, or gain
exchange credentials.

## Approaches Considered

### Selected: loopback-only bounded audit bridge in the main service

The main service already owns the production database file and can use the
existing `O_NOATIME` stable private-snapshot audit without new capabilities.
A loopback-only, proxy-refusing endpoint starts that exact audit in a separate
process with a 20-second hard timeout and a 1 MiB output ceiling. The endpoint
returns only the fixed bounded completion proof.

The sidecar calls this endpoint without receiving database-file ownership,
`CAP_FOWNER`, systemd access, Telegram credentials, or Deepcoin credentials. A
small coordinator captures the bounded response and exposes it to the existing
`get_service_audit_state` verification tool exactly once.

### Rejected: start `telegram-kol-monitor-diagnostic.service`

The sidecar is deliberately denied access to systemd's control socket and has
no service-start authority. Adding sudo, Polkit, or system-bus access would
expand privilege for a read-only playbook and weaken the existing sandbox.

### Rejected: run the private-snapshot audit directly as the sidecar identity

On Linux, the audit opens the live database with `O_NOATIME`. The dedicated
sidecar identity is not the database owner and deliberately lacks
`CAP_FOWNER`; its writable data mount also prevents the read-only-mount
fallback. Direct execution would therefore fail in production. It would also
run the full scan synchronously without a killable deadline.

## Components and Data Flow

1. The main-service endpoint validates loopback origin before doing work.
2. It starts `audit-management-batches` in a killable subprocess with fixed
   arguments, a 20-second deadline, and bounded combined output.
3. It validates and reduces the audit to fixed completion facts.
4. `RuntimeAgentProductionAuditRefresh` receives a loopback HTTP reader.
5. The action handler calls `rerun` with the exact incident and executor
   identity fields.
6. The coordinator validates and reduces the result to fixed completion facts:
   snapshot status, validation status, completeness, malformed-row count, and
   bounded alert-state counts.
7. The proof is held only in bounded process memory and keyed by incident ID.
8. The existing `get_service_audit_state` tool atomically consumes that proof
   and returns `audit_run_completed`, `complete`, and `monitor_error`.
9. The executor independently applies its existing playbook-specific
   verification rule and durably records the bounded proof in the recovery
   attempt.
10. A later audit-state query falls back to the existing passive monitor-state
   projection.

## Failure and Safety Behavior

- Invalid executor identity fails before running the audit.
- Runner exceptions fail the action and freeze the isolated incident through
  the existing executor behavior.
- A blocked or oversized subprocess is killed within the hard deadline and
  reduced to a generic unavailable result.
- Non-loopback or proxy-forwarded requests are rejected before subprocess
  creation.
- An incomplete or malformed audit is still captured as a completed run, but
  verification returns `complete: false` with a generic bounded error code.
- The capture is consumed before verification processing, so it cannot verify
  twice or become a permanent live override.
- Captures are capped at 32; oldest entries are evicted.
- No raw batches, IDs, payloads, paths, provider errors, or audit command
  output enter the Runtime Agent tool result.
- Persistent Agent/action flags remain off; missing injection continues to
  produce `executor_not_configured`.

## Testing and Deployment

Tests cover valid complete results, historical abnormal counts, incomplete and
malformed results, runner exceptions, a blocking subprocess deadline,
loopback/proxy rejection, endpoint redaction, one-shot consumption, bounded
capture storage, production CLI wiring, passive fallback, and executor
verification.
Critical Runtime Agent, listener, contextual-resolution, management, mutation,
and monitor regressions must pass.

Production deployment follows a fresh safe-window check with all Agent/action
flags off. The canary uses an isolated temporary database and the deployed
read-only production database audit. It may temporarily enable only the
in-process executor configuration for that isolated incident; it must not
persist flags, write the production incident ledger, or send Telegram.
