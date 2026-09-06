# AI Prompt Registry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Web-managed, versioned registry for every AI prompt, with shared trading template A, MiMo-only image template B, isolated draft comparison, explicit publication, audit history, and rollback.

**Architecture:** Store prompt definitions, immutable versions, test runs, and invocation audits in SQLite. Resolve all runtime prompts through one service: DeepSeek uses A+C and MiMo uses A+B+C; other AI features resolve their own registered templates. The Web UI edits drafts only, runs side-effect-free comparisons, and atomically publishes or rolls back validated versions.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, SQLite, Jinja2, vanilla JavaScript/CSS, httpx, pytest.

---

## Execution rules

- Work in an isolated worktree or verify the current branch is clean before Task 1.
- Use test-driven development for every task: failing test, minimal implementation, focused pass, then commit.
- Do not push or deploy until all local tests and the final review pass.
- Never expose provider API keys through prompt APIs or rendered HTML.
- Draft tests must not call the authoritative apply or auto-trade paths.
- Preserve MiMo authority and DeepSeek auxiliary-only behavior.

### Task 1: Add prompt registry persistence and repository operations

**Files:**
- Modify: `src/telegram_kol_research/models.py:88-160`
- Modify: `src/telegram_kol_research/db.py:14-172`
- Create: `src/telegram_kol_research/prompt_registry.py`
- Create: `tests/test_prompt_registry.py`
- Modify: `tests/test_db_bootstrap.py`

**Step 1: Write failing model/bootstrap tests**

Add tests proving a fresh database creates `ai_prompt_definitions`, `ai_prompt_versions`, `ai_prompt_test_runs`, and `ai_prompt_invocations`, and that an existing SQLite database can be reopened without losing data.

```python
def test_init_db_creates_prompt_registry_tables(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    with factory() as session:
        names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {
        "ai_prompt_definitions",
        "ai_prompt_versions",
        "ai_prompt_test_runs",
        "ai_prompt_invocations",
    } <= names
```

**Step 2: Run the bootstrap test and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_db_bootstrap.py::test_init_db_creates_prompt_registry_tables -q
```

Expected: FAIL because the four tables do not exist.

**Step 3: Add SQLAlchemy models**

Add:

```python
class AiPromptDefinition(Base):
    __tablename__ = "ai_prompt_definitions"
    __table_args__ = (
        UniqueConstraint("prompt_key", "scope_key", name="uq_ai_prompt_definition_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, default="global")
    scope_chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    consumers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    required_variables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    validation_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class AiPromptVersion(Base):
    __tablename__ = "ai_prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_definition_id", "version_number", name="uq_ai_prompt_version_number"),
        Index("ix_ai_prompt_versions_definition_status", "prompt_definition_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_definition_id: Mapped[int] = mapped_column(
        ForeignKey("ai_prompt_definitions.id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    change_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AiPromptTestRun(Base):
    __tablename__ = "ai_prompt_test_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_definition_id: Mapped[int] = mapped_column(ForeignKey("ai_prompt_definitions.id"), index=True)
    draft_version_id: Mapped[int] = mapped_column(ForeignKey("ai_prompt_versions.id"), index=True)
    raw_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_messages.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    differences_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class AiPromptInvocation(Base):
    __tablename__ = "ai_prompt_invocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feature: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    raw_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_messages.id"), nullable=True)
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
```

Use `Base.metadata.create_all()` for new tables. Add compatibility indexes to `SQLITE_COMPAT_INDEXES`; no manual `ALTER TABLE` is needed for brand-new tables.

**Step 4: Write failing repository tests**

Cover:

- idempotent definition seeding;
- exactly one draft per definition;
- saving a draft never changes `active_version_id`;
- publish supersedes the previous active version atomically;
- stale expected version is rejected;
- rollback preserves history and creates a new published version.

```python
def test_save_draft_does_not_change_active_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed("trading.analysis.shared", "published A"))
    before = get_prompt_detail(factory, "trading.analysis.shared")
    save_prompt_draft(factory, "trading.analysis.shared", content="draft A", change_note="test")
    after = get_prompt_detail(factory, "trading.analysis.shared")
    assert after.active_version.id == before.active_version.id
    assert after.draft_version.content == "draft A"
