# Unified Execution Truth Legacy Impact Ledger

## Purpose and boundary

This ledger freezes the legacy execution-state surface before the additive
execution-contract rollout. It inventories production readers and writers by
module and classifies their relationship to the future contract. Model
declarations and database bootstrap definitions are listed separately because
they define storage rather than runtime authority.

Classifications:

- `authoritative_writer`: owns a current domain truth and remains authoritative.
- `compatibility_mirror`: continues writing a legacy projection during rollout.
- `presentation_reader`: renders operator or web output but must eventually read
  contract terminality.
- `monitor_only`: observes state and may alert or reconcile, but must not grant
  execution authority.
- `retire_after_shadow`: legacy terminal inference to remove after shadow parity.

## Instruction item and result-shape surface

| Module | Fields / shape | Read or write | Classification | Migration note |
| --- | --- | --- | --- | --- |
| `authoritative_recognition.py` | item existence/identity | read | `compatibility_mirror` | Candidate projection remains authoritative; contract creation occurs only after this step. |
| `message_instruction_items.py` | `status`, `result_json`, `result.status/reason/submitted` | read/write | `compatibility_mirror` | Claim/finish stays as queue compatibility; default terminal inference is retired after enforcement. |
| `auto_trade_execution.py` | finish status derived from result shape | write | `retire_after_shadow` | `_instruction_finish_status` must stop treating unknown/deferred shapes as success. |
| `strategy_management_worker.py` | finish status and result shape | write | `compatibility_mirror` | Management execution will dual-write contract transitions before legacy finish. |
| `context_resolution_worker.py` | pending/executing instruction state | read | `monitor_only` | May wake work but cannot declare execution success. |
| `entry_assembly_admission.py` | pending entry item lookup | read | `monitor_only` | Admission controls context readiness only. |
| `management_message_targets.py` | item identity/linkage | read | `compatibility_mirror` | Target execution state remains a scoped projection, not global proof. |
| `message_recognition.py` | pending item retirement/projection | read/write | `compatibility_mirror` | Recognition authority remains upstream of the contract. |
| `message_operation_contracts.py` | item status/result | read | `monitor_only` | Operation contracts remain monitoring state only. |
| `message_operation_supervisor.py` | item status/result | read | `monitor_only` | May detect stalls/escalate; cannot mutate exchange or certify success. |
| `position_management_remediation.py` | item status/result and candidate linkage | read | `monitor_only` | Remediation evidence feeds diagnostics only until explicitly admitted. |
| `runtime_incident_snapshot.py` | item status/result snapshot | read | `monitor_only` | Runtime incident agent stays read-only. |
| `system_operator_bot.py` | blocking/pending item state | read | `presentation_reader` | Operator wording will migrate to contract state. |

`models.py` declares `MessageInstructionItem`; `db.py` supplies additive columns
and indexes. They are storage definitions and therefore outside the runtime
authority classifications above.

## Trade signal surface

| Module group | Read or write | Classification | Migration note |
| --- | --- | --- | --- |
| `trade_signals.py` | read/write `TradeSignal.status` and claims | `authoritative_writer` | Remains the idempotent writer-queue guard. It is not exchange proof. |
| `auto_trade_execution.py`, `deepcoin_execution_actions.py`, `recovery_live_submit.py`, `strategy_management_executor.py`, `strategy_management_worker.py` | read/write submission outcomes | `compatibility_mirror` | Writer-boundary transitions attach request/readback evidence. Recovery must never retry unknown outcomes. |
| `source_message_deletion.py`, `source_message_deletion_worker.py` | read/write cancel/exit signals | `compatibility_mirror` | Deletion execution remains authoritative only inside its admitted action. |
| `entry_assembly_fingerprint_repair.py`, `historical_state_repair.py`, `terminal_entry_cleanup.py` | historical read/repair | `retire_after_shadow` | Historical rows stay read-only for this rollout; no bulk replay. |

`models.py` only declares storage. No other production module directly matching
`TradeSignal` may use its status as exchange verification; the remaining
matches are covered below as execution readers, lifecycle readers, or monitors.

## Lifecycle surface

| Module group | Read or write | Classification | Migration note |
| --- | --- | --- | --- |
| `lifecycle_monitor.py` | price-derived `lifecycle_status` writer | `authoritative_writer` | Remains analytical market-state truth; `entered` alone is never exchange proof. |
| `message_recognition.py`, `strategy_records.py`, `strategy_threads.py`, `strategy_thread_candidates.py` | lifecycle projection/linkage | `compatibility_mirror` | Preserve contextual targeting and lifecycle history. |
| `execution_bindings.py`, `strategy_management_reconciliation.py`, `entry_revision_executor.py`, `strategy_revision_planner.py`, `recovery_live_submit.py`, `lifecycle_exit_intents.py` | reconcile lifecycle from execution evidence | `compatibility_mirror` | Contract verification consumes their binding/readback evidence; it does not replace targeting. |
| `management_scope.py`, `management_message_targets.py`, `strategy_management_planner.py`, `entry_revision_planner.py`, `break_even_convergence_planner.py`, `semantic_disagreement_review.py` | eligibility/planning reads | `monitor_only` | Planning must not convert lifecycle state into verified execution. |
| `web_app.py`, `web_queries.py`, `telegram_bot_commands.py`, `cli.py`, `strategy_alerts.py` | status display | `presentation_reader` | UI migrates after shadow dual-write parity. |
| `historical_state_repair.py`, `historical_attribution_cleanup.py`, `position_attribution_repair.py`, `media_retention.py` | historical/maintenance reads | `retire_after_shadow` | Do not rewrite historical records during future-watermark rollout. |

