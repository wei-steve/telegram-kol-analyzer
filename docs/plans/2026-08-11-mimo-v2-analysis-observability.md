# MiMo v2 Analysis and Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Introduce one canonical MiMo v2 intent/evidence result that powers the Web analysis card and deterministically enters the existing automatic-trading safety path, with structured attempts, pre-side-effect v1 fallback, circuit breaking, isolated server replay, and future-only rollback.

**Architecture:** Add a strict v2 contract and pure v2-to-current-execution adapter while preserving all candidate, lifecycle, ownership, binding, risk, idempotency, and Deepcoin gates. Production normally calls MiMo once; a v1 call is allowed only after a technical v2 failure and before any execution side effect. Persist immutable run/attempt audit, source-separated image evidence, and a Web view that renders stored structured data without parsing free-form text.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI, Jinja2, Telethon, httpx, Typer, pytest, existing MiMo/OpenAI-compatible provider, existing prompt registry and Deepcoin execution adapters.

---

Design reference: `docs/plans/2026-08-11-mimo-v2-analysis-observability-design.md`

Implementation constraints:

- Do not enable a production Shadow dual-call mode.
- Keep `mimo_contract_mode=v1` until the isolated server replay passes.
- Do not change `multi_instruction_mode` as part of this work.
- Do not restart or deploy during an in-flight or time-sensitive strategy operation.
- Never retry or fall back after a possible Deepcoin write.
- Use a future-message activation watermark; never replay prior messages.
- Local tests cannot replace server-side MiMo/media verification.

### Task 1: Define and validate the closed MiMo v2 contract

**Files:**
- Create: `src/telegram_kol_research/mimo_v2_contract.py`
- Create: `tests/test_mimo_v2_contract.py`

**Step 1: Write failing parser tests**

Cover one management intent, one entry intent, multiple ordered intents,
informational intent with `action=null`, per-image evidence, evidence references,
unknown intent/action enums, invalid targets, duplicate action identity, incomplete
entry strategy, out-of-range confidence, excessive list sizes, and missing image
references.

```python
def test_parse_position_management_intent_with_image_evidence():
    parsed = parse_mimo_v2_payload(_valid_payload())

    assert parsed.contract_version == "mimo-authoritative-v2"
    assert parsed.intents[0].intent_type == "position_management"
    assert parsed.intents[0].action.kind == "move_stop_to_protect"
    assert parsed.intents[0].action.target_lifecycle_id == 790
    assert parsed.evidence.images[0].asset_id == 381
    assert parsed.evidence.images[0].quality == "clear"


def test_actionable_intent_rejects_unknown_image_reference():
    payload = _valid_payload()
    payload["intents"][0]["evidence_refs"] = ["image:999:side"]

    with pytest.raises(MimoV2ContractError, match="evidence_ref_image_missing"):
        parse_mimo_v2_payload(payload)
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mimo_v2_contract.py
```

Expected: FAIL because `mimo_v2_contract` does not exist.

**Step 3: Implement immutable parsed types and strict validation**

Create frozen dataclasses for `MimoV2Result`, `MimoV2Intent`, `MimoV2Action`,
`MimoV2Evidence`, `MimoV2TextEvidence`, and `MimoV2ImageEvidence`. Use closed
sets and bounded constants.

```python
CONTRACT_VERSION = "mimo-authoritative-v2"
INTENT_TYPES = frozenset({
    "new_strategy", "position_management", "exit", "cancel_entry",
    "strategy_revision", "entry_context", "position_report",
    "market_commentary", "non_trading", "unclear",
})
ACTION_KINDS = frozenset({
    "entry", "cancel_pending_entry", "replace_entry", "full_exit",
    "partial_exit", "partial_take_profit", "move_stop_to_protect",
    "hold_update", "risk_update",
})
MAX_INTENTS = 8
MAX_IMAGES = 8
MAX_EVIDENCE_REFS = 24


class MimoV2ContractError(ValueError):
    pass


def parse_mimo_v2_payload(payload: Mapping[str, Any]) -> MimoV2Result:
    if set(payload) != {
        "contract_version", "summary", "confidence", "intents", "evidence"
    }:
        raise MimoV2ContractError("top_level_fields_invalid")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise MimoV2ContractError("contract_version_invalid")
    # Parse bounded intents/images, validate supported action shapes, verify
    # evidence references, and reject duplicate executable identities.
    return MimoV2Result(...)
```