```

**Step 5: Implement repository operations**

In `prompt_registry.py`, define typed dataclasses and these public functions:

```python
def seed_prompt_definition(session_factory, seed: PromptSeed) -> PromptDetail: ...
def list_prompt_definitions(session_factory, *, chat_id: int | None = None) -> list[PromptDetail]: ...
def get_prompt_detail(session_factory, prompt_key: str, *, chat_id: int | None = None) -> PromptDetail: ...
def resolve_active_prompt(session_factory, prompt_key: str, *, chat_id: int | None = None) -> ResolvedPrompt: ...
def save_prompt_draft(session_factory, prompt_key: str, *, content: str, change_note: str, chat_id: int | None = None, expected_active_version_id: int | None = None) -> PromptDetail: ...
def publish_prompt_draft(session_factory, prompt_key: str, *, chat_id: int | None = None, expected_draft_version_id: int) -> PromptDetail: ...
def rollback_prompt(session_factory, prompt_key: str, *, source_version_id: int, change_note: str, chat_id: int | None = None, expected_active_version_id: int) -> PromptDetail: ...
def record_prompt_invocation(session_factory, record: PromptInvocationRecord) -> None: ...
```

Use one database transaction for publish/rollback. Reject empty content, missing draft, stale IDs, and cross-definition rollback IDs with `PromptRegistryError` subclasses.

**Step 6: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_prompt_registry.py tests/test_db_bootstrap.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py src/telegram_kol_research/prompt_registry.py tests/test_prompt_registry.py tests/test_db_bootstrap.py
git commit -m "feat: add versioned AI prompt registry"
```

### Task 2: Create canonical prompt seeds A/B and migrate legacy configuration

**Files:**
- Create: `src/telegram_kol_research/prompt_defaults.py`
- Modify: `src/telegram_kol_research/ai_recognition_config.py:12-228,264-289,370-430`
- Modify: `config/ai_recognition.example.yaml`
- Modify: `tests/test_ai_recognition_config.py`
- Modify: `tests/test_prompt_registry.py`

**Step 1: Write failing seed-equivalence tests**

Assert:

- A contains new-entry rules, lifecycle rules, canonical JSON, BTC shorthand, partial-profit, protective-stop, and exit examples;
- A contains no image-only section or provider name;
- B contains image-reading and image-quality rules but no duplicate JSON schema;
- seeding from old `recognition_prompt` + `lifecycle_event_prompt` preserves both custom prefixes;
- a database active version wins over legacy YAML.

```python
def test_default_trading_templates_have_strict_boundaries():
    assert "lifecycle_event" in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "58900-59300" in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "图片模糊" not in DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    assert "图片模糊" in DEFAULT_MIMO_VISION_PROMPT
    assert '"recognition_result"' not in DEFAULT_MIMO_VISION_PROMPT
```

**Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_ai_recognition_config.py tests/test_prompt_registry.py -q
```

Expected: FAIL because canonical A/B seeds do not exist.

**Step 3: Implement prompt seeds**

Define stable keys:

```python
SHARED_TRADING_PROMPT = "trading.analysis.shared"
MIMO_VISION_PROMPT = "trading.analysis.mimo_vision"
RESEARCH_CHAT_SYSTEM_PROMPT = "research.chat.system"
STRATEGY_ALERT_PROMPT = "strategy.alert.classifier"
GROUP_RESEARCH_PROMPT = "research.chat.group"
```

Move the effective common rules into `DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT`. Move only image-specific instructions into `DEFAULT_MIMO_VISION_PROMPT`. Add seed metadata with consumers, variables, and validation profiles.

Keep provider/model configuration in `AiRecognitionConfig`; mark the three old prompt fields as transitional inputs only. Add:

```python
def build_prompt_seeds_from_legacy(config: AiRecognitionConfig) -> list[PromptSeed]:
    shared = "\n\n".join(
        part.strip()
        for part in (config.recognition_prompt, config.lifecycle_event_prompt)
        if part.strip()
    )
    return default_prompt_seeds(
        shared_trading_content=shared or DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
        mimo_vision_content=extract_image_only_legacy_content(config.mimo_direct_prompt),
    )
```

Do not automatically rewrite the production YAML. Update the example YAML comments to identify old fields as deprecated compatibility inputs.

**Step 4: Implement idempotent startup seeding**

Add `seed_default_prompt_registry(session_factory, legacy_config)` to `prompt_defaults.py`. It creates missing definitions/initial published versions only. It must never overwrite an existing active database version.

**Step 5: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_ai_recognition_config.py tests/test_prompt_registry.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/prompt_defaults.py src/telegram_kol_research/ai_recognition_config.py config/ai_recognition.example.yaml tests/test_ai_recognition_config.py tests/test_prompt_registry.py
git commit -m "refactor: define shared and MiMo prompt templates"
```

