# Recognition and Context Path Retirement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Retire the V1 recognition and contextual fallback paths so production has one authoritative message authority while preserving historical reads and continued support for new group formats.

**Architecture:** First make missing authoritative wiring fail closed while leaving old code present. After a seven-day natural-message observation, mechanically extract the active authoritative projector from `message_recognition.py`, enforce import boundaries, and then delete unreachable V1 listener, provider, local-parser, group-profile, and lifecycle-heuristic execution paths. Keep immutable evidence, exact contextual resolution, instruction-item projection, and all historical rows.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy, Telethon, pytest, Ruff, SQLite, systemd, existing GitHub/server deployment workflow.

---

## Constraints and checkpoints

- Use `@test-driven-development` for every behavior change.
- Use `@systematic-debugging` for any unexpected test or production result.
- Use `@requesting-code-review` before each deployment checkpoint.
- Do not replay a historical Telegram message or submit a synthetic trade.
- Do not remove a reader merely because its writer is retired.
- Do not combine code extraction with prompt, threshold, status, target, or
  execution-policy changes.
- Do not perform Tasks 7-12 until Task 6 has recorded seven days of healthy
  natural production intake with zero missing-authority failures.
- Each production deployment must pass the repository safe-window checks in
  `AGENTS.md`, `docs/runbook.md`, and the design document.
- One user turn may stop at any deployment or observation checkpoint. Do not
  compress observation time to finish the plan.

## Task 1: Freeze the active authority boundary in architecture tests

**Files:**
- Create: `tests/test_recognition_authority_architecture.py`
- Reference: `docs/plans/2026-08-08-recognition-context-path-retirement-design.md`

**Step 1: Write the failing production-import test**

Create an AST-based test that identifies production modules and legacy entry
symbols:

```python
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "telegram_kol_research"
PRODUCTION_AUTHORITY_MODULES = (
    "authoritative_recognition.py",
    "context_resolution.py",
    "context_resolution_worker.py",
)
FORBIDDEN_AUTHORITY_IMPORTS = {
    "parse_signal_text",
    "persist_text_signal_candidates",
    "recognize_message_now",
    "recognize_records_with_ai_config",
    "run_mimo_direct_for_message",
    "BITCOIN_JUNZHANG_PROFILE",
}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.architecture
def test_authoritative_modules_do_not_import_legacy_recognizers():
    violations = {
        filename: sorted(_imported_names(SOURCE / filename) & FORBIDDEN_AUTHORITY_IMPORTS)
        for filename in PRODUCTION_AUTHORITY_MODULES
    }
    assert all(not values for values in violations.values()), violations
```

Add a second source assertion that documents the temporary seam:

```python
def test_authoritative_projection_has_one_temporary_legacy_dependency():
    source = (SOURCE / "authoritative_recognition.py").read_text(encoding="utf-8")
    assert source.count("apply_authoritative_mimo_payload") == 2
```

The temporary test makes the current dependency explicit; Task 8 replaces it
with the final independent-boundary assertion.

**Step 2: Run the architecture test**

Run:

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
```

Expected: PASS. If it exposes an unlisted production dependency, update the
inventory, not the implementation.

**Step 3: Add a caller inventory assertion**

Assert that all non-test callers of the four legacy entry symbols are exactly
the currently reviewed files. Use AST names rather than raw substring counts so
comments do not satisfy the test. Expected initial callers:

```text
telegram_live_listener.py -> recognize_message_now
telegram_live_listener.py -> recognize_records_with_ai_config
telegram_live_listener.py -> run_mimo_direct_for_message
telegram_live_listener.py -> persist_text_signal_candidates
web_app.py                 -> recognize_message_now
```

The assertion must fail when an unexpected new production caller is added.

**Step 4: Run and commit**

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
git add tests/test_recognition_authority_architecture.py
git commit -m "test: freeze recognition authority callers"
```

Expected: PASS and one test-only commit.

## Task 2: Seal realtime intake against legacy fallback

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py:104-356`
- Modify: `tests/test_telegram_live_listener.py`

**Step 1: Write failing tests**

Add two tests for a newly persisted message with AI recognition enabled and no
authoritative processor:

```python
@pytest.mark.asyncio
async def test_live_intake_requires_authoritative_processor_when_ai_enabled(...):
    stats = await persist_live_message_event(
        event=event,
        session_factory=session_factory,
        broker=broker,
        ai_recognition_config=configured_ai,
        authoritative_processor=None,
    )
    assert stats["inserted_messages"] == 1
    assert stats["recognition_status"] == "authoritative_processor_required"
    assert legacy_recognizer_calls == []
    assert direct_mimo_calls == []
    assert auto_trade_calls == []


