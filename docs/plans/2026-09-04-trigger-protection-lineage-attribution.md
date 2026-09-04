# Trigger Protection Lineage Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the invalid live-position timestamp ownership gate for exchange-attached trigger-entry stops with a strict, account-wide, mutually unique lineage proof while preserving fail-closed behavior and existing idempotent protection convergence.

**Architecture:** Build one immutable lineage attestation from the saved parent submission, owner-specific pre-submit TPSL baseline, verified parent-to-child-to-`posId` fill chain, and current complete exchange snapshot. Feed it into the existing pure account-wide bipartite assignment, atomically finalize only mutually unique assignments, and expose first-refusal incidents before management becomes blocked. Keep the legacy timestamp rule for candidates that lack the complete attestation.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, pytest, Deepcoin REST client abstractions, existing protection ledger/mutation gateway/runtime incident monitor, immutable systemd split runtime.

---

## Safety and authorization rules

- This is L3 work because a successful local adoption unlocks existing live backup-stop and take-profit executors.
- Use test-driven development for every behavior change and systematic debugging for unexpected failures.
- Do not change recognition, strategy targeting, stop prices, TP allocations, risk sizing, or exchange request semantics.
- Do not call a real Deepcoin write endpoint from development or test tasks. Use fake clients only.
- Do not replay raw messages `14382` or `14770`, and do not reopen batches `152` or `157`.
- Keep future-only activation separate from historical repair. A production deployment must start disabled.
- Never use `git add -A`; stage only the files named in the current task and inspect `git diff --cached --name-only`.
- Another session may be changing the shared branch. Before implementation, wait for the owner to identify the final integration head, then re-check `pwd`, branch, `HEAD`, worktree and overlap. Stop on overlap.
- Any script importing an immutable release must use `python -B` or `PYTHONDONTWRITEBYTECODE=1`.
- Each task below is an independent authorization boundary. Do not continue to the next authorization group merely because the preceding task passed.

## Authorization group 1: local characterization and pure ownership logic

### Task 1: Freeze production-shaped regression fixtures

**Files:**

- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `tests/test_trigger_protection_assignment.py`
- Add: `tests/fixtures/trigger_protection_lineage_cases.json`

**Step 1: Add redacted fixtures for the two failures and one success control**

Include only bounded, non-secret fields for:

- lifecycle `1050` / binding `324` / leg `559` / intent `163` / parent `1001125090052318` / child-pos `1001125090080799` / stop `1001125090080798`;
- lifecycle `1072` / binding `336` / leg `577` / intent `177` / parent `1001125112808467` / child-pos `1001125113096711` / stop `1001125113096710`;
- binding `336` / leg `578` / intent `178`, whose stop was adopted and whose backup stop and TPs converged.

Record exact request fingerprints, owner baselines, parent requests/responses, binding submitted-order attestation, unique child regular-order identity and exchange `cTime`, live position identity, candidate shape and timestamps. Do not include chat content, KOL contact details, API responses unrelated to these objects, or credentials.

**Step 2: Write characterization tests for the current behavior**

Assert the two failure fixtures currently return `trigger_protection_candidate_predates_fill`, while the control remains adopted. Also assert the candidate is absent from its own pre-submit baseline and present in the complete post-submit pending snapshot.

**Step 3: Run the focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_trigger_protection_assignment.py \
  -k "lineage_fixture or candidate_before_position" -q
```

Expected: characterization tests pass without production-code changes.

**Step 4: Commit only after the owner separately authorizes implementation**

```bash
git add \
  tests/fixtures/trigger_protection_lineage_cases.json \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_trigger_protection_assignment.py
