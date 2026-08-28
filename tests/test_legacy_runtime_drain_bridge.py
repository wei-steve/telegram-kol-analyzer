from datetime import UTC, datetime
import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.legacy_runtime_drain_bridge import (
    LEGACY_RUNTIME_DRAIN_BRIDGE_KEY,
    LegacyRuntimeIdentity,
    build_legacy_runtime_drain_bridge_plan,
)
from telegram_kol_research.models import TradingSetting


NOW = datetime(2026, 8, 27, 22, 0, tzinfo=UTC)
OLD_SHA = "0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f"
REVIEWED_IDS = (
    "1001124718697641",
    "1001124718698413",
    "1001124760022605",
    "1001124760022650",
    "1001124898942178",
    "1001124905627977",
    "1001124905628046",
)


def _identity(**overrides):
    values = {
        "production_sha": OLD_SHA,
        "worker_pid": 2350028,
        "worker_start_ticks": 987654,
    }
    values.update(overrides)
    return LegacyRuntimeIdentity(**values)


def _store_bridge(session_factory, payload):
    value_json = payload if isinstance(payload, str) else json.dumps(payload)
    with session_factory() as session:
        session.add(
            TradingSetting(
                key=LEGACY_RUNTIME_DRAIN_BRIDGE_KEY,
                value_json=value_json,
                updated_at=NOW,
            )
        )
        session.commit()


def test_absent_bridge_plan_is_deterministic_and_read_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "bridge.db")

    first = build_legacy_runtime_drain_bridge_plan(
        session_factory,
        runtime_identity=_identity(),
        expected_production_sha=OLD_SHA,
        reviewed_order_ids=REVIEWED_IDS,
        planned_at=NOW,
    )
    second = build_legacy_runtime_drain_bridge_plan(
        session_factory,
        runtime_identity=_identity(),
        expected_production_sha=OLD_SHA,
        reviewed_order_ids=REVIEWED_IDS,
        planned_at=NOW,
    )

    assert first == second
    assert first.mode == "dry_run"
    assert first.state == "absent"
    assert first.conflicts == ()
    assert first.fenced_batch_ids == ()
    assert first.completed_order_ids == ()
    assert len(first.fingerprint) == 64
    with session_factory() as session:
        assert (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one_or_none()
            is None
        )


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        [],
        {"schema_version": 2, "state": "frozen"},
        {"schema_version": 1, "state": "unknown"},
        {
            "schema_version": 1,
            "state": "frozen",
            "bridge_token": "token",
            "production_sha": OLD_SHA,
            "worker_pid": 12,
            "worker_start_ticks": 34,
            "frozen_at": NOW.isoformat(),
            "freeze_raw_message_id": 0,
            "original_auto_trade_enabled": True,
            "original_entry_revision_v2_mode": "live",
            "reviewed_order_ids": list(REVIEWED_IDS),
            "fenced_batch_ids": [],
            "completed_order_ids": [],
            "write_boundary_reached": False,
            "updated_at": NOW.isoformat(),
            "extra": True,
        },
    ),
)
def test_malformed_or_unknown_bridge_state_fails_closed(tmp_path, payload):
    session_factory = create_session_factory(tmp_path / "invalid.db")
    _store_bridge(session_factory, payload)

    plan = build_legacy_runtime_drain_bridge_plan(
        session_factory,
        runtime_identity=_identity(),
        expected_production_sha=OLD_SHA,
        reviewed_order_ids=REVIEWED_IDS,
        planned_at=NOW,
    )

    assert plan.state == "invalid"
    assert plan.conflicts == (
        {"reason": "legacy_bridge_state_invalid"},
    )


@pytest.mark.parametrize(
    "identity",
    (
        lambda: _identity(production_sha="short"),
        lambda: _identity(worker_pid=0),
        lambda: _identity(worker_start_ticks=0),
    ),
)
def test_runtime_identity_is_strict(identity):
    with pytest.raises(ValueError):
        identity()


def test_expected_sha_and_reviewed_set_are_strict(tmp_path):
    session_factory = create_session_factory(tmp_path / "strict.db")

    with pytest.raises(ValueError, match="expected_production_sha"):
        build_legacy_runtime_drain_bridge_plan(
            session_factory,
            runtime_identity=_identity(),
            expected_production_sha="bad",
            reviewed_order_ids=REVIEWED_IDS,
            planned_at=NOW,
        )
    with pytest.raises(ValueError, match="reviewed_order_ids"):
        build_legacy_runtime_drain_bridge_plan(
            session_factory,
            runtime_identity=_identity(),
            expected_production_sha=OLD_SHA,
            reviewed_order_ids=(*REVIEWED_IDS[:-1], REVIEWED_IDS[0]),
            planned_at=NOW,
        )
