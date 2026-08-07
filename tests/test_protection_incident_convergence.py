import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionProtectionRevision,
    PositionTakeProfitOrder,
)
from telegram_kol_research.protection_incident_convergence import (
    audit_protection_incident_convergence,
)


NOW = datetime(2026, 8, 7, 2, 0, tzinfo=UTC)


def _incident(session, *, suffix, pos_id, binding_status="active"):
    binding = ExecutionBinding(
        strategy_instance_id=f"strategy-{suffix}",
        kol_id="kol",
        chat_id=1,
        message_id=int(suffix) if str(suffix).isdigit() else 99,
        symbol="BTC",
        side="long",
        status=binding_status,
    )
    session.add(binding)
    session.flush()
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=0,
        purpose="entry",
        order_kind="market",
        pos_id=pos_id,
        status="active" if binding_status == "active" else "closed",
    )
    session.add(leg)
    session.flush()
    incident = PositionProtectionIncident(
        execution_binding_id=binding.id,
        execution_order_leg_id=leg.id,
        pos_id=pos_id,
        incident_type="protection_missing",
        fingerprint=(suffix * 64)[:64],
        evidence_json='{"secret":"must-not-render"}',
        delivery_status="pending",
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
    )
    session.add(incident)
    session.flush()
    return binding, leg, incident


