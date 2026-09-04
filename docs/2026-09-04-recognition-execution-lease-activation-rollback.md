# Recognition execution lease activation rollback record

Date: 2026-09-04 (UTC)

Candidate: `792b34d79577356b3149aa67b92efcf3d662ad3c`

Scope: `web`, `monitor`, `ingest`, `worker`; immutable source; per-role rollback;
`schema_changed=false`.

## Outcome

The canonical v3 activation authorization was consumed and the standard activation helper
started the four-component cutover. Candidate Web, ingest and worker runtime identity checks
passed under the deployment entry freeze, but the candidate monitor diagnostic failed. The
activator then completed its per-role rollback and returned
`activation failed; rollback_complete`. No retry or manual service control followed.

The actual runtime after rollback is:

| Component | Release | Manifest SHA-256 | PID/state |
| --- | --- | --- | --- |
| Web | `5aa7ca077fa45728c0f3d8df93e0e90a33a4a262` | `36da5a5e03276f684b20a783ffe4f19274cf3ef1f91ede7bda19ed97090dd3a8` | `1270165`, active |
| monitor | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | timer active/waiting; rollback diagnostic success |
| ingest | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | `1270167`, active |
| worker | `0de19c1cbb2089fd58b8940d9b01a65096f9a063` | `89778577ec34a6eaaf4179c1949b119a6d66c798731ea43b641dd02016bceca1` | `1270163`, active |

All three runtime endpoints reported `loaded_artifact_verified=true`. The rollback deliberately
retained `entry_admission_frozen=true`; `auto_trade_enabled` remained true. No thaw was attempted
after the failed activation.

## Protected-position gate

Immediately before activation, worker `127.0.0.1:8002` returned a complete read-only exchange
snapshot with one position and zero ordinary open orders. The position was BTC long
`pos_id=1001125123045253`, current size 5 contracts, with these exchange-visible and locally
verified protections:

- stop `1001125123048630`, covering all current 5 contracts;
- stop `1001125123045252`, size 10 contracts;
- take profit `1001125123049649`, size 3 contracts;
- take profit `1001125123049805`, size 2 contracts.

It belongs to lifecycle `1074`, execution binding `337`, entry leg `579`, and raw message `14757`
from the configured group label `大镖客 11分组`, posted at `2026-09-04T03:48:01Z`. The child
order/position was filled at `2026-09-04T08:05:34Z`. Trigger-protection intent `179` was
`adopted`, with adopted order `1001125123045252`, zero retries, and no `last_reason_code`. It did
not hit `trigger_protection_candidate_predates_fill`.

After rollback, the same complete worker view still showed the same position, size, and four
verified protection orders. Protection did not become weaker during the attempted cutover.

## Activation and rollback evidence

- Pre- and post-attempt full-tree validation accepted all 35 retained immutable releases.
- Pre- and post-attempt `__pycache__`/`.pyc` count under the release root was zero.
- Candidate, Web rollback and monitor/ingest/worker rollback runtime-support digests were all
  `812e87daf719c8d52d7ac2880c507f56c60706b5d8e074fba477fd60477a8304`.
- Active exchange write count before activation was zero and global authority was idle.
- The new root-owned mode-0400 canonical authorization was consumed once. Its retained consumed
  marker SHA-256 is `9daeb26e7c9390b6a8a3b71dd28a792cc1e0872852ae365f360958a8248b7a98`.
- Candidate monitor `ExecStartPre` self-identity succeeded at `2026-09-04T08:56:33Z` and named
  candidate `792b34d7...`. The diagnostic main process then returned
  `loaded_artifact_verified=false`, null release/manifest identity, and exited 1 at
  `2026-09-04T08:56:39Z`.
- The rollback diagnostic named exact release `0de19c1c...`, verified its manifest and completed
  successfully at `2026-09-04T08:57:46Z`.
- This reproduces the existing precheck/main-process identity-path discrepancy. The evidence is
  not sufficient to claim the candidate monitor ran with the staged immutable identity, so the
  activation failure is correct and remains fail-closed.

## Database and observation boundary

The pre-attempt baseline was `execution_running=30`, `execution_uncertain=0`, with all three new
lease tables empty. After rollback these counts remained 30/0;
`authoritative_execution_attempts=0` and `entry_assembly_wakeup_executions=0`. During the brief
candidate runtime, the scanner initialized five durable cursor rows. These are ordinary
candidate-runtime telemetry writes and were not modified after rollback.

Two raw messages arrived during the frozen interval and remained pending at the final read-only
checkpoint. No L2 observation was started because the candidate did not remain active. No
production schema action, existing-decision repair, manual exchange write, thaw, or second
activation attempt occurred.

