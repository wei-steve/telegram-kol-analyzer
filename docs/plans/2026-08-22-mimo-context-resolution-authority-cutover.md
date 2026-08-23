# MiMo Context-Resolution Authority Cutover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
>
> **Owner execution constraint:** Execute with Codex only. Do not start Claude,
> subagents, background agents, or parallel implementation sessions.

**Goal:** Permanently move future context-resolution authority from DeepSeek to MiMo v2.5 and safely record Codex-generated, analysis-only decisions for the deduplicated HTTP 402 incident without creating any current or future exchange-write path.

**Architecture:** Keep the normal resolver contract unchanged and make its independently persisted model selection explicit, durable, and no-fallback. Store historical repairs in a new audit-only table that no operational worker reads; use standalone export/validate/apply/rollback tooling with exact hashes, a write authorizer, production-copy rehearsal, and targeted rollback. Return to Phase 6 only after the MiMo cutover and bounded L3 repair complete.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite WAL, stdlib `sqlite3`/`argparse`, FastAPI/Jinja, pytest, systemd split services, existing gated updater.

---

## Execution rules

- Before implementation, read `AGENTS.md`,
  `docs/runtime-serialization-remediation-status.md`, and only this file as the
  current phase file. Do not read other runtime-serialization phase files.
- Claim Phase 6C through the canonical status protocol and push that exact-path
  claim commit before touching implementation files.
- Use `executing-plans` and `test-driven-development`.
- Never use `git add -A`; stage only the exact files named by each task and
  inspect `git diff --cached --name-only` before every commit.
- Preserve the deployed split topology and fixed runtime settings:
  `message_lock_mode=global`, `message_pipeline_mode=queue`,
  `worker_command_mode=queue`, and `semantic_review_enabled=false`.
- Do not call DeepSeek during development or historical repair.
- Do not run historical messages through `process_authoritative_message`,
  `apply_authoritative_assessment`, any auto-trade executor, any management
  executor, or any worker-command route.
- Focused tests precede every production-code change. Run the full suite once on
  the final production-code candidate. If production code changes afterward,
  run affected focused tests and one new final full suite.
- Phase 6C is L3 for schema/data repair and L2 for the final provider-cutover
  observation. Do not broaden into a whole-database audit.
- Fail closed on missing database identity, stale source/evidence/thread state,
  incomplete external evidence, active writes, active management, SQLite
  errors, authority ambiguity, or any possible exchange-write reachability.
- Send no ad-hoc Telegram messages and manufacture no Telegram traffic or
  exchange writes. Follow the repository's single stop-notification rule when
  returning control.

## Task 1: Revalidate authority and freeze the incident definition

**Files:**
- Modify: `docs/runtime-serialization-remediation-status.md`
- Test: `tests/test_process_boundary_authority.py`

**Step 1: Run the read-only ownership and production gates**

Verify clean worktree, exact local/tracking/remote heads, Phase 6C planned and
unclaimed, no Git lock/writer, exact deployed split head, three active/enabled
roles, monolith inactive/disabled, monitor timer healthy, WAL, `quick_check=ok`,
no active writes, no active management batch, no claimed message job, and no
claimed/executing worker command.

Expected: every gate is complete and consistent. Otherwise record the blocker,
leave Phase 6C `in_progress`, release the claim, and stop without repair.

**Step 2: Claim Phase 6C**

Set `phase_status: claimed` and `claimed_by` to the current task ID, then commit
and push only the status file.

```bash
git add -- docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "docs: claim Phase 6C context authority cutover"
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

**Step 3: Re-run the incident census read-only**

Freeze a server-side JSON census containing the query window, provider,
`network_error` classification, distinct raw-message IDs, source statuses, job
statuses, source-attempt IDs, and database identity. Do not export message text
yet. Treat the design-time 33-message count as historical, not authoritative.

**Step 4: Re-run the current authority scanner**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_process_boundary_authority.py -q
```

Expected: PASS with no Web/exchange authority regression.

## Task 2: Preserve independent context-model configuration

**Files:**
- Modify: `tests/test_ai_recognition_config.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`

**Step 1: Write failing configuration/API tests**

Add tests proving:

