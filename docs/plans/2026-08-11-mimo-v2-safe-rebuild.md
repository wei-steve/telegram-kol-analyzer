# MiMo v2 Safe Rebuild Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild Tasks 6 through 11 from the reviewed Task 5 baseline with stale-context protection, atomic circuit state, complete automatic-trading equivalence tests, and truthful MiMo-first Web observability.

**Architecture:** Keep the existing v1 authority and executor as the only production path while v2 remains disabled. Persist strict source-separated v2 evidence, gate v2 by a future watermark and atomic circuit breaker, and adapt v2 into the existing authority only before the existing execution claim. Carry the exact joint message/media/context fingerprint to the final execution-claim boundary, retry one whole analysis after the first context change, and safely stop after a second change.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite, Pydantic-style closed dataclasses/parsers, FastAPI/Jinja, pytest, fake Deepcoin clients.

---

## Execution Rules

- Work only in `/Users/steven/Documents/telegram获取消息-mimo-v2-safe-rebuild` on branch `codex/mimo-v2-safe-rebuild`.
- Start every behavior change with a focused failing test and observe the expected failure.
- Commit each task independently. Request code review after every task and resolve all Critical/Important findings before proceeding.
- Do not change production, push the canonical production branch, enable v2, set a watermark, or start Task 12 in this plan.
- Preserve the existing v1 authority, authoritative generation, execution claim and established execution modules.

### Task 6: Persist source-separated v2 evidence and bind it to the audited run

**Files:**
- Modify: `src/telegram_kol_research/models.py:202-245`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/message_evidence.py:491-625`
- Test: `tests/test_message_evidence.py`
- Test: `tests/test_db_migrations.py`

**Step 1: Write failing schema and persistence tests**

Add tests that require:

```python
def test_v2_evidence_links_completed_run_and_keeps_sources_separate(tmp_path):
    result, run_id, raw_id, media_id = seed_valid_v2_result(tmp_path)
    row = persist_mimo_v2_message_evidence(
        factory,
        raw_message_id=raw_id,
        result=result,
        run_id=run_id,
        model="mimo-v2.5",
        prompt_versions={"trading.analysis.mimo_v2_authoritative": 1},
        media_root=tmp_path,
    )
    assert row.mimo_recognition_run_id == run_id
    assert json.loads(row.text_evidence_json) == result.evidence.text.to_dict()
    assert json.loads(row.image_evidence_json)["images"][0]["asset_id"] == media_id
    normalized = json.loads(row.normalized_evidence_json)
    assert normalized["contract_version"] == "mimo-authoritative-v2"
    assert "data:image" not in row.image_evidence_json


def test_v2_evidence_rejects_image_owned_by_another_message(tmp_path): ...
def test_v2_evidence_rejects_running_or_non_authoritative_run(tmp_path): ...
def test_v2_evidence_rejects_canonical_fingerprint_mismatch(tmp_path): ...
def test_v2_evidence_finalize_refuses_changed_message_or_expired_claim(tmp_path): ...
def test_migration_adds_nullable_mimo_run_foreign_key_and_index(tmp_path): ...
```

Also add a negative test whose model output contains a `data:image/...;base64,`
string in an evidence text field. Persistence must reject it instead of merely
checking that normal fixtures do not contain bytes.

**Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_message_evidence.py \
  tests/test_db_migrations.py -k 'mimo or evidence'
```

Expected: FAIL because `mimo_recognition_run_id` and v2-specific persistence do
not exist.

**Step 3: Add the nullable run relationship and additive migration**

Add to `MessageEvidenceVersion`:

```python
mimo_recognition_run_id: Mapped[int | None] = mapped_column(
    ForeignKey("mimo_recognition_runs.id"), nullable=True, index=True
)
```

Add an idempotent SQLite migration in the established `db.py` migration map.
Do not remove or reinterpret existing evidence rows.

**Step 4: Implement v2-only serialization and atomic claim finalization**

Create:

```python
def persist_mimo_v2_message_evidence(..., result: MimoV2Result, run_id: int, ...):
    ...

def finalize_claimed_mimo_v2_message_evidence(
    session_factory,
    *,
    raw_message_id: int,
    claim_token: str,
    expected_input_fingerprint: str,
    result: MimoV2Result,
    run_id: int,
    model: str,
    prompt_versions: Mapping[str, Any],
    media_root: str | Path,
) -> MessageEvidenceVersion | None:
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        # Verify the live claim, current message/media fingerprint, image asset
        # ownership, completed authoritative run, canonical payload fingerprint,
        # and absence of embedded image bytes/secrets before the one commit.
        ...
```

