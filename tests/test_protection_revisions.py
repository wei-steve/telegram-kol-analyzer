from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PositionProtectionRevision
from telegram_kol_research.protection_revisions import (
    activate_protection_revision,
    confirm_visible_protection_revision,
    expire_unconfirmed_protection_revisions,
    record_replacing_protection_revision,
)


def test_activating_replacement_supersedes_prior_revision(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        first = activate_protection_revision(
            session,
            execution_binding_id=1,
            execution_order_leg_id=2,
            strategy_instance_id="deepcoin:chat:1:BTC:long",
            pos_id="pos-1",
            source="entry_protection",
            protection_json={"orders": ["tp-1", "sl-1"]},
        )
        second = activate_protection_revision(
            session,
            execution_binding_id=1,
            execution_order_leg_id=2,
            strategy_instance_id="deepcoin:chat:1:BTC:long",
            pos_id="pos-1",
            source="management_replacement",
            protection_json={"orders": ["tp-2", "sl-2"]},
        )
        session.commit()

    with session_factory() as session:
        rows = session.query(PositionProtectionRevision).order_by(PositionProtectionRevision.id).all()

    assert rows[0].status == "superseded"
    assert rows[1].status == "active"
    assert rows[1].previous_revision_id == rows[0].id


def test_replacing_revision_expires_after_five_minutes(tmp_path):
    session_factory = create_session_factory(tmp_path / "expiry.db")
    with session_factory() as session:
        replacement = record_replacing_protection_revision(
            session, execution_binding_id=1, execution_order_leg_id=2,
            strategy_instance_id="strategy-1", pos_id="pos-1", source="replacement",
            protection_json={"order_ids": ["tp-2"]},
        )
        replacement.created_at = replacement.created_at.replace(year=2026, month=7, day=15, hour=8, minute=0, second=0, tzinfo=None)
        session.flush()
        assert expire_unconfirmed_protection_revisions(
            session, now=replacement.created_at.replace(minute=5)
        ) == [replacement.id]
        replacement_id = replacement.id
        session.commit()
    with session_factory() as session:
        assert session.get(PositionProtectionRevision, replacement_id).status == "visibility_expired"


def test_replacement_stays_replacing_until_exact_order_ids_are_visible(tmp_path):
    session_factory = create_session_factory(tmp_path / "replacing.db")
    with session_factory() as session:
        first = activate_protection_revision(
            session, execution_binding_id=1, execution_order_leg_id=2,
            strategy_instance_id="strategy-1", pos_id="pos-1", source="entry",
            protection_json={"order_ids": ["tp-1", "sl-1"]},
        )
        replacement = record_replacing_protection_revision(
            session, execution_binding_id=1, execution_order_leg_id=2,
            strategy_instance_id="strategy-1", pos_id="pos-1", source="replacement",
            protection_json={"order_ids": ["tp-2", "sl-2"]},
        )
        assert first.status == "active"
        assert replacement.status == "replacing"
        assert replacement.previous_revision_id == first.id
        assert confirm_visible_protection_revision(
            session, venue="deepcoin", pos_id="pos-1", visible_order_ids={"tp-2"}
        ) is False
        assert confirm_visible_protection_revision(
            session, venue="deepcoin", pos_id="pos-1", visible_order_ids={"tp-2", "sl-2"}
        ) is True
        session.commit()

    with session_factory() as session:
        rows = session.query(PositionProtectionRevision).order_by(PositionProtectionRevision.id).all()
    assert rows[0].status == "superseded"
    assert rows[1].status == "active"
