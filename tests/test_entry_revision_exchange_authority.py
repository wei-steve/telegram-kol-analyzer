from datetime import UTC, datetime, timedelta
import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    acquire_entry_revision_exchange_authority,
    release_entry_revision_exchange_authority,
)
from telegram_kol_research.models import TradingSetting
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)


def _stored_authority(session_factory):
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one()
        )
        return json.loads(row.value_json)


def _store_raw_authority(session_factory, payload):
    value_json = payload if isinstance(payload, str) else json.dumps(payload)
    with session_factory() as session:
        session.add(
            TradingSetting(
                key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                value_json=value_json,
                updated_at=NOW,
            )
        )
        session.commit()


def test_independent_process_owner_cannot_replace_held_authority(tmp_path):
    database_path = tmp_path / "research.db"
    worker_factory = create_session_factory(database_path)
    cli_factory = create_session_factory(database_path)

    worker = acquire_entry_revision_exchange_authority(
        worker_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:41",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )
    cli = acquire_entry_revision_exchange_authority(
        cli_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1001",
        acquired_at=NOW + timedelta(seconds=1),
        require_cancel_quiescence=True,
    )

    assert worker.acquired is True
    assert worker.reason_code is None
    assert worker.token
    assert cli.acquired is False
    assert cli.token is None
    assert cli.reason_code == "entry_revision_exchange_authority_busy"
    stored = _stored_authority(worker_factory)
    assert stored == {
        "acquired_at": NOW.isoformat(),
        "owner_id": "batch:41",
        "owner_kind": "entry_revision_worker",
        "schema_version": 1,
        "state": "held",
        "token": worker.token,
    }


def test_new_entry_worker_is_an_exact_authority_owner(tmp_path):
    session_factory = create_session_factory(tmp_path / "new-entry-owner.db")

    acquisition = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="new_entry_worker",
        owner_id="signal:71",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )

    assert acquisition.acquired is True
    assert _stored_authority(session_factory)["owner_kind"] == "new_entry_worker"


def test_legacy_entry_freeze_blocks_new_entry_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-frozen.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": False,
            "legacy_entry_submission_frozen": True,
            "entry_revision_v2_mode": "disabled",
        },
        updated_at=NOW,
    )

    acquisition = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="new_entry_worker",
        owner_id="signal:72",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )

    assert acquisition.acquired is False
    assert acquisition.reason_code == "legacy_entry_submission_frozen"


def test_exact_owner_release_allows_next_quiesced_cancellation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    worker = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:42",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )

    wrong = release_entry_revision_exchange_authority(
        session_factory,
        token="wrong-token",
        owner_kind="entry_revision_worker",
        released_at=NOW + timedelta(seconds=1),
    )
    assert wrong.released is False
    assert wrong.reason_code == "entry_revision_exchange_authority_owner_mismatch"
    assert _stored_authority(session_factory)["token"] == worker.token

    released = release_entry_revision_exchange_authority(
        session_factory,
        token=str(worker.token),
        owner_kind="entry_revision_worker",
        released_at=NOW + timedelta(seconds=2),
    )
    assert released.released is True
    assert released.reason_code is None
    assert _stored_authority(session_factory) == {
        "released_at": (NOW + timedelta(seconds=2)).isoformat(),
        "schema_version": 1,
        "state": "idle",
    }

    cancellation = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1002",
        acquired_at=NOW + timedelta(seconds=3),
        require_cancel_quiescence=True,
    )
    assert cancellation.acquired is True


@pytest.mark.parametrize(
    ("settings", "reason_code"),
    (
        (
            {"auto_trade_enabled": True, "entry_revision_v2_mode": "disabled"},
            "pending_entry_cancel_auto_trade_not_frozen",
        ),
        (
            {"auto_trade_enabled": False, "entry_revision_v2_mode": "shadow"},
            "pending_entry_cancel_revision_not_disabled",
        ),
        (
            {"auto_trade_enabled": False, "entry_revision_v2_mode": "live"},
            "pending_entry_cancel_revision_not_disabled",
        ),
    ),
)
def test_cancellation_authority_requires_frozen_settings(
    tmp_path,
    settings,
    reason_code,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, settings, updated_at=NOW)

    result = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1003",
        acquired_at=NOW + timedelta(seconds=1),
        require_cancel_quiescence=True,
    )

    assert result.acquired is False
    assert result.token is None
    assert result.reason_code == reason_code
    with session_factory() as session:
        assert (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one_or_none()
            is None
        )


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        [],
        {"schema_version": 2, "state": "idle", "released_at": NOW.isoformat()},
        {"schema_version": 1, "state": "unknown"},
        {
            "schema_version": 1,
            "state": "held",
            "owner_kind": "unknown_writer",
            "owner_id": "batch:9",
            "token": "token",
            "acquired_at": NOW.isoformat(),
        },
        {
            "schema_version": 1,
            "state": "held",
            "owner_kind": "entry_revision_worker",
            "owner_id": "batch:9",
            "token": "token",
            "acquired_at": NOW.isoformat(),
            "extra": True,
        },
    ),
)
def test_malformed_or_unknown_authority_fails_closed(tmp_path, payload):
    session_factory = create_session_factory(tmp_path / "research.db")
    _store_raw_authority(session_factory, payload)

    result = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:43",
        acquired_at=NOW + timedelta(seconds=1),
        require_cancel_quiescence=False,
    )

    assert result.acquired is False
    assert result.token is None
    assert result.reason_code == "entry_revision_exchange_authority_invalid"


def test_invalid_global_settings_fail_cancellation_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            TradingSetting(
                key="global",
                value_json="not-json",
                updated_at=NOW,
            )
        )
        session.commit()

    result = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1004",
        acquired_at=NOW + timedelta(seconds=1),
        require_cancel_quiescence=True,
    )

    assert result.acquired is False
    assert result.reason_code == "pending_entry_cancel_settings_invalid"
