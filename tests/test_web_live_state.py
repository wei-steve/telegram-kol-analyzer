from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.dialects import sqlite

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.web_app import (
    build_global_freshness_statement,
    create_web_app,
)
from telegram_kol_research.web_live_state import (
    LiveStateCache,
    build_strategy_version_statements,
    positions_version,
    strip_volatile_snapshot_keys,
)

NOW = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _snapshot_payload(last_px="60000", upl="12.5"):
    return {
        "_live_source": {
            "positions": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "59000",
                    "lastPx": last_px,
                    "unrealizedProfit": upl,
                    "liqPx": "50000",
                }
            ],
            "tpsl_orders": [],
            "tpsl_evidence_available": True,
        }
    }


def _make_app(tmp_path, **kwargs):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        now_provider=lambda: NOW,
        **kwargs,
    )
    clock = FakeClock()
    app.state.live_state_clock = clock
    return app, clock


def _versions(client):
    return client.get("/api/live/state").json()["versions"]


def _add_lifecycle(session_factory, *, status="entered", message_id=1):
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=77,
            message_id=message_id,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status=status,
            signal_at=NOW,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        return lifecycle.id


def test_live_state_response_shape(tmp_path):
    app, _clock = _make_app(tmp_path)

    body = TestClient(app).get("/api/live/state").json()

    assert set(body) == {
        "server_time",
        "monitor",
        "versions",
        "positions_captured_at",
    }
    assert body["server_time"].endswith("+08:00")
    assert {"state", "label"} <= set(body["monitor"])
    assert set(body["versions"]) == {"messages", "positions", "strategies", "groups"}
    assert body["versions"]["messages"] == "r:0"
    assert body["versions"]["positions"] == "p:none"
    assert body["positions_captured_at"] is None


def test_messages_version_follows_new_raw_message(tmp_path):
    app, clock = _make_app(tmp_path)
    client = TestClient(app)
    session_factory = app.state.session_factory
    before = _versions(client)

    with session_factory() as session:
        session.add(RawMessage(chat_id=1, message_id=1, posted_at=NOW, text="hi"))
        session.commit()
        new_id = session.query(RawMessage.id).scalar()
    clock.value += 5
    after = _versions(client)

    assert after["messages"] == f"r:{new_id}"
    assert after["messages"] != before["messages"]
    assert after["groups"] != before["groups"]


def test_strategies_version_moves_with_lifecycles_but_not_binding_updated_at(tmp_path):
    app, clock = _make_app(tmp_path)
    client = TestClient(app)
    session_factory = app.state.session_factory
    lifecycle_id = _add_lifecycle(session_factory)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="s-1",
            kol_id="77",
            chat_id=77,
            message_id=1,
            symbol="BTCUSDT",
            side="long",
            venue="deepcoin",
            pos_id="pos-1",
            status="open",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(binding)
        session.commit()
        binding_id = binding.id
    clock.value += 5
    baseline = _versions(client)

    # Reconcile rewrites execution_bindings.updated_at on every pass; it must
    # not move any version.
    with session_factory() as session:
        session.get(ExecutionBinding, binding_id).updated_at = NOW + timedelta(
            minutes=5
        )
        session.commit()
    clock.value += 5
    assert _versions(client) == baseline

    # A real change to an in-flight lifecycle does.
    with session_factory() as session:
        session.get(StrategyLifecycle, lifecycle_id).updated_at = NOW + timedelta(
            minutes=5
        )
        session.commit()
    clock.value += 5
    changed = _versions(client)
    assert changed["strategies"] != baseline["strategies"]
    assert changed["messages"] == baseline["messages"]

    # A brand-new lifecycle also does.
    _add_lifecycle(session_factory, status="pending_entry", message_id=2)
    clock.value += 5
    assert _versions(client)["strategies"] != changed["strategies"]


def test_positions_version_ignores_capture_time_but_follows_content(tmp_path):
    app, clock = _make_app(tmp_path)
    client = TestClient(app)
    store = app.state.live_position_snapshot_store

    store.finish_success(_snapshot_payload(), captured_at=NOW)
    clock.value += 5
    first = client.get("/api/live/state").json()
    assert first["versions"]["positions"].startswith("p:")
    assert first["versions"]["positions"] != "p:none"
    assert first["positions_captured_at"] == "2026-09-30T10:00:00+08:00"

    # Same content captured later: the version must not move ...
    store.finish_success(_snapshot_payload(), captured_at=NOW + timedelta(seconds=5))
    clock.value += 5
    second = client.get("/api/live/state").json()
    assert second["positions_captured_at"] == "2026-09-30T10:00:05+08:00"
    assert second["versions"]["positions"] == first["versions"]["positions"]
    assert second["versions"]["strategies"] == first["versions"]["strategies"]

    # ... a changed price does, and strategies (which show positions) follows.
    store.finish_success(
        _snapshot_payload(last_px="60100", upl="14.0"),
        captured_at=NOW + timedelta(seconds=10),
    )
    clock.value += 5
    third = client.get("/api/live/state").json()
    assert third["versions"]["positions"] != first["versions"]["positions"]
    assert third["versions"]["strategies"] != first["versions"]["strategies"]