### Task 3: Build one prompt composition and validation service

**Files:**
- Create: `src/telegram_kol_research/prompt_composition.py`
- Create: `tests/test_prompt_composition.py`

**Step 1: Write failing composition tests**

```python
def test_deepseek_uses_a_and_context_but_never_b(factory):
    composition = compose_trading_prompt(factory, model_kind="deepseek", context="C")
    assert composition.system_prompt == "A"
    assert composition.context == "C"
    assert composition.version_map == {"trading.analysis.shared": 1}
    assert "IMAGE_ONLY_MARKER" not in composition.system_prompt


def test_mimo_uses_a_and_b_exactly_once(factory):
    composition = compose_trading_prompt(factory, model_kind="mimo", context="C")
    assert composition.system_prompt.count("A_MARKER") == 1
    assert composition.system_prompt.count("B_MARKER") == 1
```

Also test unknown model kinds, missing active prompts, invalid variables, A missing required JSON fields, and B attempting to redefine `recognition_result`.

**Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_prompt_composition.py -q
```

Expected: FAIL because the service does not exist.

**Step 3: Implement immutable compositions**

```python
@dataclass(frozen=True)
class PromptComposition:
    system_prompt: str
    context: str
    version_map: dict[str, int]


def compose_trading_prompt(session_factory, *, model_kind: str, context: str) -> PromptComposition:
    shared = resolve_active_prompt(session_factory, SHARED_TRADING_PROMPT)
    prompts = [shared]
    if model_kind == "mimo":
        prompts.append(resolve_active_prompt(session_factory, MIMO_VISION_PROMPT))
    elif model_kind != "deepseek":
        raise PromptCompositionError(f"unsupported trading model kind: {model_kind}")
    return PromptComposition(
        system_prompt="\n\n".join(item.content.strip() for item in prompts),
        context=context,
        version_map={item.prompt_key: item.version_id for item in prompts},
    )
```

Add `render_registered_prompt(...)` for research chat, alerts, and group-scoped prompts. Use a strict formatter that rejects missing/unknown placeholders instead of silently leaving braces.

Add `validate_prompt_content(prompt_key, content)` with profile-specific checks. Keep deterministic parser/execution safeguards outside prompt content.

**Step 4: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_prompt_composition.py tests/test_prompt_registry.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/prompt_composition.py tests/test_prompt_composition.py
git commit -m "feat: compose registered AI prompts"
```

### Task 4: Route authoritative MiMo and auxiliary DeepSeek through A/B and audit versions

**Files:**
- Modify: `src/telegram_kol_research/recognition_experiments.py:234-330,335-483`
- Modify: `src/telegram_kol_research/message_recognition.py:580-730,900-942,1530-1580`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:82-136`
- Modify: `src/telegram_kol_research/recognition_decisions.py:14-77`
- Modify: `src/telegram_kol_research/models.py:133-159`
- Modify: `src/telegram_kol_research/db.py:14-83`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_recognition_experiments.py`
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_db_bootstrap.py`

**Step 1: Write failing authority/composition tests**

Add tests proving:

- pure-text MiMo and DeepSeek receive the same A and same C;
- only MiMo receives B;
- image messages do not invoke DeepSeek;
- recognition decisions store exact prompt-version maps;
- MiMo/DeepSeek disagreement still executes MiMo and notifies;
- MiMo failure still cannot fall back to DeepSeek action.

```python
def test_fengge_exit_records_authoritative_prompt_versions(...):
    result = process_authoritative_message(...)
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
    assert json.loads(decision.prompt_versions_json) == {
        "mimo": {
            "trading.analysis.shared": shared_version_id,
            "trading.analysis.mimo_vision": vision_version_id,
        },
        "deepseek": {"trading.analysis.shared": shared_version_id},
    }
```

**Step 2: Run targeted tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_authoritative_recognition.py tests/test_recognition_experiments.py tests/test_message_recognition.py -q
```

Expected: FAIL because calls still read legacy config prompt fields and decisions do not store version maps.

**Step 3: Extend result/audit structures**

