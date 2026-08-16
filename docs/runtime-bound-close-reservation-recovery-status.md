# Bound position close reservation recovery status

This file is the redacted handoff for the one-time gate-clearing recovery. It
must never contain raw database ids, position/order ids, provider rows, source
text, credentials, or confirmation/authorization tokens.

## Immutable scope

- recovery branch: `codex/bound-close-reservation-recovery`
- reviewed recovery SHA: `<record-after-task-10-local-review>`
- approved Phase One target: `c50887b991712340d7d5606fb6916cdbb033926e`
- MiMo mode: `v1`
- current boundary: `awaiting stopped-service read-only double-capture approval`

## Allowed production handoff fields

- classification counts: `<not-captured>`
- source fingerprint: `<not-captured>`
- exchange snapshot fingerprint: `<not-captured>`
- evidence fingerprint: `<not-captured>`
- database/exchange/history zero-write counters: `<not-captured>`
- service restoration result: `<not-run>`
- production SHA: `<not-read>`
- verified backup path after apply: `<not-created>`

Any refused, active, unknown, drifting, incomplete, oversized, or cleanup result
leaves the next boundary blocked. Diagnostic capture does not authorize apply;
apply does not authorize Batch119 or deployment.

## Return chain

```text
reservation recovery -> Batch119 apply -> stable snapshot -> ordinary preflight -> deploy exact c50887b -> Phase One canary/cutover
```
