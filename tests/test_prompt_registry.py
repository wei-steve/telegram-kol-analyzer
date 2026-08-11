import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    AiPromptDefinition,
    AiPromptInvocation,
    AiPromptTestRun,
    AiPromptVersion,
)
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    PromptRegistryConflict,
    PromptRegistryValidationError,
    PromptSeed,
    get_prompt_detail,
    list_prompt_definitions,
    publish_prompt_draft,
    record_prompt_validation,
    record_prompt_invocation,
    resolve_active_prompt,
    rollback_prompt,
    save_prompt_draft,
    seed_prompt_definition,
)
from telegram_kol_research.prompt_defaults import (
    DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT,
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    MIMO_V2_AUTHORITATIVE_PROMPT,
)


def test_shared_prompt_defines_non_executable_entry_preamble_contract():
    prompt = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT

    assert '"entry_context"' in prompt
    assert '"kind": "entry_preamble"' in prompt
    assert '"risk_multiplier": "0.5"' in prompt
    assert "半仓" in prompt
    assert "最大亏损预算乘以 50%" in prompt
    assert "30% 仓位" in prompt
    assert "非执行" in prompt
    assert "轻仓" in prompt
    assert "满仓" in prompt
    assert "不得猜测倍率" in prompt


def _seed(content: str = "published A") -> PromptSeed:
    return PromptSeed(
        prompt_key="trading.analysis.shared",
        display_name="统一交易分析 A",
        description="共享交易语义",
        category="trading",
        consumers=("deepseek", "mimo"),
        required_variables=(),
        validation_profile="trading_shared",
        content=content,
    )


def test_seed_prompt_definition_is_idempotent(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")

    first = seed_prompt_definition(factory, _seed())
    second = seed_prompt_definition(factory, _seed("must not overwrite"))

    assert first.definition_id == second.definition_id
    assert second.active_version.content == "published A"
    with factory() as session:
        assert session.query(AiPromptDefinition).count() == 1
        assert session.query(AiPromptVersion).count() == 1


def test_save_draft_does_not_change_active_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())
    before = get_prompt_detail(factory, "trading.analysis.shared")

    after = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="draft A",
        change_note="test draft",
        expected_active_version_id=before.active_version.id,
    )

    assert after.active_version.id == before.active_version.id
    assert after.active_version.content == "published A"
    assert after.draft_version is not None
    assert after.draft_version.content == "draft A"


def test_save_draft_reuses_the_single_draft_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())

    first = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="draft one",
        change_note="first",
    )
    second = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="draft two",
        change_note="second",
        expected_draft_updated_at=first.draft_version.updated_at,
    )

    assert first.draft_version is not None
    assert second.draft_version is not None
    assert second.draft_version.id == first.draft_version.id
    assert second.draft_version.content == "draft two"


def test_save_draft_rejects_stale_draft_timestamp(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())
    first = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="draft one",
        change_note="first",
    )
    save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="draft two",
        change_note="second",
        expected_draft_updated_at=first.draft_version.updated_at,
    )

    with pytest.raises(PromptRegistryConflict, match="draft version changed"):
        save_prompt_draft(
            factory,
            "trading.analysis.shared",
            content="stale overwrite",
            change_note="stale",
            expected_draft_updated_at=first.draft_version.updated_at,
        )


def test_publish_draft_atomically_supersedes_active_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    original = seed_prompt_definition(factory, _seed())
    detail = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="published next",
        change_note="publish me",
    )
    assert detail.draft_version is not None
    record_prompt_validation(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=detail.draft_version.id,
        success=True,
        errors=(),
    )

    published = publish_prompt_draft(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=detail.draft_version.id,
        expected_active_version_id=original.active_version.id,
    )

    assert published.draft_version is None
    assert published.active_version.content == "published next"
    assert resolve_active_prompt(factory, "trading.analysis.shared").version_id == published.active_version.id
    with factory() as session:
        prior = session.get(AiPromptVersion, original.active_version.id)
        assert prior.status == "superseded"


def test_stale_expected_active_version_is_rejected(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())

    with pytest.raises(PromptRegistryConflict, match="active version changed"):
        save_prompt_draft(
            factory,
            "trading.analysis.shared",
            content="draft",
            change_note="stale",
            expected_active_version_id=999,
        )


def test_rollback_creates_a_new_auditable_published_version(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    original = seed_prompt_definition(factory, _seed("version one"))
    draft = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="version two",
        change_note="second",
    )
    assert draft.draft_version is not None
    record_prompt_validation(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=draft.draft_version.id,
        success=True,
        errors=(),
    )
    current = publish_prompt_draft(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=draft.draft_version.id,
        expected_active_version_id=original.active_version.id,
    )

    restored = rollback_prompt(
        factory,
        "trading.analysis.shared",
        source_version_id=original.active_version.id,
        change_note="restore known good",
        expected_active_version_id=current.active_version.id,
    )

    assert restored.active_version.content == "version one"
    assert restored.active_version.id not in {
        original.active_version.id,
        current.active_version.id,
    }
    assert restored.active_version.source_version_id == original.active_version.id
    assert [item.version_number for item in restored.history] == [3, 2, 1]