Add `prompt_versions: dict[str, int]` to `MimoAuthoritativeResult` and the DeepSeek auxiliary result wrapper. Add `prompt_versions_json` to `RecognitionDecision` plus SQLite compatibility SQL.

Extend `RecognitionDecisionRecord`:

```python
prompt_versions: dict[str, dict[str, int]]
```

Persist it with the authoritative and auxiliary payloads.

**Step 4: Replace prompt construction at call sites**

- Build C once with `_build_authoritative_context`.
- Call `compose_trading_prompt(..., model_kind="mimo", context=context)`.
- Pass its `system_prompt` and C into the MiMo request.
- For pure text, call the same service with `model_kind="deepseek"` and the exact same C.
- Remove the lifecycle-only DeepSeek prompt call from the authoritative path.
- Keep legacy functions only for non-authoritative compatibility tests until Task 9 removes them.

Record `AiPromptInvocation` success/failure around each network request. Use stable correlation keys such as `recognition:{raw_message_id}:mimo`.

**Step 5: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_authoritative_recognition.py tests/test_recognition_experiments.py tests/test_message_recognition.py tests/test_db_bootstrap.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/recognition_experiments.py src/telegram_kol_research/message_recognition.py src/telegram_kol_research/authoritative_recognition.py src/telegram_kol_research/recognition_decisions.py src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_authoritative_recognition.py tests/test_recognition_experiments.py tests/test_message_recognition.py tests/test_db_bootstrap.py
git commit -m "refactor: apply registered prompts to message recognition"
```

### Task 5: Register research chat, strategy alert, and group prompts

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py:101-137,154-240`
- Modify: `src/telegram_kol_research/strategy_alerts.py:107-134,336-369,478-487`
- Modify: `src/telegram_kol_research/web_app.py:3023-3066`
- Modify: `tests/test_llm_chat_request.py`
- Modify: `tests/test_web_chat_api.py`
- Modify: `tests/test_strategy_alerts.py`
- Modify: `tests/test_prompt_registry.py`

**Step 1: Write failing tests for every non-recognition AI call**

Assert:

- research chat system content comes from `research.chat.system`;
- optional group content comes from `research.chat.group` scoped by `chat_id`;
- arbitrary `group_prompt` request text is no longer treated as an unversioned system prompt;
- strategy-alert classification uses `strategy.alert.classifier` and strict variables;
- each call records an `AiPromptInvocation` with exact versions.

```python
def test_chat_uses_published_group_prompt_version(client, seeded_prompts):
    response = client.post("/api/chat", json={"chat_id": 100, "question": "总结"})
    assert response.status_code == 200
    system_messages = [
        item["content"]
        for item in response.json()["proxy_payload"]["messages"]
        if item["role"] == "system"
    ]
    assert system_messages == ["published research system", "published group prompt"]
```

**Step 2: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_llm_chat_request.py tests/test_web_chat_api.py tests/test_strategy_alerts.py -q
```

Expected: FAIL because prompts are embedded or supplied directly by the request.

**Step 3: Render registered templates**

Change builders to accept already resolved content/version metadata rather than owning business text:

```python
def build_proxy_chat_payload(*, question: str, scope_context: str, model: str, system_prompt: str, group_prompt: str | None = None) -> dict[str, Any]: ...

def build_strategy_alert_prompt(*, template: str, chat_title: str, sender_name: str | None, text: str, max_chars: int = 1200) -> str:
    return render_template_strict(
        template,
        chat_title=chat_title,
        sender_name=sender_name or "",
        first_line=first_line,
        message_text=trimmed_text,
    )
```

Resolve prompts in orchestration functions where `session_factory` and `chat_id` are available. Record invocation audit rows on success and error.

**Step 4: Preserve per-group behavior safely**

If no scoped group prompt is published, omit the second system message. Do not fall back to browser-supplied unversioned prompt text after migration.

**Step 5: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_llm_chat_request.py tests/test_web_chat_api.py tests/test_strategy_alerts.py tests/test_prompt_registry.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/llm_chat.py src/telegram_kol_research/strategy_alerts.py src/telegram_kol_research/web_app.py tests/test_llm_chat_request.py tests/test_web_chat_api.py tests/test_strategy_alerts.py tests/test_prompt_registry.py
git commit -m "refactor: register remaining AI prompts"
```

