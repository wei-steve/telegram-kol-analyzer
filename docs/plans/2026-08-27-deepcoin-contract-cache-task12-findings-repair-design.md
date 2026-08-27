# Deepcoin Contract Cache Task 12 Findings Repair Design

Date: 2026-08-27
Status: approved by explicit local RED→GREEN authorization
Risk: local L1 candidate work only

## Goal

Remove the circular dependency that makes the candidate updater validate a
legacy production monitor env against the candidate-only schema before it can
upgrade that env. Also make the shipped Linux/root sticky test reproduce the
already-proven kernel behavior and update the historical refusal baseline from
14 to 15 without replay or mutation.

## Authority

- Work only in `/Users/steven/Documents/telegram获取消息`.
- Local code, tests, documentation, canonical status, and explicit-path commits
  are allowed.
- Push, SSH, deployment, restart, production/settings/database mutation,
  Telegram replay, manufactured traffic, and Deepcoin writes are forbidden.
- The four old zero-write nonterminal execution contracts and incomplete
  Deepcoin history remain production-read-only Task 12 evidence requirements;
  this local repair must not invent an explanation for them.

## Root cause

`classify_monitor_installation()`, `backup_monitor_artifacts()`, and
`sync_monitor_expectations()` all call the strict candidate-schema validator.
The current production env has one valid expected-HEAD line but predates
`TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION`. The updater therefore exits
before the synchronization function can add that governed field. This is a
source-schema/destination-schema confusion, not a reason to weaken the final
monitor contract.

The Linux/root pytest failure has a separate test-fixture cause: pytest's
root-only temporary ancestors are not traversable after the child drops to UID
65534. The sticky directory itself was configured correctly, and a direct proof
in a traversable isolated directory already passed.

## Chosen design

### Known legacy monitor env

Treat exactly one missing auto-trade expectation line as a recognized legacy
source state only when every existing invariant passes: regular non-symlink
file, exact owner/mode, one valid expected-HEAD line, and zero expectation
lines. Duplicate or invalid expectation lines remain fail-closed.

The updater will:

1. classify that narrow legacy state as managed;
2. stop the timer and oneshots;
3. back up the original env byte-for-byte before mutation;
4. create a `0600` candidate beside the env;
5. replace the one expected-HEAD line and insert the governed auto-trade option
   immediately after it when the legacy field is absent;
6. strictly validate the complete candidate schema;
7. atomically replace the env;
8. restore the byte-identical legacy env on any later failure.

Strict validation remains mandatory after the first normalization and on every
later synchronization. Secret values are preserved as opaque bytes and never
printed.

### Linux/root sticky test

The test will create a traversable isolated ancestor outside pytest's private
tree, keep only the inner directory sticky `01777`, and clean the exact test
directory afterward. It will still use a real fork plus `setgroups`, `setgid`,
and `setuid`; mocks cannot satisfy this acceptance criterion.

### Historical boundary

Prospective runbook and acceptance language will state that all 15 observed
`contract_spec_sync_unavailable` refusals remain terminal with
`attempted_exchange_write=0`, zero replay, and zero backfill. The four older
pending/deferred zero-write contracts are not reclassified locally; their
explanation remains a bounded production-read-only prerequisite for rerunning
Task 12.

## Rejected alternatives

- Manually edit production monitor env first: requires unauthorized production
  mutation and repeats for every future schema change.
- Accept any missing or malformed monitor field: weakens fail-closed behavior.
- Add a second updater or out-of-band migration service: expands authority and
  rollback surface without solving the source/destination validation error.
- Move or loosen the real cache to make the test pass: confuses a fixture issue
  with the production ownership contract.

## Verification

- RED proves the legacy env currently fails before checkout and proves the
  shipped root test cannot traverse pytest ancestors.
- GREEN covers legacy enabled/disabled upgrade, strict post-normalization,
  current-schema idempotence, duplicate/invalid fail-closed, byte-identical
  rollback, and secret redaction.
- The exact Linux/root test must pass in an isolated Linux/root environment.
- Run the full focused contract-cache/updater/monitor set and one final complete
  suite after the last production-code edit.

