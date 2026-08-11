from __future__ import annotations

import pytest

import telegram_kol_research.position_protection_legs as protection_legs
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
    materialize_verified_position_protection,
)
from telegram_kol_research.models import ExecutionOrderLeg
from datetime import datetime


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


def test_materialize_verified_position_protection_creates_one_legacy_leg_per_role(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        rows = materialize_verified_position_protection(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            pos_id="pos-1",
            primary_order_id="primary-1",
            primary_stop="62500",
            backup_stop="62375",
            take_profits=[("65100", "3"), ("65800", "1"), ("66400", "1")],
        )
        session.commit()
        observed = [
            (row.role, row.leg_index, row.planned_trigger_price, row.planned_size, row.status)
            for row in rows
        ]

    assert observed == [
        ("primary_stop", 1, "62500", None, "verified"),
        ("backup_stop", 1, "62375", "0", "waiting_fill"),
        ("take_profit", 1, "65100", "3", "waiting_fill"),
        ("take_profit", 2, "65800", "1", "waiting_fill"),
        ("take_profit", 3, "66400", "1", "waiting_fill"),
    ]


def test_bind_verified_filled_position_to_all_planned_protection_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, entry_leg_id)
        entry_leg.status = "active"
        entry_leg.attribution_status = "verified"
        entry_leg.pos_id = "pos-1"
        for role in ("primary_stop", "backup_stop", "take_profit"):
            create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=entry_leg_id,
                role=role,
                leg_index=1,
                planned_trigger_price="62000" if role != "backup_stop" else None,
                planned_size="3" if role != "backup_stop" else None,
            )

        rows = protection_legs.bind_verified_filled_position_protection(
            session,
            execution_order_leg_id=entry_leg_id,
            pos_id="pos-1",
        )

        assert {row.role for row in rows} == {
            "primary_stop",
            "backup_stop",
            "take_profit",
        }
        assert {row.pos_id for row in rows} == {"pos-1"}
        assert all(row.exchange_order_id is None for row in rows)
        assert all(row.status == "protection_recovery_pending" for row in rows)


def test_bind_filled_position_does_not_touch_unchanged_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        row = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="take_profit",
            leg_index=1,
            planned_trigger_price="65100",
            planned_size="3",
        )
        bind_filled_position(session, row, pos_id="pos-1")
        first_updated_at = row.updated_at

        bind_filled_position(session, row, pos_id="pos-1")

        assert row.status == "waiting_fill"
        assert row.updated_at == first_updated_at


def test_bind_verified_filled_position_rejects_unverified_entry_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)

    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, entry_leg_id)
        entry_leg.status = "active"
        entry_leg.attribution_status = "unassigned"
        entry_leg.pos_id = "pos-1"
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="62000",
            planned_size="3",
        )

        with pytest.raises(ValueError, match="entry_not_verified_filled"):
            protection_legs.bind_verified_filled_position_protection(
                session,
                execution_order_leg_id=entry_leg_id,
                pos_id="pos-1",
            )


def test_record_verified_take_profit_fill_is_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    entry_leg_id = _entry_leg_id(session_factory)
    completed_at = datetime(2026, 8, 2, 8, 0)

    with session_factory() as session:
        row = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            role="take_profit",
            leg_index=1,
            planned_trigger_price="65000",
            planned_size="3",
        )
        bind_filled_position(session, row, pos_id="pos-1")
        bind_verified_exchange_order(
            session,
            row,
            exchange_order_id="tp-1",
            readback_evidence={"ordId": "tp-1", "posId": "pos-1"},
        )
        first = protection_legs.record_verified_take_profit_fill(
            session,
            row,
            evidence={"evidence_tier": "exact_order_terminal"},
            completed_at=completed_at,
        )
        second = protection_legs.record_verified_take_profit_fill(
            session,
            row,
            evidence={"evidence_tier": "exact_order_terminal"},
            completed_at=completed_at,
        )

        assert first.id == second.id
        assert row.status == "filled"
        assert "exact_order_terminal" in row.readback_evidence_json
