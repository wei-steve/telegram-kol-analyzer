"""Tests for the cross-group activity timeline (「动态」page).

Covers the approved design in
docs/plans/2026-09-28-dynamics-cross-group-timeline-design.md: a global,
read-only, keyset-paginated feed across every chat with rows in
``raw_messages``, ordered by ``(posted_at DESC, id DESC)``.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import and_, event, or_, text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_app import create_web_app, _timeline_group_names_by_chat_id
from telegram_kol_research.web_queries import (
    load_latest_raw_message_id,
    load_timeline_message_page,
)

BASE_TIME = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _seed_interleaved_messages(session_factory, count_per_chat=4, chat_ids=(11, 22, 33)):
    """Seed messages across several chats, interleaved in posted_at order.

    Message N (0-indexed, oldest first) in chat C gets
    posted_at = BASE_TIME + N minutes, so the global newest-first order is
    deterministic and spans all three chats repeatedly.
    """
    with session_factory() as session:
        for n in range(count_per_chat):
            for chat_id in chat_ids:
                session.add(
                    RawMessage(
                        chat_id=chat_id,
                        message_id=n + 1,
                        posted_at=BASE_TIME + timedelta(minutes=n, seconds=chat_id),
                        sender_name=f"sender-{chat_id}",
                        text=f"chat {chat_id} message {n}",
                    )
                )
        session.commit()


def _all_ids_via_paging(session_factory, page_size):
    """Walk load_timeline_message_page to exhaustion, returning raw_message_ids in order."""
    seen: list[int] = []
    cursor = None
    guard = 0
    while True:
        guard += 1
        assert guard < 1000, "paging did not terminate"
        rows, has_more = load_timeline_message_page(
            session_factory,
            page_size=page_size,
            before_raw_message_id=cursor,
        )
        seen.extend(row["raw_message_id"] for row in rows)
        if not has_more or not rows:
            break
        cursor = rows[-1]["raw_message_id"]
    return seen


def test_timeline_query_orders_globally_across_chats_newest_first(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory)

    rows, has_more = load_timeline_message_page(session_factory, page_size=100)

    assert has_more is False
    posted_ats = [row["posted_at"] for row in rows]
    assert posted_ats == sorted(posted_ats, reverse=True)
    chat_ids_present = {row["chat_id"] for row in rows}
    assert chat_ids_present == {11, 22, 33}


def test_timeline_query_breaks_ties_on_id_descending(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    same_time = BASE_TIME
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=1, message_id=1, posted_at=same_time, text="first inserted"),
                RawMessage(chat_id=2, message_id=1, posted_at=same_time, text="second inserted"),
                RawMessage(chat_id=3, message_id=1, posted_at=same_time, text="third inserted"),
            ]
        )
        session.commit()

    rows, _ = load_timeline_message_page(session_factory, page_size=10)

    # Same posted_at for all three -> tie-break must be the global `id`
    # (insertion order here), descending: last inserted first.
    ids = [row["raw_message_id"] for row in rows]
    assert ids == sorted(ids, reverse=True)
    texts = [row["text"] for row in rows]
    assert texts == ["third inserted", "second inserted", "first inserted"]


def test_timeline_query_pages_through_without_gaps_or_duplicates(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory, count_per_chat=5, chat_ids=(1, 2, 3, 4))

    full_page_ids = [
        row["raw_message_id"]
        for row in load_timeline_message_page(session_factory, page_size=1000)[0]
    ]
    paged_ids = _all_ids_via_paging(session_factory, page_size=3)

    assert paged_ids == full_page_ids
    assert len(paged_ids) == len(set(paged_ids)) == 20


def test_timeline_query_missing_cursor_row_returns_empty_page(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory)

    rows, has_more = load_timeline_message_page(
        session_factory, page_size=10, before_raw_message_id=999_999
    )

    assert rows == []
    assert has_more is False


def test_timeline_query_null_posted_at_rows_appear_after_dated_rows_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=1, message_id=1, posted_at=BASE_TIME, text="dated"),
                RawMessage(chat_id=1, message_id=2, posted_at=None, text="undated-a"),
                RawMessage(chat_id=1, message_id=3, posted_at=None, text="undated-b"),
            ]
        )
        session.commit()

    all_ids = _all_ids_via_paging(session_factory, page_size=2)
    rows, _ = load_timeline_message_page(session_factory, page_size=100)
    texts_in_order = [row["text"] for row in rows]

    assert texts_in_order[0] == "dated"
    assert set(texts_in_order[1:]) == {"undated-a", "undated-b"}
    assert len(all_ids) == len(set(all_ids)) == 3


def _explain_plan_lines(session_factory, sql, params):
    with session_factory() as session:
        result = session.execute(text(f"EXPLAIN QUERY PLAN {sql}"), params)
        return [str(row.detail) for row in result]


@contextmanager
def _CaptureSelects(session_factory):
    """Capture the exact SQL (with parameters) the engine executes while the
    caller's block runs, via SQLAlchemy's before_cursor_execute hook. This
    tracks whatever load_timeline_message_page *actually* issues -- not a
    hand-written SQL string that can silently drift from the real function,
    which is exactly what let the cursor-page regression through review the
    first time (see test_timeline_query_cursor_page_seeks_via_search_not_scan).
    """
    engine = session_factory.kw["bind"]
    captured: list[tuple[str, object]] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


def _explain_last_select(session_factory, captured, *, contains):
    """EXPLAIN QUERY PLAN for the last captured SELECT whose text contains
    every string in `contains` -- lets a test pick out one specific query
    (e.g. the ranged cursor-page SELECT, as opposed to the single-column
    cursor lookup that precedes it) out of everything the function ran."""
    matches = [
        (stmt, params)
        for stmt, params in captured
        if all(needle in stmt for needle in contains)
    ]
    assert matches, f"no captured SELECT contained all of {contains}; captured={captured}"
    stmt, params = matches[-1]
    with session_factory() as session:
        # `params` here is the raw DBAPI-level parameter sequence SQLite's
        # qmark paramstyle uses (captured straight from before_cursor_execute),
        # not a named-bind dict, so this runs it through the raw DBAPI cursor
        # rather than session.execute(text(...)).
        raw_cursor = session.connection().connection.cursor()
        raw_cursor.execute(f"EXPLAIN QUERY PLAN {stmt}", params)
        # EXPLAIN QUERY PLAN row shape: (id, parent, notused, detail).
        return [str(row[3]) for row in raw_cursor.fetchall()]


def test_timeline_query_first_page_uses_posted_at_index_without_temp_btree(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory)

    with _CaptureSelects(session_factory) as captured:
        load_timeline_message_page(session_factory, page_size=20)

    plan = _explain_last_select(
        session_factory, captured, contains=["FROM raw_messages", "LIMIT"]
    )
    plan_text = "\n".join(plan)

    assert "ix_raw_messages_posted_at" in plan_text
    assert "TEMP B-TREE" not in plan_text
    for line in plan:
        assert not (line.startswith("SCAN raw_messages") and "USING" not in line)


def test_timeline_query_cursor_page_seeks_via_search_not_scan(tmp_path):
    """Ground truth here is the plan for the *actual bound-parameter*
    statement load_timeline_message_page sends to the DBAPI cursor -- not a
    literal-value approximation. That distinction matters: on production
    SQLite 3.42, a single OR'd condition like
    `posted_at < ? OR (posted_at = ? AND id < ?)` plans as `SEARCH ...
    (posted_at<?)` when literal values are substituted at the sqlite3 CLI,
    but as a plain `SCAN ... USING INDEX ix_raw_messages_posted_at` (an
    ordered walk from the newest row) when executed with bound `?`
    parameters -- which is what this function, and every SQLAlchemy query,
    actually does. SQLite's planner fixes the plan at prepare time, before
    parameter values are bound, so it cannot use the OR-branch selectivity a
    literal would reveal.

    That is why load_timeline_message_page issues two single-condition
    queries instead of one OR'd query: "the rest of the cursor's own
    (posted_at, id) bucket" (posted_at == cursor AND id < cursor_id), then,
    only if that bucket is exhausted on this page, "older rows"
    (posted_at < cursor). Each one is a plain equality/inequality range,
    which SQLite always turns into an index SEARCH regardless of binding.
    This test captures the real SQL the function issues (with real bound
    parameters) and asserts the "older rows" query plans as a SEARCH.
    """
    session_factory = create_session_factory(tmp_path / "research.db")
    # A single message per posted_at bucket, so the cursor's own bucket is
    # empty after it and the "older rows" query is guaranteed to run.
    _seed_interleaved_messages(session_factory, count_per_chat=10, chat_ids=(1,))
    with session_factory() as session:
        cursor_row = (
            session.query(RawMessage)
            .order_by(RawMessage.posted_at.desc(), RawMessage.id.desc())
            .offset(3)
            .first()
        )
    assert cursor_row is not None

    with _CaptureSelects(session_factory) as captured:
        load_timeline_message_page(
            session_factory, page_size=5, before_raw_message_id=cursor_row.id
        )

    # The "older rows" SELECT: a plain `posted_at < ?`, distinguishable from
    # the single-column cursor lookup and the (empty here) same-bucket query
    # by selecting whole rows with a strict posted_at inequality and no
    # equality comparison on posted_at.
    plan = _explain_last_select(
        session_factory,
        captured,
        contains=["FROM raw_messages", "LIMIT", "posted_at <"],
    )
    plan_text = "\n".join(plan)

    assert any(line.startswith("SEARCH") for line in plan)
    assert "ix_raw_messages_posted_at" in plan_text
    assert "posted_at<" in plan_text.replace(" ", "")
    assert "TEMP B-TREE" not in plan_text


def test_or_combined_cursor_filter_regresses_to_a_scan_under_real_bound_params(tmp_path):
    """Proves the finding that motivated splitting the cursor-page query in
    two: a single OR'd condition -- the natural first-draft implementation,
    and what an earlier version of this function used -- degrades to a SCAN
    once real bound parameters are involved, even *without* an
    `OR posted_at IS NULL` disjunct. This captures the exact SQL and
    parameters SQLAlchemy would send to the DBAPI cursor for that OR'd
    query (built here, not inside the real function, since the real
    function no longer contains this shape) and confirms the regression is
    about bound-vs-literal execution, not specifically about the NULL
    disjunct.

    Verified by hand during development: substituting literal values for
    this same OR'd query at the SQL text level (`compile_kwargs=
    {"literal_binds": True}`) instead produces `SEARCH ...
    (posted_at<?)` -- which is what an EXPLAIN QUERY PLAN run against
    literal SQL (e.g. pasted into the sqlite3 CLI) would show, and is the
    misleading number a purely-literal test would have signed off on.
    """
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory)
    with session_factory() as session:
        cursor_row = (
            session.query(RawMessage)
            .order_by(RawMessage.posted_at.desc(), RawMessage.id.desc())
            .offset(3)
            .first()
        )
        cursor_posted_at = cursor_row.posted_at
        cursor_id = cursor_row.id

    with _CaptureSelects(session_factory) as captured:
        with session_factory() as session:
            session.query(RawMessage).filter(
                or_(
                    RawMessage.posted_at < cursor_posted_at,
                    and_(
                        RawMessage.posted_at == cursor_posted_at,
                        RawMessage.id < cursor_id,
                    ),
                )
            ).order_by(RawMessage.posted_at.desc(), RawMessage.id.desc()).limit(6).all()

    plan = _explain_last_select(
        session_factory, captured, contains=["FROM raw_messages", "LIMIT"]
    )

    assert any(line.startswith("SCAN") for line in plan)
    assert not any(line.startswith("SEARCH") for line in plan)


def test_load_latest_raw_message_id_seeks_rather_than_scans(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_interleaved_messages(session_factory)

    assert load_latest_raw_message_id(session_factory) > 0

    plan = _explain_plan_lines(
        session_factory, "SELECT max(id) FROM raw_messages", {}
    )
    for line in plan:
        assert not (line.startswith("SCAN raw_messages") and "USING" not in line)


def test_load_latest_raw_message_id_returns_zero_for_empty_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    assert load_latest_raw_message_id(session_factory) == 0


def test_timeline_group_names_helper_uses_group_config_in_memory():
    config = GroupConfig(
        groups=[
            TargetGroupConfig(chat_title="Raw Title", chat_id=11, custom_group_label="峰哥"),
            TargetGroupConfig(chat_title="Plain Group", chat_id=22),
        ]
    )
    names = _timeline_group_names_by_chat_id(config, group_labels_by_title={"Plain Group": "陈哥"})

    assert names[11] == "峰哥"
    assert names[22] == "陈哥"
    assert 33 not in names


def test_activity_timeline_route_shows_multiple_chats_with_group_badges(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    # More than one page's worth (MESSAGE_PAGE_SIZE == 20) so has_more is
    # true and the load-more button carries a before_raw_message_id cursor.
    _seed_interleaved_messages(session_factory, count_per_chat=8)

    group_config = GroupConfig(
        groups=[
            TargetGroupConfig(chat_title="Group Eleven", chat_id=11, custom_group_label="峰哥群"),
        ]
    )
    client = TestClient(
        create_web_app(database_path=database_path, group_config=group_config)
    )
    response = client.get("/activity/timeline")

    assert response.status_code == 200
    body = response.text
    assert 'data-message-scope="all"' in body
    assert "峰哥群" in body
    # Chat 22 has no configured label -> falls back to "群 22".
    assert "群 22" in body
    assert "data-message-filters" not in body
    assert "data-before-raw-message-id=" in body
    assert "message-group-badge" in body


def test_activity_timeline_new_message_baseline_is_max_id_not_newest_loaded_card(tmp_path):
    """Reconcile can back-fill a late-arriving message with a larger `id`
    but an older `posted_at`. If the panel's new-message baseline were
    `messages[0].raw_message_id` (the newest by posted_at) instead of
    `max(id)`, that back-filled row would make the baseline look stale
    forever right after a fresh load. Assert the rendered attribute is
    `max(id)`, which here is *not* the first card's raw_message_id.
    """
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=1,
                    message_id=1,
                    posted_at=BASE_TIME,
                    text="newest by posted_at",
                ),
                # Inserted after, so it has a larger id, but reconcile gave
                # it an older posted_at -- the back-fill scenario.
                RawMessage(
                    chat_id=2,
                    message_id=1,
                    posted_at=BASE_TIME - timedelta(hours=1),
                    text="back-filled: larger id, older posted_at",
                ),
            ]
        )
        session.commit()
        backfilled_id = (
            session.query(RawMessage.id)
            .filter(RawMessage.text.like("back-filled%"))
            .scalar()
        )
        newest_by_posted_at_id = (
            session.query(RawMessage.id)
            .filter(RawMessage.text == "newest by posted_at")
            .scalar()
        )

    assert backfilled_id > newest_by_posted_at_id

    client = TestClient(create_web_app(database_path=database_path))
    body = client.get("/activity/timeline").text

    marker = 'data-latest-raw-message-id="'
    start = body.index(marker) + len(marker)
    end = body.index('"', start)
    rendered_latest = int(body[start:end])

    assert rendered_latest == backfilled_id
    assert rendered_latest != newest_by_posted_at_id


def test_activity_messages_route_returns_next_page(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    _seed_interleaved_messages(session_factory, count_per_chat=15, chat_ids=(1, 2))

    client = TestClient(create_web_app(database_path=database_path))
    first = client.get("/activity/timeline").text
    marker_start = first.index('data-before-raw-message-id="') + len('data-before-raw-message-id="')
    marker_end = first.index('"', marker_start)
    cursor = first[marker_start:marker_end]

    response = client.get(f"/activity/messages?before_raw_message_id={cursor}")

    assert response.status_code == 200
    assert 'data-messages-panel' in response.text
    assert 'data-message-scope="all"' in response.text
    # The next page must not repeat the cursor row itself.
    assert f'id="message-{cursor}"' not in response.text


def test_group_messages_route_has_no_group_badge_and_keeps_single_group_filters(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    # 25 rows in a single chat so has_more is true for /groups/11/messages
    # (MESSAGE_PAGE_SIZE == 20).
    _seed_interleaved_messages(session_factory, count_per_chat=25, chat_ids=(11,))

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/11/messages")

    assert response.status_code == 200
    body = response.text
    assert "message-group-badge" not in body
    assert "data-message-filters" in body
    assert "data-before-message-id=" in body
    assert "data-before-raw-message-id=" not in body
    assert 'data-message-scope="all"' not in body
