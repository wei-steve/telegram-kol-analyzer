import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import AiPromptDefinition, AiPromptInvocation, AiPromptVersion
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    PromptRegistryConflict,
    PromptSeed,
    get_prompt_detail,
    list_prompt_definitions,
    publish_prompt_draft,
    record_prompt_invocation,
    resolve_active_prompt,
    rollback_prompt,
    save_prompt_draft,
    seed_prompt_definition,
)


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
    )

    assert first.draft_version is not None
    assert second.draft_version is not None
    assert second.draft_version.id == first.draft_version.id
    assert second.draft_version.content == "draft two"


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

    published = publish_prompt_draft(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=detail.draft_version.id,
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
    current = publish_prompt_draft(
        factory,
        "trading.analysis.shared",
        expected_draft_version_id=draft.draft_version.id,
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