@pytest.mark.asyncio
async def test_live_intake_without_recognition_config_stays_raw_only(...):
    stats = await persist_live_message_event(
        event=event,
        session_factory=session_factory,
        broker=broker,
        ai_recognition_config=None,
        authoritative_processor=None,
    )
    assert stats["inserted_messages"] == 1
    assert signal_candidate_count(session_factory) == 0
```

Also assert there is no `MessageInstructionItem`, `TradeSignal`, or
`ExecutionEvent` for the raw message.

**Step 2: Run the tests and verify failure**

```bash
uv run pytest tests/test_telegram_live_listener.py \
  -k 'requires_authoritative_processor or stays_raw_only' -q
```

Expected: FAIL because the current `else` branch invokes V1 recognition and
direct MiMo comparison.

**Step 3: Implement the fail-closed branch**

In `persist_live_message_event()`, replace the implicit V1 branch with a fixed
result. Preserve raw persistence and downstream UI refresh, but do not create a
business interpretation:

```python
if authoritative_processor is None:
    logger.error(
        "recognition authority unavailable raw_message_id=%s "
        "reason=authoritative_processor_required",
        raw_message.id,
    )
    stats["recognition_status"] = "authoritative_processor_required"
else:
    processing_result = await asyncio.to_thread(
        authoritative_processor,
        raw_message.id,
    )
    # existing authoritative handling remains unchanged
```

Do not call `recognize_message_now`, `run_mimo_direct_for_message`, the conflict
builder, the auto-trade executor, or the legacy lifecycle hook from this
branch.

**Step 4: Delete now-unreachable realtime-only imports and helpers**

Remove the realtime caller imports for:

```python
recognize_message_now
run_mimo_direct_for_message
```

Delete `_build_ai_recognition_conflict_payload()`,
`_classify_deepseek_recognition()`, and `_classify_mimo_recognition()` only
after `rg` proves they have no remaining non-test caller. Do not yet remove the
history fallback imports; Task 3 owns them.

**Step 5: Run focused tests**

```bash
uv run pytest tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/telegram_live_listener.py \
  tests/test_telegram_live_listener.py tests/test_web_live_listener_startup.py
git commit -m "fix: require authority for live recognition"
```

## Task 3: Seal history reconciliation against legacy fallback

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py:865-1135`
- Modify: `tests/test_telegram_live_listener.py`

**Step 1: Write the failing history test**

```python
@pytest.mark.asyncio
async def test_history_reconcile_without_authority_persists_raw_only(...):
    result = await run_reconcile_once(
        client=client,
        session_factory=session_factory,
        broker=broker,
        target_titles={"group"},
        authoritative_processor=None,
        discover_dialogs_fn=fake_dialogs,
        fetch_dialog_messages_fn=fake_messages,
    )
    assert result["inserted_messages"] == 1
    assert result["inserted_candidates"] == 0
    assert result["inserted_trade_ideas"] == 0
    assert result["recognition_status"] == "authoritative_processor_required"
```

Assert no fallback recognizer or local parser call.

**Step 2: Run and verify failure**

```bash
uv run pytest tests/test_telegram_live_listener.py \
  -k 'history_reconcile_without_authority' -q
```

Expected: FAIL because `recognize_records_with_ai_config(...,
fallback_recognizer=persist_text_signal_candidates)` currently runs.

**Step 3: Remove the history fallback**

Replace the `else` branch with raw-only accounting and the fixed reason. Do not
call `persist_trade_ideas_from_candidates()` for records that were not
authoritatively processed in the current run.

```python
if authoritative_processor is None:
    logger.error(
        "history recognition authority unavailable "
        "reason=authoritative_processor_required"
    )
    recognition_status = "authoritative_processor_required"
else:
    # existing authoritative loop
```

Return `recognition_status` in the bounded result. Remove unused imports of
`persist_text_signal_candidates` and `recognize_records_with_ai_config` from
the listener module.

**Step 4: Run focused tests and commit**

```bash
uv run pytest tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py -q
git add src/telegram_kol_research/telegram_live_listener.py \
  tests/test_telegram_live_listener.py
git commit -m "fix: require authority for history recognition"
```