### Task 6: Add prompt registry, draft, publish, history, and rollback APIs

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:1760-1830,2168-2212,2841-2881`
- Create: `tests/test_web_prompt_registry.py`
- Modify: `tests/test_web_app.py:1709-1834`

**Step 1: Write failing API tests**

Cover:

- list definitions without secret fields;
- read active/draft/history;
- save draft;
- reject empty/stale draft;
- validation endpoint;
- publish requires successful validation and change note;
- rollback requires expected active ID and change note;
- group-scoped definitions require a valid `chat_id`;
- old `/api/ai-recognition-config` no longer saves prompt text but still saves provider/model selection.

Proposed routes:

```text
GET  /api/ai-prompts
GET  /api/ai-prompts/{prompt_key}?chat_id=
PUT  /api/ai-prompts/{prompt_key}/draft
POST /api/ai-prompts/{prompt_key}/validate
POST /api/ai-prompts/{prompt_key}/publish
POST /api/ai-prompts/{prompt_key}/rollback
GET  /api/ai-prompts/{prompt_key}/history
```

**Step 2: Run test and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_web_prompt_registry.py -q
```

Expected: FAIL with 404 routes.

**Step 3: Implement thin HTTP handlers**

Handlers validate request shapes, call repository/composition services, translate domain errors to 404/409/422, and return version metadata. They must never return provider credentials.

Publishing must require:

```python
if not change_note.strip():
    raise HTTPException(422, "change_note is required")
if not validation.success:
    raise HTTPException(422, "draft validation has not passed")
```

Store the validation result against the exact draft version so later draft edits invalidate it.

**Step 4: Separate provider configuration endpoint**

Keep `/api/ai-recognition-config` for mode, provider/model metadata, and model selection. Ignore/reject legacy prompt fields rather than rewriting YAML from the new UI.

**Step 5: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_web_prompt_registry.py tests/test_web_app.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_prompt_registry.py tests/test_web_app.py
git commit -m "feat: add AI prompt management APIs"
```

### Task 7: Implement side-effect-free active-versus-draft tests

**Files:**
- Create: `src/telegram_kol_research/prompt_testing.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_prompt_testing.py`
- Modify: `tests/test_web_prompt_registry.py`

**Step 1: Write failing isolation tests**

Take database counts before and after draft comparison and assert no production tables change:

```python
def test_draft_recognition_test_has_no_production_side_effects(...):
    before = counts(session_factory, SignalCandidate, StrategyLifecycle, ExecutionBinding, StrategyAlert)
    result = run_prompt_draft_test(
        session_factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=draft_id,
        raw_message_id=raw_id,
        model_kind="mimo",
        model_caller=fake_caller,
    )
    after = counts(session_factory, SignalCandidate, StrategyLifecycle, ExecutionBinding, StrategyAlert)
    assert after == before
    assert result.differences == ["lifecycle_event.event_type"]
```

Also test image media forwarding, unreadable image failure, model/network failure, raw JSON storage, duration, and active/draft version mismatch.

**Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_prompt_testing.py -q
```

Expected: FAIL because the isolated runner does not exist.

**Step 3: Implement the isolated runner**

The runner may reuse read-only message/media/context loaders and low-level model request functions. It must not call:

- `apply_authoritative_mimo_payload`;
- `process_authoritative_message`;
- `_persist_ai_result`;
- `auto_trade_executor`;
- strategy-alert sender.

Return/store:

```python
@dataclass(frozen=True)
class PromptDraftTestResult:
    test_run_id: int
    active_payload: dict[str, Any]
    draft_payload: dict[str, Any]
    differences: list[str]
    duration_ms: int
    error_message: str | None
```

For A, run both configured models when requested. For B, only permit MiMo and require image input for the publish regression set.

**Step 4: Add API route**

```text
POST /api/ai-prompts/{prompt_key}/test
```

Request fields: `draft_version_id`, `raw_message_ids`, optional `model_kinds`. Return one comparison per message/model.

