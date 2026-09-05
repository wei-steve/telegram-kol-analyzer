# Protection order side semantics production activation

## Scope and result

- Candidate: `af8676dca5ce83acfc060a8b856ccf3884f25150`.
- Stage manifest SHA-256: `5ae834ad537676e849b0be58c128fc9721ebf52f5ba993b84e329f4c68a97b28`.
- Components: `web`, `monitor`, `ingest`, `worker`; immutable source mode; `schema_changed=false`.
- Measured per-role rollback before activation: all four components were on
  `9501a5f39f0c5f196cc29f24f3e3b8786267126b`, manifest
  `2fed57c881a89c89916ebb2e08a378d0dc282a601c6b9266f3c8bd62bffce603`.
- The standard activation helper returned `status=activated`. No manual service
  control or identity-gate bypass was used.

## Authorization and immutable evidence

- A new root-owned mode-0400 canonical v3 authorization was generated for this
  activation. Its SHA-256 was
  `3cd10fa4238fa09583fe8d36718b3ae0d1d799f3b02068b4c0ae70876d439d0a`;
  its action-plan SHA-256 was
  `d0925eac6299bad24500cf98470745b2407f5f56ca10f212b638882a3258e6b6`.
  The standard helper consumed this authorization and left the canonical
  consumed marker. It was not the authorization used by the earlier dry-run.
- The pre-activation gate revalidated 39 immutable releases and found no
  `__pycache__`, `.pyc`, or `.pyo` path. The post-activation full-tree pass again
  validated 39/39 releases with zero bytecode pollution.
- The worker-owned bounded exchange snapshot immediately before activation was
  complete and had one position, zero regular open orders, and fingerprint
  `eb64273c29d5b075d6e162c4f752cb1f931d9f273c5e31e574ec2ea2b218de8d`.
  The exact target position and primary stop were visible before service
  control.

Detailed evidence is in
`/var/lib/telegram-kol-cutover-evidence/af8676dca5ce83acfc060a8b856ccf3884f25150/protection-side-activation-20260905T1619Z`.

## Freeze and stale-message check

- The activation command started at `2026-09-05T16:25:50Z`. The frozen release
  drop-ins were installed at approximately `16:25:58Z`; the unfrozen drop-ins
  were installed at `16:27:16Z`, and worker, Web, and ingest were active again
  at `16:27:17Z`. The effective frozen interval was therefore about 78 seconds.
- The standard activator completed its own thaw. A prepared exact-thaw helper
  was attempted while the post-activation ports were still starting, but it
  exited before any write or service control. Once the ports were available,
  all three runtime identities already reported `entry_admission_frozen=false`,
  so the helper was not rerun.
- No raw message arrived in the activation/freeze interval. The all-history
  `expired / expired_stale_instruction` count was 420 both before and
  immediately after activation, and no freeze-window stale-expiry row was
  found. Consequently there is no message ID, chat, text summary, or terminal
  status to report for this activation window.

## Immediate post-cutover protection result

- Binding 339 / entry leg 583 / position `1001125135694798` remained a BTC long
  position of six contracts. The verified primary stop
  `1001125135694875 @ 77500` remained present and full-position scoped; it was
  not cancelled or replaced.
- The deployed system itself created and verified a backup stop
  `1001125143685194 @ 77345`, also full-position scoped. This was an ordinary
  runtime protection action, not an operator or deployment write.
- Convergence 227 did not become ready and did not submit the three planned take
  profits. It moved from `waiting_backup_stop` to `conflicted`, with reason
  `convergence_pending_alias_conflict` at `2026-09-05T16:26:13.455876Z`.
  That code means the complete pending-TPSL snapshot contained at least one
  protection-bearing row whose position-side/order-side or other protected
  aliases were not internally consistent. The offending raw snapshot row is
  not persisted on the convergence record, so its exact field pair cannot be
  reconstructed from that row alone. The current cached exchange rows for the
  two BTC stops now show the valid combinations `posSide=long / side=sell`.

The candidate implementation was also applied directly to those two current
cached rows under `python -B`; both returned alias-consistent. This proves the
new long/sell rule is active, while also showing that the earlier conflicting
snapshot cannot safely be reconstructed as one of the current rows.

## L2 observation

