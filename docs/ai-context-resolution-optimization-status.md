# AI Context Resolution Optimization Status

```yaml
workstream: ai_context_resolution_observability_and_network_backoff
phase_state: in_progress
current_phase: activation_with_transient_entry_freeze_then_immediate_thaw
claimed_by: null
base_sha: 387f638ba4afec26c106795724dcb27becdf30a7
change_a_sha: b1385ba4ab305d1406bea28bf12f987cbf5db546
change_b_sha: 18434b4552938ae3acb1160ad32618aab9c3ecf4
pushed_sha: 18434b4552938ae3acb1160ad32618aab9c3ecf4
production_sha_before: 6e2321cecbb3adf61d7a5972d391e662d4aea300
production_sha_after: 6e2321cecbb3adf61d7a5972d391e662d4aea300
source_mode: immutable
entry_admission_frozen_expected: false
auto_trade_enabled_expected: true
```

## Scope contract

- Add direct nullable observability to `context_resolution_attempts`.
- Replace only immediate provider network-error retries with durable backoff and a provider-keyed circuit.
- Do not change triggers, words, thresholds, context windows, whitelist/settings, prompts, first-pass inputs or trading semantics.
- Do not replay messages, reconcile/terminate lifecycle state or write to Deepcoin.

## Pre-implementation timing evidence

- Durable `next_attempt_at` uses UTC; the context worker runs once per default 60-second lifecycle-monitor cycle.
- Initial provider bound is two requests. Durable reanalysis defaults to three attempts and exhausts when the stored count has reached that bound.
- The message-processing/recovery 15-minute maximum age applies to raw messages missing authoritative decisions and expired message-job claims. A context failure leaves a fail-closed decision and its retry is handled independently; context retry has no age TTL.
- Exact per-provider-call timestamps were not historically persisted. For 174 completed rows with `attempts=2`, row insert-to-update proxy P50/P90/max is 8.777/56.444/47,124.634 seconds. Four have later reanalysis events. The 170-row no-trigger-event cohort is 8.362/50.230/153.399 seconds, with zero over 15 minutes.
- Default initial backoff is 5 seconds. With 60-second polling, uncontended due-to-send latency is approximately 5-65 seconds. No context retry age window exists, so the configured default puts zero rows outside such a window. This is a scheduler-contract conclusion; exact historical request intervals are unavailable.

## Rollback contract

Code rollback returns all services to the control release while leaving the five additive nullable columns in place. Old code ignores them. Physical column removal is not part of normal rollback and would require a stopped-service production-copy rehearsal and restore.

## Evidence log

- Design approved with an independent status file; the completed recovery ledger remains untouched.
- No production code, settings, database, service or exchange state had been changed when this file was created.
- Change A RED: the four exact observability tests failed at collection because `ContextProviderResult` and the five schema/write paths did not exist.
- Change A GREEN: the four exact tests passed; the focused migration/context/authority/replay/worker regression set passed `115 passed in 6.15s`.
- Change A exact-base review used base `387f638ba4afec26c106795724dcb27becdf30a7`. It confirmed the change is additive, no decision branch reads a new field, trigger order is passed from the already-computed tuple, provider usage is never inferred from bytes, and null telemetry retains cached-decision behavior. No Critical, Important or Minor finding remained before commit.
- Change A was committed independently as `b1385ba4ab305d1406bea28bf12f987cbf5db546`.
- Change B RED proved two pre-implementation gaps: a legacy retry row with nullable observability columns collapsed its provider request count from two to one, and a reanalysis network failure left both the source generation and the new generation queued. The exact tests failed with those two mismatches before the production fixes.
- Change B GREEN preserves legacy request numbering with an explicit `legacy_provider_usage_unavailable` entry and supersedes the source generation when the resolver has already persisted a new durable retry. The exact two tests passed, followed by the focused context/worker/replay/authority/migration set: `157 passed in 8.53s`.
- The required isolated-network-error case schedules request two durably, sends it on the next worker cycle at 65 seconds in the deterministic test, completes successfully, and yields the same decision object as the no-error baseline. It remains below 15 minutes even though context retry has no dependency on that message-recovery age gate.
- Change B exact-base review used Change A commit `b1385ba4ab305d1406bea28bf12f987cbf5db546`. The review confirmed provider-key isolation, a single half-open probe, no provider-call/count increment while the circuit is open, no conversion of unknown to no-action, no silent durable-row deletion, and unchanged immediate retry behavior for non-network contract correction. No Critical, Important or Minor finding remained.
- Final candidate full suite after the last production-code edit: `6749 passed, 4 skipped, 32 warnings in 413.26s`. The warnings are existing deprecation warnings; there were no failures.
- The implementation itself remained exactly two commits. This later status-only stop record is required because deployment evidence cannot causally exist inside the candidate commit that must be pushed before staging.