Expected: PASS.

## Task 4: Remove the web application's V1 recognizer default

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:70-80`
- Modify: `src/telegram_kol_research/web_app.py:3414-3445`
- Modify: `src/telegram_kol_research/web_app.py:3915-3930`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Write the failing construction test**

```python
def test_web_app_does_not_install_v1_message_recognizer(tmp_path):
    app = create_web_app(tmp_path / "research.db")
    assert not hasattr(app.state, "message_recognizer")
```

Change tests that intentionally exercised `app.state.message_recognizer` to
call the authoritative processor fixture or `process_authoritative_message()`
directly. Keep assertions about durable results, not implementation attributes.

**Step 2: Run and verify failure**

```bash
uv run pytest tests/test_web_app.py \
  -k 'message_recognizer or authoritative' -q
```

Expected: FAIL because the app currently defaults to
`recognize_message_now`.

**Step 3: Remove the state and constructor argument**

Delete:

```python
from telegram_kol_research.message_recognition import recognize_message_now
```

Remove the `message_recognizer` constructor parameter and assignment:

```python
app.state.message_recognizer = message_recognizer or recognize_message_now
```

Do not replace it with a second generic callback. The existing
`app.state.authoritative_processor` is the sole production entry point.

**Step 4: Tighten the caller inventory and commit**

Update the architecture test so `web_app.py -> recognize_message_now` is no
longer allowed.

```bash
uv run pytest tests/test_web_app.py \
  tests/test_web_live_listener_startup.py \
  tests/test_recognition_authority_architecture.py -q
git add src/telegram_kol_research/web_app.py tests/test_web_app.py \
  tests/test_recognition_authority_architecture.py
git commit -m "refactor: remove web v1 recognizer wiring"
```

## Task 5: Add a deterministic monitor signal for missing authority

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `docs/runbook.md`

**Step 1: Write the failing monitor test**

Add a bounded journal fixture containing:

```text
recognition authority unavailable raw_message_id=123 reason=authoritative_processor_required
```

Assert:

```python
assert result.reason_codes == ("authoritative_processor_required",)
assert result.severity == "critical"
```

The operator message must say that raw intake may continue but automatic
interpretation is stopped; it must not include raw text or IDs.

**Step 2: Run and verify failure**

```bash
uv run pytest tests/test_production_safety_monitor.py \
  -k authoritative_processor_required -q
```

Expected: FAIL because the reason is not classified.

**Step 3: Add the closed reason**

Add `authoritative_processor_required` to the closed monitor reason catalog and
human-readable message mapping. Reuse the existing bounded journal adapter; do
not add a writable database path or a new notification service.

**Step 4: Document operator response**

In `docs/runbook.md`, state:

- do not enable a legacy recognizer;
- inspect production construction and current commit;
- keep raw intake running if healthy;
- repair authority wiring and redeploy through a safe window;
- never replay older-than-gap messages automatically.

**Step 5: Run and commit**

```bash
uv run pytest tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py -q
git add src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py docs/runbook.md
git commit -m "feat: monitor missing recognition authority"
```

## Task 6: Review, deploy the sealed entry point, and observe

**Files:**
- Modify after successful verification: `docs/plans/2026-08-08-recognition-context-path-retirement.md`
- Modify after successful verification: `docs/runtime-incident-agent-status.md` only if the existing status file routes this production fact

**Step 1: Run the complete Stage 1 regression**

```bash
uv run pytest -q \
  tests/test_recognition_authority_architecture.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py \
  tests/test_web_app.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_evidence.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_production_safety_monitor.py