```python
def test_model_selection_save_preserves_independent_context_model():
    # Existing config has context_resolution_model_id="mimo-v2.5".
    # Saving unrelated active text/image selection retains that exact value.

def test_model_selection_api_accepts_text_capable_context_model():
    # POST context_resolution_model_id="mimo-v2.5" and read it back exactly.

def test_model_selection_api_rejects_non_text_context_model():
    # A GLM-OCR-only model returns 422 and does not alter the file.

def test_model_selection_page_exposes_independent_context_selector():
    # HTML contains data-context-resolution-model-id and selects mimo-v2.5.
```

Extend the asset smoke test so the submitted JSON contains
`context_resolution_model_id`.

**Step 2: Run the tests and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_ai_recognition_config.py \
  tests/test_web_app.py -k 'ai_recognition_config' \
  tests/test_web_page_render.py -k 'model_selection' \
  tests/test_web_assets_smoke.py -q
```

Expected: failures show the API drops the independent context model and the UI
has no context selector.

**Step 3: Implement the minimum preservation and selector**

In `update_ai_recognition_config`, pass the submitted value, falling back to the
existing independent value rather than `active_text_model_id`:

```python
context_resolution_model_id=str(
    payload.get("context_resolution_model_id")
    or existing_config.context_resolution_model_id
),
```

Return that value in the response. Add a text-capable model selector to the
existing model-selection form and add the exact value to
`buildAiRecognitionConfigPayload()`.

**Step 4: Run the focused tests and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit exact paths**

```bash
git add -- \
  tests/test_ai_recognition_config.py \
  tests/test_web_app.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js
git diff --cached --name-only
git commit -m "feat: preserve context model authority selection"
```

## Task 3: Add an atomic no-fallback model-cutover tool

**Files:**
- Create: `src/telegram_kol_research/context_authority_cutover.py`
- Create: `tests/test_context_authority_cutover.py`

**Step 1: Write failing tests**

Cover dry-run, apply, repeated apply, wrong before hash, wrong old model,
unconfigured/non-text new model, secret preservation, exact backup, rollback,
rollback hash mismatch, and atomic replacement. The returned receipt must contain
only hashes/model IDs/paths and never API-key values.

Required public interface:

```python
@dataclass(frozen=True)
class ContextAuthorityCutoverReceipt:
    mode: str
    before_sha256: str
    after_sha256: str
    old_model_id: str
    new_model_id: str
    backup_path: str | None

def plan_context_authority_cutover(...): ...
def apply_context_authority_cutover(...): ...
def rollback_context_authority_cutover(...): ...
```

The module must expose a standalone `python -m` CLI. Apply requires
`--apply`, exact `--expected-before-sha`, exact `--expected-old-model`, and an
explicit backup path. Rollback requires the expected current hash and backup
hash.

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_context_authority_cutover.py -q
```

Expected: import/module failure.

**Step 3: Implement minimal atomic cutover**

Use `load_ai_recognition_config` and `save_ai_recognition_config` against a
temporary file in the same directory, fsync it, preserve file mode/owner where
permitted, then `os.replace`. Never log the serialized configuration. Verify the
new ID exists, supports text, and is configured. Do not add a fallback field.

**Step 4: Run and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_context_authority_cutover.py \
  src/telegram_kol_research/context_authority_cutover.py
git diff --cached --name-only
git commit -m "feat: add atomic context authority cutover"
```

## Task 4: Prove MiMo selection and fail-closed semantics

**Files:**
- Modify: `tests/test_context_resolution.py`
- Modify: `tests/test_context_resolution_replay.py`
- Modify: `src/telegram_kol_research/context_resolution.py`

**Step 1: Write failing characterization tests**

Add a real-config test selecting `mimo-v2.5` and assert the model caller receives
the MiMo base URL, key, model, timeout, unchanged system prompt, and unchanged
request payload. Add a test where MiMo raises an HTTP/provider error and assert:

- exactly two MiMo attempts;
- zero DeepSeek calls;
- terminal `network_error`/exhausted behavior remains unchanged;
- no decision or operational row is created.

Also assert provider/model identity participates in the context fingerprint or
cache identity. A previously exhausted DeepSeek result must not suppress a new
MiMo attempt for the same request.

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_context_resolution.py tests/test_context_resolution_replay.py -q
```