## Deployment stop record — 2026-08-31T23:32:12Z

- Change B is commit `18434b4552938ae3acb1160ad32618aab9c3ecf4`; the remote `codex/deepcoin-auto-trading-v1` ref matched that exact SHA before staging.
- The additive migration was rehearsed on an online, pinned production snapshot. The pristine backup is 819,150,848 bytes, SHA-256 `a9f94e7a4578d776d68ab4f43936a872e9003da675b181513d08f3fb25b430b5`, and passed `PRAGMA quick_check`. Before/after rehearsal counts were identical for `raw_messages`, `recognition_decisions`, `signal_candidates`, `strategy_lifecycles`, `execution_bindings`, `execution_events`, `message_processing_jobs` and `context_resolution_attempts`. The rehearsal copy passed `quick_check`, had zero foreign-key violations, and exposed exactly the five nullable columns with all 4,248 legacy rows still NULL.
- A non-root candidate-code preflight proved those five columns were the complete set of missing compatibility columns in production. The separately authorized L3 schema step then added exactly those columns. The same eight critical counts were unchanged across the step, all 4,248 legacy rows remained NULL in every new column, and the live database passed `PRAGMA quick_check=ok` afterwards. The rollback contract therefore remains “leave nullable columns in place”; no business row was edited.
- The first stage declared `schema_changed=true`. Static preflight of the standard activator proved it rejects that declaration with `L3 database activation requires a separate backup/integrity executor`; no activation was attempted with that receipt, which was rejected and preserved under the evidence directory. After the separate L3 schema step, a fresh inactive receipt was created for the runtime-only activation: tree `7c0a76b96c368c37eb151af52a55398bcdf72c73`, content SHA-256 `ce8737d1c4c5edd9985aa8cd03f5daee936511de991a13f0018c9c6f19545f9a`, manifest SHA-256 `e58bc07d59fdeb977482ee77c4a62343012371660001983803c9d8d7ea83bfdc`.
- The installer was rerun against the fresh receipt. Candidate and installed SHA-256 matched by exact filename for `telegram-kol-monitor.service`, `telegram-kol-monitor.timer`, `telegram-kol-monitor-diagnostic.service` and `telegram-kol-monitor-test-notification.service`; monitor env pointed to the candidate and `systemd-analyze verify` exited successfully, with only unrelated host-unit warnings.
- Activation was stopped before authorization creation or consumption. The immutable activator derives `require_authority` from the exact component set containing ingest/worker and unconditionally sets `preserve_entry_freeze=true`. It would therefore change current `entry_admission_frozen=false` to true. This conflicts with the owner requirement that entry admission remain unfrozen throughout activation. No component-set reduction, helper bypass, transient freeze, post-activation thaw or activator modification was authorized.
- The monitor installer was rolled back to `6e2321cecbb3adf61d7a5972d391e662d4aea300`; all four exact unit pairs still match, monitor env again names the control release, and its timer is active/waiting. Web PID 3,746,349, ingest PID 3,746,355 and worker PID 3,746,343 never restarted or changed release; all self-report exact control release, verified artifacts and `entry_admission_frozen=false`. Trading settings remain `auto_trade_enabled=true`.
- A complete ordinary monitor cycle after rollback loaded `6e2321ce...`, returned `healthy=true`, `reason_codes=[]`, `monitor_error=null` and `notification_status=not_needed`. A complete read-only exchange snapshot returned zero positions and zero regular open orders. The open-orders view returned exactly two current BTC conditional short entries, IDs `1001125071413372` and `1001125071413427`; lifecycle 1037 remains `pending_entry`, binding 321 remains `open/entry_order_pending`, and local legs 555/556 remain pending with those exact IDs.
- No activation authorization was created or consumed. No web/ingest/worker restart, Deepcoin write, message replay, settings change, trigger/window/word-list change, lifecycle/binding transition or order mutation occurred. Because the candidate never ran, there is no post-deployment context-resolution attempt and no claim that the five fields were live-written.
- Complete root-owned evidence is under `/var/lib/telegram-kol-cutover-evidence/18434b4552938ae3acb1160ad32618aab9c3ecf4/ai-context`. Resumption requires a separately reviewed activation mechanism that can preserve an already-unfrozen authority state while retaining the exact four-component and fail-closed gates.
- Three redundant 819,150,848-byte rehearsal working copies were removed after the pristine backup and successful migrated copy were safely retained in the evidence directory; no production database or business row was removed.