git diff --cached --name-only
git commit -m "test: capture attached stop lineage regressions"
```

### Task 2: Define the closed lineage attestation builder

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write failing positive tests**

Add tests for `build_trigger_protection_lineage_attestation(...)` requiring:

- exact intent/leg/event/binding identity;
- exact parent order ID and client order ID;
- successful parent response;
- identical request fingerprints across intent, leg, event and binding submitted order;
- exact stop-only protection request and persisted attached marker;
- verified unique parent-child-`posId` chain;
- parseable child regular-order exchange `cTime`;
- exact current live position;
- valid owner-specific complete baseline.

The positive result must be an immutable value object with a stable bounded fingerprint.

**Step 2: Write failing negative tests**

One test per closed failure reason:

- missing or multiple binding `submitted_orders` rows;
- parent order/client/order response mismatch;
- request fingerprint mismatch;
- `code`/`sCode` not successful;
- `attached_on_trigger_order` missing/false;
- TP+SL or unexpected protection shape;
- parent child absent/multiple/not filled;
- child exchange `cTime` missing or malformed;
- child `posId` conflict;
- current position missing/duplicate/size drifted;
- baseline malformed or unavailable.

Every negative test must assert no partial attestation object is returned.

**Step 3: Run RED**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  -k "lineage_attestation" -q
```

Expected: FAIL because the builder does not exist.

**Step 4: Implement the immutable value object and builder**

Add `TriggerProtectionLineageAttestation` and a result type containing exactly one of `attestation` or `refusal`. Reuse existing parsing and fingerprint helpers; do not create permissive defaults.

Treat `attached_on_trigger_order=true` as a persisted local submission attestation, not an exchange child-order ID. Do not claim it directly links the candidate order.

**Step 5: Run GREEN and the whole file**

```bash
.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_entry_protection_ledger_repair.py
git diff --cached --name-only
git commit -m "feat: prove attached stop submission lineage"
```

### Task 3: Make the account-wide assignment owner-specific and lineage-aware

**Files:**

- Modify: `src/telegram_kol_research/trigger_protection_assignment.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_trigger_protection_assignment.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write failing assignment tests**

Cover:

1. Candidate created after the durable pre-submit intent but before live-position `cTime`, with exchange `cTime` exactly equal to its verified child regular order and absent from that owner's baseline: assigned.
2. Candidate in that owner's baseline: excluded.
3. Candidate in a later owner's baseline but absent from the earlier owner's baseline: eligible only for the earlier owner.
4. Two owners and two indistinguishable candidates whose owner-specific evidence does not make both edges mutually unique: zero assignments.
5. Candidate `cTime` differs from its unique child regular-order `cTime`: zero assignments.
6. Two identical children share the same exchange `cTime` and two candidates remain indistinguishable: zero assignments.
7. Candidate earlier than the durable pre-submit intent creation time: zero assignments.
8. Candidate later than snapshot observation: zero assignments.
9. Missing timestamp: zero assignments.
10. Explicit exact `posId`: direct identity path, still subject to current-position, shape, snapshot and owner-conflict gates.
11. Existing immutable ledger owner conflict: zero assignments.
12. Reversing owner/candidate input order produces identical output and fingerprint.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest tests/test_trigger_protection_assignment.py -q
```

Expected: the pre-position lineage case is rejected and owner-specific baselines are not represented.

**Step 3: Extend the pure types and edge predicate**

Add to `ProtectionOwner` the durable pre-submit intent time, verified child exchange `cTime`, owner baseline IDs/fingerprint and lineage-attestation fingerprint. Build an edge only when either direct explicit identity or the complete attached-stop attestation succeeds, then apply exact shape, owner baseline, exact candidate-child `cTime`, pre-submit-time/snapshot-time sanity, existing owner and mutual-uniqueness gates. Do not use the later parent-ack event timestamp as the lower bound because an immediately triggered stop can be created before that event is persisted.

Remove only the attested edge's `candidate.created_at < position_created_at` rejection. Keep the legacy path unchanged.

Do not union all owners' baselines into one global exclusion set. Evaluate baseline membership per owner.

**Step 4: Emit bounded adoption evidence**

Set `match=lineage_attested_attached_stop` for the new anonymous path and include the lineage, baseline and snapshot fingerprints plus exact parent/child/position/candidate IDs and candidate timestamp.

**Step 5: Run focused and regression tests**

```bash
.venv/bin/python -m pytest \
  tests/test_trigger_protection_assignment.py \
  tests/test_entry_protection_ledger_repair.py -q
```

Expected: PASS, including the existing ambiguity and immutable-owner tests.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/trigger_protection_assignment.py \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_trigger_protection_assignment.py \
  tests/test_entry_protection_ledger_repair.py