Expected: the cache-identity test fails because the current context fingerprint
does not include provider authority.

**Step 3: Add provider identity to the fingerprint**

Extend only `fingerprint_payload`:

```python
"provider": {
    "model_id": ai_recognition_config.context_resolution_model_id,
    "model": provider.model,
    "base_url": provider.base_url.rstrip("/"),
},
```

Select the provider before computing the fingerprint. Do not include the API
key and do not alter the request or decision contract.

**Step 4: Run and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_context_resolution.py \
  tests/test_context_resolution_replay.py \
  src/telegram_kol_research/context_resolution.py
git diff --cached --name-only
git commit -m "fix: isolate context cache by provider authority"
```

## Task 5: Add the audit-only backfill table

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `tests/test_context_analysis_backfill_schema.py`

**Step 1: Write failing schema/bootstrap tests**

Define tests for first bootstrap, repeated bootstrap, exact columns, foreign keys
to raw message/source attempt only, and unique `(run_id, raw_message_id)`. Assert
there is no foreign key to a strategy thread and no trigger.

The model contract is:

```python
class ContextAnalysisBackfill(Base):
    __tablename__ = "context_analysis_backfills"
    id: int
    run_id: str
    raw_message_id: int
    source_attempt_id: int
    source_request_sha256: str
    source_state_fingerprint: str | None
    prompt_version: str
    analyst_model: str
    decision_json: str | None
    status: str
    skip_reason: str | None
    created_at: datetime
```

Statuses are closed to `analysis_only_completed`, `skipped_deleted`, and
`skipped_stale`.

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_context_analysis_backfill_schema.py -q
```

Expected: missing model/table.

**Step 3: Implement the model and idempotent bootstrap**

Use the repository's normal SQLAlchemy metadata/bootstrap pattern. Add a closed
status check constraint and only the two source foreign keys.

**Step 4: Run and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_context_analysis_backfill_schema.py \
  src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py
git diff --cached --name-only
git commit -m "feat: add context analysis backfill ledger"
```

## Task 6: Build deterministic incident export and validation

**Files:**
- Create: `src/telegram_kol_research/context_analysis_backfill.py`
- Create: `tests/test_context_analysis_backfill.py`

**Step 1: Write failing export tests**

Fixtures must include repeated fingerprints for one message, malformed newest
request, deleted source, expired/failed jobs, unrelated providers/errors, and
secret-looking values outside the persisted request. Assert export selects one
newest valid source per raw message, classifies deleted rows, emits canonical
JSON, includes exact allowed ID sets, and excludes credentials.

Required top-level manifest fields:

```json
{
  "schema_version": "context-analysis-backfill-v1",
  "run_id": "...",
  "database_identity": "sha256:...",
  "incident_filter": {},
  "record_count": 0,
  "records_sha256": "...",
  "records": []
}
```

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_context_analysis_backfill.py -k 'export or validate' -q
```

Expected: missing module/functions.

**Step 3: Implement read-only export and strict validator**

Open SQLite with `mode=ro`, set `query_only=1`, and verify `total_changes=0`.
Reuse `parse_context_resolution_decision` for every non-skipped decision. Reject
unknown fields, target/evidence IDs outside the source request, record-count or
hash drift, duplicated raw messages, missing source attempts, and any analyst
model other than `codex-manual-context-v1`.

Expose standalone commands:

```bash
python -m telegram_kol_research.context_analysis_backfill export ...
python -m telegram_kol_research.context_analysis_backfill validate ...
```

Both are read-only and write only their requested evidence output file.

**Step 4: Run and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_context_analysis_backfill.py \
  src/telegram_kol_research/context_analysis_backfill.py