Serialize text, images/conflicts and normalized intents into separate JSON
columns. Retain confidence `0.0` rather than replacing it with a fallback.

**Step 5: Run focused and migration tests**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_message_evidence.py \
  tests/test_db_migrations.py
```

Expected: PASS.

**Step 6: Review and commit Task 6**

```bash
git diff --check
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/message_evidence.py \
  tests/test_message_evidence.py tests/test_db_migrations.py
git commit -m "feat: persist safe mimo v2 evidence"
```

Request code review with base `998da1a` and the new HEAD. Do not begin Task 7
until no Critical/Important issue remains.

### Task 7: Add future-only mode and an atomic durable circuit breaker

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Create: `src/telegram_kol_research/mimo_contract_circuit.py`
- Test: `tests/test_trading_settings.py`
- Create: `tests/test_mimo_contract_circuit.py`
- Test: `tests/test_db_migrations.py`

**Step 1: Write failing mode and sequential circuit tests**

Require defaults and validation:

```python
assert TradingSettings().mimo_contract_mode == "v1"
assert TradingSettings().mimo_v2_activation_after_raw_message_id == 0

@pytest.mark.parametrize("value", ["shadow", "v2", "live", ""])
def test_mimo_contract_mode_fails_closed(value): ...
```

Require immediate open for contract/adapter failures, open after exactly three
transport failures, reset after success, and no counter changes for business
outcomes or safety refusals.

**Step 2: Write the failing concurrent update test**

```python
def test_three_concurrent_transport_failures_open_circuit(tmp_path):
    factory = create_session_factory(tmp_path / "circuit.db")
    barrier = threading.Barrier(3)

    def fail_once():
        barrier.wait()
        return record_mimo_v2_outcome(factory, outcome="provider_timeout")

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: fail_once(), range(3)))

    state = load_mimo_contract_circuit(factory)
    assert state.consecutive_transport_failures == 3
    assert state.is_open is True
```

**Step 3: Run tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_trading_settings.py -k mimo \
  tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py -k circuit
```

Expected: FAIL because the settings and circuit table/API do not exist.

**Step 4: Implement settings, table and atomic update**

Add the singleton state model and an additive migration. Implement the update
with a serialized write transaction:

```python
def record_mimo_v2_outcome(session_factory, *, outcome, observed_at=None):
    if outcome not in _OUTCOMES:
        raise ValueError("mimo_v2_outcome_invalid")
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(MimoContractCircuitState, 1)
        if row is None:
            row = MimoContractCircuitState(id=1)
            session.add(row)
            session.flush()
        _apply_outcome(row, outcome, observed_at or utc_now())
        session.commit()
        session.refresh(row)
        return _snapshot(row)
```

Do not hold the transaction during a provider request. Do not add shadow mode.

**Step 5: Run focused tests repeatedly**

```bash
for i in 1 2 3 4 5; do
  PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
    tests/test_mimo_contract_circuit.py::test_three_concurrent_transport_failures_open_circuit
done
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_trading_settings.py \
  tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py
```

Expected: every run PASS.

**Step 6: Review and commit Task 7**

```bash
git diff --check
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/mimo_contract_circuit.py \
  tests/test_trading_settings.py tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py
git commit -m "feat: add atomic mimo v2 circuit gate"
```

Request code review and resolve Critical/Important findings.

### Task 8A: Integrate v2 authority with one pre-claim fallback and no side effects

**Files:**
- Modify: `src/telegram_kol_research/recognition_experiments.py:290-655`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:664-880`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write failing eligibility/fallback tests**

Cover:

```python
def test_v2_runs_only_strictly_above_future_watermark(...): ...
def test_open_circuit_routes_future_message_to_v1_without_v2_call(...): ...
def test_contract_failure_falls_back_once_before_execution_claim(...): ...
def test_v2_never_falls_back_after_execution_claim(...): ...
def test_v2_and_v1_cannot_both_create_execution_candidates(...): ...
```

Assert fallback run lineage uses `retry_of_run_id`, and that a failed v2 run
cannot itself create evidence/candidates.

**Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q tests/test_authoritative_recognition.py \
  -k 'mimo_v2 and (watermark or circuit or fallback or claim)'
```