git diff --cached --name-only
git commit -m "fix: attribute attached stops by exact lineage"
```

## Authorization group 2: reconciliation, convergence and management integration

### Task 4: Add a dedicated disabled-by-default rollout gate

**Files:**

- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_web_trading_settings.py`

**Step 1: Write failing setting tests**

Add tests for:

- `trigger_protection_lineage_attribution_mode=disabled|shadow|live`, default `disabled`;
- `trigger_protection_lineage_activation_after_intent_id`, default `None`;
- invalid modes, booleans, negatives and missing live watermark fail closed;
- existing global/management/liveness gates can only reduce the effective mode;
- a saved production payload that lacks both new fields remains disabled.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_trading_settings.py \
  -k "trigger_protection_lineage" -q
```

Expected: FAIL because the settings do not exist.

**Step 3: Implement and expose the settings**

Store both values in the existing global JSON settings row; do not add a table or column. A `live` mode with no finite reviewed watermark must have effective mode `disabled`. Keep the Web control explicit and warning-labelled; require a non-negative watermark for `live`, and ensure saving unrelated settings preserves both values.

**Step 4: Run GREEN**

```bash
.venv/bin/python -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_trading_settings.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  tests/test_trading_settings.py \
  tests/test_web_trading_settings.py
git diff --cached --name-only
git commit -m "feat: gate trigger protection lineage rollout"
```

### Task 5: Feed the same lineage proof into every live adoption caller

**Files:**

- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write failing reconciliation tests**

Use the production-shaped fixtures to prove:

- lifecycle `1050` and `1072` create exactly one proposed adoption in shadow and exactly one ledger row in live-mode fake integration;
- the logical primary stop receives the exact candidate `exchange_order_id`;
- the intent becomes adopted once;
- repeated reconciliation is idempotent;
- parent/child ambiguity, baseline conflict, incomplete snapshot or concurrent ledger ownership produces no adoption.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  -k "attached_stop_lineage" -q
```

Expected: FAIL because callers do not load the binding submission attestation and the current time gate rejects the candidates.

**Step 3: Build attestation contexts from one coherent reconciliation snapshot**

Load each exact binding and its single parent event/verified child evidence, build the closed attestation, and pass it to the account-wide planner and per-intent fallback. Snapshot errors remain non-consuming retryable evidence failures.

Do not broaden eligible intent, leg or venue statuses.

**Step 4: Preserve finalizer conflict checks**

Before committing, prove a competing logical leg or ledger owner makes the whole transaction roll back and leaves the intent unadopted.

**Step 5: Run adjacent tests**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_trigger_protection_intents.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_ledger.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_entry_protection_ledger_repair.py
git diff --cached --name-only
git commit -m "fix: reconcile attached stops with lineage evidence"
```

### Task 6: Prove idempotent backup-stop and take-profit convergence

**Files:**

- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_trigger_backup_stop_executor.py`
- Modify: `tests/test_trigger_take_profit_convergence_executor.py`
- Modify only if a failed test proves a real gap: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify only if a failed test proves a real gap: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`

**Step 1: Add an end-to-end fake exchange test**

Starting with one exchange-attached primary stop visible before the position `cTime`, run:

1. reconciliation/adoption;
2. backup-stop planning and confirmed readback;
3. TP convergence and confirmed readback;
4. a second identical worker cycle.

Assert one primary ledger row, at most one backup stop, exactly the desired TP tiers, total TP size not exceeding the current position, and no duplicate exchange-write calls on the second cycle.

**Step 2: Add partial-existing-protection cases**

Cover existing verified backup stop, one already confirmed TP tier, unowned possible TP, size drift, and unknown mutation outcome. The first two create only missing protection; the latter three create no new exchange write.

**Step 3: Run RED/GREEN**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_trigger_backup_stop_executor.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  -k "lineage or idempotent or existing_protection" -q
```

Expected: PASS after the minimum necessary change. If existing production code already passes, do not modify executor code.

**Step 4: Commit tests and only proven-needed production edits**

```bash
git add \
  tests/test_execution_bindings.py \
  tests/test_trigger_backup_stop_executor.py \
  tests/test_trigger_take_profit_convergence_executor.py
# Add executor source files only if the tests required a scoped correction.
git diff --cached --name-only
git commit -m "test: prove lineage protection convergence is idempotent"
```