Do not normalize free-form reasons or derive any missing trade field.

**Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mimo_v2_contract.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/mimo_v2_contract.py tests/test_mimo_v2_contract.py
git commit -m "feat: define mimo v2 intent contract"
```

### Task 2: Add a dedicated versioned MiMo v2 prompt without modifying v1 fallback

**Files:**
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Modify: `src/telegram_kol_research/prompt_composition.py`
- Modify: `src/telegram_kol_research/prompt_registry.py`
- Modify: `src/telegram_kol_research/prompt_testing.py`
- Modify: `tests/test_prompt_composition.py`
- Modify: `tests/test_prompt_registry.py`
- Modify: `tests/test_prompt_testing.py`

**Step 1: Write failing prompt-composition tests**

Assert that:

- v1 composition is byte-for-byte unchanged;
- v2 composition contains exactly one v2 JSON contract;
- v2 includes current business/lifecycle rules and MiMo image rules;
- DeepSeek never receives the v2 MiMo output contract;
- draft prompt testing validates with `parse_mimo_v2_payload`;
- publication rejects a v2 prompt that drops required intent, evidence, source
  separation, or JSON-only instructions.

```python
def test_mimo_v2_composition_has_one_v2_contract_and_preserves_v1():
    v1 = compose_trading_prompt(factory, model_kind="mimo", contract_version="v1")
    v2 = compose_trading_prompt(factory, model_kind="mimo", contract_version="v2")

    assert '"contract_version": "mimo-authoritative-v2"' not in v1.system_prompt
    assert v2.system_prompt.count("mimo-authoritative-v2") == 1
    assert "图文证据分离" in v2.system_prompt
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_prompt_composition.py \
  tests/test_prompt_registry.py \
  tests/test_prompt_testing.py
```

Expected: FAIL because no v2 prompt definition/composition exists.

**Step 3: Seed and compose an independent v2 prompt**

Add a dedicated prompt key, for example:

```python
MIMO_V2_AUTHORITATIVE_PROMPT = "trading.analysis.mimo_v2_authoritative"
```

Keep the active v1 prompt untouched so it remains a safe fallback. The v2
definition contains the approved v2 contract, the same business/lifecycle
rules, bounded per-image evidence, and JSON-only output. Add a v2 validation
profile that rejects removal of critical schema/safety clauses.

Update `compose_trading_prompt(..., contract_version="v1")` so the default is
still v1 and current callers remain unchanged. Only the explicit v2 caller
selects the new prompt.

**Step 4: Run prompt tests**

Run the command from Step 2.

Expected: PASS, including byte-stable v1 composition assertions.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/prompt_defaults.py \
  src/telegram_kol_research/prompt_composition.py \
  src/telegram_kol_research/prompt_registry.py \
  src/telegram_kol_research/prompt_testing.py \
  tests/test_prompt_composition.py tests/test_prompt_registry.py \
  tests/test_prompt_testing.py
git commit -m "feat: add versioned mimo v2 prompt"
```