Expected: FAIL because v2 is not connected to the coordinator.

**Step 3: Add future eligibility and audited v1 fallback**

Implement a small eligibility helper that requires mode `v2_live_adapter`, raw
message ID strictly greater than the watermark, and a closed circuit. Route
strict v2 success through the existing `apply_authoritative_mimo_payload` path.
On allowed technical failures, create exactly one `v1_fallback` run before any
execution claim. Preserve current behavior for v1 mode.

**Step 4: Run focused tests and commit Task 8A**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py
git diff --check
git add src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/recognition_decisions.py \
  tests/test_authoritative_recognition.py tests/test_message_recognition.py
git commit -m "feat: route mimo v2 through existing authority"
```

Request review before continuing to the context-safety half of Task 8.

### Task 8B: Retry once on joint-context drift and recheck before execution claim

**Files:**
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `src/telegram_kol_research/mimo_recognition_runs.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write a failing prior-message drift integration test**

The test must exercise `assess_message_authoritatively`, not only the standalone
inference helper:

```python
def test_v2_discards_first_result_and_retries_after_prior_message_changes(...):
    calls = 0
    def requester(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            update_prior_message_text(factory, "new context")
        return valid_v2_payload(summary=f"run-{calls}")

    assessment = assess_with_v2(requester=requester)
    assert calls == 2
    assert assessment.mimo.payload["summary"] == "run-2"
    assert linked_runs(factory) == [
        ("failed", False, None),
        ("completed", True, first_run_id),
    ]
    assert exactly_one_current_evidence(factory)
```

**Step 2: Write failing lifecycle drift and second-change tests**

```python
def test_v2_retries_after_active_lifecycle_changes(...): ...

def test_second_joint_input_change_stops_without_candidate_or_claim(...):
    # Mutate a context dependency during both whole runs.
    result = process_with_v2(...)
    assert result.automation["reason"] == "mimo_input_changed_twice"
    assert current_candidates(factory) == []
    assert execution_claim(factory) is None
```

**Step 3: Write the failing pre-claim race test**

Use a test hook immediately before `claim_authoritative_execution` to change an
active lifecycle or prior message after evidence finalization:

```python
def test_joint_context_change_before_claim_refuses_execution(...):
    result = process_with_pre_claim_hook(change_active_lifecycle)
    assert result.automation["status"] == "skipped"
    assert result.automation["reason"] == "mimo_input_changed_before_claim"
    assert auto_trade_calls == []
    assert execution_claim(factory) is None
```

**Step 4: Run the three tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q tests/test_authoritative_recognition.py \
  -k 'prior_message_changes or active_lifecycle_changes or before_claim'
```

Expected: FAIL by accepting/persisting/claiming a stale result.

**Step 5: Expose and carry the exact joint fingerprint**

Extend `MimoV2InferenceResult` and `AuthoritativeAssessment` with the selected
analysis fingerprint. Use one public helper for both inference and later checks:

```python
def build_current_mimo_v2_analysis_input_fingerprint(
    session_factory, *, raw_message_id: int, media_root: str | Path
) -> str:
    message_fp = build_current_message_input_fingerprint(...)
    context = build_authoritative_context_for_message(...)
    composition = compose_trading_prompt(
        session_factory,
        model_kind="mimo",
        context=context,
        contract_version="v2",
    )
    return _mimo_v2_analysis_input_fingerprint(
        message_input_fingerprint=message_fp,
        context_text=composition.context,
    )
```

Do not implement a second approximate context serializer.

**Step 6: Implement one bounded whole-run retry**

Have standalone inference return terminal
`input_changed_during_analysis` when its exact joint fingerprint changes. The
coordinator handles that code before fallback:

```python
for whole_run_ordinal in (1, 2):
    v2 = infer_mimo_authoritative_v2(
        ...,
        context_text=None,  # inference builds and rechecks the exact context
        retry_of_run_id=prior_run_id,
    )
    if v2.error_code != "input_changed_during_analysis":
        break
    if whole_run_ordinal == 2:
        return safe_input_changed_assessment(...)
    prior_run_id = v2.run_id
```

An input-change run is never selected authoritative and never enters v1
fallback. Provider attempts inside each whole run remain bounded separately.

**Step 7: Add the final pre-claim check**

Immediately before the existing claim, compare the current joint fingerprint
with `assessment.analysis_input_fingerprint`. On mismatch, save a terminal
non-executing decision and return without calling
`claim_authoritative_execution`, `apply_authoritative_assessment` candidate
persistence, or the auto-trade executor. Do not hold a database transaction
during model calls.

**Step 8: Run focused and authority suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_message_evidence.py
```