### Task 7: Prove the management blocker clears only with current exact order evidence

**Files:**

- Modify: `tests/test_strategy_management_planner.py`
- Modify only if a failed test proves a gap: `src/telegram_kol_research/strategy_management_planner.py`

**Step 1: Add planner tests**

For an adopted primary stop:

- verified ledger + one exact current pending TPSL row -> management batch is not blocked by `protection_missing_cancellable_order_id`;
- ledger only, current order absent -> still blocked;
- current row only, no ledger -> still blocked;
- duplicate order ID, price/size/side conflict or incomplete snapshot -> blocked;
- historical batch `152` remains terminal and is not selected for replay.

**Step 2: Run focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_management_planner.py \
  -k "cancellable_order_id or lineage_adopted" -q
```

Expected: PASS after the minimum necessary change. Do not weaken `_ledger_confirmed_position_protection()`.

**Step 3: Commit tests and only proven-needed production edits**

```bash
git add tests/test_strategy_management_planner.py
# Add strategy_management_planner.py only if a real scoped gap was proven.
git diff --cached --name-only
git commit -m "test: require current exact protection for management"
```

## Authorization group 3: proactive observability

### Task 8: Alert on first visible-but-unowned attached stop

**Files:**

- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_strategy_records.py`

**Step 1: Write failing incident-capture tests**

On the first complete snapshot proving a live verified position plus attached-stop attestation plus exact-shape visible candidate but no verified adoption, assert a durable incident is created immediately. Its bounded payload must distinguish:

- `native_stop_visible_ownership_unverified`;
- `live_position_stop_absent`;
- `ownership_conflict`;
- `ownership_recovered`.

Assert candidate order IDs are bounded and no raw exchange payload or secrets are stored.

**Step 2: Write notification timing and deduplication tests**

Assert the first refusal is eligible for notification without waiting for retry 5/manual review; identical cycles are suppressed; a management-blocked transition and recovery transition generate new fingerprints. Exact verified backup stop lowers severity but does not hide the condition.

**Step 3: Run RED**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_records.py \
  -k "native_stop_visible or ownership_unverified or ownership_recovered" -q
```

Expected: FAIL because current durable scanning normally waits for due/terminal intent state, and the first refusal audit can remain pending.

**Step 4: Implement the smallest durable incident projection**

Reuse the existing runtime incident and notification mechanism. Do not add a new notification transport. Make the incident summary say that an exchange stop is visible but not safely owned and that management may be blocked; do not say the exchange has no stop.

Expose the same distinction in strategy records/Web data, while keeping active push as the primary operator signal.

**Step 5: Run focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_records.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/strategy_records.py \
  tests/test_execution_bindings.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_records.py
git diff --cached --name-only
git commit -m "feat: alert on unowned attached stops"
```

## Authorization group 4: final local verification and independent review

### Task 9: Run the final local verification once

**Files:**

- No planned source changes.

**Step 1: Run targeted safety suites**

```bash
.venv/bin/python -m pytest \
  tests/test_trigger_protection_assignment.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_backup_stop_executor.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_planner.py \
  tests/test_production_safety_monitor.py \
  tests/test_strategy_records.py -q
```

Expected: PASS.

**Step 2: Run the full suite once on the final candidate**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. If production code changes after this run, rerun affected focused tests and the full suite once on the new final candidate.

**Step 3: Run static checks**

```bash
.venv/bin/python -m compileall -q src tests
git diff --check
git status --short --branch
```

Expected: compile succeeds, no whitespace errors, and only authorized paths changed.

### Task 10: Perform a dedicated fail-open review

**Files:**

- Review only; fixes require returning to the relevant prior task and rerunning tests.

The independent reviewer must explicitly answer:

1. Can a candidate from another time period enter an edge despite the owner's pre-submit baseline?
2. Can one candidate map to two owners, or one owner to two candidates, especially when two children share a `cTime`?
3. Can a synthetic/persisted attached marker be mistaken for a child order ID?
4. Can incomplete positions/TPSL/history evidence produce an adoption?
5. Can a history-only anonymous order become a current cancellable order?
6. Can an existing ledger/logical-leg owner be overwritten?
7. Can a restart or duplicate tick create a second backup stop or TP tier?
8. Can management proceed with only a ledger row or only a current exchange row?
9. Can activation touch an intent before the future-only watermark?