### Task 3: Add immutable run and attempt audit storage

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/mimo_recognition_runs.py`
- Modify: `tests/test_db_migrations.py`
- Create: `tests/test_mimo_recognition_runs.py`

**Step 1: Write failing schema and persistence tests**

Test additive creation/migration, immutable run creation, ordered attempts,
retry-of linkage, final selection, sanitized errors, payload/projection
fingerprints, and authoritative/fallback run kinds.

```python
def test_run_records_ordered_attempts_and_terminal_selection(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    run = start_mimo_run(
        factory,
        raw_message_id=7,
        run_kind="v2_authoritative",
        contract_version="mimo-authoritative-v2",
        model="mimo-v2.5",
        input_kind="text+image",
        input_fingerprint="sha256:input",
        prompt_versions={"v2": 12},
    )
    record_mimo_attempt(factory, run_id=run.id, ordinal=1, status="timeout", ...)
    record_mimo_attempt(factory, run_id=run.id, ordinal=2, status="completed", ...)
    completed = complete_mimo_run(factory, run_id=run.id, selected_ordinal=2, ...)

    assert completed.attempt_count == 2
    assert completed.status == "completed"
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_db_migrations.py -k mimo \
  tests/test_mimo_recognition_runs.py
```

Expected: FAIL because tables and repository functions do not exist.

**Step 3: Implement additive models and repository**

Add `MimoRecognitionRun` and `MimoRecognitionAttempt` with indexes on
`raw_message_id/status/created_at` and unique `(run_id, ordinal)`. Store only
sanitized error messages and canonical JSON/fingerprints. Use append-only
attempts and guarded terminal run updates.

Do not make attempt-audit persistence a second execution claim. The existing
authoritative generation remains the execution owner.

**Step 4: Run schema and repository tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/mimo_recognition_runs.py \
  tests/test_db_migrations.py tests/test_mimo_recognition_runs.py
git commit -m "feat: persist mimo recognition attempts"
```

### Task 4: Implement the pure v2-to-current-execution adapter

**Files:**
- Create: `src/telegram_kol_research/mimo_v2_execution_adapter.py`
- Create: `tests/test_mimo_v2_execution_adapter.py`
- Modify: `tests/test_authoritative_instructions.py`

**Step 1: Write failing adapter tests**

Cover every action mapping, no-action intents, ordered management-before-entry,
unsupported multi-management combinations, exact target/parameter copying,
and deterministic fingerprints.

```python
def test_move_stop_maps_without_parsing_reason():
    result = parse_mimo_v2_payload(_management_payload(reason="ignore: 1940?"))
    adapted = adapt_mimo_v2_to_current_payload(result)

    assert adapted["recognition_result"] == "非策略"
    assert adapted["lifecycle_event"] == {
        "event_type": "position_update",
        "management_action": "move_stop_to_protect",
        "target_lifecycle_id": 790,
        "stop_loss": "1940",
        "confidence": 0.95,
        "reason": "ignore: 1940?",
    }
    assert adapted["instructions"][0]["kind"] == "move_stop_to_protect"
```

Add a test that changes punctuation in `reason` and proves the adapter output
fields/fingerprint do not change except for the copied audit reason.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_mimo_v2_execution_adapter.py \
  tests/test_authoritative_instructions.py
```

Expected: FAIL because the adapter does not exist.

**Step 3: Implement the pure adapter**

Implement:

```python
@dataclass(frozen=True, slots=True)
class AdaptedMimoV2Payload:
    payload: dict[str, Any]
    canonical_v2_json: str
    canonical_v2_fingerprint: str
    projection_fingerprint: str


def adapt_mimo_v2_to_current_payload(
    result: MimoV2Result,
) -> AdaptedMimoV2Payload:
    instructions = [_adapt_intent(intent) for intent in result.intents if intent.action]
    payload = _build_current_compatibility_view(instructions, result)
    return AdaptedMimoV2Payload(...)
```

Reuse closed action semantics from `authoritative_instructions.py`; do not call
the database, read source text, or query external state. Reject any combination
that the current execution projection cannot safely represent.

**Step 4: Run adapter tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/mimo_v2_execution_adapter.py \
  tests/test_mimo_v2_execution_adapter.py tests/test_authoritative_instructions.py
git commit -m "feat: adapt mimo v2 to current execution"
```

### Task 5: Call MiMo v2 once and capture structured attempts

**Files:**
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `src/telegram_kol_research/prompt_registry.py`
- Modify: `tests/test_recognition_experiments.py`

**Step 1: Write failing provider-attempt tests**

Test first-attempt success, timeout then success, exhausted timeout, invalid
JSON, v2 contract failure, unreadable image, input change, prompt versions, and
run/attempt persistence.

```python
def test_v2_timeout_then_success_records_two_attempts(monkeypatch, factory):
    requester = _sequence_requester(TimeoutError("slow"), _valid_v2_response())

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=7,
        config=_config(),
        requester=requester,
        retry_delay_seconds=0,
    )

    assert result.error_message is None
    assert [row.status for row in load_attempts(factory, result.run_id)] == [
        "timeout", "completed"
    ]
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_recognition_experiments.py -k 'mimo_v2 or attempt'
```

Expected: FAIL because the v2 inference function does not exist.

**Step 3: Implement explicit v2 inference**

Add `infer_mimo_authoritative_v2` without changing the v1 function. It selects
the v2 prompt, sends the same text/media/context input, parses strict JSON,
validates via `parse_mimo_v2_payload`, records attempts, and returns the parsed
result plus run identity.

Classify errors into stable codes:

```python
provider_timeout
provider_http_error
invalid_json
contract_validation_failed
image_unavailable
input_changed_during_analysis
```

Keep provider error text sanitized and bounded.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/recognition_experiments.py \
  src/telegram_kol_research/prompt_registry.py \
  tests/test_recognition_experiments.py
git commit -m "feat: run mimo v2 with attempt audit"
```

### Task 6: Persist v2 text/image evidence without source mixing

**Files:**
- Modify: `src/telegram_kol_research/message_evidence.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `tests/test_message_evidence.py`
- Modify: `tests/test_contextual_message_window.py`

**Step 1: Write failing evidence tests**

Test per-image `asset_id`, quality, observed text, summary, fields, confidence,
multi-image preservation, explicit conflicts, canonical JSON, and later
text-only contextual window serialization.

```python
def test_v2_evidence_preserves_each_image_for_text_only_context(factory):
    saved = persist_mimo_v2_message_evidence(factory, raw_message_id=7, result=_parsed())

    images = json.loads(saved.image_evidence_json)["images"]
    assert [row["asset_id"] for row in images] == [381, 382]
    assert images[0]["observed_text"] == "ETHUSDT ..."
    assert "data:image" not in saved.image_evidence_json
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_message_evidence.py \
  tests/test_contextual_message_window.py
```

Expected: FAIL because v2 evidence persistence is absent.

**Step 3: Add v2 normalization/persistence**

Reuse the existing immutable evidence-version mechanism. Serialize text and
images directly from the parsed v2 result, keep conflicts explicit, and store
no image bytes. Link the authoritative run/evidence generation using additive
metadata where needed; do not overwrite a completed evidence version for a
different input fingerprint.

**Step 4: Run evidence tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/message_evidence.py \
  src/telegram_kol_research/models.py \
  tests/test_message_evidence.py tests/test_contextual_message_window.py
git commit -m "feat: persist mimo v2 image evidence"
```

### Task 7: Add v1/v2 mode, future watermark, and circuit state

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/mimo_contract_circuit.py`
- Modify: `tests/test_trading_settings.py`
- Create: `tests/test_mimo_contract_circuit.py`
- Modify: `tests/test_db_migrations.py`

**Step 1: Write failing settings/circuit tests**

Add tests for:

- default `mimo_contract_mode == "v1"`;
- accepted values `v1` and `v2_live_adapter` only;
- nonnegative `mimo_v2_activation_after_raw_message_id`;
- one contract/adapter failure opening the breaker;
- three consecutive transport failures opening the breaker;
- business/safety refusals not counting;
- a successful v2 run clearing consecutive transport failures;
- breaker state preserved across restart.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py -k mimo \
  tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py -k mimo
```

Expected: FAIL because settings/state do not exist.

**Step 3: Implement settings and durable circuit state**

Extend `TradingSettings` with:

```python
mimo_contract_mode: Literal["v1", "v2_live_adapter"] = "v1"
mimo_v2_activation_after_raw_message_id: int = 0
```

Add a single durable circuit-state row with consecutive transport failures,
opened reason/time, and last success. Circuit opening changes only future
message selection; it does not replay, mutate, or delete existing work.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/mimo_contract_circuit.py \
  tests/test_trading_settings.py tests/test_mimo_contract_circuit.py \
  tests/test_db_migrations.py
git commit -m "feat: gate mimo v2 with circuit breaker"
```

### Task 8: Integrate v2 authority with pre-side-effect v1 fallback

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_message_recognition.py`

**Step 1: Write failing coordinator and fallback tests**

Test:

- v1 remains default;
- v2 applies only above the future watermark and with a closed circuit;
- valid v2 adapts once and follows current projection;
- v2 timeout/JSON/contract/adapter failure calls v1 at most once;
- fallback happens before authoritative execution claim/mutation;
- no fallback after instruction creation, lifecycle mutation, submit, unknown
  exchange result, or post-submit persistence failure;
- v2 and v1 cannot both own/execute one message;
- adapter fingerprint is persisted with the authoritative generation;
- circuit opening routes later messages to v1.

```python
def test_v2_contract_failure_falls_back_before_execution_claim(monkeypatch, factory):
    events = []
    monkeypatch.setattr(module, "infer_mimo_authoritative_v2", lambda **_: _failed("contract_validation_failed"))
    monkeypatch.setattr(module, "infer_mimo_authoritative", lambda **_: _v1_success())
    monkeypatch.setattr(module, "claim_authoritative_execution", lambda *a, **k: events.append("claim") or True)

    result = process_authoritative_message(factory, raw_message_id=7, ...)

    assert result.assessment.mimo.contract_version == "v1"
    assert result.assessment.fallback_from == "mimo-authoritative-v2"
    assert events == ["claim"]
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_authoritative_recognition.py -k 'mimo_v2 or fallback or watermark' \
  tests/test_message_recognition.py -k 'mimo_v2 or authoritative'
```

Expected: FAIL because v2 selection/fallback are not integrated.

**Step 3: Implement the guarded selection boundary**

Select v2 only when mode, watermark, and circuit permit it. Complete v2 call,
strict validation, evidence persistence, and deterministic adaptation before
entering the existing save/claim/apply/automation pipeline.

On an eligible technical failure, call the unchanged v1 path once. Persist the
v2 failure run and v1 fallback run. Never catch an exception from or after
execution claim and reinterpret it as fallback eligibility.

Keep `apply_authoritative_mimo_payload`, candidate/lifecycle resolution,
`MessageInstructionItem` projection, auto-trade executor, and Deepcoin writer
semantics unchanged.

**Step 4: Run focused recognition tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/recognition_decisions.py \
  tests/test_authoritative_recognition.py tests/test_message_recognition.py
git commit -m "feat: route mimo v2 through existing authority"
```

### Task 9: Prove automatic-trading equivalence through the existing executor

**Files:**
- Create: `tests/test_mimo_v2_execution_equivalence.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_deepcoin_execution_actions.py`
- Modify: `tests/test_message_operation_projection.py`

**Step 1: Write failing equivalence tests**

For entry, cancel, full exit, partial exit, move stop, hold, revision, and a
supported multi-action message, produce current v1 and v2-adapted inputs and
compare:

- candidates;
- instruction items and order;
- lifecycle/binding ownership;
- risk budget;
- order drafts;
- idempotency keys;
- fake Deepcoin request bodies;
- skip/block/defer reasons.

Normalize only non-semantic IDs/timestamps before comparison.

```python
@pytest.mark.parametrize("fixture_name", EXECUTION_FIXTURES)
def test_v2_adapter_matches_v1_execution_snapshot(fixture_name, tmp_path):
    v1 = run_fixture_through_current_path(fixture_name, tmp_path / "v1.db")
    v2 = run_fixture_through_v2_adapter(fixture_name, tmp_path / "v2.db")
    assert normalize_snapshot(v2) == normalize_snapshot(v1)
```

**Step 2: Run tests and verify failures**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_mimo_v2_execution_equivalence.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_message_operation_projection.py
```

Expected: new equivalence cases FAIL until fixtures/adapter integration are
complete; existing tests must remain green.

**Step 3: Make only minimal adapter/coordinator corrections**

Fix v2 contract/adapter/coordinator code, not the established executor, unless
an existing executor defect is independently demonstrated and separately
approved. Do not weaken a safety refusal to obtain parity.

**Step 4: Run the full equivalence suite**

Run the command from Step 2.

Expected: PASS with no live client calls.

**Step 5: Commit**

```bash
git add tests/test_mimo_v2_execution_equivalence.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_message_operation_projection.py \
  src/telegram_kol_research/mimo_v2_contract.py \
  src/telegram_kol_research/mimo_v2_execution_adapter.py \
  src/telegram_kol_research/authoritative_recognition.py
git commit -m "test: prove mimo v2 execution equivalence"
```

### Task 10: Build the structured MiMo Web projection

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Create: `tests/test_web_mimo_analysis_projection.py`
- Modify: `tests/test_web_queries_messages.py`

**Step 1: Write failing Web projection tests**

Test successful v2, failed v2 with v1 fallback, exhausted failure, multiple
intents, per-image evidence, context changes, projection refusal, execution
truth, and historical v1 fallback rendering.

```python
def test_web_projection_separates_mimo_success_from_application_failure(factory):
    message = load_group_messages(factory, chat_id=88, limit=10)[0]

    assert message["mimo_analysis"]["runtime"]["status"] == "completed"
    assert message["mimo_analysis"]["intents"][0]["intent_label"] == "仓位管理"
    assert message["system_acceptance"]["status"] == "failed"
    assert message["system_acceptance"]["reason_code"] == "target_unresolved"
```

Assert Web serialization never calls a source-text parser or regex extractor.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_mimo_analysis_projection.py \
  tests/test_web_queries_messages.py
```

Expected: FAIL because the structured projection is absent.

**Step 3: Implement projection from persisted v2/run/evidence rows**

Add a serializer that:

- reads stored v2 canonical JSON and attempt rows;
- maps only closed enums to Chinese labels;
- joins image evidence to `MediaAsset` by `asset_id`;
- returns first-pass, context, system acceptance, and execution as separate
  objects;
- returns explicit historical-v1/missing-detail flags;
- never parses message text or free-form model reasons.

Remove no legacy Web field yet; keep the new projection additive until the
template migration passes.

**Step 4: Run projection tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py \
  tests/test_web_mimo_analysis_projection.py tests/test_web_queries_messages.py
git commit -m "feat: project structured mimo analysis for web"
```

### Task 11: Replace the message-card hierarchy with MiMo-first rendering

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `tests/test_web_group_messages_route.py`

**Step 1: Write failing route/rendering tests**

Assert order and labels:

1. Telegram message/media;
2. `MiMo第一次识别`;
3. image evidence;
4. contextual second-stage result when present;
5. `系统接纳与自动交易`;
6. collapsed DeepSeek auxiliary review.

Also test attempts/errors, v1 fallback authority, raw image JSON disclosure,
multi-intent rows, and historical-v1 labels.

```python
assert response.text.index("MiMo第一次识别") < response.text.index("系统接纳与自动交易")
assert response.text.index("系统接纳与自动交易") < response.text.index("DeepSeek辅助复核")
assert "仓位管理" in response.text
assert "历史v1格式" in historical_response.text
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_web_group_messages_route.py
```

Expected: FAIL because the current decision/history hierarchy remains.

**Step 3: Implement the accessible MiMo-first card**

Use semantic sections and native `<details>` for raw evidence/DeepSeek. Image
summaries are expanded by default only when image evidence exists. Escape all
model strings through Jinja defaults. Keep runtime, model intent, context,
system acceptance, and exchange outcome visually and textually distinct.

JavaScript may toggle sections only; it must not derive any semantic label or
trade field.

**Step 4: Run route tests and focused Web tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_group_messages_route.py \
  tests/test_web_mimo_analysis_projection.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/static/app.css \
  src/telegram_kol_research/static/app.js \
  tests/test_web_group_messages_route.py
git commit -m "feat: show mimo first-pass analysis on messages"
```

### Task 12: Add an isolated server replay and benchmark command

**Files:**
- Create: `src/telegram_kol_research/mimo_v2_replay.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_mimo_v2_replay.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing replay-safety tests**

Test bounded selection, output into an explicit artifact directory, temporary
database usage, v1/v2 comparison, mismatch classification, latency percentiles,
and hard prohibition of production writes, listener replay, Deepcoin writers,
and notifications.

```python
def test_replay_never_constructs_deepcoin_writer(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "DeepcoinTradingClient", _raise_if_called)
    result = run_mimo_v2_replay(
        source_database=tmp_path / "source.db",
        artifact_dir=tmp_path / "artifacts",
        raw_message_ids=[7],
        requester=_fake_requester,
    )
    assert result.processed == 1
    assert result.production_writes == 0
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py \
  tests/test_cli_smoke.py -k mimo_v2_replay
```

Expected: FAIL because replay command does not exist.

**Step 3: Implement explicit read-only CLI**

Add a command such as:

```bash
telegram-kol-research replay-mimo-v2 \
  --database /path/to/research.db \
  --message-id-file /path/to/approved-ids.txt \
  --artifact-dir /path/to/new-empty-dir \
  --max-messages 200
```

Require an explicit bounded ID list or bounded filter and a new/empty artifact
directory. Open the source database read-only where supported. Never import or
construct auto-trade/Deepcoin/notifier writers. Emit JSON/CSV comparison and
performance summaries with source message IDs but no credentials/image bytes.

Exit nonzero on unsafe mismatches or performance-gate failure.

**Step 4: Run replay/CLI tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/mimo_v2_replay.py \
  src/telegram_kol_research/cli.py \
  tests/test_mimo_v2_replay.py tests/test_cli_smoke.py
git commit -m "feat: add isolated mimo v2 replay"
```

### Task 13: Add operator settings, runbook, and rollback controls

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/settings.html`
- Modify: `tests/test_web_app.py`
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Write failing settings API/UI tests**

Test GET/POST round-trip for mode/watermark, rejection of `shadow`, inability
to activate at/below an unsafe watermark, visible circuit state, and explicit
future-only rollback.

```python
def test_trading_settings_rejects_mimo_shadow_mode(client):
    payload = client.get("/api/trading-settings").json()
    payload["mimo_contract_mode"] = "shadow"
    response = client.post("/api/trading-settings", json=payload)
    assert response.status_code == 422
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_app.py -k 'trading_settings and mimo'
```

Expected: FAIL because fields and controls do not exist.

**Step 3: Implement controls and document exact procedures**

Expose only `v1` and `v2_live_adapter`. Require an explicit activation
watermark. Show circuit-open reason/time. Do not add a replay button to the
live Web UI.

Document:

- local test commands;
- server isolated replay and performance gates;
- pre-activation no-in-flight checks;
- future watermark capture;
- enabling `v2_live_adapter`;
- technical fallback and circuit behavior;
- rollback to v1 without replay/deletion;
- evidence/log queries for the first future messages.

**Step 4: Run settings tests and documentation checks**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_app.py -k 'trading_settings and mimo' \
  tests/test_trading_settings.py -k mimo
git diff --check
```

Expected: PASS and no whitespace errors.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/settings.html \
  tests/test_web_app.py docs/runbook.md docs/migration-handoff.md
git commit -m "docs: add mimo v2 rollout controls"
```

### Task 14: Run the complete local regression and review the change

**Files:**
- Review: all files changed in Tasks 1-13

**Step 1: Run focused suites**

```bash
.venv/bin/python -m pytest -q \
  tests/test_mimo_v2_contract.py \
  tests/test_mimo_v2_execution_adapter.py \
  tests/test_mimo_recognition_runs.py \
  tests/test_mimo_contract_circuit.py \
  tests/test_mimo_v2_execution_equivalence.py \
  tests/test_mimo_v2_replay.py \
  tests/test_recognition_experiments.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_message_evidence.py \
  tests/test_web_queries_messages.py \
  tests/test_web_group_messages_route.py
```

Expected: PASS.

**Step 2: Run execution-critical regressions**

```bash
.venv/bin/python -m pytest -q \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_executor.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_message_operation_projection.py \
  tests/test_message_operation_supervisor.py \
  tests/test_source_message_deletion_worker.py \
  tests/test_recovery_live_submit.py
```

Expected: PASS with fake/recorded clients only.

**Step 3: Run the full local suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. If environment-only tests are skipped, record exact skip names
and reasons; do not convert failures into skips.

**Step 4: Perform code review and safety diff audit**

Verify:

- no Web free-form semantic parsing was added;
- no existing safety gate was weakened;
- no Deepcoin retry was added after unknown/submitted outcomes;
- v1 remains default;
- no production Shadow mode exists;
- no fallback catch spans execution claim or writer calls;
- migrations are additive;
- unrelated dirty-worktree files are untouched.

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -15
```

**Step 5: Commit any review-only corrections**

```bash
git add <only-reviewed-correction-files>
git commit -m "fix: harden mimo v2 rollout"
```

Skip this commit if no corrections are needed.

### Task 15: Push, run isolated server verification, and enable only in a safe window

**Files:**
- No source edits expected; record verification evidence in the approved
  artifact location and update runbook/status docs only if the implementation
  process requires it.

**Step 1: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds and remote branch points to the reviewed commit.

**Step 2: Update the server with v1 still active**

Use the existing helper from the project workflow:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

The deployment must pull the reviewed commit, reinstall the editable package,
run additive schema bootstrap, and restart `telegram-kol.service` only after the
preflight proves no in-flight/time-sensitive operation. Confirm health with
`mimo_contract_mode=v1`.

**Step 3: Run isolated MiMo v2 replay and benchmark**

On the server, build an approved bounded message-ID corpus and run
`replay-mimo-v2` into a new artifact directory. Expected:

- zero unsafe semantic/execution mismatch;
- zero production writes/notifications;
- adapter P95 below 50 ms;
- v2 end-to-end P95 at most 115% of v1;
- no credentials or image bytes in artifacts.

If any gate fails, leave production on v1 and stop.

**Step 4: Prove a safe activation window and set the future watermark**

Verify there are no `executing`, `submitted`, `unknown`, or
`recovery_required` instructions, no active recovery/reconciliation, and no
time-sensitive strategy operation. Capture the current maximum terminal raw
message ID as the activation watermark.

Do not close existing positions solely for activation. Do not activate while a
message operation is in flight.

**Step 5: Enable v2 for future messages and verify without replay**

Set:

```text
mimo_contract_mode=v2_live_adapter
mimo_v2_activation_after_raw_message_id=<captured-watermark>
```

For naturally arriving future messages, inspect run/attempt, canonical v2
payload, adapter fingerprint, accepted candidates/items, automation result,
and service logs. Do not send a synthetic live trade message as a shortcut.

If a contract/adapter failure occurs, confirm the current message used the
pre-side-effect v1 fallback and the circuit returned future messages to v1. If
any possible write is unknown, do not fall back; enter reconciliation/manual
recovery.

**Step 6: Record the final production verification**

Update the implementation handoff/runbook evidence with commit, replay artifact
fingerprint, performance summary, activation watermark, mode, service health,
and rollback status. Commit and push documentation-only evidence if required.

## Execution handoff

Plan complete. Execute task-by-task with review checkpoints. Do not combine
Tasks 1-13 into a single large change, and do not perform Task 15 until all
local and isolated-server gates have passed.