Expected: PASS.

**Step 9: Review and commit Task 8B**

```bash
git diff --check
git add src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/mimo_recognition_runs.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_authoritative_recognition.py tests/test_message_recognition.py
git commit -m "fix: reject stale mimo v2 context before execution"
```

Request code review with special attention to evidence-before-claim ordering,
candidate side effects and `reuse_current_evidence` behavior.

### Task 9: Prove complete execution equivalence through current executors

**Files:**
- Create: `tests/test_mimo_v2_execution_equivalence.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_deepcoin_execution_actions.py`
- Modify: `tests/test_message_operation_projection.py`
- Modify only if a test demonstrates an adapter defect: `src/telegram_kol_research/mimo_v2_execution_adapter.py`
- Modify only if a test demonstrates a coordinator defect: `src/telegram_kol_research/authoritative_recognition.py`

**Step 1: Build shared v1/v2 execution fixtures**

Define `EXECUTION_FIXTURES` for entry, cancel, full exit, partial exit, partial
take profit, move stop, hold, revision and supported multi-action. Each fixture
must provide semantically equivalent legacy v1 and strict v2 model payloads.

**Step 2: Write failing full-snapshot parameterized tests**

```python
@pytest.mark.parametrize("fixture_name", EXECUTION_FIXTURES)
def test_v2_adapter_matches_v1_execution_snapshot(fixture_name, tmp_path):
    v1 = run_fixture_through_current_path(fixture_name, tmp_path / "v1.db")
    v2 = run_fixture_through_v2_adapter(fixture_name, tmp_path / "v2.db")
    assert normalize_snapshot(v2) == normalize_snapshot(v1)
```

The snapshot must include candidates, instruction items/order, lifecycle and
binding ownership, risk reservation, order drafts, idempotency keys, fake
Deepcoin request bodies, and automation status/reason.

**Step 3: Write explicit refusal equivalence tests**

Cover target ambiguity, missing verified entry, source deletion barrier, risk
rejection, duplicate idempotency, disabled management, and deferred execution.
Assert both inputs produce the same skip/block/defer reason and zero fake
exchange calls where required.

**Step 4: Run the intended equivalence suite and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_mimo_v2_execution_equivalence.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_message_operation_projection.py
```

Expected: new cases FAIL until the fixture harness and any demonstrated minimal
adapter corrections are complete; existing tests remain green.

**Step 5: Make only minimal v2 adapter/coordinator corrections**

Do not change the established executor or weaken a refusal. If parity exposes
an existing executor defect, stop and request separate approval instead of
folding it into this task.

**Step 6: Run the full equivalence suite**

Run the command from Step 4. Expected: PASS with no network calls.

**Step 7: Review and commit Task 9**

```bash
git diff --check
git add tests/test_mimo_v2_execution_equivalence.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_message_operation_projection.py \
  src/telegram_kol_research/mimo_v2_execution_adapter.py \
  src/telegram_kol_research/authoritative_recognition.py
git commit -m "test: prove complete mimo v2 execution equivalence"
```

Stage only production files that actually changed. Request review.

### Task 10: Project current authority and latest MiMo call as separate Web facts

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Create: `tests/test_web_mimo_analysis_projection.py`
- Modify: `tests/test_web_queries_messages.py`

**Step 1: Write failing successful-v2 and source-separated evidence tests**

Require validated intents, per-image evidence, retry count, failure reason and
separate system acceptance/execution truth. Assert serialization never invokes
a source-text regex/parser.

**Step 2: Write the key failing stale-success/latest-failure test**

```python
def test_web_keeps_current_authority_but_exposes_latest_failed_rerun(tmp_path):
    seed_old_authoritative_v1_evidence(...)
    latest = seed_later_failed_v2_run(error_code="provider_timeout")
    message = load_group_messages(factory, chat_id=88, limit=10)[0]

    assert message["mimo_analysis"]["format"] == "v1"
    assert message["mimo_analysis"]["authoritative_runtime"]["status"] == "completed"
    assert message["mimo_analysis"]["latest_call"]["run_id"] == latest.id
    assert message["mimo_analysis"]["latest_call"]["status"] == "failed"
    assert message["mimo_analysis"]["latest_call"]["error_code"] == "provider_timeout"