git diff --cached --name-only
git commit -m "feat: export bounded context analysis gaps"
```

## Task 7: Add write-authorized apply and exact rollback

**Files:**
- Modify: `tests/test_context_analysis_backfill.py`
- Modify: `src/telegram_kol_research/context_analysis_backfill.py`

**Step 1: Write failing apply/rollback tests**

Test dry-run default, missing `--effects analysis-only`, database/hash/count
mismatch, active-write/management/job/command gates, stale evidence, missing or
cross-chat target threads, deleted classification, repeated apply, receipt
hashing, exact rollback, rollback row drift, and rollback of another run.

Install an SQLite authorizer in tests and assert the apply transaction performs
only `INSERT` on `context_analysis_backfills`; rollback performs only `DELETE`
on that table. Explicitly assert unchanged counts/hashes for:

- message jobs and recognitions/decisions;
- signal candidates and message instruction/operation tables;
- strategy links/lifecycles;
- management batches and worker commands;
- execution events, trade signals, bindings, and mutation intents;
- notification outboxes.

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_context_analysis_backfill.py -k 'apply or rollback or authorizer' -q
```

Expected: apply/rollback functions absent.

**Step 3: Implement the minimum guarded transaction**

Use stdlib `sqlite3`, `BEGIN IMMEDIATE`, a strict `set_authorizer`, canonical
row serialization, and an external receipt file. Apply never updates an existing
row. Repeated exact apply returns `already_applied` only after matching every row
hash. Rollback deletes the exact receipt IDs only after matching each preimage.

**Step 4: Run and confirm GREEN**

Run the Step 2 command, then the complete backfill test files:

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_context_analysis_backfill.py \
  tests/test_context_analysis_backfill_schema.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_context_analysis_backfill.py \
  src/telegram_kol_research/context_analysis_backfill.py
git diff --cached --name-only
git commit -m "feat: apply analysis-only context backfills"
```

## Task 8: Project the historical analysis without operational links

**Files:**
- Modify: `tests/test_web_queries_messages.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_messages.html`

**Step 1: Write failing projection tests**

Assert the latest backfill is rendered separately as:

```python
{
    "status": "analysis_only_completed",
    "non_authoritative": True,
    "decision": "manage_thread",
    "target_thread_ids": [123],
    "confidence": 0.91,
    "reason": "...",
    "analyst_model": "codex-manual-context-v1",
}
```

The page must visibly say `历史分析补齐（不执行）`. Assert no live
`linked_threads`, instruction, lifecycle, or operation row is synthesized.

**Step 2: Run and confirm RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_web_queries_messages.py -k 'context_analysis_backfill' \
  tests/test_web_page_render.py -k 'context_analysis_backfill' -q
```

Expected: missing projection/label.

**Step 3: Implement read-only projection**

Load the newest `ContextAnalysisBackfill` per visible raw message. Parse only
bounded safe decision fields. Keep it under a separate
`historical_context_analysis` key; never merge it into `context_resolution` or
`linked_threads`.

**Step 4: Run and confirm GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add -- \
  tests/test_web_queries_messages.py \
  tests/test_web_page_render.py \
  src/telegram_kol_research/web_queries.py \
  src/telegram_kol_research/templates/_messages.html
git diff --cached --name-only
git commit -m "feat: show non-authoritative context backfills"
```

## Task 9: Prove the historical tool has no authority path

**Files:**
- Modify: `tests/test_process_boundary_authority.py`
- Create: `tests/test_context_analysis_backfill_authority.py`
- Modify: `src/telegram_kol_research/context_analysis_backfill.py` only if RED requires it

**Step 1: Write failing authority tests**

Statically and behaviorally assert the standalone backfill module cannot reach
or import authoritative application, auto trading, management execution,
worker-command execution, Telegram notification, Deepcoin client construction,
position locks, or mutation gateways. Monkeypatch those boundaries to raise and
prove export/validate/apply/rollback never call them.

**Step 2: Run and confirm RED or characterization PASS**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_context_analysis_backfill_authority.py \
  tests/test_process_boundary_authority.py -q
```

If the first implementation already satisfies the authority test, record it as
characterization PASS and do not invent a production change. If it fails, make
only the smallest import/call-graph correction.

**Step 3: Run the affected acceptance slice**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_ai_recognition_config.py \
  tests/test_context_authority_cutover.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_replay.py \
  tests/test_context_analysis_backfill.py \
  tests/test_context_analysis_backfill_schema.py \
  tests/test_context_analysis_backfill_authority.py \
  tests/test_web_queries_messages.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  tests/test_process_boundary_authority.py -q
