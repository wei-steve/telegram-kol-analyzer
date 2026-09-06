# Message Recognition Labels Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add owner-entered, provenance-preserving human labels to group-message AI recognition without allowing labels to influence recognition, context, candidates, execution, notifications, or trading.

**Architecture:** Add one observation-only table and a dedicated Web labeling service. The service validates the three client-owned fields, derives every `labeled_*` snapshot field from the current authoritative projection in the same transaction, and upserts by raw message. Existing message-page loading bulk-loads labels for rendering; recognition and trading modules never import or query the table. Schema installation and Web runtime activation are separate production actions.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI, Jinja2, vanilla JavaScript/CSS, pytest, immutable scoped release helpers.

---

### Task 1: RED schema and migration tests

**Files:**
- Modify: `tests/test_db_bootstrap.py`
- Modify: `tests/test_db_migrations.py` or add a focused schema test when the existing file boundary is clearer

1. Add failing tests for the exact new table, columns, foreign key, index, unique constraint, enum checks, verdict/error-kind coupling, note length, prompt provenance and nullable snapshot columns.
2. Add a legacy-database bootstrap test proving only the new table/indexes appear and a second bootstrap is idempotent.
3. Run the focused tests and record RED because the model/table do not exist.

### Task 2: GREEN additive model

**Files:**
- Modify: `src/telegram_kol_research/models.py`

1. Add `MessageRecognitionLabel` with the approved constraints and an explicit observation-only/non-consumption comment.
2. Keep every snapshot field nullable and avoid defaults for unknown facts.
3. Re-run Task 1 tests until GREEN.

### Task 3: RED/GREEN dedicated labeling service

**Files:**
- Add: `src/telegram_kol_research/message_recognition_labels.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Add or modify: `tests/test_message_recognition_labels.py`

1. Add failing tests for all verdict/error-kind/note validation rules, unknown message handling, create/update semantics, created/updated timestamps, and strict rejection of client-supplied snapshot fields.
2. Add fixtures covering MiMo-run prompt provenance, RecognitionDecision fallback provenance, absent provenance, and `None` preservation for every missing snapshot field.
3. Add tests proving snapshot values follow the current authoritative projection and an update recaptures the new current snapshot.
4. Implement one transaction-scoped snapshot/upsert service. Reuse the current authoritative-run selection semantics instead of adding a second definition of “current.”
5. Add one bulk label query to `_serialize_raw_messages`; expose the saved label and a render-only drift flag computed from current recognition result/event type/confidence versus the saved snapshot.
6. Prove no recognition, context, candidate, execution or notification module imports or queries the new model/service.

### Task 4: RED/GREEN GET/POST API and runtime ownership

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_runtime_role_selection.py`
- Modify: `tests/test_web_page_render.py`

1. Add failing API tests for the exact accepted key set, enums, 2000-character boundary, 422 failures, 404 unknown message, create/update response, GET `label: null`, and server-owned snapshots.
2. Add role tests proving `all`/`web` ownership and exact 503 code `label_not_owned_by_runtime_role` for worker/ingest.
3. Implement the two routes as thin adapters over the labeling service. Do not call Telegram, recognition, candidate, execution or worker proxy paths.
4. Re-run the focused tests until GREEN.

### Task 5: RED/GREEN inline UI, drift hint, filters and stats

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`

1. Add failing render tests for the label button/form, verdict chips, purple/gray separation, conditional error kind, saved values, and nullable values.
2. Add failing tests for the render-only “识别已变更” chip, exact title, all three comparison fields, no database mutation, no red/yellow styling and no contribution to attention.
3. Add failing asset tests for POST payload ownership, success/error UI, labeled/unlabeled filters, loaded-row labeled count, state refresh after submit and recomputation after loading more.
4. Implement minimal markup, event handling and CSS. Never auto-delete, invalidate or rewrite a label because the current recognition changed.
5. Run the render, asset and role/API focused tests until GREEN.

### Task 6: Candidate verification and review

1. Run all affected focused suites and `git diff --check`.
2. Audit imports/read paths to prove the label table is Web-only observation data.
3. Run exactly one complete pytest suite after the final production-code edit.
4. Review the exact diff for schema, snapshot truthfulness, write isolation, UI semantics and rollback completeness; fix findings with focused RED/GREEN and repeat the final full suite if production code changes.
5. Commit explicit paths and push the exact candidate to `codex/deepcoin-auto-trading-v1`.

### Task 7: Separate L3 database-copy rehearsal and live schema step

1. Read all three live deployment-identity endpoints and cross-check systemd release commit/manifest evidence; record the measured Web rollback SHA.
2. Create a root-owned mode-0600 verified production backup with SHA-256, `quick_check=ok` and zero foreign-key violations.
3. Rehearse exact candidate DDL on an independent copy. Verify the new table/constraints/indexes, `message_recognition_labels=0`, repeated bootstrap idempotence, and unchanged counts for `raw_messages`, `recognition_decisions`, `mimo_recognition_runs`, `mimo_recognition_attempts`, `signal_candidates`, `strategy_lifecycles`, `execution_bindings`, and `execution_events`.
4. On the rehearsal copy, exercise create/update/GET and snapshot provenance without creating a production verdict.
5. Under the runtime-control lock, apply only the new table/indexes to production in one `BEGIN IMMEDIATE` transaction. Repeat all integrity/count gates. Do not stage, activate or restart in this step.

### Task 8: Runtime-only Web activation and bounded observation

1. After schema acceptance, create a fresh Web-only stage/activation manifest with `schema_changed=false`, `production_data_mutation=false`, no authority change and no exchange-write-semantic change.
2. Stage and activate the exact pushed SHA using the measured Web rollback SHA. Do not restart ingest/worker and do not create any production label.
3. Verify Web/ingest/worker runtime identities, loaded manifests, HTTP/UI rendering, GET `label: null`, invalid-POST no-write behavior, database integrity and unchanged key business counts.
4. Observe 15 continuous minutes or 5 real messages, whichever comes first. Record traffic honestly and do not extend the window.
5. Update `docs/ai-context-resolution-optimization-status.md` current role SHAs and `docs/known-issues-and-deferred-work.md` to state that the labeling entry exists while the 47-message backlog remains uncleared.
6. Commit only the documentation evidence update, push it, and send the single required Telegram stop notification.

### Exact rollback procedure

1. If labels exist, export them and verify export row count/hash before any destructive schema action.
2. Activate the measured pre-deploy Web release first and verify its immutable identity and health.
3. Only after old Web is running, drop `message_recognition_labels` in a separate schema transaction; then run `quick_check`, foreign-key check and critical-count verification.
4. Never drop the table while the new runtime is active because `init_db()` calls `Base.metadata.create_all()` and would recreate it.
