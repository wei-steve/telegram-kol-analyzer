from __future__ import annotations

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.position_protection_legs import (
    bind_filled_position,
    bind_parent_entry_order,
    bind_verified_exchange_order,
    create_or_get_protection_leg,
)


def _entry_leg_id(session_factory) -> int:
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            venue="deepcoin",
            kol_id="kol-1",
            chat_id=101,
            message_id=202,
            symbol="BTCUSDT",
            side="long",
        ),
    )
    return upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=0,
            purpose="entry",
            venue="deepcoin",
            order_kind="trigger_limit",
        ),
    )


def test_protection_leg_exists_before_exchange_position_or_order_exists(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        primary = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="62000",
            planned_size="3",
        )
        same = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="62000",
            planned_size="3",
        )
        session.commit()
        assert primary.id == same.id
        assert primary.protection_leg_id
        assert primary.status == "planned"
        assert primary.parent_entry_order_id is None
        assert primary.pos_id is None
        assert primary.exchange_order_id is None


def test_protection_leg_binds_parent_then_position_then_verified_exchange_order(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        protection = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="take_profit",
            leg_index=2,
            planned_trigger_price="68000",
            planned_size="1",
        )
        bind_parent_entry_order(session, protection, parent_entry_order_id="entry-1")
        bind_filled_position(session, protection, pos_id="pos-1")
        verified = bind_verified_exchange_order(
            session,
            protection,
            exchange_order_id="tp-1",
            readback_evidence={"ordId": "tp-1", "posId": "pos-1"},
        )
        session.commit()
        assert verified.status == "verified"
        assert verified.parent_entry_order_id == "entry-1"
        assert verified.pos_id == "pos-1"
        assert verified.exchange_order_id == "tp-1"


def test_verified_protection_leg_rejects_conflicting_exchange_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        protection = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="backup_stop",
            leg_index=1,
            planned_trigger_price="61000",
            planned_size="3",
        )
        bind_filled_position(session, protection, pos_id="pos-1")
        bind_verified_exchange_order(
            session,
            protection,
            exchange_order_id="backup-1",
            readback_evidence={"ordId": "backup-1", "posId": "pos-1"},
        )
        with pytest.raises(ValueError, match="exchange_order_id"):
            bind_verified_exchange_order(
                session,
                protection,
                exchange_order_id="backup-2",
                readback_evidence={"ordId": "backup-2", "posId": "pos-1"},
            )