```

Expected: PASS.

**Step 4: Commit exact paths if changed**

```bash
git add -- \
  tests/test_context_analysis_backfill_authority.py \
  tests/test_process_boundary_authority.py \
  src/telegram_kol_research/context_analysis_backfill.py
git diff --cached --name-only
git commit -m "test: prove context backfill has no trade authority"
```

## Task 10: Run final local verification once

**Files:**
- Modify only files required by an actual focused failure

**Step 1: Review the complete diff**

Verify scope, closed statuses, secret redaction, SQL allowlist, no normal-table
writes, no fallback, exact rollback, and no Phase 6 stall fix.

**Step 2: Run static checks**

```bash
git diff --check
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m compileall -q src/telegram_kol_research
```

Expected: PASS.

**Step 3: Run the one final full suite**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest -q
```

Expected: all tests pass with only recorded baseline skips/warnings.

Do not rerun the full suite unless production code changes afterward.

**Step 4: Commit any test-only final adjustments**

Stage exact paths only. If production code changed after Step 3, rerun the
affected focused tests and one new final full suite before committing the final
candidate.

## Task 11: Rehearse schema, apply, repeat apply, and rollback on a production copy

**Files:**
- No repository changes expected
- Server evidence: `/opt/telegram-kol-analyzer/data/backups/phase6c-<sha>-<utc>/`

**Step 1: Push the reviewed candidate**

Confirm local HEAD is the reviewed commit, worktree is clean, remote is an exact
fast-forward, and the claim still belongs to this task. Push without force.

**Step 2: Create an immutable online backup and evidence root**

Record source DB identity/size, WAL, `quick_check`, current candidate SHA, active
topology/modes, active-write/management/job/command gates, and targeted before
counts. Keep all sensitive manifest content root-readable on the server.

**Step 3: Bootstrap and rehearse on copies only**

On a copy:

1. bootstrap the new table twice;
2. export the freshly frozen incident set;
3. validate a deterministic test decision manifest;
4. apply with `--effects analysis-only`;
5. prove only the new table changed;
6. apply the same manifest again and prove exact idempotence;
7. rollback by exact receipt and prove targeted preimage restoration;
8. run `quick_check` after every mutation stage.

Expected: every step passes with zero operational-table change. Any mismatch
leaves Phase 6C `in_progress` and stops before deploy.

## Task 12: Deploy the final code candidate

**Files:**
- No repository changes expected

**Step 1: Re-prove the production safe window**

At two bounded checkpoints verify active-write count zero, no active management,
no claimed jobs/commands, exact split topology, WAL, quick check, and a complete
read-only Deepcoin snapshot if the phase file's execution-path gate requires it.

**Step 2: Deploy through the gated updater**

```bash
EXPECTED_COMMIT=<exact-40-hex-candidate> ./scripts/server_git_update.sh
```

Expected: exit 0, exact deployed HEAD, worker/Web/ingest active and enabled,
monolith inactive and disabled, monitor healthy, modes unchanged, and new table
present empty. Do not hand-pull or force-push.

**Step 3: Verify rollback readiness**

Record the exact previous code SHA and the exact unchanged pre-cutover AI config
hash/backup path. Do not apply rollback while the new candidate is healthy.

## Task 13: Cut future context authority to MiMo v2.5

**Files:**
- Server configuration only: `/opt/telegram-kol-analyzer/config/ai_recognition.yaml`

**Step 1: Verify MiMo provider readiness**

Without exposing secrets, prove `mimo-v2.5` is configured, supports text, and
the worker can reach its endpoint. Use one bounded minimal contract probe only
if recent real MiMo success is not sufficiently fresh. Do not call DeepSeek.

**Step 2: Dry-run the exact cutover**

Run the standalone tool with exact before hash, expected old model
`deepseek-v4-flash`, new model `mimo-v2.5`, and backup path, without `--apply`.
Verify the receipt contains no credentials.

**Step 3: Apply atomically**

Recheck the safe-window gates, then run the same exact command with `--apply`.
Verify file hash, owner/mode, independent model fields, and no fallback setting.

**Step 4: Verify runtime adoption without restart**