uv run ruff check src tests
git diff --check
```

Expected: PASS.

**Step 2: Request code review**

Use `@requesting-code-review`. Resolve every Critical or Important authority,
fallback, raw-intake, restart, notification, and missing-test finding. Rerun
Step 1 after fixes.

**Step 3: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Prove a production safe window**

Use the existing read-only gate. Require two stable passes with:

- latest raw and recognition decision terminal;
- no evidence extraction claim;
- no context attempt claim;
- no ready/running management component;
- no active entry revision or assembly wake claim;
- no position mutation, rescue, Runtime Agent, or notification claim;
- complete stable exchange and protection readback.

**Step 5: Deploy through the approved helper**

```bash
./scripts/server_git_update.sh
```

Expected: exact reviewed SHA, editable package installed, bounded clean restart,
service active, HTTP 200.

**Step 6: Run server verification**

Run the focused Stage 1 suite on the server and the independent no-notify
monitor diagnostic. Verify raw intake and the next natural recognition. Do not
send a test message.

**Step 7: Observe for seven days**

Record daily, bounded evidence:

- latest raw/decision freshness;
- count of `authoritative_processor_required` since activation: exactly zero;
- count of new candidates with legacy `parse_source`: exactly zero;
- context completion and exhaustion counts;
- no historical replay;
- no unexpected exchange write;
- monitor health.

If missing authority occurs, stop the retirement and repair production wiring;
do not re-enable V1. If a new message format fails, handle it through the
authoritative evidence/context contract in a separate change.

**Step 8: Record the checkpoint and commit**

Document dates, deployed SHA, natural message counts, and exact zero results.

```bash
git add docs/plans/2026-08-08-recognition-context-path-retirement.md \
  docs/runtime-incident-agent-status.md
git commit -m "docs: record recognition authority observation"
git push origin codex/deepcoin-auto-trading-v1
```

Do not continue until the full observation criterion is satisfied.

## Task 7: Add authoritative projection characterization fixtures

**Files:**
- Create: `tests/fixtures/recognition_authority/`
- Create: `tests/test_authoritative_projection_characterization.py`
- Reference: `tests/test_authoritative_recognition.py`
- Reference: `tests/test_message_recognition.py`

**Step 1: Create redacted fixtures**

Add minimal synthetic fixtures for:

```text
text_entry.json
image_entry.json
non_strategy.json
full_exit.json
partial_take_profit.json
cancel_entry.json
adjust_stop.json
multi_target_partial_take_profit.json
context_resolved_exit.json
context_revision.json
entry_fragments.json
malformed_payload.json
```

Each fixture contains only synthetic chat/message/lifecycle IDs and expected
durable row projections. Do not copy production message text or identifiers.

**Step 2: Write the characterization harness**

Call the current `apply_authoritative_mimo_payload()` and serialize only stable
fields:

```python
def project_fixture(session_factory, fixture):
    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=fixture["raw_message_id"],
        payload=fixture["payload"],
        model="fixture-mimo",
        authoritative_generation="fixture-generation",
        multi_target_management_config=fixture_config(),
    )
    return {
        "recognition": stable_recognition(result),
        "candidates": stable_candidates(session_factory),
        "instructions": stable_instruction_items(session_factory),
        "envelopes": stable_envelopes(session_factory),
        "targets": stable_targets(session_factory),
        "fragments": stable_fragments(session_factory),
    }
```

Exclude timestamps and database-generated IDs from expected snapshots; include
relationships, statuses, reason codes, exact lifecycle identities, and
fingerprints.

**Step 3: Run the characterization suite**

```bash
uv run pytest tests/test_authoritative_projection_characterization.py -q
```

Expected: PASS against the pre-extraction implementation.

**Step 4: Commit fixtures only**

```bash
git add tests/fixtures/recognition_authority \
  tests/test_authoritative_projection_characterization.py
git commit -m "test: characterize authoritative projection"
```

## Task 8: Extract the authoritative projection façade

**Files:**
- Create: `src/telegram_kol_research/authoritative_projection.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:25-35`
- Modify: `src/telegram_kol_research/message_recognition.py:2758-3157`
- Modify: `tests/test_authoritative_projection_characterization.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Write the failing import-boundary test**

Replace the temporary seam assertion with:

```python
@pytest.mark.architecture
def test_authoritative_modules_do_not_import_message_recognition():
    modules = (
        "authoritative_recognition.py",
        "authoritative_projection.py",
        "context_resolution.py",
        "context_resolution_worker.py",
    )
    violations = imports_from_modules(modules, {"message_recognition"})
    assert violations == []
```

Expected initial result: FAIL.

**Step 2: Create the façade and move the public function**

Move `apply_authoritative_mimo_payload()` and its committed multi-target capture
helpers into `authoritative_projection.py`. Preserve its signature exactly.
Move code; do not copy and maintain two implementations.

Initially import the still-shared private helpers explicitly from a temporary
compatibility module if necessary. Do not import the `recognize_message_now`
entry point or V1 provider functions.

Update `authoritative_recognition.py`:

```python
from telegram_kol_research.authoritative_projection import (
    apply_authoritative_mimo_payload,
)
```

Add only a temporary re-export in `message_recognition.py` for tests or
historical callers:

