from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PendingTpslSnapshotObservation
from telegram_kol_research.protection_snapshot import (
    observe_pending_tpsl,
    record_pending_tpsl_observation,
)


def test_observation_marks_unknown_pagination_as_incomplete():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}], "nextCursor": "abc"},
    )

    assert observation["complete"] is False
    assert observation["order_ids"] == ["tp-1"]
    assert observation["reason"] == "pagination_metadata_unsupported"


def test_observation_proves_expected_exact_order_ids_visible():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}, {"ordId": "sl-1"}]},
        expected_order_ids={"tp-1", "sl-1"},
    )

    assert observation["complete"] is True
    assert observation["expected_order_ids_visible"] is True


def test_observation_is_persisted_append_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-observation.db")
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}]},
    )

    record_pending_tpsl_observation(session_factory, observation=observation)
    record_pending_tpsl_observation(session_factory, observation=observation)

    with session_factory() as session:
        rows = session.query(PendingTpslSnapshotObservation).all()
    assert len(rows) == 2
    assert rows[0].instrument_id == "BTC-USDT-SWAP"
    assert rows[0].response_count == 1
    assert rows[0].order_ids_json == '["tp-1"]'
    assert rows[0].complete is True