**Step 5: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_prompt_testing.py tests/test_web_prompt_registry.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/prompt_testing.py src/telegram_kol_research/web_app.py tests/test_prompt_testing.py tests/test_web_prompt_registry.py
git commit -m "feat: add isolated AI prompt comparisons"
```

### Task 8: Build the responsive AI prompt center UI

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html:25-170,385-405`
- Create: `src/telegram_kol_research/templates/_ai_prompt_center.html`
- Modify: `src/telegram_kol_research/static/app.js:1-120,1180-1200,1760-2100,2320-2360`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_prompt_registry.py`
- Create: `tests/test_prompt_center_assets.py`

**Step 1: Write failing render/asset tests**

Assert the page contains:

- prompt-center navigation;
- registry list and detail containers;
- active/draft tabs;
- save, validate, test, publish, history, rollback controls;
- A/B/C composition labels;
- mobile-friendly selectors;
- no rendered API key.

Add static-JS assertions for API paths, stale-request guards, publish confirmation, and legacy localStorage import.

**Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_prompt_center_assets.py -q
```

Expected: FAIL because the new UI is absent.

**Step 3: Replace the old prompt cards with the registry shell**

The partial must provide:

```html
<section data-ai-prompt-center>
  <div data-ai-prompt-list></div>
  <article data-ai-prompt-detail hidden>
    <textarea data-ai-prompt-draft></textarea>
    <textarea data-ai-prompt-change-note></textarea>
    <button data-ai-prompt-save-draft>保存草稿</button>
    <button data-ai-prompt-validate>校验</button>
    <button data-ai-prompt-test>历史消息测试</button>
    <button data-ai-prompt-publish>发布</button>
    <button data-ai-prompt-history>历史版本</button>
  </article>
</section>
```

Render data through safe DOM text APIs; never inject prompt content with `innerHTML`.

**Step 4: Implement UI state and API calls**

Track selected prompt, active/draft version IDs, in-flight request ID, validation status, comparisons, and history. A draft edit must immediately clear the client-side `validated` state.

Require a confirmation dialog containing the active/draft diff before publish/rollback.

**Step 5: Migrate legacy per-group localStorage**

When selecting a group-scoped research prompt and no server draft/published override exists, detect `telegram-workbench:prompt:<chatId>` and offer `导入为草稿`. Never auto-publish it. Remove the old behavior that sends localStorage prompt text directly with every `/api/chat` request.

**Step 6: Add responsive styling**

- desktop: registry list beside editor/comparison;
- mobile: prompt selector, then vertically stacked editor/actions/results;
- keep publish/rollback buttons visually distinct from save/test;
- show active/draft badges and version timestamps;
- preserve existing workbench navigation behavior.

**Step 7: Run focused tests**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_prompt_registry.py tests/test_prompt_center_assets.py -q
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/templates/index.html src/telegram_kol_research/templates/_ai_prompt_center.html src/telegram_kol_research/static/app.js src/telegram_kol_research/static/app.css tests/test_web_page_render.py tests/test_web_prompt_registry.py tests/test_prompt_center_assets.py
git commit -m "feat: add Web AI prompt center"
```

### Task 9: Enforce complete AI prompt coverage and remove duplicate runtime rules

**Files:**
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `src/telegram_kol_research/strategy_alerts.py`
- Modify: `tests/test_prompt_composition.py`
- Create: `tests/test_ai_prompt_inventory.py`
- Modify: `docs/migration-handoff.md`
- Create: `docs/context/ai-prompt-registry.md`

**Step 1: Write a failing prompt inventory test**

Create a narrow AST/text inventory test that lists approved AI network call modules and requires each to reference the registry/composition service. It must also reject known embedded prompt markers at request call sites.

```python
AI_CALL_MODULES = {
    "message_recognition.py",
    "recognition_experiments.py",
    "llm_chat.py",
    "strategy_alerts.py",
}

def test_every_ai_call_site_uses_prompt_registry():
    for filename in AI_CALL_MODULES:
        source = (SRC / filename).read_text(encoding="utf-8")
        assert "prompt_registry" in source or "prompt_composition" in source
        assert "You are an analyst for Telegram" not in source
        assert "Classify one Telegram trading-group message" not in source
```

**Step 2: Run test and verify failure**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_ai_prompt_inventory.py -q
```

Expected: FAIL until all duplicate runtime prompt text is removed.

**Step 3: Remove deprecated runtime prompt composition**

- Delete `MIMO_AUTHORITATIVE_OUTPUT_INSTRUCTIONS` and other duplicated runtime appenders after their content exists in A/B seeds.
- Delete experiment-specific business-rule appenders.
- Keep legacy YAML parsing only as a seed fallback, with a deprecation warning.
- Keep deterministic JSON parsers, validators, confidence thresholds, lifecycle resolution, exact binding, and reconciliation logic unchanged.