Acceptance: no P0/P1/P2 finding remains; reviewer states in writing that no fail-open path was found in the reviewed diff and names the exact commit.

Do not push or stage for production until the owner authorizes integration after reviewing this evidence.

## Authorization group 5: immutable deployment, shadow, and live activation

### Task 11: Stage the exact reviewed commit with lineage authority disabled

This task requires a new explicit owner authorization and exact 40-character commit. Follow the repository's immutable stage helper and reviewed action manifest. Do not activate in the same command or evidence boundary.

Pre-stage checks:

- no active time-sensitive management/revision operation;
- active write count and unknown exchange outcome inventory;
- worker/web/ingest identities and ports 8002/8000/8001;
- current complete worker GET snapshot;
- no unprotected live position;
- release digest and rollback commit.

Acceptance: staged artifact matches the reviewed commit, active runtime remains unchanged, and lineage mode remains disabled.

### Task 12: Activate shadow only

Requires a separate authorization. Shadow must compute old/new decisions and bounded evidence but write no protection ledger, logical protection leg, intent terminal state, mutation intent, or exchange order.

Observe according to the approved risk-adaptive window and natural traffic. In addition to live traffic, replay the redacted lifecycle `1050`/`1072` fixtures offline against the staged pure planner. Require:

- proposed owner/candidate pair is unique for both known failures;
- no proposal for every negative/collision fixture;
- no unrelated position/order candidate enters a proposed edge;
- zero exchange write delta and zero DB authority takeover;
- monitor notification preview is accurate and deduplicated.

### Task 13: Activate future-only live lineage attribution

Requires another explicit authorization because it can unlock existing protection writers. Bind activation to an exact intent/raw watermark and exact reviewed release. Do not include historical failed/manual-review intents.

For the first natural eligible trigger fill, require direct evidence for:

- parent request/response and attestation;
- unique child/`posId`;
- owner-specific baseline;
- complete current positions and TPSL snapshot;
- unique adopted order ID and immutable ledger owner;
- exact backup/TP convergence without duplicates;
- management capability only after current-order readback;
- no change to other positions or orders.

Unknown/incomplete evidence leaves the phase in progress and the adoption blocked.

## Authorization group 6: historical inventory and optional repair

### Task 14: Re-run a read-only inventory of all impacted intents

Use production SQLite `mode=ro + query_only=ON` with `python3 -B` and worker 8002 GET only. For each impacted intent, classify exactly one:

- `historical_closed`;
- `live_exact_repair_candidate`;
- `live_ambiguous_or_incomplete`;
- `unknown`.

Never interpret a failed external read as zero. The output is a signed/fingerprinted plan; no DB or exchange write.

### Task 15: Leave historical-closed records untouched by default

For `historical_closed`, do not create a ledger row, protection order, TP order, management batch, or replay. If the owner later wants terminal metadata cleanup, write a separate L3 data-repair plan with database backup, `PRAGMA quick_check`, exact before/after counts and rollback proof.

### Task 16: Apply one live DB adoption only if separately authorized

Only for one `live_exact_repair_candidate`, rebuild the plan from a fresh complete worker snapshot, require exact action fingerprint, back up the database, run `PRAGMA quick_check`, and atomically write only the logical primary binding/ledger/intent/revision ownership state. Re-read the database and exchange before considering it complete.

This task performs no exchange write.

### Task 17: Converge exchange protection only if separately authorized

After Task 16 proves exact ownership and a new worker GET still proves the same live position and protection set, separately authorize backup-stop/TP convergence for that one `posId`. Use existing mutation reservations, idempotency keys, account lock and mandatory readback. Stop on any unknown result and do not retry blindly.

## Completion handoff

At each authorization group's stop point, update the status/evidence document only if that update was included in the authorization, run the repository Telegram stop notification exactly once, and return control to the owner. Do not combine code implementation, review, staging, activation, historical DB repair, or exchange convergence into one implied approval.