```python
from telegram_kol_research.authoritative_projection import (
    apply_authoritative_mimo_payload,
)
```

The re-export must be removed in Task 10.

**Step 3: Run the exact characterization suite**

```bash
uv run pytest tests/test_authoritative_projection_characterization.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py -q
```

Expected: PASS with identical fixture snapshots.

**Step 4: Run the architecture test**

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
```

Expected: the final boundary may still fail on shared private-helper imports.
List those helpers explicitly for Task 9; do not weaken the forbidden-module
assertion.

**Step 5: Commit the mechanical move**

```bash
git add src/telegram_kol_research/authoritative_projection.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_authoritative_projection_characterization.py \
  tests/test_recognition_authority_architecture.py
git commit -m "refactor: extract authoritative projection"
```

## Task 9: Move the active projection dependency closure

**Files:**
- Modify: `src/telegram_kol_research/authoritative_projection.py`
- Create if the split is justified: `src/telegram_kol_research/authoritative_entry_projection.py`
- Create if the split is justified: `src/telegram_kol_research/authoritative_management_projection.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `tests/test_authoritative_projection_characterization.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Generate the exact private-helper dependency list**

Use AST call analysis and `rg` to list every private helper referenced by the
extracted projector. Classify each as:

- active entry projection;
- active management projection;
- shared serialization/upsert;
- V1-only.

Review the classification before moving code. In particular, do not delete or
misclassify exact management scope, multi-target projection,
`_project_authoritative_instruction_items`, `_upsert_entry_signal_candidate`,
symbol/side normalization, lifecycle creation, duplicate protection, or source
generation handling.

**Step 2: Write failing module-independence tests**

Assert the new authoritative modules do not import:

```text
message_recognition
parsing.text_parser
recognition_profiles
recognition_experiments.run_mimo_direct_for_message
```

Also assert they do not reference these symbols:

```text
recognize_message_now
_apply_bitcoin_junzhang_profile_if_matched
_apply_lifecycle_transition_signal_if_matched
```

**Step 3: Move one cohesive helper family at a time**

Use separate commits for:

1. common normalization and stable result projection;
2. entry candidate/lifecycle projection;
3. management/multi-target/instruction projection;
4. committed failure capture.

After each move run:

```bash
uv run pytest tests/test_authoritative_projection_characterization.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py -q
```

Expected: PASS with unchanged snapshots.

**Step 4: Make the architecture test pass**

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
```

Expected: PASS with no authoritative import of `message_recognition`.

**Step 5: Commit the final closure**

```bash
git add src/telegram_kol_research/authoritative_projection.py \
  src/telegram_kol_research/authoritative_entry_projection.py \
  src/telegram_kol_research/authoritative_management_projection.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_authoritative_projection_characterization.py \
  tests/test_recognition_authority_architecture.py
git commit -m "refactor: isolate authoritative projection core"
```

Omit nonexistent optional modules from `git add` if the reviewed extraction
uses one smaller module.

## Task 10: Remove V1 entry points and production-only tests

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_recognition_experiments.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Tighten the caller test to require zero callers**

Require zero production callers and zero definitions for:

```text
recognize_message_now
recognize_records_with_ai_config
run_mimo_direct_for_message
```

Require `persist_text_signal_candidates` to have no recognition/live-listener
caller. It may remain only if another reviewed non-recognition feature still
needs it.

**Step 2: Run and verify failure**

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
```

Expected: FAIL while definitions remain.

**Step 3: Remove V1 orchestration**

Delete:

- `recognize_message_now()`;
- `recognize_records_with_ai_config()`;
- V1 DeepSeek provider orchestration used only by those functions;
- OCR-to-local-parser candidate creation;
- direct MiMo experiment execution used only for production comparison;
- the temporary re-export of `apply_authoritative_mimo_payload`.

Retain general OCR/media utilities only if the authoritative evidence pipeline
or historical display imports them. Move retained utilities to their owning
module before deleting their old location.

**Step 4: Remove obsolete tests, not coverage**

Delete tests asserting that production falls back. Move still-valid payload,
normalization, historical-read, or projection assertions to the authoritative
test files before removing the V1 tests.

**Step 5: Run focused and architecture tests**

```bash
uv run pytest -q \
  tests/test_recognition_authority_architecture.py \
  tests/test_authoritative_projection_characterization.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_evidence.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/telegram_live_listener.py \
  tests/test_message_recognition.py tests/test_telegram_live_listener.py \
  tests/test_recognition_experiments.py \
  tests/test_recognition_authority_architecture.py