**Step 4: Document durable operation**

Record:

- stable prompt IDs;
- A/B/C composition;
- draft/test/publish/rollback behavior;
- database tables and audit lookup;
- legacy YAML fallback/removal criteria;
- server verification and rollback procedure;
- explicit statement that prompt rollback does not roll back application code.

**Step 5: Run inventory and feature regressions**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest tests/test_ai_prompt_inventory.py tests/test_prompt_registry.py tests/test_prompt_composition.py tests/test_prompt_testing.py tests/test_authoritative_recognition.py tests/test_ai_recognition_config.py tests/test_message_recognition.py tests/test_recognition_experiments.py tests/test_llm_chat_request.py tests/test_web_chat_api.py tests/test_strategy_alerts.py tests/test_web_prompt_registry.py tests/test_prompt_center_assets.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/ai_recognition_config.py src/telegram_kol_research/message_recognition.py src/telegram_kol_research/recognition_experiments.py src/telegram_kol_research/llm_chat.py src/telegram_kol_research/strategy_alerts.py tests/test_prompt_composition.py tests/test_ai_prompt_inventory.py docs/migration-handoff.md docs/context/ai-prompt-registry.md
git commit -m "refactor: enforce registered AI prompts"
```

### Task 10: Full review, local verification, GitHub push, and server verification

**Files:**
- Review: all files changed in Tasks 1-9
- Update if needed: `docs/context/ai-prompt-registry.md`

**Step 1: Review the complete diff**

Run:

```bash
git status --short --branch
git diff --check origin/codex/deepcoin-auto-trading-v1...HEAD
git diff --stat origin/codex/deepcoin-auto-trading-v1...HEAD
```

Expected: only scoped prompt-registry changes; no secrets or unrelated edits.

Review specifically for:

- prompt content accidentally logged with secrets/context;
- publish/rollback transaction correctness;
- stale draft races;
- draft test side effects;
- unregistered AI calls;
- MiMo authority regression;
- DeepSeek action fallback;
- prompt version audit gaps;
- HTML/JavaScript prompt injection.

**Step 2: Run the full local suite**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m pytest -q
```

Expected: all new tests pass. If unrelated known baseline failures remain, rerun the scoped suite and document exact baseline evidence before continuing; do not attribute them to this feature without isolation.

**Step 3: Run syntax/static checks**

Run:

```bash
PYTHONPATH="$PWD/src" ./.venv/bin/python -m compileall -q src tests
git diff --check origin/codex/deepcoin-auto-trading-v1...HEAD
```

Expected: exit code 0.

**Step 4: Request code review and fix findings**

Use the project code-review workflow. Re-run affected focused tests after each fix, then rerun the full scoped prompt suite.

**Step 5: Commit final fixes/documentation**

```bash
git add <reviewed-files>
git commit -m "test: verify AI prompt registry"
```

Skip this commit if review produces no changes.

**Step 6: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub branch advances to the reviewed local HEAD.

**Step 7: Update the server through the existing GitHub workflow**

Prefer the repository helper from the Mac environment:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

If PowerShell is unavailable, use the existing Mac-compatible helper documented in `docs/migration-handoff.md`; do not copy source files manually.

Expected server actions: pull the same GitHub commit, reinstall the editable package, and restart `telegram-kol.service`.

**Step 8: Verify production behavior**

On the server verify:

```bash
git rev-parse HEAD
systemctl is-active telegram-kol.service
journalctl -u telegram-kol.service -n 200 --no-pager
```

Then verify through the Web UI/API:

- prompt registry seeded all required definitions;
- no API key appears in responses or HTML;
- A/B compositions show `DeepSeek=A+C`, `MiMo=A+B+C`;
- saving a draft does not affect a fresh live recognition;
- historical text and image comparison creates only test-run rows;
- publishing a tested draft changes the version used by the next recognition;
- rollback restores the previous prompt content;
- a Fengge-style exit still creates `exit_requested` and waits for exchange reconciliation;
- disagreement notification still sends while MiMo remains authoritative;
- Deepcoin reconciliation loop remains healthy.

**Step 9: Record deployment evidence**

Update `docs/context/ai-prompt-registry.md` with the deployed commit, focused/full test counts, service status, seeded prompt/version IDs, and any retained legacy fallback. Commit and push the evidence if the repository convention requires it.