def _complete_replacement(session, *, binding, leg, pos_id):
    replacements = [
        ("primary_stop", "stop_loss", "primary-current", "64100"),
        ("backup_stop", "backup_stop", "backup-current", "63900"),
        ("take_profit", "take_profit", "tp-current", "66000"),
    ]
    for role, purpose, order_id, price in replacements:
        session.add(
            PositionProtectionLedger(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=order_id,
                purpose=purpose,
                trigger_price=price,
                size_text="1",
                status="verified",
                evidence_source="management_tpsl_readback",
                evidence_json="{}",
            )
        )
    session.add(
        PositionProtectionRevision(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id=pos_id,
            source="management_tpsl_readback",
            status="active",
            protection_json=json.dumps(
                {
                    "roles": [item[0] for item in replacements],
                    "order_ids": [item[2] for item in replacements],
                    "replacements": [
                        {
                            "role": role,
                            "order_id": order_id,
                            "trigger_price": price,
                            "size_text": "1",
                        }
                        for role, _purpose, order_id, price in replacements
                    ],
                }
            ),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        PositionBackupStopOrder(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            trigger_price="63900",
            order_id="backup-current",
            client_order_id=f"backup-{pos_id}",
            status="active",
            request_json="{}",
        )
    )


def _legacy_current_protection(session, *, binding, leg, incident, pos_id):
    revision = PositionProtectionRevision(
        execution_binding_id=binding.id,
        execution_order_leg_id=leg.id,
        strategy_instance_id=binding.strategy_instance_id,
        pos_id=pos_id,
        source="entry_protection",
        status="active",
        protection_json=json.dumps({"order_ids": ["legacy-primary"]}),
        created_at=incident.created_at - timedelta(hours=1),
        updated_at=incident.created_at - timedelta(hours=1),
    )
    session.add(revision)
    rows = (
        ("legacy-primary", "stop_loss", "64100", "6"),
        ("legacy-backup", "stop_loss", "63971.8", None),
        ("legacy-tp", "take_profit", "67000", "6"),
    )
    for order_id, purpose, trigger_price, size_text in rows:
        session.add(
            PositionProtectionLedger(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=order_id,
                purpose=purpose,
                trigger_price=trigger_price,
                size_text=size_text,
                status="verified",
                evidence_source="position_mutation_intent_readback",
                evidence_json="{}",
            )
        )
    session.add(
        PositionBackupStopOrder(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            trigger_price="63971.8",
            order_id="legacy-backup",
            client_order_id=f"backup-{pos_id}",
            status="active",
            request_json='{"slTriggerPx":"63971.8"}',
        )
    )
    session.add(
        PositionTakeProfitOrder(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=pos_id,
            order_id="legacy-tp",
            trigger_price="67000",
            size_text="6",
            status="active",
        )
    )


def _legacy_current_snapshot(*, pending_orders=None, observations=None):
    return SimpleNamespace(
        positions=[
            {
                "posId": "pos-legacy-current",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "6",
            }
        ],
        pending_trigger_orders=pending_orders
        if pending_orders is not None
        else [
            {
                "ordId": "legacy-primary",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "sz": "6",
                "slTriggerPx": "64100",
            },
            {
                "ordId": "legacy-backup",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "sz": "0",
                "slTriggerPx": "63971.8",
            },
            {
                "ordId": "legacy-tp",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "sz": "6",
                "tpTriggerPx": "67000",
            },
        ],
        pending_tpsl_observations=observations
        if observations is not None
        else [{"instrument_id": "BTC-USDT-SWAP", "complete": True}],
        errors={},
    )


def _source_rows(session_factory):
    models = (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionProtectionIncident,
        PositionProtectionLedger,
        PositionProtectionRevision,
        PositionBackupStopOrder,
        PositionTakeProfitOrder,
    )
    with session_factory() as session:
        return {
            model.__tablename__: [
                tuple(getattr(row, column.name) for column in model.__table__.columns)
                for row in session.query(model).order_by(model.id).all()
            ]
            for model in models
        }


def test_legacy_current_exchange_evidence_without_pending_pos_id_resolves(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, first = _incident(
            session, suffix="b", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=first,
            pos_id="pos-legacy-current",
        )
        session.add(
            PositionProtectionIncident(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-legacy-current",
                incident_type="backup_stop_blocked",
                fingerprint="c" * 64,
                evidence_json='{"reason_code":"readback_unavailable"}',
                delivery_status="pending",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    snapshot = _legacy_current_snapshot()

    result = audit_protection_incident_convergence(
        session_factory, snapshot=snapshot, limit=100
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 2
    assert result["counts"]["current_risk"] == 0
    rendered = json.dumps(result)
    assert "pos-legacy-current" not in rendered
    assert "legacy-primary" not in rendered
    assert "legacy-backup" not in rendered
    assert "legacy-tp" not in rendered


def test_current_evidence_rejects_wrong_local_instrument(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _incident(
            session, suffix="d", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=incident,
            pos_id="pos-legacy-current",
        )
        session.flush()
        primary = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id="legacy-primary")
            .one()
        )
        primary.instrument_id = "ETH-USDT-SWAP"
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=_legacy_current_snapshot(),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["current_risk"] == 1


@pytest.mark.parametrize(
    "missing_order_id",
    ("legacy-primary", "legacy-backup", "legacy-tp"),
)
def test_current_evidence_rejects_missing_required_order(
    tmp_path,
    missing_order_id,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _incident(
            session, suffix="e", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=incident,
            pos_id="pos-legacy-current",
        )
        session.commit()
    pending = [
        row
        for row in _legacy_current_snapshot().pending_trigger_orders
        if row["ordId"] != missing_order_id
    ]

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=_legacy_current_snapshot(pending_orders=pending),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["current_risk"] == 1


def test_current_evidence_requires_target_instrument_completeness(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _incident(
            session, suffix="f", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=incident,
            pos_id="pos-legacy-current",
        )
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=_legacy_current_snapshot(
            observations=[
                {"instrument_id": "ETH-USDT-SWAP", "complete": True}
            ]
        ),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["evidence_insufficient"] == 1


def test_current_evidence_rejects_unowned_order_for_exact_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _incident(
            session, suffix="g", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=incident,
            pos_id="pos-legacy-current",
        )
        session.commit()
    pending = deepcopy(_legacy_current_snapshot().pending_trigger_orders)
    pending.append(
        {
            "ordId": "manual-stop",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-legacy-current",
            "posSide": "long",
            "sz": "0",
            "slTriggerPx": "63800",
        }
    )

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=_legacy_current_snapshot(pending_orders=pending),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["current_risk"] == 1


def test_current_evidence_audit_preserves_every_source_row(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _incident(
            session, suffix="h", pos_id="pos-legacy-current"
        )
        _legacy_current_protection(
            session,
            binding=binding,
            leg=leg,
            incident=incident,
            pos_id="pos-legacy-current",
        )
        session.commit()
    before = _source_rows(session_factory)

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=_legacy_current_snapshot(),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 1
    assert _source_rows(session_factory) == before


def test_audit_classifies_live_before_history_and_redacts_output(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        resolved = _incident(session, suffix="1", pos_id="pos-resolved")
        _complete_replacement(
            session, binding=resolved[0], leg=resolved[1], pos_id="pos-resolved"
        )
        _incident(session, suffix="2", pos_id="pos-risk")
        _incident(
            session,
            suffix="3",
            pos_id="pos-terminal",
            binding_status="closed",
        )
        _incident(session, suffix="4", pos_id="pos-unknown")
        session.commit()

    snapshot = SimpleNamespace(
        positions=[
            {
                "posId": "pos-resolved",
                "instId": "BTC-USDT-SWAP",
                "pos": "1",
            },
            {
                "posId": "pos-risk",
                "instId": "BTC-USDT-SWAP",
                "pos": "1",
            },
        ],
        pending_trigger_orders=[
            {"ordId": "primary-current", "posId": "pos-resolved"},
            {"ordId": "backup-current", "posId": "pos-resolved"},
            {"ordId": "tp-current", "posId": "pos-resolved"},
        ],
        pending_tpsl_observations=[
            {"instrument_id": "BTC-USDT-SWAP", "complete": True}
        ],
        errors={},
    )

    result = audit_protection_incident_convergence(
        session_factory, snapshot=snapshot, limit=100
    )

    assert result["counts"] == {
        "resolved_by_current_exchange_evidence": 1,
        "current_risk": 1,
        "historical_terminal": 1,
        "evidence_insufficient": 1,
    }
    assert [item["classification"] for item in result["incidents"]] == [
        "resolved_by_current_exchange_evidence",
        "current_risk",
        "historical_terminal",
        "evidence_insufficient",
    ]
    assert result["output_complete"] is True
    rendered = json.dumps(result)
    assert "must-not-render" not in rendered
    assert "primary-current" not in rendered
    assert "pos-resolved" not in rendered


def test_incomplete_exchange_snapshot_never_resolves_and_marks_output_incomplete(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        resolved = _incident(session, suffix="5", pos_id="pos-resolved")
        _complete_replacement(
            session, binding=resolved[0], leg=resolved[1], pos_id="pos-resolved"
        )
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=SimpleNamespace(
            positions=[{"posId": "pos-resolved", "pos": "1"}],
            pending_trigger_orders=[],
            pending_tpsl_observations=[
                {"instrument_id": "BTC-USDT-SWAP", "complete": False}
            ],
            errors={"pending_trigger_orders": "unavailable"},
        ),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["evidence_insufficient"] == 1
    assert result["output_complete"] is False


def test_audit_is_bounded_and_reports_truncation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        for suffix in ("6", "7", "8"):
            _incident(session, suffix=suffix, pos_id=f"pos-{suffix}")
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=SimpleNamespace(positions=[], pending_trigger_orders=[], errors={}),
        limit=2,
    )

    assert result["incident_total"] == 3
    assert result["incidents_returned"] == 2
    assert result["incidents_truncated"] is True
    assert result["output_complete"] is False


def test_incomplete_pending_pagination_cannot_resolve_current_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        resolved = _incident(session, suffix="9", pos_id="pos-resolved")
        _complete_replacement(
            session, binding=resolved[0], leg=resolved[1], pos_id="pos-resolved"
        )
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=SimpleNamespace(
            positions=[{"posId": "pos-resolved", "pos": "1"}],
            pending_trigger_orders=[
                {"ordId": order_id, "posId": "pos-resolved"}
                for order_id in ("primary-current", "backup-current", "tp-current")
            ],
            pending_tpsl_observations=[
                {"instrument_id": "BTC-USDT-SWAP", "complete": False}
            ],
            errors={},
        ),
        limit=100,
    )

    assert result["counts"]["resolved_by_current_exchange_evidence"] == 0
    assert result["counts"]["evidence_insufficient"] == 1
    assert result["output_complete"] is False


def test_hostile_incident_type_is_hashed_and_never_rendered(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    hostile = "api_key=secret-value\n" + "x" * 500
    with session_factory() as session:
        _binding, _leg, incident = _incident(
            session, suffix="a", pos_id="pos-hostile", binding_status="closed"
        )
        incident.incident_type = hostile
        incident.evidence_json = '{"password":"also-secret"}'
        session.commit()

    result = audit_protection_incident_convergence(
        session_factory,
        snapshot=SimpleNamespace(
            positions=[],
            pending_trigger_orders=[],
            pending_tpsl_observations=[],
            errors={},
        ),
        limit=100,
    )

    rendered = json.dumps(result)
    assert hostile not in rendered
    assert "secret-value" not in rendered
    assert "also-secret" not in rendered
    assert "incident_type" not in result["incidents"][0]
    assert result["incidents"][0]["incident_type_ref"].startswith("type:")