git commit -m "refactor: retire v1 recognition entry points"
```

## Task 11: Remove group-profile and local lifecycle mutation paths

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify or delete if unused: `src/telegram_kol_research/recognition_profiles.py`
- Modify: `tests/test_message_recognition.py`
- Modify or delete if unused: `tests/test_recognition_profiles.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Write the zero-definition assertions**

Assert the repository no longer defines or calls the V1 entry families:

```text
_apply_bitcoin_junzhang_profile_if_matched
_apply_bitcoin_junzhang_management_if_matched
_apply_lifecycle_transition_signal_if_matched
_apply_entry_confirmation_signal_if_matched
_apply_exit_signal_if_matched
_apply_cancel_signal_if_matched
_apply_pending_entry_invalidation_if_matched
```

Do not include similarly named helpers used by the authoritative projector
unless Task 9 proved they are V1-only.

**Step 2: Run and verify failure**

```bash
uv run pytest tests/test_recognition_authority_architecture.py -q
```

Expected: FAIL.

**Step 3: Delete the V1-only closure**

Use the caller inventory to delete group-profile parsing, local lifecycle
mutation, and their candidate-upsert helpers. If `recognition_profiles.py`
contains display or configuration data still used outside V1, keep that data
and rename the module to reflect its actual owner in a separate mechanical
commit.

**Step 4: Preserve historical readers**

Add a test that loads representative rows with legacy values such as
`exit_heuristic`, `cancel_heuristic`, and the old group-profile parse source and
renders them without importing the retired writer functions.

**Step 5: Run and commit**

```bash
uv run pytest -q \
  tests/test_recognition_authority_architecture.py \
  tests/test_authoritative_projection_characterization.py \
  tests/test_message_recognition.py \
  tests/test_recognition_profiles.py \
  tests/test_strategy_records.py \
  tests/test_web_queries.py
git add src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/recognition_profiles.py \
  tests/test_message_recognition.py tests/test_recognition_profiles.py \
  tests/test_recognition_authority_architecture.py \
  tests/test_strategy_records.py tests/test_web_queries.py
git commit -m "refactor: retire legacy recognition heuristics"
```

Adjust the staged file list for files proven fully unused and deleted.

## Task 12: Narrow contextual resolution to two invocation modes

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/context_resolution_worker.py`
- Modify: `src/telegram_kol_research/web_app.py:3229-3410`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_context_resolution_worker.py`
- Modify: `tests/test_recognition_authority_architecture.py`

**Step 1: Write invocation-boundary tests**

Assert contextual resolution can be invoked only by:

```text
authoritative_recognition.assess_message_authoritatively
web_app._run_context_resolution_worker_for_app -> run_context_resolution_once
```

Assert scheduler event types are exactly the closed `EVENT_TRIGGER_MAP` keys
and that an unchanged context fingerprint produces no new attempt.

**Step 2: Add production-shaped behavior tests**

Cover:

- no trigger: resolver not called;
- one initial trigger: one resolver call;
- unchanged scheduled fingerprint: no reanalysis;
- allowlisted state change: one claimed reanalysis;
- terminal instruction: no reanalysis;
- exhausted attempt: no deployment-triggered replay;
- reused first-pass evidence: no new MiMo evidence request.

**Step 3: Run and verify current differences**

```bash
uv run pytest tests/test_authoritative_recognition.py \
  tests/test_context_resolution_worker.py \
  tests/test_recognition_authority_architecture.py -q
```

Expected: any duplicate or compatibility invocation identified by the tests
fails explicitly.

**Step 4: Remove only proven duplicate invocations**

Keep the initial resolver and durable worker. Remove compatibility schedulers,
retries, or callbacks that do not correspond to a closed event and changed
fingerprint. Do not change the resolver prompt, parser, confidence threshold,
candidate generator, or exact-risk-reduction policy in this task.

**Step 5: Run and commit**

```bash
uv run pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_context_resolution_prompt.py \
  tests/test_recognition_authority_architecture.py
git add src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/context_resolution_worker.py \
  src/telegram_kol_research/web_app.py \
  tests/test_authoritative_recognition.py \
  tests/test_context_resolution_worker.py \
  tests/test_recognition_authority_architecture.py
git commit -m "refactor: narrow contextual resolution entry points"
```