## Transient-freeze activation gate — 2026-09-01T00:53:49Z

- The owner approved one exact four-component activation with a transient deployment entry freeze, followed immediately by removal of the freeze from the web, ingest and worker release drop-ins and a worker -> web -> ingest restart. The completion condition remains all three roles self-reporting `entry_admission_frozen=false`; a terminal frozen state is not accepted.
- The deployment freeze is an entry-admission fence, not a global worker stop. `web_app.py` reads it once at process creation and uses it to withhold the message-processing worker (`ensure_message_processing_worker_mode`, lines 5454-5460). Entry submission and entry revision also reject on `deployment_entry_admission_frozen()` in `auto_trade_execution.py`. In contrast, the worker role starts `deepcoin_reconcile`, `strategy_management_worker`, `break_even_convergence_worker`, lifecycle monitoring and the other protection/management tasks independently of that flag (`web_app.py` lines 327-347 and 4637-4707).
- A filled trigger remains covered by the normal attribution/protection pipeline during the frozen runtime: the Deepcoin reconciliation loop applies the exchange snapshot and exact leg attribution, adopts saved trigger-protection intent evidence, and then invokes `submit_verified_trigger_backup_stops` (`execution_bindings.py` lines 420-452 and 1040-1049). The rescue path is governed by the live liveness/management setting, not by the deployment entry-freeze flag. With `auto_trade_enabled=true`, the current live rollout settings therefore remain effective during the transient freeze.
- The production rows support that code-level conclusion. Lifecycle 1037 is `pending_entry`; binding 321 is `open/entry_order_pending`. Legs 555 and 556 are pending `trigger_limit` entries for order IDs `1001125071413372` and `1001125071413427`, each with a stop request at 82,200. Each leg has one planned primary stop, one planned backup stop and three planned take-profit legs, plus its own pending `trigger_protection_intent` (IDs 160 and 161). If a trigger fills during the brief service stop/restart interval, protection is not instantaneous while the process is down; attribution and protection resume after the worker starts (the Deepcoin reconcile startup delay is 5 seconds and its normal interval is 30 seconds). The freeze itself does not suppress those paths.
- A fresh Deepcoin public ticker read returned `BTC-USDT-SWAP last=78620` with exchange timestamp `2026-09-01T00:53:49Z`. The two short-entry triggers are above that price by `(80510-78620)/78620 = 2.403968%` and `(81110-78620)/78620 = 3.167133%`, respectively. Because protection is not blocked by the freeze, the conditional stop rule “protection frozen and market close to trigger” does not apply; activation may proceed under the approved transient-freeze contract.
