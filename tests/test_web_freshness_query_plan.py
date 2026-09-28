from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.dialects import sqlite

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_app import build_global_freshness_statement, create_web_app


def _query_plan(session, statement) -> list[str]:
    sql = str(
        statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )
    rows = session.connection().exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    return [row[-1] for row in rows]


def test_global_freshness_statement_never_scans_raw_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        plan = _query_plan(session, build_global_freshness_statement())

    assert plan
    # "SCAN raw_messages USING ... INDEX" would still be a full index walk, so
    # any SCAN of the table counts; only SEARCH steps are acceptable.
    assert not [step for step in plan if step.startswith("SCAN raw_messages")], plan
    assert any("ix_raw_messages_posted_at" in step for step in plan), plan
    assert any("INTEGER PRIMARY KEY" in step for step in plan), plan


def test_freshness_api_keeps_response_shape_on_empty_database(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    body = client.get("/api/freshness").json()

    assert body["global"] == {"raw_message_id": 0, "created_at": None, "posted_at": None}
    assert body["selected"]["raw_message_id"] == 0
    assert body["selected"]["message_count"] == 0


def test_freshness_api_reports_latest_insert_and_latest_post(tmp_path):
    database_path = tmp_path / "research.db"
    app = create_web_app(database_path=database_path)
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=1,
                    message_id=10,
                    posted_at=datetime(2026, 9, 27, 12, 0),
                    created_at=datetime(2026, 9, 27, 12, 0, 1),
                    text="newest post, inserted first",
                ),
                RawMessage(
                    chat_id=2,
                    message_id=5,
                    posted_at=datetime(2026, 9, 26, 8, 0),
                    created_at=datetime(2026, 9, 27, 12, 5, 0),
                    text="backfilled older post, inserted last",
                ),
            ]
        )
        session.commit()
        latest_id = max(row.id for row in session.query(RawMessage).all())

    body = TestClient(app).get("/api/freshness?chat_id=1").json()

    assert body["global"] == {
        "raw_message_id": latest_id,
        "created_at": "2026-09-27T12:05:00",
        "posted_at": "2026-09-27T12:00:00",
    }
    assert body["selected"]["message_id"] == 10
    assert body["selected"]["message_count"] == 1
    assert body["selected"]["posted_at"] == "2026-09-27T12:00:00"
