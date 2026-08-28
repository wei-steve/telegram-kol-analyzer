from datetime import UTC, datetime
import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.legacy_runtime_drain_bridge import (
    LEGACY_RUNTIME_DRAIN_BRIDGE_KEY,
    LegacyRuntimeIdentity,
    begin_legacy_runtime_bridge_cancellation,
    build_legacy_runtime_drain_bridge_plan,
    complete_legacy_runtime_bridge_cancellation,
    fence_legacy_runtime_revisions,
    freeze_legacy_runtime_drain_bridge,
    mark_legacy_runtime_bridge_unknown,
    rollback_legacy_runtime_drain_bridge,
)
from telegram_kol_research.models import (
    MessageProcessingJob,
    RawMessage,
    TradingSetting,
)
from telegram_kol_research.trading_settings import (
    load_trading_settings,
    save_trading_settings,
)


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


def _absent_plan(session_factory):
    return build_legacy_runtime_drain_bridge_plan(
        session_factory,
        runtime_identity=_identity(),
        expected_production_sha=OLD_SHA,
        reviewed_order_ids=REVIEWED_IDS,
        planned_at=NOW,
    )


def test_freeze_atomically_records_settings_and_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "freeze.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=1, message_id=1, text="one"),
                RawMessage(chat_id=1, message_id=2, text="two"),
            ]
        )
        session.commit()
    plan = _absent_plan(session_factory)

    result = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="freeze-token-one",
        frozen_at=NOW,
    )

    assert result.status == "frozen"
    assert result.bridge_token
    settings = load_trading_settings(session_factory)
    assert settings.auto_trade_enabled is False
    assert settings.entry_revision_v2_mode == "disabled"
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one()
        )
        stored = json.loads(row.value_json)
    assert stored["state"] == "frozen"
    assert stored["freeze_raw_message_id"] == 2
    assert stored["original_auto_trade_enabled"] is True
    assert stored["original_entry_revision_v2_mode"] == "live"
    assert stored["fenced_batch_ids"] == []


def test_freeze_rechecks_plan_snapshot_inside_write_transaction(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-plan.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    plan = _absent_plan(session_factory)
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "shadow"},
        updated_at=NOW,
    )

    result = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="freeze-stale-plan-token",
        frozen_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "legacy_bridge_plan_mismatch"
    settings = load_trading_settings(session_factory)
    assert settings.auto_trade_enabled is True
    assert settings.entry_revision_v2_mode == "shadow"
    with session_factory() as session:
        assert (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one_or_none()
            is None
        )


def test_prefreeze_claimed_message_job_blocks_fence(tmp_path):
    session_factory = create_session_factory(tmp_path / "claimed-job.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="claimed")
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=1,
                status="claimed",
                shadow=False,
                claim_token="old-worker",
                claimed_at=NOW,
            )
        )
        session.commit()
    plan = _absent_plan(session_factory)
    frozen = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="freeze-token-two",
        frozen_at=NOW,
    )

    result = fence_legacy_runtime_revisions(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=_identity(),
        fenced_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "legacy_bridge_prefreeze_jobs_active"


def test_fence_succeeds_after_prefreeze_job_is_terminal(tmp_path):
    session_factory = create_session_factory(tmp_path / "fence.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    plan = _absent_plan(session_factory)
    frozen = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="freeze-token-three",
        frozen_at=NOW,
    )

    fenced = fence_legacy_runtime_revisions(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=_identity(),
        fenced_at=NOW,
    )

    assert fenced.status == "fenced"
    assert fenced.reason_code is None


def test_exact_prewrite_rollback_restores_settings(tmp_path):
    session_factory = create_session_factory(tmp_path / "rollback.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    plan = _absent_plan(session_factory)
    frozen = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="freeze-token-four",
        frozen_at=NOW,
    )
    fence_legacy_runtime_revisions(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=_identity(),
        fenced_at=NOW,
    )

    rolled_back = rollback_legacy_runtime_drain_bridge(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=_identity(),
        rolled_back_at=NOW,
    )

    assert rolled_back.status == "rolled_back"
    settings = load_trading_settings(session_factory)
    assert settings.auto_trade_enabled is True
    assert settings.entry_revision_v2_mode == "live"
    with session_factory() as session:
        assert (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one_or_none()
            is None
        )


def _fenced_bridge(session_factory):
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    plan = _absent_plan(session_factory)
    frozen = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=plan,
        runtime_identity=_identity(),
        reviewed_order_ids=REVIEWED_IDS,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="bridge-cancel-freeze-token",
        frozen_at=NOW,
    )
    fenced = fence_legacy_runtime_revisions(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=_identity(),
        fenced_at=NOW,
    )
    assert fenced.status == "fenced"
    return str(frozen.bridge_token)


def _stored_bridge(session_factory):
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one()
        )
        return json.loads(row.value_json)


def test_one_bridge_cancellation_records_only_exact_completed_order(tmp_path):
    session_factory = create_session_factory(tmp_path / "bridge-cancel.db")
    bridge_token = _fenced_bridge(session_factory)

    started = begin_legacy_runtime_bridge_cancellation(
        session_factory,
        bridge_token=bridge_token,
        runtime_identity=_identity(),
        order_id=REVIEWED_IDS[0],
        started_at=NOW,
    )
    during = _stored_bridge(session_factory)
    completed = complete_legacy_runtime_bridge_cancellation(
        session_factory,
        bridge_token=bridge_token,
        runtime_identity=_identity(),
        order_id=REVIEWED_IDS[0],
        completed_at=NOW,
    )
    after = _stored_bridge(session_factory)

    assert started.status == "cancelling"
    assert during["state"] == "cancelling"
    assert during["active_order_id"] == REVIEWED_IDS[0]
    assert during["write_boundary_reached"] is True
    assert completed.status == "fenced"
    assert after["state"] == "fenced"
    assert after["active_order_id"] is None
    assert after["completed_order_ids"] == [REVIEWED_IDS[0]]


def test_unknown_bridge_cancellation_is_permanently_non_retryable(tmp_path):
    session_factory = create_session_factory(tmp_path / "bridge-unknown.db")
    bridge_token = _fenced_bridge(session_factory)
    begin_legacy_runtime_bridge_cancellation(
        session_factory,
        bridge_token=bridge_token,
        runtime_identity=_identity(),
        order_id=REVIEWED_IDS[0],
        started_at=NOW,
    )

    unknown = mark_legacy_runtime_bridge_unknown(
        session_factory,
        bridge_token=bridge_token,
        runtime_identity=_identity(),
        order_id=REVIEWED_IDS[0],
        reason_code="cancel_outcome_unknown",
        observed_at=NOW,
    )
    retry = begin_legacy_runtime_bridge_cancellation(
        session_factory,
        bridge_token=bridge_token,
        runtime_identity=_identity(),
        order_id=REVIEWED_IDS[0],
        started_at=NOW,
    )
    stored = _stored_bridge(session_factory)

    assert unknown.status == "unknown_locked"
    assert retry.status == "blocked"
    assert retry.reason_code == "legacy_bridge_state_mismatch"
    assert stored["state"] == "unknown_locked"
    assert stored["active_order_id"] == REVIEWED_IDS[0]