```

Also cover latest running rerun, failed v2 followed by authoritative v1
fallback, exhausted v2 without evidence, malformed stored v2, current v1,
historical pre-run v1, zero confidence and cross-message image references.

**Step 3: Run tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_queries_messages.py
```

Expected: FAIL because the projection does not exist.

**Step 4: Implement two independent run selections**

In `_serialize_mimo_analysis`, select:

```python
authoritative_run = run linked by current evidence or latest became_authoritative
latest_run = runs[-1] if runs else None
```

Return both `authoritative_runtime` and `latest_call`; retain `runtime` only as a
documented compatibility alias for `authoritative_runtime`. The latest call
must not replace the canonical evidence or semantic intents.

Validate stored v2 canonical payloads with `parse_mimo_v2_payload` before
projecting summary/intents/images. Invalid semantic data is not rendered.
Resolve media only when the asset belongs to the evidence message.

**Step 5: Project system acceptance without conflating execution**

Match validated intents to current, non-retired MiMo-authoritative candidates
using structured action/target/strategy fields. Report acceptance separately
from the execution outcome and preserve explicit automation failure reasons.

**Step 6: Run Web query tests**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_queries_messages.py
```

Expected: PASS.

**Step 7: Review and commit Task 10**

```bash
git diff --check
git add src/telegram_kol_research/web_queries.py \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_queries_messages.py
git commit -m "feat: project authoritative and latest mimo runs"
```

Request review, especially for query count, stale candidates, cross-message
media and failed-rerun visibility.

### Task 11: Render the MiMo-first one-level message hierarchy

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_group_messages_route.py`

**Step 1: Write failing route/rendering tests**

Assert this order:

1. source message and source media;
2. MiMo current authoritative analysis;
3. latest MiMo call warning/status when it differs;
4. per-image evidence;
5. context resolution;
6. system acceptance;
7. automatic-trading result;
8. collapsed DeepSeek auxiliary review and legacy debug details.

Require native `<details>` for optional raw JSON/attempt history, no JavaScript
toggle dependency, clear current-v1/historical-v1 labels, and visible latest
failure despite an older current result.

**Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q tests/test_web_group_messages_route.py \
  -k 'mimo or authoritative_model_summary or decision_card'
```

Expected: FAIL because the legacy hierarchy is still primary.

**Step 3: Implement the minimal one-level hierarchy**

Render structured values only. Do not reparse message text in Jinja or Python.
Keep legacy debug content available but collapsed; do not silently remove
historical authority details when MiMo v1 projection exists. Use Jinja
autoescaping and `tojson` for raw structured evidence.

**Step 4: Run Web suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_web_group_messages_route.py \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_queries_messages.py
```

Expected: PASS.

**Step 5: Review and commit Task 11**

```bash
git diff --check
git add src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/static/app.css \
  tests/test_web_group_messages_route.py
git commit -m "feat: render safe mimo-first message cards"
```

Request review for accessibility, escaping, historical v1 display and latest
failed-call visibility.

### Final checkpoint: Local regression and independent review only

**Files:**
- Modify only if tests demonstrate a rebuild defect.
- Do not add the Task 12 replay command.

**Step 1: Run focused safety suites**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q \
  tests/test_message_evidence.py \
  tests/test_contextual_message_window.py \
  tests/test_trading_settings.py \
  tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_mimo_v2_execution_equivalence.py \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_group_messages_route.py
```

Expected: PASS.

**Step 2: Run the complete local suite**

```bash
PYTHONPATH=src "/Users/steven/Documents/telegram获取消息/.venv/bin/python" -m pytest -q
git diff --check
```

Expected: PASS and no whitespace errors.

**Step 3: Run independent code review**

Review the complete range `998da1a..HEAD` against
`docs/plans/2026-08-11-mimo-v2-safe-rebuild-design.md`. Resolve every
Critical/Important finding and rerun all affected suites plus the full suite.

**Step 4: Verify production remained untouched**

Read only:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'git -C /opt/telegram-kol-analyzer rev-parse HEAD; \
   systemctl is-active telegram-kol.service'
```

Confirm the production database setting is still `mimo_contract_mode=v1`.
Do not restart or deploy.

**Step 5: Stop before Task 12**

Report the rebuilt commit range, local verification, review result and
production read-only status. Ask for a separate approval before preparing or
running Task 12 isolated replay.
