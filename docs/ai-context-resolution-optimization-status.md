# AI Context Resolution Optimization Status

```yaml
workstream: ai_context_resolution_full_column_archive_r1
phase_state: blocked
current_phase: r1_activation_blocked_candidate_identity_unverified
claimed_by: null
base_sha: 387f638ba4afec26c106795724dcb27becdf30a7
change_a_sha: b1385ba4ab305d1406bea28bf12f987cbf5db546
change_b_sha: 18434b4552938ae3acb1160ad32618aab9c3ecf4
pushed_sha: 5c0ca501825163049da5062693fb46e5297e9e77
production_sha_before: 6e2321cecbb3adf61d7a5972d391e662d4aea300
production_sha_after: 18434b4552938ae3acb1160ad32618aab9c3ecf4
r1_base_sha: 51abb3177892c0ee0c8dd1cd249a083aa27d9abe
r1_code_sha: 5c0ca501825163049da5062693fb46e5297e9e77
r1_production_sha: null
r1_schema_columns_added: true
source_mode: immutable
entry_admission_frozen_expected: false
auto_trade_enabled_expected: true
entry_admission_frozen_observed: false
auto_trade_enabled_observed: true
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

## Production activation and live verification — 2026-09-01

- The existing runtime-only immutable release was reused; no new stage was needed. Its canonical receipt still declared `schema_changed=false`, tree `7c0a76b96c368c37eb151af52a55398bcdf72c73`, content SHA-256 `ce8737d1c4c5edd9985aa8cd03f5daee936511de991a13f0018c9c6f19545f9a`, and manifest SHA-256 `e58bc07d59fdeb977482ee77c4a62343012371660001983803c9d8d7ea83bfdc` for exact commit `18434b4552938ae3acb1160ad32618aab9c3ecf4`.
- Because the previous attempt had restored monitor to the control release, the candidate installer was rerun under the update lock after preserving the old env and four base units. All four installed unit SHA-256 values matched their candidate files by exact filename; the monitor env names the candidate and its manifest; the diagnostic-only arguments are present only on the diagnostic unit; retired arguments remain absent; `systemd-analyze verify` succeeded. Installer evidence is under `/var/lib/telegram-kol-cutover-evidence/18434b4552938ae3acb1160ad32618aab9c3ecf4/ai-context/activation-20260901T0100Z/monitor-install`.
- The activation manifest declared exactly web, monitor, ingest and worker with `schema_changed=false`, `production_data_mutation=false`, `exchange_write_semantics_changed=false` and source mode `immutable`. A canonical nine-field, root-owned mode-0400 authorization bound to action-plan SHA-256 `0ab07af5c317f297e0a4c927485206ccfd859d6eba10f5876b66e3bcc20606a3` was printed, persisted and consumed exactly once. The immutable helper activated the candidate with rollback release `6e2321cecbb3adf61d7a5972d391e662d4aea300`; its deployment diagnostic loaded the candidate, verified the artifact, completed all sources, ran no daily audit, and returned healthy with no reason codes.
- The activator's deliberate freeze was transient. Candidate frozen processes began serving at approximately `2026-09-01T00:59:32Z`; immediately after helper success the single freeze line was removed from each web/ingest/worker release drop-in, followed under the update lock by the required worker -> web -> ingest restart at `01:00:30Z` to `01:00:39Z`. No unrelated work occurred between activation and thaw.
- Final identities are web PID 657593, ingest PID 657598 and worker PID 657588. All three report exact release `18434b45...`, manifest `e58bc07d...`, `loaded_artifact_verified=true` and `entry_admission_frozen=false`. The web event loop is healthy; ingest listener and reconcile tasks are healthy; worker message processing and command tasks are healthy, and global exchange authority plus management, protection, close, TPSL and rescue capabilities are all true. `auto_trade_enabled` remained true throughout.
- Two complete ordinary monitor runs succeeded. The first legitimately performed the due daily management audit and returned `healthy=true`, `reason_codes=[]`; the post-live-context run had `audit_ran=false` and also returned healthy with no reasons. The monitor timer remains active/waiting.
- Read-only Deepcoin verification before and after the live sample returned zero positions, zero regular open orders, and exactly the same two BTC pending conditional entries: `1001125071413372` at 80,510 and `1001125071413427` at 81,110. Lifecycle 1037 remains `pending_entry`, binding 321 remains `open/entry_order_pending`, and legs 555-556 remain pending and mapped to those exact IDs. No fill occurred and no protection attachment was required during the window.
- Natural traffic produced context attempts 4252 and 4253 after activation. Both populated all five additive fields: `invocation_triggers_json`, `attempt_phase`, `provider_request_count`, `provider_usage_json`, and `request_component_bytes_json`. Attempt 4252 recorded one provider request, 15,320 total tokens and 39,324 request bytes, decided `new_thread`, and projected raw message 14182 to one `entry_signal` candidate. Attempt 4253 recorded one provider request, 11,646 total tokens and 31,782 request bytes, decided `hold`, and preserved raw message 14180 as non-strategy. In both cases the first provider request succeeded, with no error, no durable retry time and no extra request; this is the unchanged pre-existing success decision path, so the observability and backoff changes did not alter the decision.
- The live calls did not encounter a provider network error. The expected success-path behavior was observed: no backoff was scheduled and the provider circuit remained closed with zero consecutive transport failures. The network-error scheduling/open-circuit branch therefore retains its previously completed deterministic RED/GREEN and full-suite proof; this window does not claim a production fault-injection test.
- Root-owned evidence for activation, thaw, identities, monitor cycles, exchange readbacks and live context rows is under `/var/lib/telegram-kol-cutover-evidence/18434b4552938ae3acb1160ad32618aab9c3ecf4/ai-context/activation-20260901T0100Z`. No activation rollback was needed. The five nullable columns remain in place by contract, and no recovery ledger, recognition setting, trigger condition, threshold, context window, whitelist, historical message or existing exchange order was modified by the rollout procedure.

## `context_resolution_attempts` retention and pruning design — read-only measurement 2026-09-01

This section is a design and read-only measurement only. No row, schema, setting, service, release or exchange state was changed. The production database was opened with `sqlite3 -readonly`; UTF-8 byte counts use `length(CAST(column AS BLOB))`. The fixed measurement anchor is `2026-09-01 01:41:17Z`, so the 30/60/90-day cutoffs do not move between queries.

### Trigger-history ordering decision

There are 4,256 attempts. The 4,251 legacy attempts have no `invocation_triggers_json`; the five post-rollout attempts do. Every `request_summary_json` is valid JSON and all 4,256 rows contain the four complete inputs consumed by `requires_context_resolution`: `mimo_first_pass`, `saved_evidence`, `message_context` and `candidate_strategy_threads`.

The trigger reconstruction is deterministic and, for this dataset, semantically equivalent to the production invocation decision:

- `requires_context_resolution` and its eight trigger terms/order were introduced by `663fe8b386871f2b86fce610eb0baa9a066f2e7a` and those lines have not changed since the first stored attempt.
- `request_summary_json` is built from the same four objects immediately after that predicate is evaluated; all fields read by the predicate are retained.
- Re-running the exact current Python predicate against the five rows that also carry directly persisted trigger lists produced exact ordered-list equality for all five rows. The earlier SQL analysis is a faithful hand translation, but any production backfill must call the pinned Python predicate rather than treat the SQL translation as the write authority.

The only non-equivalence is provenance: a backfilled value is reconstructed after the event rather than captured during the event. It is trustworthy for the eight-trigger distribution, but it is not evidence that the other new telemetry was captured. Queries must continue to distinguish the legacy cohort by `attempt_phase IS NULL` / `provider_request_count IS NULL`; otherwise a blanket non-NULL coverage metric would be misleading.

| Sequence choice | Implementation cost | Main risk | Information retained/lost |
|---|---|---|---|
| A. Backfill the 4,251 legacy trigger lists, then archive/prune old requests | Medium, L3 data update: pin the predicate commit, preserve exact ID/input hashes, update only NULL trigger fields with compare-and-set, and verify all lists before request pruning | Mixing reconstructed and directly observed provenance unless the legacy cohort and reconstruction manifest remain explicit | Preserves exact eight-trigger history online. Full historic prompt/window replay is still lost from the live DB after request pruning, but remains available from the archive |
| B. Do not backfill, accept that old request inputs disappear | Lowest write complexity | Without an archive, the only source for the historic eight-trigger distribution and full resolver input is permanently lost | Keeps attempt/decision outcomes but loses online trigger reconstruction and full input replay. If a verified archive is retained, the loss is from the live DB rather than permanent |
| C. Keep all legacy requests forever and apply retention only to new rows | Lowest historical risk | Does not solve the existing 328.239 MB request payload; at the measured 9.41 MB/day, another approximately 282.3/564.6/846.9 MB can accumulate before a 30/60/90-day policy first reaches steady state | Preserves all old history online, but gives no immediate space or audit-copy reduction |

Recommended ordering is A only if online historical trigger analysis is still a requirement; otherwise use B **with a verified archive**, not destructive B. C is a temporary deferral, not a storage remedy. Backfilling directly into `invocation_triggers_json` does not pollute trigger counts because the output is exact, but its reconstructed cohort must never be presented as live-captured observability.

### Live column profile

The live database file is 820,178,944 bytes. `dbstat` attributes 334,442,496 bytes (40.78%) to the table body and 421,888 bytes to its indexes. The six pre-existing JSON columns contain 329,862,145 logical bytes, 98.63% of the table body. The three new JSON telemetry fields are immaterial to this storage issue: only five rows are populated and their combined payload is 2,238 bytes.

| JSON column | Non-NULL / NULL rows | Logical bytes | Share of six JSON | Row-size distribution |
|---|---:|---:|---:|---|
| `request_summary_json` | 4,256 / 0 | 328.239 MB | 99.508% | min 4,348 B; avg 77,123.91 B; max 161,598 B; 32 rows 1–10 KiB, 3,238 rows 10–100 KiB, 986 rows at least 100 KiB |
| `decision_json` | 2,778 / 1,478 | 1.369 MB | 0.415% | all non-NULL rows below 1 KiB; avg 492.97 B; max 987 B |
| `prompt_versions_json` | 4,256 / 0 | 0.196 MB | 0.059% | every row exactly 46 B |
| `trigger_event_json` | 266 / 3,990 | 0.033 MB | 0.010% | all non-NULL rows below 1 KiB; avg 122.96 B; max 131 B |
| `reanalysis_triggers_json` | 4,256 / 0 | 0.019 MB | 0.006% | 3,981 rows are `[]`; avg 4.48 B; max 78 B |
| `rejected_response_diagnostic_json` | 60 / 4,196 | 0.006 MB | 0.002% | all non-NULL rows below 1 KiB; avg 95.52 B; max 100 B |

`decision_json` is the durable explanation of the resolver's historical decision. Keeping all of it costs only 1.369 MB: 0.409% of the table body and 0.167% of the whole database. It should not be pruned. The same conclusion applies to the other four small JSON fields: at the current 30-day cutoff, pruning all five in addition to `request_summary_json` would recover only another 0.120 MB.

Row status distribution is 2,571 `completed`, 1,439 `exhausted`, 178 `superseded` and 68 `failed`. The request payload distribution is correspondingly 181.545, 131.169, 12.096 and 3.429 MB. Current code can use a completed unresolved attempt with non-empty `reanalysis_triggers_json` for a future reanalysis and state fingerprint, so a retention predicate must always exclude such runtime-eligible rows. At this measurement point there are zero such rows; all 256 rows older than 30 days pass this additional runtime-safety predicate.

### 30/60/90-day logical recovery

Only 256 rows (6.02%) are older than 30 days. The oldest attempt is `2026-07-27 09:38:38Z`; therefore no row is yet older than 60 or 90 days. Values below are logical JSON payload bytes. The main SQLite file will not shrink until a database rewrite.

| Column | 30 days | 60 days | 90 days |
|---|---:|---:|---:|
| `request_summary_json` | 11.101 MB | 0 | 0 |
| `decision_json` | 0.104 MB | 0 | 0 |
| `prompt_versions_json` | 0.012 MB | 0 | 0 |
| `trigger_event_json` | 0.003 MB | 0 | 0 |
| `reanalysis_triggers_json` | 0.001 MB | 0 | 0 |
| `rejected_response_diagnostic_json` | 0 | 0 | 0 |
| All six | 11.222 MB | 0 | 0 |

The preferred request-only 30-day policy retains 317.138 MB of current request payload. Using the observed table physical/logical ratio only as a planning estimate, a subsequent full SQLite rewrite would reduce the table by approximately 11.26 MB and the database from 820.179 MB to approximately 808.923 MB. A 60- or 90-day policy recovers nothing today and therefore does not address the present audit-copy cost.

The daily management audit creates and verifies two complete private snapshots. Under the same estimate, the 30-day request-only pruning would reduce bytes copied across those two snapshots by approximately 22.51 MB. A naive linear projection changes the observed 1.2 GB cgroup peak to about 1.184 GB, but this is low-confidence: source page cache, destination page cache, SQLite process memory and timing do not scale one-for-one. The defensible forecast is “roughly unchanged to modestly lower”; the next authorized implementation must measure the post-compaction peak rather than use 1.184 GB as an acceptance value.

### Ranked implementation plan for a later authorized L3 window

1. **P1 — archive + exact legacy-trigger backfill + 30-day request-only retention.** Recover 11.101 MB logically today and bound future request growth to a rolling window. Preserve every row, every `decision_json`, status/error/retry metadata and all small JSON. Archive the full request first, then backfill only the 4,251 NULL trigger lists using the pinned unchanged predicate, and finally replace only runtime-ineligible, pre-cutoff request payloads with an explicit archived marker. This needs a minimal reader guard so offline backfill/analysis treats the marker as archived rather than as an empty valid request; the current NOT NULL column must not be silently set to NULL or `{}`. A short write-maintenance window is recommended for the one-transaction compare-and-set; physical compaction is a separate stopped-service window.
2. **P2 — archive + 30-day request-only retention without trigger backfill.** Same 11.101 MB recovery and same runtime guards, with fewer live-row writes. Trigger reconstruction and full replay move to the archive. Choose this if online historical trigger queries are not required. The traceability loss is operational convenience, not evidence destruction, provided archive coverage is complete.
3. **P3 — new-row-only retention.** No immediate recovery and legacy payload remains permanently in the live table. It can cap only future generations after the selected age is reached. This is safe as a temporary hold but does not meet the current size/audit objective.
4. **Do not prioritize six-column pruning or whole-row deletion.** Six-column pruning gains only 0.120 MB beyond request-only at the current cutoff while deleting decisions and diagnostics. Whole-row deletion additionally destroys attempt/error/retry identity and index relationships for an unmeasured marginal benefit.

| Priority | Current recovery | Traceability loss | Required interruption | Rollback |
|---|---:|---|---|---|
| P1 | 11.101 MB logical; approximately 11.26 MB physical after compaction | Full requests leave the live DB but remain in the archive; trigger lists remain online with reconstructed provenance | Short write-maintenance window for backfill/prune; full writer stop only for physical compaction | Transaction rollback before commit; verified archive field restore after commit; full backup only for disaster recovery |
| P2 | Same as P1 | Full requests and trigger reconstruction leave online queries but remain recoverable from the archive | Short write-maintenance window for prune; full writer stop only for physical compaction | Transaction rollback before commit; verified archive field restore after commit |
| P3 | 0 today | None for legacy rows | None until future eligible rows are pruned; eventual physical compaction still needs a stopped window | Stop applying the future policy; no legacy restoration needed |
| Six-column / row deletion | At most another 0.120 MB at today's cutoff, plus unmeasured row/index overhead for deletion | Loses decision/diagnostic or entire attempt history | At least the same L3 write and compaction windows | More complex multi-column or row restoration; not justified by the marginal saving |

Archive format should be a root-owned mode-0700 evidence directory containing mode-0600, ID-ordered canonical JSONL compressed with zstd. Each record should contain at least attempt ID, raw message ID, context fingerprint, creation time, the original `request_summary_json` bytes and their SHA-256. Its manifest should bind source database backup hash, fixed cutoff, predicate/code commit, row count, exact ordered ID-list hash, min/max IDs and timestamps, uncompressed canonical-stream SHA-256, compressed-file SHA-256 and byte counts. Before any live update, verify decompression, both hashes, exact one-to-one ID coverage and per-row request hashes. Keeping one copy off the live database filesystem is preferable; no compression ratio is claimed until it is measured.

Rollback boundaries:

- Before commit, one `BEGIN IMMEDIATE` transaction provides full rollback. It must re-read the exact cutoff set, confirm no row is currently claimable/reanalysis-eligible, compare input hashes and update only the expected rows.
- After commit but before physical compaction, restore request bytes from the verified archive in a new bounded transaction; the original full database backup remains the disaster-recovery boundary.
- After compaction, field-level restoration is still possible from the archive but grows the DB again. Whole-database restore requires stopped writers and separate authorization; it must not overwrite writes that occurred after the backup.

SQLite is in WAL mode with `auto_vacuum=NONE`. Nulling/replacing large payloads can free pages for reuse, but it does not reduce the main file and can create a large WAL. Physical shrink therefore requires a full `VACUUM`-class rewrite. Reuse the established production SQLite backup path, require `quick_check=ok`, an empty foreign-key check and before/after critical counts, then stop all database writers for compaction. Allow at least one additional database-size temporary copy plus WAL and safety headroom, preserve ownership/mode, fsync, and rerun integrity/count checks before restart. Do not attempt online compaction against active ingest/web writes; leaving freed pages for later reuse is the no-downtime alternative, with the explicit tradeoff that file size and audit snapshot bytes do not fall until the maintenance window.

## Integrated full-column archive design — dependency removal, source reduction and complete history

This supersedes the 30-day retention proposal. The age-based plan is rejected: at 35 days of history it would recover only 11.101 MB while still requiring both a write-maintenance window and physical compaction. This section designs a full-column archive and a reference-only future write path. It is design and read-only sizing only; no code, schema, row, service, release or exchange state was changed.

### Dependency graph and release boundaries

The safe order is strict:

1. persist the exact eight invocation triggers for the legacy cohort;
2. persist the exact thread-ID projection and cut online readers over to it;
3. stop writing duplicate request payloads and write references/fingerprints instead;
4. archive every remaining full historical request, replace it with an explicit marker, then compact the stopped database.

Steps 2 and 3 share schema, parsers and tests, but the actual cutover should use two backward-compatible code releases rather than one authority jump:

- **Compatibility release R1:** add nullable reference/fingerprint columns through the existing L3 migration path; dual-write the full request plus the new fields; make the two worker readers prefer the new thread-ID column and use the old request only while that column is NULL; make the Web projection derive `context_message_count` from the new message references; add a strict tagged parser for legacy-full, reference-only and archived request storage. R1 does not change provider input or stop the old write.
- Run Step 1 and Step 2 as independent, evidenced L3 data transactions. Once all rows have exact thread IDs and runtime tests show zero fallback reads, the full request is no longer an online authority.
- **Reference-only release R2:** retain construction of the exact in-memory request and the existing context fingerprint, but store only the reference marker and new provenance fields. Remove the runtime fallback to request payload; an unexpected NULL thread-ID column fails closed instead of reopening the large JSON. This is the source reduction in Step 3.
- Step 4 is a separate L3 archive/marker transaction plus a stopped-writer physical-compaction window. It is not part of either deployment.

R1 contains the code support for Steps 2 and 3, but R2 is intentionally a second activation after the historical backfill proves coverage. Combining the two activations would make a failed or partial backfill share a failure boundary with the source cutover and is not worth saving one deployment. Step 1 and Step 2 may share one verified database backup and one operator window, but they retain separate transactions, before-images and acceptance receipts.

### Step 1 — exact trigger backfill

Use the pinned Python `requires_context_resolution` predicate as the only write authority for the 4,251 rows where `invocation_triggers_json IS NULL`. Do not write from the analysis SQL translation. The exact predicate is deterministic against the stored four input objects and has already matched all five directly observed rows.

- **Storage change:** approximately 177,877 bytes in total at the measured legacy distribution, averaging 41.84 bytes per row. No behavior or decision input changes.
- **Provenance:** leave `attempt_phase IS NULL` and `provider_request_count IS NULL` unchanged. Trigger queries may combine reconstructed and directly observed values; observability-coverage queries must continue to separate those cohorts.
- **Backup/transaction:** reuse the verified SQLite backup path; require `quick_check=ok`, zero foreign-key rows, exact 4,251-ID/hash manifest and complete preimages. In one `BEGIN IMMEDIATE`, re-read the NULL set, recompute from the pinned Python code, compare source hashes, update only still-NULL cells and require an exact 4,251 compare-and-set count.
- **Acceptance:** zero legacy NULL trigger cells; all five directly observed rows byte-identical; a second independent recomputation matches every stored ordered list; attempt/status/decision/request counts and hashes unchanged.
- **Rollback:** any pre-commit issue rolls back the transaction. After commit, restore only the 4,251 before-images in a separately authorized transaction; no whole-database restore while writers are active.

### Step 2 — remove the online request dependency

Add `candidate_thread_ids_json TEXT NULL` and define it as the sorted unique output of the existing recursive `_collect_candidate_thread_ids(request_payload)` over the **whole request**, not merely `candidate_strategy_threads`. This distinction preserves IDs found in reply links and active-strategy context. The live sample averages 9.29 IDs, P90 26, maximum 36; canonical storage averages 34.95 bytes per attempt.

The two worker reads have no hidden dependency on message text or any other request value:

- `build_context_state_fingerprint` currently loads the latest attempt only when the caller did not supply candidate IDs, projects thread IDs, unions them with durable `StrategyMessageLink` IDs and then reads current thread/lifecycle state.
- `build_redacted_exchange_state` always projects IDs from the latest attempt and unions them with IDs supplied by the caller before reading current thread/binding/protection state.

The implicit contracts that must remain exact are the recursive key set (`thread_id` and `strategy_thread_id`), sorted/deduplicated integer normalization, latest-attempt selection and union with already-linked IDs. Neither function may reconstruct candidates differently after cutover. Keep the existing `context_fingerprint` computation over the complete in-memory request; replacing it with a reference hash would alter cache/retry identity and is forbidden.

There are two non-trading consumers to remove from the large column as well. `web_queries.py` reads it only to calculate `context_message_count`; it should use the new message-reference column. `context_analysis_backfill.py` is an offline one-time tool, not an online decision path; after archival it must explicitly open the verified archive or fail closed with `request_payload_archived`, never treat an archive marker as `{}` or a valid empty request.

- **Storage/behavior:** about 0.149 MB for the historical thread-ID projection. The change is storage-only if exact old/new projection equality holds; any difference blocks cutover.
- **Backup/backfill:** use the same L3 controls as Step 1, but a separate `BEGIN IMMEDIATE` and exact all-row watermark. New R1 rows dual-write the projection, so the transaction only fills NULL rows and refuses any non-NULL mismatch.
- **Acceptance:** full-row old-vs-new ID equality, zero NULL/malformed projections at the watermark, both worker functions produce identical state fingerprints and redacted exchange state before/after, Web context counts are identical, static call-site review finds no online full-payload reader, and a runtime fallback counter remains zero through the observation window.
- **Rollback:** before R2, roll code back to request fallback and restore the new cells if necessary; the original request remains intact, so no archive restore is involved.

### Step 3 — reference-only future persistence

R2 must continue to build exactly the same request object and render exactly the same provider messages in memory. Persistence happens as a side effect after those bytes are fixed; it must not rebuild the live provider input from references. Store:

- `context_message_refs_json`: an ordered, role-preserving object containing chat ID plus `current`, chronological `messages` and `reply_chain` references, each as `[raw_message_id, message_id, evidence_version_id]`;
- `candidate_thread_ids_json`: the exact Step 2 projection;
- `rendered_prompt_sha256`: SHA-256 of the exact canonical provider `messages` array containing the existing system prompt and the already-rendered user prompt;
- `request_component_sha256_json`: hashes for `current_message`, `saved_evidence`, `message_context`, `candidate_strategy_threads`, `redacted_exchange_state` and `mimo_first_pass`, so a later mismatch can name the changed component rather than merely say that the whole prompt differs;
- a valid NOT-NULL marker in `request_summary_json`, exactly shaped as `{"contract":"context-resolution-request-storage-v1","storage":"reference_only"}`. It is neither NULL nor `{}` and the tagged parser rejects unknown shapes.

Read-only modeling over the live request shape found an average 37.73 message references (P90 51, maximum 54) and no unresolved reference lacking a raw-message ID. The modeled storage is:

| New persisted part | Average bytes/attempt |
|---|---:|
| Ordered message/evidence references | 741.70 |
| Six component SHA-256 values with keys | 529.00 |
| Exact thread-ID list | 34.95 |
| Rendered prompt SHA-256 | 64.00 |
| Explicit reference-only marker | 79.00 |
| **Total replacing the 77,123.91-byte request** | **1,448.65** |

This is a 98.12% reduction. At the accepted 9.41 MB/day request growth, the replacement grows approximately 0.177 MB/day, saving about 9.233 MB/day. This estimate deliberately includes component hashes because without them an overall hash mismatch cannot identify its cause.

Exact retrospective rendering is conditional, not guaranteed. Raw message text is available by ID and evidence rows are versioned, but active strategy state, candidate snapshots, exchange state and even edited raw-message content may later differ. Re-render with the exact prompt/code version, compare each component hash, then compare the final prompt hash. Equality proves byte-exact reproduction; inequality must return `not_exactly_reproducible` plus the mismatched component names. It must never silently substitute current state and claim historical equivalence. Full historical payloads remain exactly recoverable from the Step 4 archive; new reference-only rows intentionally retain verifiable provenance rather than a guaranteed replay copy.

This source change does **not** alter model input or the trading decision path if all of the following are enforced: the provider still receives the original in-memory request, system/user prompt bytes are byte-equal in RED/GREEN tests, context and state fingerprints remain unchanged, new fields are never read by trigger/decision logic, and reference/hash failure can fail persistence but cannot select a different decision branch. Feeding a re-rendered request to the live provider, changing fingerprint construction or rebuilding candidates from references would be a behavioral change and would raise the work above storage-only scope; none belongs in this plan.

- **Migration/rollback:** add nullable columns first and retain old-reader compatibility in R1. R2 is rollback-safe because the old release tolerates the new columns, but rows created under R2 contain explicit markers rather than full requests; rolling code back to an old reader that assumes full JSON is forbidden unless the compatibility parser is also present.
- **Acceptance:** byte-for-byte provider request equality on all trigger families, identical decisions/fingerprints, correct tagged markers, non-NULL references/hashes for every R2 row, and measured average/day growth near the modeled envelope without changing trigger, prompt, context-window or settings contracts.

### Step 4 — complete historical archive and live-column replacement

Freeze an exact attempt watermark after R2 is live. Rows at or below the watermark that still contain legacy full payloads form the archive set; newer R2 rows already contain reference-only markers. Build a dedicated root-owned archive under `/var/lib/telegram-kol-context-archives/<UTC-run-id>/` (directory mode 0700, files mode 0600), preferably on storage separate from the live database. Use one bounded SQLite archive file rather than a new general framework:

- table keyed by attempt ID containing raw message ID, context fingerprint, original `request_summary_json` bytes and per-record SHA-256;
- manifest containing source backup SHA-256, exact watermark, ordered ID-list SHA-256, row count, sum of source bytes, min/max IDs and timestamps, per-record digest aggregate, archive-file SHA-256 and the trigger/thread backfill receipts;
- `PRAGMA quick_check=ok`, exact one-to-one ID coverage, exact 328,239,382-byte baseline sum for the approved 4,256-row snapshot, and full per-row hash equality before the live database is writable;
- one independently retained compressed copy outside the live database filesystem. Provision approximately 340 MB for the uncompressed primary archive plus the secondary copy; the compressed size is recorded only after actual verified creation and is not guessed in this design.

The historical live marker is a valid tagged object, for example `{"archive_artifact_sha256":"<64 hex>","contract":"context-resolution-request-storage-v1","record_sha256":"<64 hex>","storage":"archive"}`. Its modeled size is 248 bytes/row. The offline loader resolves attempt ID plus record hash against the manifest/archive, verifies the original byte hash, and only then parses the request. Unknown/missing archive, hash mismatch or duplicate record fails closed.

After archive verification, stop all database writers. In one `BEGIN IMMEDIATE`, re-read the watermark set and source hashes, replace exactly those full payloads with their markers, and assert `decision_json`, statuses, fingerprints and all unrelated tables are unchanged. At the accepted baseline:

- request payload removed: 328.239 MB;
- archive markers retained in the live DB: approximately 1.055 MB;
- exact thread-ID backfill retained: approximately 0.149 MB;
- exact trigger backfill retained: approximately 0.178 MB;
- **net logical live-DB recovery:** approximately **326.857 MB**.

Using the previously measured table physical/logical ratio only as a planning estimate, stopped physical compaction should reclaim approximately 331.396 MB and reduce the 820.179 MB database to about 488.783 MB. The two daily-audit private snapshots would together copy approximately 662.792 MB fewer bytes. A naive size-linear projection moves the observed 1.2 GB cgroup peak toward 0.715 GB, but this is not a promised memory result: the other large tables, SQLite/process memory and page-cache timing remain. The acceptance criterion is a post-compaction production audit measurement, not the projection.

Reuse the existing verified backup, integrity and `VACUUM` path. Production evidence shows an approximately 805 MB backup plus durability/hash/integrity work can exceed 30 seconds, so do not plan a 30-second outage. Rehearse the exact marker update and compaction on a current production copy, then reserve a 10-minute stopped-writer window for final hash recheck, one transaction, checkpoint, compaction, `quick_check`, foreign-key/count verification, ownership/mode/fsync and restart. The expected core rewrite is tens of seconds to low minutes; the rehearsal sets the fail-closed upper bound.

- **Rollback before marker commit:** transaction rollback leaves the live DB unchanged.
- **Rollback after commit but before restart:** because writers remain stopped, restore the verified pre-change database backup or restore request bytes from the archive and re-verify.
- **Rollback after service restart:** do not replace the whole database and discard newer writes. Restore only archived request bytes in a new authorized transaction if needed; R1/R2 code rollback must retain marker awareness.
- **Final acceptance:** every pre-watermark attempt is represented exactly once in the archive; every corresponding live row has the correct explicit archive marker; every later row has the explicit reference-only marker; no online code reads a full request; `decision_json` and all business rows are byte/count stable; archive retrieval reproduces sampled and boundary payloads exactly; database integrity is clean; physical size and the next daily-audit peak are recorded.

## R1 implementation checkpoint — local candidate 2026-09-01

- R1 code is commit `5c0ca501825163049da5062693fb46e5297e9e77`, exact-base reviewed from `51abb3177892c0ee0c8dd1cd249a083aa27d9abe`. It adds only the four nullable compatibility columns `context_message_refs_json`, `candidate_thread_ids_json`, `rendered_prompt_sha256` and `request_component_sha256_json` through `SQLITE_COMPAT_COLUMNS` and the ORM model. No trigger or thread-ID backfill, request archive, marker rewrite, `VACUUM` or R2 source cutover exists in this candidate.
- RED was recorded before implementation: the dedicated storage-contract test failed at collection because `telegram_kol_research.context_request_storage` and all four schema/write paths did not exist. GREEN passed the exact six new acceptance tests, then the context/migration/worker/Web/offline-backfill regression set passed `147 passed`; a broader authority/window/Web set passed `189 passed`.
- New attempts keep `request_summary_json` as the complete non-empty legacy request and additionally persist ordered message/evidence references, the whole-request recursive sorted unique thread-ID projection, the SHA-256 of the exact provider messages array and six named request-component SHA-256 values. The provider message builder is shared by the live call and persisted hash, while the existing request object, context fingerprint, trigger predicates, prompt text and decision parser are unchanged.
- Both online worker readers select `candidate_thread_ids_json` when it is non-NULL and parse it as a sorted unique integer list. Only a SQL NULL activates the legacy full-request fallback. The Web projection selects the new chronological message references for `context_message_count`, with its legacy fallback retained for historical NULL rows.
- The tagged parser distinguishes untagged `legacy-full`, exact `reference-only` and exact `archived` markers; unknown or extra tagged fields and invalid archive hashes fail closed. Worker fallback requires a legacy full request, and the offline analysis backfill also requires legacy-full storage, so an archived marker is never accepted as an empty request.
- Independent exact-base review reported **no findings** and passed its own `113` focused tests. The final full suite ran after the last production-code edit and passed `6753 passed, 4 skipped, 32 warnings in 447.81s`; warnings are the existing deprecation warnings.
- At this local-only checkpoint, production had not yet been touched by R1; the subsequent production attempt and its terminal state are recorded immediately below.

## R1 production attempt — fail-closed rollback 2026-09-01

- The separate L3 schema step completed before staging. The verified root-owned mode-0600 backup is `/var/lib/telegram-kol-cutover-evidence/4284d1a61226eb16812407c4f2489a207241db4c/ai-context-r1-20260901T030531Z/pre-r1-schema.db`, size 812,068,864 bytes, SHA-256 `da30f56e45ccc9d185d83c2d713ad5f2e3bf54cba8ff5ecedfd48aba5b78ea05`. It passed `PRAGMA quick_check=ok` and had zero foreign-key violations. The migration ran from an independent archive of reviewed code with `python -B`, under the runtime-control lock and one `BEGIN IMMEDIATE`; it did not import an immutable release or the mutable checkout.
- Exactly the four R1 columns were added: `context_message_refs_json`, `candidate_thread_ids_json`, `rendered_prompt_sha256` and `request_component_sha256_json`. All 4,271 historical rows remained NULL in all four columns. Before/after counts were identical for `context_resolution_attempts=4271`, `execution_bindings=321`, `execution_events=3883`, `message_processing_jobs=2443`, `raw_messages=14204`, `recognition_decisions=14203`, `signal_candidates=2124` and `strategy_lifecycles=1038`; post-migration integrity remained clean. These additive nullable columns are retained under the approved rollback contract.
- A fresh immutable runtime-only stage was created at exact pushed HEAD `4284d1a61226eb16812407c4f2489a207241db4c`, with `schema_changed=false`. Its tree is `0abf86970133f2d6d67bb21b42e9cd2436cf605d`, content SHA-256 `49ac8d529dcf3bdc2bf2e4951575413f26c84724862df9175be8c4ad4e72fe42`, manifest SHA-256 `81d5ffb5cfe8d713f9cc21b84ecabda0e93e897c5dfea9439f28bb60328b3167`, and stage action-plan SHA-256 `aa8f2bdc71c3be810f02562f7137902b889722a2e36ce3e27fee9a8f0708f48b`. No post-receipt command imported or executed release code.
- The candidate monitor installer completed under the update lock after byte-preserving backups. The four installed base units matched the candidate by exact filename and SHA-256; the diagnostic-only arguments were present only on the diagnostic unit, retired arguments were absent, monitor env pointed to the candidate, and `systemd-analyze verify` passed. The monitor timer's prior enabled/active state was preserved.
- The pre-activation read-only price gate passed at `2026-09-01T03:13:02Z`: BTC-USDT-SWAP last was 78,444.2, with 80,510 and 81,110 respectively 2.633464% and 3.398339% above market. Neither distance was below 1%.
- The activation manifest declared exactly web, monitor, ingest and worker, source mode `immutable`, rollback `18434b4552938ae3acb1160ad32618aab9c3ecf4`, `schema_changed=false`, no production-data mutation and no exchange-write semantics change. Its canonical nine-field authorization was root-owned, mode 0400, printed and preserved with the exact manifest before being consumed once; the activation action-plan SHA-256 was `0ab07af5c317f297e0a4c927485206ccfd859d6eba10f5876b66e3bcc20606a3`.
- Activation failed at the candidate deployment diagnostic and was not retried. The oneshot started and completed business-source collection, reporting `healthy=true`, `reason_codes=[]`, `adapter_failures=[]`, `sources_complete=true`, `result_complete=true` and `audit_ran=false`, but returned exit 1 because `loaded_artifact_verified=false`, `release_commit=null` and `manifest_sha256=null`. The exact unresolved blocker is therefore candidate runtime-identity proof, not a business-health reason. Per the fail-closed gate, the helper rejected the candidate and completed rollback to `18434b4552938ae3acb1160ad32618aab9c3ecf4` with exit code 4. No second activation, release repair or diagnostic workaround was attempted.
- The rollback initially retained the deliberate deployment freeze. A first thaw edit incorrectly matched an unquoted environment line and changed nothing; the immediate identity check correctly caught all three roles still frozen. Reading the exact drop-ins showed the quoted line `Environment="TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1"`. The corrected bounded edit removed exactly that line from web, ingest and worker, then performed `daemon-reload` and the required worker -> web -> ingest restart under the update lock.
- The final runtime is the rollback release: web PID 944570, ingest PID 944575 and worker PID 944565 all report release `18434b4552938ae3acb1160ad32618aab9c3ecf4`, manifest `e58bc07d59fdeb977482ee77c4a62343012371660001983803c9d8d7ea83bfdc`, `loaded_artifact_verified=true` and `entry_admission_frozen=false`. Web event-loop, ingest listener/reconcile, and worker command/message-processing health are true with zero observed stalls. Trading settings report `auto_trade_enabled=true`; monitor's rollback release drop-in names `18434b45...`, and its timer is enabled and active. R1 never became the running production release, so there is no live dual-write sample and no deployment-success claim.
- The owner-provided order premise had already drifted before this activation. Final read-only evidence shows zero positions, zero regular open orders and zero pending BTC triggers. Both exact trigger IDs remain in exchange history with no fills and exchange `uTime=2026-09-01T02:02:38Z`, more than an hour before the `03:14–03:15Z` activation. Local state matches that earlier terminalization: lifecycle 1037 is `expired` with `exit_reason=expired` and `management_action=expiry_cancelled_and_expired`; binding 321 is `closed/entry_legs_terminal`; legs 555 and 556 are `cancelled`. This deployment did not submit, cancel or modify a Deepcoin order.
- No Step 1 trigger backfill, Step 2 thread-ID backfill, Step 4 archive/marker transaction, `VACUUM`, R2 source cutover, setting change, prompt/threshold/window change, message replay or business-row repair was performed. Complete production evidence is rooted at `/var/lib/telegram-kol-cutover-evidence/4284d1a61226eb16812407c4f2489a207241db4c/ai-context-r1-20260901T030531Z`.

### Behavior classification

| Part | Storage change | Possible behavior change |
|---|---|---|
| Trigger backfill | Yes, one small nullable telemetry cell per legacy row | None; predicate output is persisted, not consumed to change a decision |
| Thread-ID projection/read cutover | Yes | None only if exact projection/state-fingerprint equivalence passes; otherwise cutover is blocked |
| Reference/hash source write | Yes, large duplicate replaced by about 1.45 KB | None for live model/decision bytes; retrospective exact replay becomes conditional for future rows and is explicitly reported as such |
| Historical archive/marker/compaction | Yes, approximately 326.857 MB logical recovery | No trading behavior; Web/offline diagnostics must display/resolve archive state explicitly rather than treating it as empty context |

No step changes `requires_context_resolution`, its vocabulary/order, context-window limits, prompts, settings, model input, decision parsing, exchange-write semantics or the two existing BTC conditional orders and their lifecycle/binding state.