def test_positions_version_strips_volatile_keys_at_any_depth():
    left = {"a": {"captured_at": "1", "rows": [{"fetched_at": "x", "px": 1}]}}
    right = {"a": {"captured_at": "2", "rows": [{"fetched_at": "y", "px": 1}]}}
    assert positions_version(left) == positions_version(right)
    assert strip_volatile_snapshot_keys(left) == {"a": {"rows": [{"px": 1}]}}
    assert positions_version(None) == "p:none"


def test_groups_version_follows_group_config_file(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text("groups: []\n", encoding="utf-8")
    app, clock = _make_app(
        tmp_path,
        group_config=GroupConfig(groups=[TargetGroupConfig(chat_title="g", chat_id=1)]),
        group_config_path=config_path,
    )
    client = TestClient(app)
    before = _versions(client)

    config_path.write_text("groups: []\n# toggled\n", encoding="utf-8")
    clock.value += 5
    after = _versions(client)

    assert after["groups"] != before["groups"]
    assert after["messages"] == before["messages"]


def test_live_state_is_cached_for_two_seconds_and_recomputed_after(tmp_path):
    app, clock = _make_app(tmp_path)
    client = TestClient(app)
    store = app.state.live_position_snapshot_store
    reads = []
    original_read = store.read
    store.read = lambda: (reads.append(1), original_read())[1]

    for _ in range(6):
        client.get("/api/live/state")
        clock.value += 0.2  # 1.2 s in total, all inside the TTL
    assert len(reads) == 1

    clock.value += 1.5  # past the 2 s TTL
    client.get("/api/live/state")
    assert len(reads) == 2


def test_live_state_cache_is_single_flight_and_does_not_cache_errors():
    clock = FakeClock()
    cache = LiveStateCache(ttl_seconds=2.0, clock=clock)
    calls = []

    def failing():
        calls.append("boom")
        raise RuntimeError("boom")

    for _ in range(2):
        try:
            cache.get(failing)
        except RuntimeError:
            pass
    assert calls == ["boom", "boom"]

    assert cache.get(lambda: "one") == "one"
    assert cache.get(lambda: "two") == "one"
    clock.value += 2.5
    assert cache.get(lambda: "two") == "two"


def _bound_query_plan(session, statement) -> list[str]:
    compiled = statement.compile(
        dialect=sqlite.dialect(), compile_kwargs={"render_postcompile": True}
    )
    params = tuple(compiled.params[name] for name in compiled.positiontup)
    rows = (
        session.connection()
        .exec_driver_sql(f"EXPLAIN QUERY PLAN {compiled}", params)
        .fetchall()
    )
    return [row[-1] for row in rows]


def test_live_state_queries_never_scan_tables_with_bound_parameters(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    statements = [
        ("global_freshness", build_global_freshness_statement()),
        *build_strategy_version_statements(),
    ]
    assert len(statements) >= 6
    with session_factory() as session:
        for label, statement in statements:
            plan = _bound_query_plan(session, statement)
            assert plan, label
            # Any "SCAN <table>" (even "USING INDEX", a full index walk) is
            # refused; SQLite answers MAX(id) with a SEARCH on the rowid.
            scans = [
                step
                for step in plan
                if step.startswith("SCAN ") and step != "SCAN CONSTANT ROW"
            ]
            assert not scans, (label, plan)
        active_plan = _bound_query_plan(
            session, dict(build_strategy_version_statements())["active_lifecycles"]
        )
        assert any(
            # The table carries two equivalent single-column status indexes
            # (ix_strategy_lifecycles_status and the column's own
            # ..._lifecycle_status); SQLite may pick either.
            step.startswith("SEARCH strategy_lifecycles USING INDEX ix_strategy_lifecycles_")
            and "status" in step
            for step in active_plan
        ), active_plan


def test_strategy_list_partial_is_rendered_once_per_version_and_query(tmp_path):
    app, clock = _make_app(tmp_path)
    client = TestClient(app)
    cache = app.state.strategy_list_render_cache
    _add_lifecycle(app.state.session_factory)

    first = client.get("/strategy-records?filter=all")
    second = client.get("/strategy-records?filter=all")
    assert first.status_code == 200
    assert "data-strategy-record-list" in first.text
    assert second.text == first.text
    assert cache.render_count == 1

    # A different query is a different entry.
    client.get("/strategy-records?filter=needs_attention")
    assert cache.render_count == 2
    client.get("/strategy-records?filter=all")
    assert cache.render_count == 2

    # A change in the underlying data re-renders once the version moves.
    _add_lifecycle(app.state.session_factory, status="pending_entry", message_id=2)
    clock.value += 5
    client.get("/strategy-records?filter=all")
    client.get("/strategy-records?filter=all")
    assert cache.render_count == 3


def test_strategy_list_cache_is_bounded_and_keeps_latest_entry_per_query():
    from telegram_kol_research.web_live_state import StrategyListRenderCache

    cache = StrategyListRenderCache(max_entries=3)
    for index in range(5):
        cache.get(("q", index), "v1", lambda index=index: f"body-{index}".encode())
    assert len(cache._entries) == 3
    assert ("q", 0) not in cache._entries and ("q", 4) in cache._entries

    assert cache.get(("q", 4), "v1", lambda: b"unused") == b"body-4"
    assert cache.get(("q", 4), "v2", lambda: b"fresh") == b"fresh"
    assert cache._entries[("q", 4)] == ("v2", b"fresh")