## Task 13: Complete regression and quantitative cleanup review

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/plans/2026-08-08-recognition-context-path-retirement-design.md` only if implementation clarified a non-semantic boundary
- Modify: `docs/plans/2026-08-08-recognition-context-path-retirement.md`

**Step 1: Measure subtraction**

Record before/after values for:

- production recognition entry points;
- production callers of legacy symbols;
- lines and function count in `message_recognition.py`;
- recognition/context test count;
- authoritative import violations;
- supported historical `parse_source` readers.

The required semantic result is one authority and zero production legacy
callers. Line-count reduction alone is not success.

**Step 2: Run the complete focused suite**

```bash
uv run pytest -q \
  tests/test_recognition_authority_architecture.py \
  tests/test_authoritative_projection_characterization.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_evidence.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_context_resolution_prompt.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py \
  tests/test_web_app.py \
  tests/test_strategy_records.py \
  tests/test_web_queries.py \
  tests/test_auto_trade_execution.py \
  tests/test_production_safety_monitor.py
```

Expected: PASS.

**Step 3: Run repository-wide validation**

```bash
uv run pytest -q
uv run ruff check src tests scripts
git diff --check
```

Expected: PASS. Report any proven pre-existing unrelated failure separately;
do not weaken or deselect a cleanup regression to obtain green output.

**Step 4: Request final code review**

Use `@requesting-code-review`. Require explicit review of:

- historical-read compatibility;
- authoritative generation ownership;
- context retry and exhaustion;
- multi-target and adjacent-entry projection;
- source edit/deletion behavior;
- raw intake during authority failure;
- absence of hidden fallback;
- rollback feasibility.

Resolve findings and rerun Steps 2-3.

**Step 5: Update operator documentation**

Document the single authority, raw-only fail-closed behavior, context invocation
modes, historical reader boundary, monitoring reason, and rollback. Remove
instructions that tell an operator to enable or call V1 recognition.

**Step 6: Commit documentation**

```bash
git add docs/runbook.md docs/server-deployment.md \
  docs/plans/2026-08-08-recognition-context-path-retirement-design.md \
  docs/plans/2026-08-08-recognition-context-path-retirement.md
git commit -m "docs: finalize recognition path retirement"
```

## Task 14: Deploy the extracted single authority and verify naturally

**Files:**
- Modify after successful verification: `docs/plans/2026-08-08-recognition-context-path-retirement.md`
- Modify after successful verification: `docs/runtime-incident-agent-status.md` only when appropriate

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 2: Prove the safe window twice**

Repeat the Task 6 gate. Additionally require no new
`authoritative_processor_required`, no new legacy candidate source, and no
nonterminal recognition/context row created by the cleanup observation.

**Step 3: Deploy**

```bash
./scripts/server_git_update.sh
```

Expected: exact reviewed SHA, clean bounded restart, active service, HTTP 200.

**Step 4: Run deployed focused tests and read-only checks**

Run architecture, authoritative projection, context, listener, historical-read,
and monitor tests on the server. Run the no-notify diagnostic. Do not inject a
message or call a Deepcoin mutation.

**Step 5: Verify the first natural examples**

Observe, without replay:

- one ordinary non-strategy message;
- one new entry if naturally received;
- one management message if naturally received;
- one context-triggered message if naturally received;
- one image message if naturally received.

For each available class, trace raw message, evidence version, recognition
decision, optional context attempt, instruction items, automation outcome, and
exchange reconciliation. Missing natural examples remain an explicit pending
canary; they are not synthesized.

**Step 6: Verify invariants**

Require:

- zero production legacy entry points and callers in deployed source;
- zero `authoritative_processor_required` events;
- zero new legacy `parse_source` rows;
- no historical row replay or rewrite;
- no target, ownership, protection, or idempotency regression;
- healthy service, listener, Runtime Agent, scanner, timer, and monitor;
- complete stable exchange/protection readback.

**Step 7: Record and commit the production checkpoint**

```bash
git add docs/plans/2026-08-08-recognition-context-path-retirement.md \
  docs/runtime-incident-agent-status.md
git commit -m "docs: record single authority verification"
git push origin codex/deepcoin-auto-trading-v1
```

If any natural-message class remains unobserved, record it as pending and do
not claim full behavioral completion. The retired path remains removed; future
format compatibility work continues through authoritative evidence and
contextual resolution.