The bounded observation ran from `2026-09-05T16:32:31.968188Z` through
`17:03:53.346158Z`, 31 minutes 21 seconds. It received three real messages from
two chats, short of the five-message target; the window ended on time and the
sample is recorded as traffic-limited.

| raw message | chat | job result | lease result |
|---:|---:|---|---|
| 15012 | -1003048800035 | `succeeded / worker_completed` | attempt 198 `succeeded`, `exchange_effect=not_started`, `skipped / mimo_no_action` |
| 15013 | -1003048800035 | `failed / processing_error:RuntimeError`, 5 attempts | attempt 199 `uncertain`, `exchange_effect=outcome_unknown`, `ExecutionBoundaryOutcomeUnknown / partial_failed` |
| 15014 | -1002344190971 | `succeeded / worker_completed` | attempt 200 `succeeded`, `exchange_effect=not_started`, `skipped / mimo_no_action` |

The four pre-existing uncertain attempts 6, 25, 77 and 191 remained byte-for-
byte identical in the database snapshots. A new uncertain attempt was
nevertheless created for raw 15013, so both the decision
`execution_uncertain` count and attempt `status=uncertain` count increased from
4 to 5; `execution_running` stayed zero. This means the no-new-uncertain
acceptance condition did **not** pass.

Raw 15013 was projected as a management `position_update` for lifecycle 1088,
candidate 2203 (`partial_then_break_even`, fraction 0.5). Instruction 981 and
management batch 159 stopped `blocked / protection_price_or_size_mismatch`
without creating a management leg, component, position-mutation intent, or
execution event. The lease had already crossed its exact side-effect fence and
the adapter returned `partial_failed`, so the candidate correctly preserved the
outcome as unknown rather than claiming a safe retry. Fresh exchange evidence
still showed the BTC position at six contracts with both stop orders and no
take-profit order, but that current snapshot does not erase the durable
uncertain classification. The new row was not modified or reconciled in this
deployment task.

One `/positions-panel` response at `16:52:47Z` contained no position or
protection cards while the bounded snapshot still reported one position. The
observer stopped fail-closed. A fresh independent GET at `16:53:05Z` returned
the exact target and both stops again; the protection ledger had also refreshed
the same two rows six seconds before the empty panel. The remaining ten minutes
were rerun with one reasoned GET retry and produced no further missing-
protection sample. This is recorded as a transient Web projection gap, not as
evidence that the exchange stop disappeared.

During the observation, an independent concurrent production probe outside
this deployment task submitted a 0.1-contract ETH short at `16:54:12Z`. Its
evidence directory is
`/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-no-clordid-test-20260905/live-3a22b4272f01`.
The resulting position `1001125143973255` had native combined TPSL order
`1001125143973254`, with stop 2482.62, take profit 2462.62, and full 0.1 size.
This task made no exchange write, but the unrelated probe is an explicit
contaminating event in the observation window.

At the final target-position check, convergence 227 remained
`conflicted / convergence_pending_alias_conflict`; no
`position_take_profit_orders` row existed for it. The BTC primary stop
`1001125135694875 @ 77500` and backup stop `1001125143685194 @ 77345` both
remained verified and full-position scoped, so protection did not worsen.
Binding 337 and legs 579/580 retained their `active` statuses and their existing
stale posIds; their rows were not cleaned, although normal reconciliation
advanced their `updated_at` timestamps.

All three HTTP runtime identities ended on candidate
`af8676dca5ce83acfc060a8b856ccf3884f25150`, manifest
`5ae834ad537676e849b0be58c128fc9721ebf52f5ba993b84e329f4c68a97b28`,
with `loaded_artifact_verified=true`, `entry_admission_frozen=false`, and
`NRestarts=0`. Final PIDs were worker 1874433, Web 1874438 and ingest 1874444.
`auto_trade_enabled=true`. The monitor timer remained `enabled/active`; natural
cycles at approximately `16:31Z` and `17:01Z` loaded the candidate. The monitor
main service continued to exit 1 only for the pre-existing
`stale_entry_preamble_unresolved` / preamble 16 business finding, with
`monitor_error=null`; as authorized, this was not treated as an activation
identity failure and no preamble data was changed.

The final correct stale-expiry query remained 420 rows (`status=expired` and
`last_reason=expired_stale_instruction`). Available root-disk space was
7,065,546,752 bytes at the terminal checkpoint, without an observation-period
step change attributable to this activation.