Other lifecycle matches in `contextual_message_window.py`,
`context_resolution_worker.py`, `management_scope.py`, and
`runtime_incident_snapshot.py` are `monitor_only`: they provide context or
diagnostics and cannot establish exchange terminality.

## Binding and order-leg surface

| Module group | Fields | Read or write | Classification | Migration note |
| --- | --- | --- | --- | --- |
| `execution_bindings.py` | `ExecutionBinding.status/last_exchange_status`; `ExecutionOrderLeg.status/attribution_status` | read/write | `authoritative_writer` | Continues as exchange/ownership truth; verified contracts reference binding evidence. |
| `deepcoin_execution_actions.py`, `auto_trade_execution.py`, `strategy_management_executor.py`, `recovery_live_submit.py`, `source_message_deletion_worker.py` | binding/leg submission and reconciliation | read/write | `authoritative_writer` | Existing writer boundaries remain; contract transitions wrap them without new authority. |
| `position_mutation_gateway.py`, `position_mutation_authority.py`, `management_scope.py` | exact-position ownership gates | read | `authoritative_writer` | These gates remain mandatory and cannot be bypassed by contract state. |
| `entry_revision_executor.py`, `break_even_convergence_executor.py`, `trigger_backup_stop_executor.py`, `trigger_take_profit_convergence_executor.py`, `legacy_conditional_cancel.py`, `native_tpsl_migration.py` | order mutation/readback | read/write | `compatibility_mirror` | Attach evidence to the admitted action; do not broaden target scope. |
| `strategy_management_reconciliation.py`, `position_management_liveness_recovery.py`, `protection_incident_convergence.py`, `current_protection_backfill.py`, `entry_protection_ledger_repair.py`, `tpsl_ledger_backfill.py` | reconcile/repair | read/write | `monitor_only` | Existing repair authority stays bounded; no automatic retry after unknown submit. |
| `message_operation_supervisor.py`, `runtime_incident_scanner.py`, `runtime_incident_snapshot.py`, `system_operator_bot.py` | status inspection | read | `monitor_only` | Alert and explain only. |
| `web_app.py`, `web_queries.py`, `cli.py`, `telegram_bot_commands.py` | operator display | read | `presentation_reader` | Presentation switches after shadow parity. |
| historical/backfill modules (`historical_state_repair.py`, `position_attribution_repair.py`, `management_history_recovery.py`) | old binding/leg state | read/write | `retire_after_shadow` | Excluded from future contract backfill and replay. |

The following specialized readers are explicitly in scope as `monitor_only` or
`compatibility_mirror` according to whether they only inspect or execute an
already-authorized action: `backup_stop_repair.py`,
`break_even_convergence_worker.py`, `entry_assembly_fingerprint_repair.py`,
`position_attribution.py`, `position_protection_legs.py`,
`position_take_profit_orders.py`, `recovery_execution_queue.py`,
`recovery_live_submit_gate.py`, `strategy_management_batches.py`,
`terminal_entry_cleanup.py`, `trigger_protection_intents.py`, and
`trigger_take_profit_convergence.py`.

## Confirmed legacy regression shapes

Two production admissions shared the same anonymized persisted shape:

```text
instruction item: status=succeeded
result: status=deferred, reason=adjacent_entry_context_pending, submitted=false
assembly attempt: status=pending
trade signal: absent
execution binding: absent
```

One record came from the affected strategy source and one from another group;
message text, chat identifiers, credentials, and order details are deliberately
excluded. A separate legacy shape is a lifecycle marked `entered` by price
monitoring while `execution_binding_id` is null. Both shapes are valid
historical evidence but are not verified exchange outcomes.

## Change history and rollout exclusions

- `d168fdf` introduced adjacent-entry admission.
- `b72f776` extended the admission/wakeup path.
- `1269fa3` supplied the P0 repair: all-null strategy evidence is non-actionable,
  adjacent deferral remains pending, and wakeup targets the exact item.
- Existing rows are read-only. The execution contract begins above a fresh
  watermark; there is no history replay, bulk rewrite, or synthetic live order.
- Message operation contracts and the runtime incident agent remain monitoring
  only. Neither may target strategies, place orders, or certify terminality.

## Source-search reconciliation

The review search is:

```bash
rg -n "MessageInstructionItem|lifecycle_status.*entered|TradeSignal.status|finish_message_instruction_item" src/telegram_kol_research
```

Matches in `models.py` and `db.py` are schema definitions. Matches in web,
operator, CLI, and alert modules are `presentation_reader`; matches in runtime,
supervisor, scope, planning, and context modules are `monitor_only`; queue and
legacy completion writers are `compatibility_mirror` or
`retire_after_shadow`; exchange binding/leg and exact-position ownership
writers remain `authoritative_writer`. This exhausts the search without
granting the new contract strategy-selection or exchange-write authority.