Use read-only runtime/config evidence to prove the worker now selects MiMo for
context resolution. If adoption cannot be proven without a natural message,
record it as pending for the L2 window; do not restart merely to manufacture
proof.

Rollback only if the configuration write/adoption itself is invalid, using the
exact backup/hash tool. A later provider failure remains fail closed.

## Task 14: Produce and rehearse the Codex analysis manifest

**Files:**
- No repository files
- Root-readable server evidence only

**Step 1: Export the final incident manifest read-only**

Use the freshly deployed export command and exact approved incident filter.
Record count and hash. Deleted sources remain excluded/skipped.

**Step 2: Generate Codex decisions**

For each newest valid non-deleted record, use the exact persisted request,
`CONTEXT_RESOLUTION_SYSTEM_PROMPT`, and rendered user prompt. Emit only the
closed JSON object. Do not infer target IDs outside the manifest. When evidence
is insufficient, choose `hold` or `unresolved` rather than guessing.

**Step 3: Validate read-only**

Run the strict validator. Resolve validation errors by correcting the decision,
not by weakening the validator or editing source context.

**Step 4: Rehearse the exact real manifest on a fresh production copy**

Apply, repeat apply, and rollback the exact decision manifest. Prove
`quick_check=ok`, exact targeted counts/hashes, and zero operational-table
change. Preserve the rehearsal summary and receipt.

## Task 15: Apply the analysis-only historical repair

**Files:**
- Production database targeted inserts only

**Step 1: Take the final backup and preimage**

Create an online backup, record database identity, `quick_check`, new-table
count, critical operational counts, exact source rows, active-write/management/
job/command gates, and complete apply-manifest hash.

**Step 2: Apply once**

Run the exact rehearsed command with `--effects analysis-only`, exact database
identity/hash/count, and apply enabled.

**Step 3: Verify immediately**

Require:

- receipt count equals inserted plus explicit skipped count;
- only `context_analysis_backfills` changed;
- deleted/stale rows did not become completed;
- message job/recognition/decision and every operational critical-table count
  and targeted hash are unchanged;
- no new worker command, management batch, execution event, trade signal,
  mutation intent, notification, SQLite error, or exchange write;
- workbench labels the results `历史分析补齐（不执行）`.

If verification fails, stop workers only if required to prevent further damage,
run the exact targeted rollback, prove the preimage, restore service health, keep
Phase 6C `in_progress`, and stop.

## Task 16: Run the fixed L2 provider observation

**Files:**
- Server evidence only

**Step 1: Start one fixed observation window**

Observe 30 continuous minutes and at least five natural messages, trying to
cover two chats. If fewer than five arrive, stop at 30 minutes without extension
and leave the phase `in_progress`.

**Step 2: Verify provider and queue behavior**

For every message that requires context resolution, prove model
`mimo-v2.5`, strict contract success or explicit fail-closed error, no DeepSeek
call/402 delta, one durable message job, no duplicate decision, bounded backlog,
and no stale historical job replay.

**Step 3: Verify process and exchange safety**

Check stable topology/PIDs, ingest-only Telegram session, Web-load isolation,
monitor health, SSE, WAL, quick check, zero SQLite lock errors, loop health, and
natural management behavior when available. Compare direct exchange history
when a natural path can affect execution.

## Task 17: Complete Phase 6C and restore Phase 6

**Files:**
- Modify: `docs/runtime-serialization-remediation-status.md`

**Step 1: Record exact evidence**

Record claim/design/plan/code/deployed/config hashes, focused tests, final full
suite, rehearsal/apply/rollback receipts, actual repaired/skipped counts, zero
operational mutation proof, topology, modes, provider evidence, L2 traffic,
SQLite/loop/monitor/exchange findings, and unresolved items.

**Step 2: Decide status**

- If every Phase 6C functional, L3, and L2 gate passes, mark Phase 6C complete
  and restore the preserved Phase 6 pointer at final L2 acceptance.
- If any gate fails or traffic is insufficient, keep Phase 6C `in_progress`.
- In both cases release the claim with `claimed_by: null`.

**Step 3: Validate and commit exact path**

```bash
git diff --check
git add -- docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "docs: record Phase 6C context cutover status"
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

Do not deploy a documentation-only terminal status commit.
