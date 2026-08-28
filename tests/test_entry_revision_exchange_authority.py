from datetime import UTC, datetime, timedelta
import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    EntryRevisionAuthorityProcessIdentity,
    acquire_entry_revision_exchange_authority,
    block_entry_revision_exchange_authority,
    mark_entry_revision_exchange_write_boundary,
    release_entry_revision_exchange_authority,
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.models import TradingSetting
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
PROCESS_IDENTITY = EntryRevisionAuthorityProcessIdentity(
    pid=4321,
    start_ticks=987654,
)


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


def _seed_idle(session_factory, *, generation=0):
    result = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
        initial_generation=generation,
    )
    assert result.seeded is True
    return result


def test_missing_authority_row_fails_closed_instead_of_auto_creating(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "missing.db")

    result = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:40",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )

    assert result.acquired is False
    assert result.reason_code == "entry_revision_exchange_authority_missing"
    with session_factory() as session:
        assert (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one_or_none()
            is None
        )


def test_idle_acquire_increments_generation_and_binds_owner_identity(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "generation.db")
    _seed_idle(session_factory, generation=4)

    result = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1001",
        action_id="drain-001",
        acquired_at=NOW + timedelta(seconds=1),
        deadline_at=NOW + timedelta(minutes=1),
        expected_generation=4,
        owner_identity=PROCESS_IDENTITY,
        authority_token="fresh-confirmation-token",
        plan_sha256="a" * 64,
        evidence_sha256="b" * 64,
        require_cancel_quiescence=True,
    )

    assert result.acquired is True
    assert result.generation == 5
    stored = _stored_authority(session_factory)
    assert stored == {
        "acquired_at": (NOW + timedelta(seconds=1)).isoformat(),
        "action_id": "drain-001",
        "deadline_at": (NOW + timedelta(minutes=1)).isoformat(),
        "evidence_sha256": "b" * 64,
        "generation": 5,
        "owner_kind": "reviewed_pending_entry_cancel",
        "owner_pid": 4321,
        "owner_start_ticks": 987654,
        "plan_sha256": "a" * 64,
        "schema_version": 2,
        "state": "held",
        "token_sha256": __import__("hashlib").sha256(
            b"fresh-confirmation-token"
        ).hexdigest(),
        "write_boundary_reached": False,
    }


def test_stale_generation_cannot_acquire_release_or_block(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale.db")
    _seed_idle(session_factory, generation=3)

    stale = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:41",
        acquired_at=NOW,
        expected_generation=2,
        require_cancel_quiescence=False,
    )

    assert stale.acquired is False
    assert stale.reason_code == "entry_revision_exchange_authority_generation_mismatch"
    assert _stored_authority(session_factory)["generation"] == 3


def test_write_boundary_unknown_blocks_and_retains_token_hash(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown.db")
    _seed_idle(session_factory)
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:1001",
        action_id="drain-001",
        acquired_at=NOW,
        deadline_at=NOW + timedelta(minutes=1),
        expected_generation=0,
        owner_identity=PROCESS_IDENTITY,
        authority_token="fresh-confirmation-token",
        plan_sha256="a" * 64,
        evidence_sha256="b" * 64,
        require_cancel_quiescence=True,
    )
    boundary = mark_entry_revision_exchange_write_boundary(
        session_factory,
        token="fresh-confirmation-token",
        owner_kind="reviewed_pending_entry_cancel",
        expected_generation=1,
        marked_at=NOW + timedelta(seconds=1),
    )
    blocked = block_entry_revision_exchange_authority(
        session_factory,
        token="fresh-confirmation-token",
        owner_kind="reviewed_pending_entry_cancel",
        expected_generation=1,
        reason_code="exchange_outcome_unknown",
        blocked_at=NOW + timedelta(seconds=2),
    )

    assert acquired.acquired is True
    assert boundary.marked is True
    assert blocked.blocked is True
    stored = _stored_authority(session_factory)
    assert stored["state"] == "blocked"
    assert stored["generation"] == 1
    assert stored["write_boundary_reached"] is True
    assert stored["reason_code"] == "exchange_outcome_unknown"
    assert stored["token_sha256"] == __import__("hashlib").sha256(
        b"fresh-confirmation-token"
    ).hexdigest()


def test_expired_held_authority_becomes_blocked_never_idle(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    _seed_idle(session_factory)
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:41",
        acquired_at=NOW,
        deadline_at=NOW + timedelta(seconds=1),
        expected_generation=0,
        owner_identity=PROCESS_IDENTITY,
        require_cancel_quiescence=False,
    )

    later = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:42",
        acquired_at=NOW + timedelta(seconds=2),
        expected_generation=1,
        owner_identity=PROCESS_IDENTITY,
        require_cancel_quiescence=False,
    )

    assert acquired.acquired is True
    assert later.acquired is False
    assert later.reason_code == "entry_revision_exchange_authority_expired_blocked"
    assert _stored_authority(session_factory)["state"] == "blocked"


def test_release_cas_failure_leaves_exact_held_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "release-cas.db")
    _seed_idle(session_factory)
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:41",
        acquired_at=NOW,
        expected_generation=0,
        owner_identity=PROCESS_IDENTITY,
        require_cancel_quiescence=False,
    )
    before = _stored_authority(session_factory)

    released = release_entry_revision_exchange_authority(
        session_factory,
        token=str(acquired.token),
        owner_kind="entry_revision_worker",
        expected_generation=99,
        released_at=NOW + timedelta(seconds=1),
    )

    assert released.released is False
    assert released.reason_code == "entry_revision_exchange_authority_generation_mismatch"
    assert _stored_authority(session_factory) == before


def test_ordinary_settings_write_cannot_overwrite_authority_key(tmp_path):
    session_factory = create_session_factory(tmp_path / "settings-owner.db")
    _seed_idle(session_factory, generation=9)
    before = _stored_authority(session_factory)

    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW + timedelta(seconds=1),
    )

    assert _stored_authority(session_factory) == before


def test_independent_process_owner_cannot_replace_held_authority(tmp_path):
    database_path = tmp_path / "research.db"
    worker_factory = create_session_factory(database_path)
    cli_factory = create_session_factory(database_path)
    _seed_idle(worker_factory)

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
    assert stored["acquired_at"] == NOW.isoformat()
    assert stored["action_id"] == "batch:41"
    assert stored["generation"] == 1
    assert stored["owner_kind"] == "entry_revision_worker"
    assert stored["schema_version"] == 2
    assert stored["state"] == "held"
    assert stored["token_sha256"] == __import__("hashlib").sha256(
        str(worker.token).encode("utf-8")
    ).hexdigest()


def test_new_entry_worker_is_an_exact_authority_owner(tmp_path):
    session_factory = create_session_factory(tmp_path / "new-entry-owner.db")
    _seed_idle(session_factory)

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
    _seed_idle(session_factory)
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
    assert _stored_authority(session_factory)["token_sha256"] == (
        __import__("hashlib").sha256(str(worker.token).encode("utf-8")).hexdigest()
    )

    released = release_entry_revision_exchange_authority(
        session_factory,
        token=str(worker.token),
        owner_kind="entry_revision_worker",
        released_at=NOW + timedelta(seconds=2),
    )
    assert released.released is True
    assert released.reason_code is None
    assert _stored_authority(session_factory) == {
        "generation": 1,
        "released_at": (NOW + timedelta(seconds=2)).isoformat(),
        "schema_version": 2,
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
