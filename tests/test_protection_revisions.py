from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PositionProtectionRevision
from telegram_kol_research.protection_revisions import activate_protection_revision


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
