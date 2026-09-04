# Trigger-protection lineage production activation

Date: 2026-09-04

## Outcome

The reviewed immutable candidate
`877fbc33d783546ad2379b688c7648363a92c4a8` was activated for Web, monitor,
ingest, and worker with `schema_changed=false`. All three runtime identity
endpoints now report manifest
`92c0ca4aafc60f44b935c646d74d2e0cdcf2557ad0729a0918748826957e9242`,
`loaded_artifact_verified=true`, and the candidate commit. The monitor's natural
run independently reported the same verified candidate identity.

The 30-minute L2 window completed without a new position, open order, stuck
decision, scanner error, or runtime warning. Seven real messages from one chat
were received; the requested five-message target was met, but two-chat coverage
was not. No real entry occurred, so this window does **not** prove the new
protection-lineage attribution on a real fill. In addition, the production
setting `trigger_protection_lineage_attribution_mode` remained `disabled`; no
setting was changed in this deployment.

## Bound release and authorization

- Candidate manifest SHA-256:
  `92c0ca4aafc60f44b935c646d74d2e0cdcf2557ad0729a0918748826957e9242`.
- Candidate content SHA-256:
  `9914f268b84032b2801941ce3015342e39bfcd6fa86ac28b4a518497a8771be5`.
- Components: `web`, `monitor`, `ingest`, `worker`.
- Per-role rollback for all four roles:
  `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71`, manifest
  `7ac1c94946d58b8ed9eca52cef9ff6504582632ed799f14ad01e90a43f1a2468`.
- A new canonical v3 authorization bound action-plan SHA-256
  `5951dbeae165a05374b66d91ff9fc64544a87e18fd0944db5123313b6cb86400`
  and controller-bundle SHA-256
  `743c2478a4831b5237239588457daba5082604c333f7ae47e6eaf59be36789ae`.
  The source authorization disappeared and its mode-0400 consumed marker is
  present with SHA-256
  `928f0b24e6fba8f37cbc44709904076e8507d4a1a4ffc61b55141c3eb02974d7`.

Immediately before invocation, the loopback worker exchange snapshot was
complete with zero positions and zero open orders, all roles still matched the
reviewed rollback, and active exchange writes were zero. The standard helper
was invoked at `2026-09-04T16:10:04Z`. Its local SSH output channel timed out
after 30 seconds without returning a reusable session, so the command was not
retried. Completion was instead proven by the exactly-once consumed
authorization, systemd transition journal, candidate runtime identities, and
post-activation release validation; no helper return payload is claimed.

## Integrity and freeze interval

The pre-activation full-tree gate accepted all 37 retained immutable releases
with no `__pycache__` or `.pyc` path. The post-activation pass again accepted
37/37; the candidate reproduced the manifest and content digests above, and
bytecode-path counts remained zero.

The conservative freeze interval was `2026-09-04T16:10:04Z` through
`16:12:55Z`, or 171 seconds. The first thaw attempt at `16:11:49Z` incorrectly
looked for `ENTRY_ADMISSION_FROZEN`; the actual deployment variable is
`TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN`, so that attempt did not thaw the newly
started processes. A first correction command also made no change because its
exact-match expression was invalid. At `16:12:54Z` the exact single freeze line
was removed from each role drop-in, followed by daemon reload and the standard
worker → Web → ingest restart order; all three endpoints reported
`entry_admission_frozen=false` at `16:12:55Z`. This missed the 89-second target
but remained below the 15-minute stale-instruction threshold.

No raw message arrived during that conservative interval. No
`expired_stale_instruction` job completed in the interval, and the all-history
count remained 420. Therefore there is no frozen-window message ID, group,
message summary, or terminal expiry to enumerate for this activation.

## L2 observation

The fixed window ran from `2026-09-04T16:12:55Z` through `16:42:55Z` and was
not extended. Raw messages `14907`–`14913` arrived, all from one chat. Every job
ended `succeeded/worker_completed`; every decision ended `completed`, and every
new authoritative attempt (`94`–`100`) ended `succeeded` with
`exchange_effect=not_started`, `automation_status=skipped`, and
`automation_reason=mimo_no_action`.

The relevant counters were:

| Counter | Before | After |
| --- | ---: | ---: |
| `authoritative_execution_attempts` | 93 | 100 |
| `entry_assembly_wakeup_executions` | 0 | 0 |
| `recognition_execution_scan_cursors` | 5 | 5 |
| `comparison_status=execution_running` | 30 | 30 |
| `comparison_status=execution_uncertain` | 3 | 3 |

The prompt's expected uncertain baseline was 2, but the activation preflight at
`16:09:27Z`, before authorization creation and service control, measured 3.
That pre-existing row was not touched. All five durable scanner cursors advanced
and wrapped during the observation; no scanner failure or incident appeared.
The backlog-expiry guard implementation is byte-identical between rollback and
candidate, and no backlog-refusal/error was observed; no live maintenance
mutation was invoked merely to test the guard.

The read-only exchange snapshot remained complete with zero positions and zero
open orders throughout, using fingerprint
`e0f66201bc8350918de6835335b70f9c5ba216820a8bd80dba07848e32b66f4a`.
Consequently neither protection completeness nor binding transition to
`active`/`pos_id` had a real entry to validate. Root free space ended at
12,599,771,136 bytes; no abnormal decline occurred.

Final runtime PIDs are Web `1412196`, ingest `1412201`, and worker `1412175`,
all active with `NRestarts=0`. Entry admission is unfrozen and
`auto_trade_enabled=true`.

The timer's natural run at `16:31:29Z` verified the candidate identity but
returned exit 1 solely for the already documented
`stale_entry_preamble_unresolved` and `stalled_composite_component` business
residuals; it introduced no new reason code. The failed latch was cleared at
`16:43:40Z` without restarting the service. Final state is timer
enabled/active and main service inactive/dead, not failed.

## Evidence

Root-owned evidence is under
`/var/lib/telegram-kol-cutover-evidence/877fbc33d783546ad2379b688c7648363a92c4a8/protection-lineage-activation-20260904T161004Z`.
`summary.json` is mode 0600 with SHA-256
`e10bf7d18ea7ae92f5df64b9b8ffbd4506e84f3f4f0d6f41db2d33e1324a52a4`;
`post-release-integrity.txt` is mode 0600 with SHA-256
`2d97b33f82fc690f09673f236e274172f74bf20c5a8703d23173b19d11a42122`.

No schema action, manual exchange write, repair of the 30 legacy
`execution_running` rows, change to the three pre-existing uncertain rows, or
cleanup of historical intents/candidates/business residuals was performed.
