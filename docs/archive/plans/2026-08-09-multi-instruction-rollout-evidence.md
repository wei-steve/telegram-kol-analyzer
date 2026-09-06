# Multi-Instruction Rollout Evidence

## Local verification

- Contract, recognition, context, projection, execution, Deepcoin cancellation,
  operation-supervisor, notification, settings, and database-bootstrap suites
  passed together (`728` tests collected and passed).
- `python3 -m compileall -q src` and `git diff --check` passed.
- Sanitized temporary-database fixtures for 大镖客 `#4206`, `#4210`, and
  `#4212` passed without a production database, production credentials, or a
  live Deepcoin writer.
- Independent review covered the complete change set. All Critical/Important
  findings were fixed and the final review reported none remaining.

## Activation constraints

- Deployment mode remains `disabled` until a production safe window is proven.
- Shadow activation must record the latest terminal raw-message ID as the
  future-only watermark before changing mode.
- Historical messages must never be replayed through the live listener.
- Live activation requires reviewed natural future-message shadow evidence and
  a second safe-window check.
- Rollback changes only the mode to `disabled` and preserves all evidence.

## Required production evidence

Record the reviewed commit, two stable read-only database/exchange snapshots,
zero in-flight recognition/context/management/revision/recovery/write work,
server focused-test result, service and HTTP health, activation watermark,
shadow comparison counts, and the unchanged exchange fingerprint. Do not mark
live activation complete until those fields have been captured.
