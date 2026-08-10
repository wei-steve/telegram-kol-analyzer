import hashlib
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.instruction_execution_projection import (
    instruction_execution_mode_for_item,
    project_instruction_execution_contracts,
)
from telegram_kol_research.models import (
    InstructionExecutionContract,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import TradingSettings


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _persist_item(
    session_factory,
    *,
    message_id=9974,
    instruction_kind="entry",
    parse_source="mimo_authoritative",
    retired=False,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=message_id,
            posted_at=NOW,
            text="anonymized",
        )
        session.add(raw)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=message_id - 1,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type=(
                "entry_signal" if instruction_kind == "entry" else "position_update"
            ),
            target_lifecycle_id=(
                None if instruction_kind == "entry" else lifecycle.id
            ),
            management_action=(
                None if instruction_kind == "entry" else "partial_take_profit"
            ),
            parse_source=parse_source,
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind=instruction_kind,
            idempotency_key=hashlib.sha256(
                f"{message_id}:{instruction_kind}".encode()
            ).hexdigest(),
            retired_at=NOW if retired else None,
        )
        session.add(item)
        session.commit()
        return raw.id, candidate.id, item.id, lifecycle.id


def test_projector_does_nothing_while_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "disabled.db")
    raw_id, *_ = _persist_item(session_factory)

    projected = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        settings=TradingSettings(instruction_execution_contract_mode="disabled"),
        projected_at=NOW,
    )

    assert projected == ()
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).count() == 0


def test_shadow_projects_only_entry_items_above_strict_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-watermark.db")
    first_raw_id, _, first_item_id, _ = _persist_item(
        session_factory, message_id=9974
    )
    second_raw_id, _, second_item_id, _ = _persist_item(
        session_factory, message_id=9975
    )
    settings = TradingSettings(
        instruction_execution_contract_mode="shadow",
        instruction_execution_entry_after_item_id=first_item_id,
    )

    assert project_instruction_execution_contracts(
        session_factory,
        raw_message_id=first_raw_id,
        settings=settings,
        projected_at=NOW,
    ) == ()
    projected = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=second_raw_id,
        settings=settings,
        projected_at=NOW,
    )

    assert tuple(contract.message_instruction_item_id for contract in projected) == (
        second_item_id,
    )
    assert projected[0].state == "pending"


def test_projector_uses_independent_management_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-watermark.db")
    raw_id, _, item_id, _ = _persist_item(
        session_factory,
        instruction_kind="management",
    )

    blocked = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        settings=TradingSettings(
            instruction_execution_contract_mode="shadow",
            instruction_execution_management_after_item_id=item_id,
        ),
        projected_at=NOW,
    )
    projected = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        settings=TradingSettings(
            instruction_execution_contract_mode="shadow",
            instruction_execution_management_after_item_id=item_id - 1,
        ),
        projected_at=NOW,
    )

    assert blocked == ()
    assert len(projected) == 1
    assert projected[0].intent_kind == "management"


def test_projector_rejects_retired_and_non_authoritative_items(tmp_path):
    retired_factory = create_session_factory(tmp_path / "retired.db")
    retired_raw_id, *_ = _persist_item(retired_factory, retired=True)
    legacy_factory = create_session_factory(tmp_path / "legacy.db")
    legacy_raw_id, *_ = _persist_item(
        legacy_factory,
        parse_source="text",
    )
    settings = TradingSettings(
        instruction_execution_contract_mode="shadow",
    )

    assert project_instruction_execution_contracts(
        retired_factory,
        raw_message_id=retired_raw_id,
        settings=settings,
        projected_at=NOW,
    ) == ()
    assert project_instruction_execution_contracts(
        legacy_factory,
        raw_message_id=legacy_raw_id,
        settings=settings,
        projected_at=NOW,
    ) == ()


def test_projection_is_idempotent_and_does_not_retarget_or_create_candidates(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "idempotent.db")
    raw_id, candidate_id, item_id, lifecycle_id = _persist_item(
        session_factory,
        instruction_kind="management",
    )
    settings = TradingSettings(
        instruction_execution_contract_mode="shadow",
    )

    first = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        settings=settings,
        projected_at=NOW,
    )
    repeated = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        settings=settings,
        projected_at=NOW,
    )

    assert first[0].id == repeated[0].id
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).count() == 1
        assert session.query(SignalCandidate).count() == 1
        candidate = session.get(SignalCandidate, candidate_id)
        assert candidate.target_lifecycle_id == lifecycle_id
        assert (
            session.query(InstructionExecutionContract)
            .one()
            .message_instruction_item_id
            == item_id
        )


def test_item_mode_is_disabled_at_or_below_future_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "item-mode.db")
    _, _, item_id, _ = _persist_item(session_factory, instruction_kind="entry")
    settings = TradingSettings(
        instruction_execution_contract_mode="live",
        instruction_execution_entry_after_item_id=item_id,
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)

        assert instruction_execution_mode_for_item(item, settings) == "disabled"
        settings.instruction_execution_entry_after_item_id = item_id - 1
        assert instruction_execution_mode_for_item(item, settings) == "live"
