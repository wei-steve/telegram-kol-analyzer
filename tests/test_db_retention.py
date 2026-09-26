"""Daily DB retention: pending-TPSL pruning and context-request stubbing.

``docs/plans/2026-09-26-server-disk-usage-analysis.md`` section 5.3. These pin:
the newest observation per instrument survives exactly as its only reader sees
it; the day boundary; dry-run writes nothing; a second run changes nothing;
rows the context worker can still pick up are never touched; every reader of
``request_summary_json`` reads a stub as "no full request" without failing or
changing a fingerprint; and batching, the time cap and the lock exit.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research import db_retention
from telegram_kol_research.context_analysis_backfill import _select_export_records
from telegram_kol_research.context_request_storage import (
    ContextRequestStorageError,
    build_retention_stub,
    collect_candidate_thread_ids,
    parse_context_request_storage,
)
from telegram_kol_research.context_resolution import _upsert_attempt
from telegram_kol_research.context_resolution_worker import (
    _attempt_candidate_thread_ids,
    build_context_state_fingerprint,
    build_redacted_exchange_state,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    PendingTpslSnapshotObservation,
    RawMessage,
    StrategyThread,
)
from telegram_kol_research.web_queries import _serialize_context_resolution


NOW = datetime(2026, 9, 27, 4, 10, tzinfo=UTC)


class _FakeClock:
    """Monotonic clock that moves only when a batch pause sleeps.

    Each pause advances it by ``per_pause`` seconds, so a budget of N.5 pauses
    lets exactly N+1 batches start.
    """

    def __init__(self, per_pause=0.0):
        self.value = 0.0
        self.per_pause = per_pause
        self.sleeps: list[float] = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += self.per_pause


def _run(db_path, *, clock=None, **kwargs):
    clock = clock or _FakeClock()
    kwargs.setdefault("now", NOW)
    return db_retention.run_retention(
        db_path, monotonic=clock, sleep=clock.sleep, **kwargs
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "research.db"
    session_factory = create_session_factory(path)
    return path, session_factory


def _count(path, table, where="1=1", params=()):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]


# --------------------------------------------------------------------------
# pending_tpsl_snapshot_observations
# --------------------------------------------------------------------------


def _observe(session_factory, instrument_id, observed_at, *, venue="deepcoin"):
    with session_factory() as session:
        row = PendingTpslSnapshotObservation(
            venue=venue,
            instrument_id=instrument_id,
            response_count=0,
            order_ids_json="[]",
            complete=True,
            observed_at=observed_at,
        )
        session.add(row)
        session.commit()
        return row.id


def _latest_like_the_reader(session_factory, instrument_id, venue="deepcoin"):
    # The exact query shape of strategy_records.py (pending snapshot lookup).
    with session_factory() as session:
        row = (
            session.query(PendingTpslSnapshotObservation)
            .filter(
                PendingTpslSnapshotObservation.venue == venue,
                PendingTpslSnapshotObservation.instrument_id == instrument_id,
            )
            .order_by(
                PendingTpslSnapshotObservation.observed_at.desc(),
                PendingTpslSnapshotObservation.id.desc(),
            )
            .first()
        )
        return None if row is None else row.id


def test_newest_observation_of_every_instrument_survives(db):
    path, sf = db
    # BTC: every row is old; the newest (a tie on observed_at, so higher id)
    # must stay.
    old = NOW - timedelta(days=20)
    for day in range(5):
        _observe(sf, "BTC-USDT-SWAP", old + timedelta(days=day))
    tie_low = _observe(sf, "BTC-USDT-SWAP", old + timedelta(days=5))
    tie_high = _observe(sf, "BTC-USDT-SWAP", old + timedelta(days=5))
    # ETH: old rows plus a recent one -- every old one goes.
    for day in range(4):
        _observe(sf, "ETH-USDT-SWAP", old + timedelta(days=day))
    eth_recent = _observe(sf, "ETH-USDT-SWAP", NOW - timedelta(days=1))
    # Same instrument under another venue is its own group.
    other_venue = _observe(sf, "BTC-USDT-SWAP", old, venue="other")

    before = {
        "btc": _latest_like_the_reader(sf, "BTC-USDT-SWAP"),
        "eth": _latest_like_the_reader(sf, "ETH-USDT-SWAP"),
        "other": _latest_like_the_reader(sf, "BTC-USDT-SWAP", venue="other"),
    }
    assert before == {"btc": tie_high, "eth": eth_recent, "other": other_venue}

    summary = _run(path, apply=True)

    task = summary["tasks"]["pending_tpsl_snapshot_observations"]
    assert task["candidates"] == 6 + 4
    assert task["processed"] == 10
    assert task["groups"] == 3
    assert summary["stop_reason"] == "completed"
    with sqlite3.connect(path) as connection:
        remaining = sorted(
            row[0]
            for row in connection.execute(
                "SELECT id FROM pending_tpsl_snapshot_observations"
            )
        )
    assert remaining == sorted([tie_high, eth_recent, other_venue])
    assert tie_low not in remaining
    after = {
        "btc": _latest_like_the_reader(sf, "BTC-USDT-SWAP"),
        "eth": _latest_like_the_reader(sf, "ETH-USDT-SWAP"),
        "other": _latest_like_the_reader(sf, "BTC-USDT-SWAP", venue="other"),
    }
    assert after == before


def test_day_boundary_is_strictly_older_than_the_cutoff(db):
    path, sf = db
    cutoff = NOW - timedelta(days=7)
    exactly_at = _observe(sf, "BTC-USDT-SWAP", cutoff)
    just_before = _observe(sf, "BTC-USDT-SWAP", cutoff - timedelta(microseconds=1))
    _observe(sf, "BTC-USDT-SWAP", NOW)

    summary = _run(path, apply=True)

    assert summary["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 1
    assert _count(path, "pending_tpsl_snapshot_observations", "id = ?", (exactly_at,)) == 1
    assert _count(path, "pending_tpsl_snapshot_observations", "id = ?", (just_before,)) == 0


def test_retain_days_is_a_parameter(db):
    path, sf = db
    _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=3))
    _observe(sf, "BTC-USDT-SWAP", NOW)

    assert _run(path, apply=True)["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 0
    summary = _run(path, apply=True, tpsl_retain_days=2)
    assert summary["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 1


def test_tpsl_batches_are_bounded_and_paused_between(db):
    path, sf = db
    for minute in range(10):
        _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=30, minutes=minute))
    _observe(sf, "BTC-USDT-SWAP", NOW)
    clock = _FakeClock()

    summary = _run(path, apply=True, tpsl_batch_size=3, clock=clock)

    task = summary["tasks"]["pending_tpsl_snapshot_observations"]
    assert task["processed"] == 10
    assert task["batches"] == 4  # 3 + 3 + 3 + 1
    assert clock.sleeps.count(db_retention.DEFAULT_BATCH_SLEEP_SECONDS) >= 4


def test_batch_size_above_the_hard_cap_is_refused(db):
    path, _ = db
    with pytest.raises(ValueError):
        _run(path, apply=True, tpsl_batch_size=db_retention.MAX_BATCH_SIZE + 1)
    with pytest.raises(ValueError):
        _run(path, apply=True, context_batch_size=db_retention.MAX_BATCH_SIZE + 1)


def test_time_cap_stops_between_batches_and_the_next_run_continues(db):
    path, sf = db
    for minute in range(10):
        _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=30, minutes=minute))
    _observe(sf, "BTC-USDT-SWAP", NOW)

    # Each batch pause is one second; a 2.5 s budget lets three batches start.
    summary = _run(
        path,
        apply=True,
        tpsl_batch_size=2,
        max_runtime_seconds=2.5,
        clock=_FakeClock(per_pause=1.0),
    )

    task = summary["tasks"]["pending_tpsl_snapshot_observations"]
    assert summary["stop_reason"] == "time_limit"
    assert task["stop_reason"] == "time_limit"
    assert (task["processed"], task["batches"]) == (6, 3)
    assert summary["tasks"]["context_resolution_attempts"]["stop_reason"] == "not_started"

    rest = _run(path, apply=True, tpsl_batch_size=2)
    assert rest["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 10 - task["processed"]
    assert _count(path, "pending_tpsl_snapshot_observations") == 1


def test_dry_run_counts_and_writes_nothing(db):
    path, sf = db
    for day in range(3):
        _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=10 + day))
    _observe(sf, "BTC-USDT-SWAP", NOW)
    raw_id, attempt_id = _legacy_attempt(sf, status="exhausted")
    before_bytes = path.read_bytes()

    summary = _run(path)

    assert summary["mode"] == "dry_run"
    tpsl = summary["tasks"]["pending_tpsl_snapshot_observations"]
    context = summary["tasks"]["context_resolution_attempts"]
    assert (tpsl["candidates"], tpsl["processed"], tpsl["batches"]) == (3, 0, 0)
    assert context["eligible"] == 1 and context["processed"] == 0
    assert _count(path, "pending_tpsl_snapshot_observations") == 4
    assert _request_of(sf, attempt_id) == _canonical(_request())
    assert path.read_bytes() == before_bytes


def test_dry_run_opens_the_database_read_only(db):
    path, _ = db
    connection = db_retention.connect(path, apply=False)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM pending_tpsl_snapshot_observations")
    finally:
        connection.close()


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        db_retention.connect(tmp_path / "absent.db", apply=True)
    assert not (tmp_path / "absent.db").exists()


def test_tpsl_second_run_is_a_no_op(db):
    path, sf = db
    for day in range(3):
        _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=10 + day))

    first = _run(path, apply=True)
    second = _run(path, apply=True)

    assert first["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 2
    assert second["tasks"]["pending_tpsl_snapshot_observations"]["candidates"] == 0
    assert second["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 0
    assert _count(path, "pending_tpsl_snapshot_observations") == 1


def test_database_locked_abandons_the_batch_and_exits_cleanly(db, monkeypatch):
    path, sf = db
    for day in range(3):
        _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=10 + day))
    monkeypatch.setattr(db_retention, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        summary = _run(path, apply=True)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    task = summary["tasks"]["pending_tpsl_snapshot_observations"]
    assert summary["stop_reason"] == "database_locked"
    assert task["stop_reason"] == "database_locked"
    assert "locked" in task["error"]
    assert task["processed"] == 0
    assert summary["tasks"]["context_resolution_attempts"]["stop_reason"] == "not_started"
    assert _count(path, "pending_tpsl_snapshot_observations") == 3

    again = _run(path, apply=True)
    assert again["tasks"]["pending_tpsl_snapshot_observations"]["processed"] == 2


# --------------------------------------------------------------------------
# context_resolution_attempts
# --------------------------------------------------------------------------


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request(filler=4000):
    return {
        "current_message": {"raw_message_id": 1, "chat_id": -100, "message_id": 10},
        "saved_evidence": {"version": 1, "text": "x" * filler},
        "message_context": {
            "messages": [{"raw_message_id": 2, "strategy_links": [{"strategy_thread_id": 17}]}],
            "reply_chain": [{"raw_message_id": 3, "thread_id": 16}],
        },
        "candidate_strategy_threads": [{"thread_id": 19}],
        "redacted_exchange_state": {},
        "mimo_first_pass": {"recognition_result": "是策略"},
    }


_MESSAGE_SEQ = iter(range(1000, 100000))


def _legacy_attempt(
    sf,
    *,
    status="completed",
    decision="resolved",
    triggers=(),
    posted_at=None,
    updated_at=None,
    request=None,
    with_projection=False,
):
    """A pre-R1 shaped row: full request, NULL thread-ID projection."""

    posted_at = posted_at or (NOW - timedelta(days=45))
    updated_at = updated_at or posted_at
    payload = request if request is not None else _request()
    with sf() as session:
        raw = RawMessage(
            chat_id=-100,
            message_id=next(_MESSAGE_SEQ),
            text="更新策略",
            posted_at=posted_at,
        )
        session.add(raw)
        session.flush()
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint=f"sha256:{raw.id}",
            model="deepseek-v4-flash",
            prompt_versions_json='{"context_resolution":"v1"}',
            request_summary_json=_canonical(payload) if isinstance(payload, dict) else payload,
            candidate_thread_ids_json=(
                _canonical(collect_candidate_thread_ids(payload)) if with_projection else None
            ),
            decision_json=_canonical({"decision": decision, "reason": "r"}),
            status=status,
            error_class="network_error" if status == "exhausted" else None,
            reanalysis_triggers_json=_canonical(list(triggers)),
            attempts=1,
            created_at=posted_at,
            updated_at=updated_at,
        )
        session.add(attempt)
        session.commit()
        return raw.id, attempt.id


def _request_of(sf, attempt_id):
    with sf() as session:
        return session.get(ContextResolutionAttempt, attempt_id).request_summary_json


def _full_row(path, attempt_id):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM context_resolution_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        )


def test_stub_replaces_only_the_request_column(db):
    path, sf = db
    _, attempt_id = _legacy_attempt(sf, status="exhausted")
    original = _request_of(sf, attempt_id)
    before = _full_row(path, attempt_id)

    summary = _run(path, apply=True)

    context = summary["tasks"]["context_resolution_attempts"]
    assert (context["candidates"], context["processed"], context["eligible"]) == (1, 1, 1)
    assert context["original_bytes"] == len(original.encode("utf-8"))
    after = _full_row(path, attempt_id)
    assert {k: v for k, v in after.items() if k != "request_summary_json"} == {
        k: v for k, v in before.items() if k != "request_summary_json"
    }
    parsed = parse_context_request_storage(after["request_summary_json"])
    assert parsed.storage == "retention-stub"
    assert parsed.request_payload is None
    assert parsed.record_sha256 == hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert parsed.candidate_thread_ids == (16, 17, 19)
    stub = json.loads(after["request_summary_json"])
    assert stub["original_bytes"] == len(original.encode("utf-8"))
    assert stub["stubbed_at"] == "2026-09-27T04:10:00Z"
    assert len(after["request_summary_json"]) < 400


@pytest.mark.parametrize(
    "status",
    ["running", "retry_pending", "pending_reanalysis", "pending", "some_future_status"],
)
def test_rows_the_worker_can_still_pick_up_are_never_touched(db, status):
    path, sf = db
    _, attempt_id = _legacy_attempt(sf, status=status)
    original = _request_of(sf, attempt_id)

    summary = _run(path, apply=True)

    assert summary["tasks"]["context_resolution_attempts"]["candidates"] == 0
    assert _request_of(sf, attempt_id) == original


@pytest.mark.parametrize(
    "status",
    [
        "exhausted",
        "superseded",
        "failed",
        "blocked_disabled",
        "blocked_execution_terminal",
        "reanalysis_capped",
    ],
)
def test_every_settled_status_is_stubbed(db, status):
    path, sf = db
    _, attempt_id = _legacy_attempt(sf, status=status)

    _run(path, apply=True)

    assert parse_context_request_storage(_request_of(sf, attempt_id)).storage == "retention-stub"


def test_completed_row_that_can_still_be_rescheduled_is_kept(db):
    path, sf = db
    _, eligible = _legacy_attempt(
        sf, decision="unresolved", triggers=("exchange_state_changed",)
    )
    _, hold_eligible = _legacy_attempt(sf, decision="hold", triggers=("x_later_trigger",))
    _, no_triggers = _legacy_attempt(sf, decision="unresolved", triggers=())
    _, resolved = _legacy_attempt(sf, decision="resolved", triggers=("message_edited",))

    summary = _run(path, apply=True)

    context = summary["tasks"]["context_resolution_attempts"]
    assert context["skipped_reanalysis_eligible"] == 2
    assert context["processed"] == 2
    for kept in (eligible, hold_eligible):
        assert parse_context_request_storage(_request_of(sf, kept)).storage == "legacy-full"
    for stubbed in (no_triggers, resolved):
        assert parse_context_request_storage(_request_of(sf, stubbed)).storage == "retention-stub"


def test_age_uses_message_time_and_row_activity(db):
    path, sf = db
    cutoff = NOW - timedelta(days=30)
    _, recent_message = _legacy_attempt(sf, status="exhausted", posted_at=NOW - timedelta(days=5))
    _, recently_touched = _legacy_attempt(
        sf,
        status="superseded",
        posted_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=2),
    )
    _, at_cutoff = _legacy_attempt(sf, status="exhausted", posted_at=cutoff)
    _, just_old = _legacy_attempt(
        sf, status="exhausted", posted_at=cutoff - timedelta(microseconds=1)
    )

    summary = _run(path, apply=True)

    context = summary["tasks"]["context_resolution_attempts"]
    assert context["candidates"] == 2  # recently_touched + just_old
    assert context["skipped_recent_activity"] == 1
    assert context["processed"] == 1
    assert parse_context_request_storage(_request_of(sf, just_old)).storage == "retention-stub"
    for kept in (recent_message, recently_touched, at_cutoff):
        assert parse_context_request_storage(_request_of(sf, kept)).storage == "legacy-full"


def test_context_retain_days_is_a_parameter(db):
    path, sf = db
    _, attempt_id = _legacy_attempt(sf, status="exhausted", posted_at=NOW - timedelta(days=10))

    assert _run(path, apply=True)["tasks"]["context_resolution_attempts"]["processed"] == 0
    summary = _run(path, apply=True, context_request_retain_days=7)
    assert summary["tasks"]["context_resolution_attempts"]["processed"] == 1


def test_row_age_falls_back_when_row_timestamps_are_missing():
    cutoff = "2026-08-28 04:10:00.000000"
    base = {
        "status": "exhausted",
        "decision_json": None,
        "reanalysis_triggers_json": "[]",
    }
    # Production has created_at NULL on nearly every row: updated_at decides.
    assert db_retention._context_row_is_settled(
        {**base, "updated_at": "2026-08-01 00:00:00.000000", "created_at": None}, cutoff
    ) is None
    assert db_retention._context_row_is_settled(
        {**base, "updated_at": "2026-09-20 00:00:00.000000", "created_at": None}, cutoff
    ) == "skipped_recent_activity"
    # updated_at missing: created_at decides.
    assert db_retention._context_row_is_settled(
        {**base, "updated_at": None, "created_at": "2026-09-20 00:00:00.000000"}, cutoff
    ) == "skipped_recent_activity"
    # Both missing: the message age (the candidate query) stands alone.
    assert db_retention._context_row_is_settled(
        {**base, "updated_at": None, "created_at": None}, cutoff
    ) is None


def test_context_stubbing_is_idempotent_and_leaves_markers_alone(db):
    path, sf = db
    _, legacy = _legacy_attempt(sf, status="exhausted")
    reference_only = (
        '{"contract":"context-resolution-request-storage-v1","storage":"reference_only"}'
    )
    _, marker = _legacy_attempt(sf, status="exhausted", request=reference_only)
    _, malformed = _legacy_attempt(sf, status="exhausted", request="{not json" + "x" * 5000)

    first = _run(path, apply=True)
    stub_after_first = _request_of(sf, legacy)
    second = _run(
        path, apply=True, now=NOW + timedelta(days=1)
    )

    assert first["tasks"]["context_resolution_attempts"]["processed"] == 1
    assert first["tasks"]["context_resolution_attempts"]["skipped_not_legacy_full"] == 2
    assert second["tasks"]["context_resolution_attempts"]["processed"] == 0
    assert second["tasks"]["context_resolution_attempts"]["skipped_not_legacy_full"] == 3
    assert _request_of(sf, legacy) == stub_after_first
    assert _request_of(sf, marker) == reference_only
    assert _request_of(sf, malformed).startswith("{not json")


def test_small_request_is_not_replaced_by_a_larger_stub(db):
    path, sf = db
    tiny = {"candidate_strategy_threads": []}
    _, attempt_id = _legacy_attempt(sf, status="exhausted", request=tiny)

    summary = _run(path, apply=True)

    assert summary["tasks"]["context_resolution_attempts"]["skipped_stub_not_smaller"] == 1
    assert _request_of(sf, attempt_id) == _canonical(tiny)


def test_context_batches_and_time_cap(db):
    path, sf = db
    ids = [_legacy_attempt(sf, status="exhausted")[1] for _ in range(5)]

    partial = _run(
        path,
        apply=True,
        context_batch_size=2,
        max_runtime_seconds=1.5,
        clock=_FakeClock(per_pause=1.0),
    )
    context = partial["tasks"]["context_resolution_attempts"]
    assert partial["stop_reason"] == "time_limit"
    assert context["stop_reason"] == "time_limit"
    assert (context["processed"], context["batches"]) == (4, 2)

    rest = _run(path, apply=True, context_batch_size=2)
    assert rest["tasks"]["context_resolution_attempts"]["processed"] == 1
    # The rerun pages through all five again; four are already stubs.
    assert rest["tasks"]["context_resolution_attempts"]["batches"] == 3
    assert rest["tasks"]["context_resolution_attempts"]["skipped_not_legacy_full"] == 4
    assert all(
        parse_context_request_storage(_request_of(sf, i)).storage == "retention-stub"
        for i in ids
    )


def test_context_lock_rolls_back_the_batch(db, monkeypatch):
    path, sf = db
    _, attempt_id = _legacy_attempt(sf, status="exhausted")
    original = _request_of(sf, attempt_id)
    monkeypatch.setattr(db_retention, "BUSY_TIMEOUT_MS", 50)

    real_batch = db_retention._context_batch

    def locking_batch(connection, *args, **kwargs):
        real_batch(connection, *args, **kwargs)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_retention, "_context_batch", locking_batch)
    summary = _run(path, apply=True)

    assert summary["stop_reason"] == "database_locked"
    assert summary["tasks"]["context_resolution_attempts"]["stop_reason"] == "database_locked"
    assert _request_of(sf, attempt_id) == original


# --------------------------------------------------------------------------
# readers of request_summary_json after stubbing
# --------------------------------------------------------------------------


def test_parser_accepts_only_the_exact_stub_shape():
    stub = build_retention_stub(_canonical(_request()), stubbed_at="2026-09-27T04:10:00Z")
    parsed = parse_context_request_storage(stub)
    with pytest.raises(ContextRequestStorageError, match="removed by retention"):
        parsed.require_legacy_full()

    body = json.loads(stub)
    for broken in (
        {**body, "extra": 1},
        {**body, "original_sha256": "nothex"},
        {**body, "original_bytes": True},
        {**body, "original_bytes": -1},
        {**body, "candidate_thread_ids": [3, 2]},
        {**body, "candidate_thread_ids": ["1"]},
        {**body, "stubbed_at": 5},
    ):
        with pytest.raises(ContextRequestStorageError):
            parse_context_request_storage(_canonical(broken))
    # The untagged shape an ad-hoc stub might have used would read as a full
    # legacy request -- which is why the stub is tagged.
    assert parse_context_request_storage('{"retention_stub":true}').storage == "legacy-full"


def test_stub_builder_refuses_markers_and_garbage():
    assert build_retention_stub("{not json", stubbed_at="t") is None
    assert build_retention_stub("[1,2]", stubbed_at="t") is None
    stub = build_retention_stub(_canonical(_request()), stubbed_at="t")
    assert build_retention_stub(stub, stubbed_at="t") is None


def _thread(sf, thread_id):
    with sf() as session:
        session.add(
            StrategyThread(
                id=thread_id,
                chat_id=-100,
                root_message_id=thread_id,
                symbol="BTC",
                side="long",
            )
        )
        session.commit()


def test_worker_fingerprint_and_exchange_state_are_unchanged_by_the_stub(db):
    path, sf = db
    for thread_id in (16, 17, 19):
        _thread(sf, thread_id)
    raw_id, attempt_id = _legacy_attempt(sf, status="exhausted")
    fingerprint_before = build_context_state_fingerprint(sf, raw_id)
    state_before = build_redacted_exchange_state(sf, raw_id)

    _run(path, apply=True)

    stored = _request_of(sf, attempt_id)
    assert parse_context_request_storage(stored).storage == "retention-stub"
    assert _attempt_candidate_thread_ids((None, stored)) == {16, 17, 19}
    assert build_context_state_fingerprint(sf, raw_id) == fingerprint_before
    assert build_redacted_exchange_state(sf, raw_id) == state_before


def test_worker_prefers_the_projection_column_when_present(db):
    path, sf = db
    raw_id, attempt_id = _legacy_attempt(sf, status="exhausted", with_projection=True)

    _run(path, apply=True)

    row = _full_row(path, attempt_id)
    assert _attempt_candidate_thread_ids(
        (row["candidate_thread_ids_json"], row["request_summary_json"])
    ) == {16, 17, 19}


def test_web_card_reads_a_stub_as_no_request(db):
    path, sf = db
    raw_id, attempt_id = _legacy_attempt(sf, status="exhausted")
    _run(path, apply=True)

    with sf() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        raw = session.get(RawMessage, raw_id)
        card = _serialize_context_resolution(
            raw_message=raw,
            attempt=attempt,
            decision=None,
            evidence=None,
            links=[],
            thread_history_by_thread_id={},
        )
        assert card["context_message_count"] == 0
        assert card["attempt_status"] == "exhausted"
        assert card["execution_state"] == "exhausted"

        # With R1 message references the count still comes from them.
        attempt.context_message_refs_json = '{"messages":[[1,2,3],[4,5,6]]}'
        card = _serialize_context_resolution(
            raw_message=raw,
            attempt=attempt,
            decision=None,
            evidence=None,
            links=[],
            thread_history_by_thread_id={},
        )
        assert card["context_message_count"] == 2


def _export_row(attempt_id, raw_message_id, request_summary_json):
    return {
        "source_attempt_id": attempt_id,
        "raw_message_id": raw_message_id,
        "source_state_fingerprint": "sha256:x",
        "prompt_versions_json": '{"context_resolution":"v1"}',
        "request_summary_json": request_summary_json,
        "source_status": "active",
        "job_status": "failed",
    }


def test_backfill_export_drops_a_message_whose_requests_were_all_stubbed():
    full = _canonical(_request())
    stub = build_retention_stub(full, stubbed_at="t")

    records = _select_export_records(
        [
            _export_row(10, 1, stub),
            _export_row(9, 1, stub),
            _export_row(20, 2, full),
        ]
    )
    assert [record["raw_message_id"] for record in records] == [2]

    # A stub next to a newer valid request: the valid one is still used.
    records = _select_export_records([_export_row(11, 3, stub), _export_row(10, 3, full)])
    assert records[0]["source_attempt_id"] == 10

    # Something genuinely malformed still fails the export, as before.
    with pytest.raises(ValueError, match="no valid source request"):
        _select_export_records([_export_row(12, 4, stub), _export_row(11, 4, "{bad")])


def test_upsert_over_a_stubbed_row_restores_the_full_request(db):
    path, sf = db
    raw_id, attempt_id = _legacy_attempt(sf, status="superseded")
    _run(path, apply=True)
    with sf() as session:
        fingerprint = session.get(ContextResolutionAttempt, attempt_id).context_fingerprint

    _upsert_attempt(
        sf,
        raw_message_id=raw_id,
        evidence_version_id=None,
        context_fingerprint=fingerprint,
        model="deepseek-v4-flash",
        request_payload=_request(),
        decision=None,
        status="retry_pending",
        error_class="network_error",
        attempts=1,
    )

    stored = _request_of(sf, attempt_id)
    assert parse_context_request_storage(stored).storage == "legacy-full"


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_cli_defaults_to_dry_run_and_prints_one_json_line(db, capsys):
    path, sf = db
    _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=400))
    _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=300))

    exit_code = db_retention.main(
        ["--database-path", str(path), "--batch-sleep-seconds", "0"]
    )

    out = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert len(out) == 1
    summary = json.loads(out[0])
    assert summary["mode"] == "dry_run"
    task = summary["tasks"]["pending_tpsl_snapshot_observations"]
    assert set(task) >= {"candidates", "processed", "batches", "elapsed_seconds"}
    assert task["candidates"] == 1 and task["processed"] == 0
    assert _count(path, "pending_tpsl_snapshot_observations") == 2


def test_systemd_units_run_as_the_worker_identity_at_idle_priority():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    service = (root / "telegram-kol-db-retention.service").read_text()
    timer = (root / "telegram-kol-db-retention.timer").read_text()
    lines = {line.strip() for line in service.splitlines()}

    assert "Type=oneshot" in lines
    assert "User=telegram-kol-worker" in lines
    assert "Group=telegram-kol-runtime" in lines
    assert "UMask=0007" in lines
    assert "Nice=19" in lines
    assert "IOSchedulingClass=idle" in lines
    assert "WorkingDirectory=/opt/telegram-kol-analyzer" in lines
    assert (
        "ExecStart=/opt/telegram-kol-analyzer/.venv/bin/python -m "
        "telegram_kol_research.db_retention --database-path data/research.db --apply"
    ) in lines
    assert "VACUUM" not in service.replace("never runs VACUUM", "")
    timer_lines = {line.strip() for line in timer.splitlines()}
    assert "OnCalendar=*-*-* 04:10:00" in timer_lines
    assert "Persistent=true" in timer_lines
    assert "Unit=telegram-kol-db-retention.service" in timer_lines


def test_a_busy_read_outside_a_batch_also_stops_cleanly(db, monkeypatch):
    path, sf = db
    _observe(sf, "BTC-USDT-SWAP", NOW - timedelta(days=30))

    def busy(connection):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_retention, "_tpsl_groups", busy)
    summary = _run(path, apply=True)

    assert summary["stop_reason"] == "database_locked"
    assert summary["tasks"]["pending_tpsl_snapshot_observations"]["stop_reason"] == "database_locked"
    assert summary["tasks"]["context_resolution_attempts"]["stop_reason"] == "not_started"


def test_connect_handles_a_non_ascii_path(tmp_path):
    directory = tmp_path / "telegram获取消息 dir"
    directory.mkdir()
    path = directory / "research.db"
    create_session_factory(path)

    summary = db_retention.run_retention(path, apply=True, now=NOW, sleep=lambda _s: None)

    assert summary["stop_reason"] == "completed"
