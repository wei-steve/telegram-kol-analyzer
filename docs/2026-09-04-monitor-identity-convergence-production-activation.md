# Monitor identity convergence production activation

Date: 2026-09-04 (UTC)

Candidate: `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71`

Scope: `web`, `monitor`, `ingest`, `worker`; immutable source; canonical v3
per-role rollback; `schema_changed=false`.

## Outcome

The standard helper activated the candidate successfully. It consumed a new canonical v3
authorization once and preserved the observed rollback mapping:

| Component | Observed rollback release | Rollback manifest SHA-256 |
| --- | --- | --- |
| Web | `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` | `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8` |
| monitor | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` |

The candidate manifest is
`7ac1c94946d58b8ed9eca52cef9ff6504582632ed799f14ad01e90a43f1a2468`.
Both the pre-activation and post-cutover immutable-tree passes accepted all 36 retained releases;
both found zero `__pycache__` directories and zero `.pyc` files.
The activation monitor diagnostic at `2026-09-04T12:26:25.453772Z` loaded that exact
candidate and manifest with `loaded_artifact_verified=true`, `result_complete=true`,
`sources_complete=true`, no adapter failure and no monitor error. The four known historical
business residuals still made its informational `healthy` field false; those residuals were
not identity-gate failures and were not modified.

The first local helper invocation at `2026-09-04T12:24:35Z` stopped before contacting the
server because that isolated worktree had no `.venv`. The authorization remained unconsumed and
the runtime did not change. The same unmodified standard helper was invoked with the repository's
existing planner Python at `12:25:07Z`; it completed activation at `12:26:29Z`. The authorization
source is absent and its root-owned mode-0400 consumed marker has SHA-256
`5ecfac8484d6d14e10a3796926b95894134d196bbfed871945c03086407ae0f2`.

## Freeze and message-loss check

The conservative freeze interval was `2026-09-04T12:25:07Z` through
`2026-09-04T12:26:36Z` (89 seconds). The prebuilt exact thaw procedure ran immediately after
helper success, and `entry_admission_frozen` was false on all three runtime identity endpoints.
No raw message arrived in that interval, the total stale-expiry count did not change, and no
freeze-window message entered `expired_stale_instruction`.

## L2 observation

The intended fixed window was `2026-09-04T12:26:36Z` through
`2026-09-04T12:56:36Z`. It was interrupted at `12:47:49Z` after a complete read-only exchange
view found a newly opened, unbound BTC short position `1001125126568015`, size 2 contracts, with
zero verified stop-loss or take-profit orders and `自动管理已冻结`. This is an active risk
requiring owner action; no protection, close, binding, retry, or other exchange/business write was
performed. The target position named in the activation authorization remained fully protected,
but continuing routine L2 acceptance after discovering a different naked live position would
have hidden the higher-priority incident.

The cutover itself is complete, but L2 acceptance is **not all green**. The live lease fence
classified raw message `14825` as `execution_uncertain`: attempt `6` reached
`side_effect_started_at`, the adapter boundary returned `in_progress`, and the lease recorded
`exchange_effect=outcome_unknown` with `ExecutionBoundaryOutcomeUnknown`. The immediate retry was
blocked by the active-or-uncertain guard. No candidate binding, order leg, execution event,
management batch/envelope, or exchange-position change was observed for that message, but the
system correctly did not infer that the exchange boundary was untouched. The row was left intact;
there was no replay, clearing, retry, or historical-row repair.

Before that safety stop, 23 real messages from 7 chats had arrived, exceeding the traffic target.
Thirty-eight consecutive observation samples from `12:29:00Z` through `12:47:45Z` had no
collection error; root free space stayed between 12,559,659,008 and 12,593,836,032 bytes.
The three runtime endpoints held stable PIDs Web `1338479`, ingest `1338490`, worker `1338473`,
all with `NRestarts=0`, the exact candidate/manifest and
`loaded_artifact_verified=true`. Entry admission remained unfrozen and automatic trading remained
enabled. The database checkpoint showed:

- `authoritative_execution_attempts=24`: 20 `succeeded`, 3 `failed_safe`, 1 `uncertain`;
- `entry_assembly_wakeup_executions=0` and `recognition_execution_scan_cursors=5`;
- legacy/new decision counts `execution_running=30`, `execution_uncertain=1`;
- no stale expiry since thaw.

The three `failed_safe` attempts all belonged to raw message `14830`. They ended before
`side_effect_started_at` with `PermissionError: [Errno 13] Permission denied:
'config/telegram.env'`; exact claims were released rather than orphaned. Its original message job
had already succeeded, but the later context reanalysis finalized automation as failed. This is a
second L2 runtime anomaly, even though the new lease behaved safely and created no uncertain row.

The scanner found the new uncertain attempt within one scan cycle and continued to report the 30
legacy `execution_running` rows. This is the intended active detection, not a scanner false
positive. Backlog expiry continues to fail closed for both `execution_running` and
`execution_uncertain`; no backlog-expiry protection was weakened or invoked as a write action.

The target BTC long position `1001125123045253` remained 5 contracts with the same four verified
protection orders throughout: stops `1001125123048630` and `1001125123045252`, and take profits
`1001125123049649` (3 contracts) and `1001125123049805` (2 contracts). A second protected BTC
position `1001125126414222`, size 14 contracts, appeared through normal production activity and
had one stop and one take-profit order covering 14 contracts. The later unbound 2-contract short
position changed the complete bounded snapshot to three positions and caused the safety stop.
The target-position card and its protection fingerprint did not change.

A natural timer invocation completed during the window. Its runtime identity and source evidence
were complete, but the service exited 1 because of the already documented
`stale_entry_preamble_unresolved` and `stalled_composite_component` business residuals. Therefore
the requested “successful natural timed run” criterion was not met and is not reported as passed.
The timer remained enabled/active; the transient failed state of the oneshot service was cleared
at the end without restarting or rerunning it.

## Evidence

Root-owned production evidence is under
`/var/lib/telegram-kol-cutover-evidence/6a493d1588a2a4cdd34abfb2abd85580fc8f3b71/monitor-identity-activation-20260904T122104Z`.
It contains the preflight, helper output, consumed-authorization proof, thaw timestamps and
identities, post-cutover checkpoint, target-position snapshots, the partial JSONL series,
and the safety-interruption record. No schema action, legacy-decision mutation, business-data
repair, manual exchange write, or rollback occurred in this phase.