def test_publish_rejects_a_draft_without_successful_validation(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())
    detail = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="not validated",
        change_note="unsafe",
    )

    with pytest.raises(PromptRegistryConflict, match="successful validation"):
        publish_prompt_draft(
            factory,
            "trading.analysis.shared",
            expected_draft_version_id=detail.draft_version.id,
            expected_active_version_id=detail.active_version.id,
        )


def test_publish_rejects_invalid_mimo_v2_even_if_validation_was_marked_successful(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "research.db")
    original = seed_prompt_definition(
        factory,
        PromptSeed(
            prompt_key=MIMO_V2_AUTHORITATIVE_PROMPT,
            display_name="MiMo v2",
            description="MiMo v2 authoritative contract",
            category="trading",
            consumers=("mimo",),
            required_variables=(),
            validation_profile="mimo_v2_authoritative",
            content=DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT,
        ),
    )
    detail = save_prompt_draft(
        factory,
        MIMO_V2_AUTHORITATIVE_PROMPT,
        content=DEFAULT_MIMO_V2_AUTHORITATIVE_PROMPT.replace('"intents"', ""),
        change_note="unsafe removal",
    )
    record_prompt_validation(
        factory,
        MIMO_V2_AUTHORITATIVE_PROMPT,
        expected_draft_version_id=detail.draft_version.id,
        success=True,
        errors=(),
    )

    with pytest.raises(PromptRegistryValidationError, match="MiMo v2"):
        publish_prompt_draft(
            factory,
            MIMO_V2_AUTHORITATIVE_PROMPT,
            expected_draft_version_id=detail.draft_version.id,
            expected_active_version_id=original.active_version.id,
        )


def test_publish_rejects_empty_change_note_even_after_validation(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    original = seed_prompt_definition(factory, _seed())
    detail = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="validated content",
        change_note="",
    )
    record_prompt_validation(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=detail.draft_version.id,
        success=True,
        errors=(),
    )

    with pytest.raises(PromptRegistryValidationError, match="change note is required"):
        publish_prompt_draft(
            factory,
            "trading.analysis.shared",
            expected_draft_version_id=detail.draft_version.id,
            expected_active_version_id=original.active_version.id,
        )


def test_editing_a_validated_draft_clears_validation_state(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())
    detail = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="validated draft",
        change_note="first",
    )
    validated = record_prompt_validation(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=detail.draft_version.id,
        success=True,
        errors=(),
    )
    assert validated.draft_version.validated_at is not None

    edited = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="edited again",
        change_note="second",
        expected_draft_updated_at=validated.draft_version.updated_at,
    )

    assert edited.draft_version.validated_at is None
    assert edited.draft_version.validation_result is None


def test_editing_a_draft_invalidates_its_historical_test_runs(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_prompt_definition(factory, _seed())
    detail = save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="first tested content",
        change_note="first",
    )
    with factory() as session:
        session.add(
            AiPromptTestRun(
                prompt_definition_id=detail.definition_id,
                draft_version_id=detail.draft_version.id,
                model="mimo-v2.5",
                status="completed",
                differences_json="[]",
            )
        )
        session.commit()

    save_prompt_draft(
        factory,
        "trading.analysis.shared",
        content="second untested content",
        change_note="second",
        expected_draft_updated_at=detail.draft_version.updated_at,
    )

    with factory() as session:
        assert session.query(AiPromptTestRun).count() == 0


def test_scoped_prompts_are_unique_per_chat(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    global_seed = _seed()
    chat_seed = PromptSeed(
        **{
            **global_seed.__dict__,
            "scope_chat_id": 88,
            "content": "chat 88",
        }
    )

    global_detail = seed_prompt_definition(factory, global_seed)
    scoped_detail = seed_prompt_definition(factory, chat_seed)

    assert global_detail.definition_id != scoped_detail.definition_id
    assert get_prompt_detail(
        factory,
        "trading.analysis.shared",
        chat_id=88,
    ).active_version.content == "chat 88"
    assert len(list_prompt_definitions(factory)) == 2


def test_record_prompt_invocation_persists_version_audit(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")

    record_prompt_invocation(
        factory,
        PromptInvocationRecord(
            feature="message_recognition",
            correlation_key="recognition:7:mimo",
            model="mimo-v2.5",
            prompt_versions={"trading.analysis.shared": 3},
            status="completed",
            raw_message_id=None,
            chat_id=88,
        ),
    )

    with factory() as session:
        row = session.query(AiPromptInvocation).one()
        assert json.loads(row.prompt_versions_json) == {
            "trading.analysis.shared": 3
        }
