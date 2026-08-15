from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
import inspect

import pytest

from telegram_kol_research.deepcoin_client import (
    DeepcoinCredentials,
    DeepcoinRestClient,
)

from telegram_kol_research.production_monitor_refresher import (
    DeepcoinMonitorReadProtocol,
    ProductionMonitorRefreshConfigurationError,
    ReadOnlyDeepcoinMonitorClient,
    refresh_production_monitor_snapshot,
)
from telegram_kol_research.production_monitor_snapshot import (
    ProductionMonitorSnapshotStore,
)


NOW = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
UID_HASH = "a" * 64


class RecordingDeepcoinClient:
    def __init__(self, *, responses=None, failures=None, uid_scope_hash=UID_HASH):
        self.calls: list[str] = []
        self.responses = responses or {}
        self.failures = failures or {}
        self.uid_scope_hash = uid_scope_hash

    def _read(self, name: str):
        self.calls.append(name)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure
        return self.responses.get(name, {"data": []})

    def read_positions(self, *, inst_id=None):
        return self._read("read_positions")

    def read_open_orders(self, *, inst_id=None):
        return self._read("read_open_orders")

    def read_trigger_orders_pending(self, *, inst_id):
        return self._read("read_trigger_orders_pending")

    def place_order(self, payload):
        pytest.fail("refresher reached an exchange write method")

    def cancel_order(self, payload):
        pytest.fail("refresher reached an exchange write method")

    def set_position_sltp(self, payload):
        pytest.fail("refresher reached an exchange write method")


class _AdvancingClock:
    def __init__(self, current: float):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds: float):
        self.current += seconds


class _JsonResponse:
    status_code = 200
    headers = {}

    def json(self):
        return {"code": "0", "data": []}

    def iter_raw(self):
        yield b'{"code":"0","data":[]}'


class _TimeoutCapturingHttpClient:
    def __init__(self, clock):
        self.clock = clock
        self.timeouts: list[float] = []

    def request(self, method, request_path, content="", headers=None, timeout=None):
        self.timeouts.append(timeout)
        self.clock.advance(1.0)
        return _JsonResponse()

    @contextmanager
    def stream(self, method, request_path, content="", headers=None, timeout=None):
        self.timeouts.append(timeout)
        self.clock.advance(1.0)
        yield _JsonResponse()


class _OversizedWireResponse:
    status_code = 200
    headers = {}

    def __init__(self):
        self.yielded = 0

    def iter_raw(self):
        for chunk in (b'{"code":"0","data":[', b"x" * (1024 * 1024), b"]}"):
            self.yielded += 1
            yield chunk


class _OversizedStreamingHttpClient:
    def __init__(self):
        self.response = _OversizedWireResponse()

    def request(self, *args, **kwargs):
        raise AssertionError("monitor refresher must use bounded streaming HTTP")

    @contextmanager
    def stream(self, *args, **kwargs):
        yield self.response


def _store(path):
    return ProductionMonitorSnapshotStore(path, now_factory=lambda: NOW)


def _refresh(client, store, **kwargs):
    return refresh_production_monitor_snapshot(
        client=ReadOnlyDeepcoinMonitorClient(client),
        store=store,
        now=NOW,
        **kwargs,
    )


def test_refresher_has_no_exchange_mutation_surface(tmp_path):
    client = RecordingDeepcoinClient()

    outcome = _refresh(client, _store(tmp_path / "snapshot.json"))

    assert outcome.execution_status == "COMPLETED"
    assert outcome.snapshot_outcome == "SUCCESS"
    assert client.calls == [
        "read_positions",
        "read_open_orders",
        "read_trigger_orders_pending",
    ]
    assert {
        name
        for name, value in DeepcoinMonitorReadProtocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    } == {
        "read_positions",
        "read_open_orders",
        "read_trigger_orders_pending",
    }


def test_refresher_module_contains_no_exchange_write_names_or_paths():
    import telegram_kol_research.production_monitor_refresher as module

    source = inspect.getsource(module)
    for prohibited in (
        "place_order",
        "cancel_order",
        "cancel_trigger_order",
        "set_position_sltp",
        "replace_order_sltp",
        "/deepcoin/trade/order",
        "/deepcoin/trade/cancel-order",
    ):
        assert prohibited not in source


def test_complete_empty_collections_seal_success(tmp_path):
    store = _store(tmp_path / "snapshot.json")

    outcome = _refresh(RecordingDeepcoinClient(), store)

    assert outcome.snapshot_outcome == "SUCCESS"
    loaded = store.load()
    assert loaded.last_success is not None
    assert [item.row_count for item in loaded.last_success.collections] == [0, 0, 0]
    assert all(item.complete for item in loaded.last_success.collections)


def test_failed_empty_read_seals_failure_without_refreshing_last_success(tmp_path):
    store = _store(tmp_path / "snapshot.json")
    _refresh(RecordingDeepcoinClient(), store)
    previous_success = store.load().last_success
    client = RecordingDeepcoinClient(
        failures={"read_open_orders": TimeoutError("credential=secret")}
    )

    outcome = _refresh(client, store)

    assert outcome.execution_status == "COMPLETED"
    assert outcome.snapshot_outcome == "FAILURE"
    assert outcome.failure_code == "exchange_timeout"
    loaded = store.load()
    assert loaded.last_success == previous_success
    assert loaded.latest_attempt is not None
    assert loaded.latest_attempt.generation == 1
    assert loaded.latest_attempt.failure_code == "exchange_timeout"
    assert len(loaded.latest_attempt.collections) == 1
    failed_collection = loaded.latest_attempt.collections[0]
    assert failed_collection.name == "open_orders"
    assert failed_collection.available is False
    assert failed_collection.schema_valid is False
    assert failed_collection.complete is False
    assert failed_collection.row_count == 0
    assert failed_collection.rows == ()
    assert failed_collection.reason_code == "exchange_timeout"
    assert "secret" not in store.path.read_text(encoding="utf-8")


def test_refresher_projects_only_canonical_bounded_monitor_fields(tmp_path):
    client = RecordingDeepcoinClient(
        responses={
            "read_positions": {
                "data": [{
                    "positionId": " pos-1 ",
                    "instrumentId": "BTC-USDT-SWAP",
                    "positionSide": "long",
                    "positionSize": "2",
                    "markPrice": "70000",
                    "apiSecret": "must-not-persist",
                    "arbitraryExchangeBlob": {"nested": "ignored"},
                }]
            },
            "read_open_orders": {
                "data": [{
                    "orderId": " order-1 ",
                    "instrumentId": "BTC-USDT-SWAP",
                    "positionId": "pos-1",
                    "state": "live",
                    "size": "2",
                    "authorization": "must-not-persist",
                }]
            },
            "read_trigger_orders_pending": {
                "data": [{
                    "algoId": " trigger-1 ",
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "triggerPx": "68000",
                    "unknown": "ignored",
                }]
            },
        }
    )
    store = _store(tmp_path / "snapshot.json")

    outcome = _refresh(client, store)

    assert outcome.snapshot_outcome == "SUCCESS"
    loaded = store.load().last_success
    assert loaded is not None
    positions, orders, triggers = loaded.collections
    assert dict(positions.rows[0]) == {
        "instId": "BTC-USDT-SWAP",
        "markPx": "70000",
        "pos": "2",
        "posId": "pos-1",
        "posSide": "long",
    }
    assert dict(orders.rows[0]) == {
        "instId": "BTC-USDT-SWAP",
        "ordId": "order-1",
        "posId": "pos-1",
        "state": "live",
        "sz": "2",
    }
    assert dict(triggers.rows[0]) == {
        "instId": "BTC-USDT-SWAP",
        "ordId": "trigger-1",
        "posId": "pos-1",
        "triggerPx": "68000",
    }
    persisted = store.path.read_text(encoding="utf-8")
    assert "must-not-persist" not in persisted
    assert "arbitraryExchangeBlob" not in persisted
    assert "unknown" not in persisted


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ({"data": [], "hasMore": True}, "snapshot_pagination_incomplete"),
        ({"data": [{}]}, "snapshot_schema_invalid"),
        ({"data": [{"posId": "p"}] * 100}, "snapshot_page_limit_ambiguous"),
    ],
)
def test_refresher_fails_closed_on_incomplete_schema_or_page_limit(
    tmp_path, response, expected_code
):
    client = RecordingDeepcoinClient(responses={"read_positions": response})
    store = _store(tmp_path / "snapshot.json")

    outcome = _refresh(client, store)

    assert outcome.execution_status == "COMPLETED"
    assert outcome.snapshot_outcome == "FAILURE"
    assert outcome.failure_code == expected_code
    loaded = store.load()
    assert loaded.last_success is None
    if expected_code == "snapshot_pagination_incomplete":
        assert loaded.latest_attempt.collections[0].schema_valid is True


def test_refresher_validates_scope_and_advances_attempt_generation(tmp_path):
    store = _store(tmp_path / "snapshot.json")
    first = _refresh(RecordingDeepcoinClient(), store)
    second = _refresh(RecordingDeepcoinClient(), store)

    assert (first.generation, second.generation) == (0, 1)

    mismatched = RecordingDeepcoinClient(uid_scope_hash="b" * 64)
    with pytest.raises(
        ProductionMonitorRefreshConfigurationError,
        match="account scope mismatch",
    ):
        _refresh(mismatched, store)
    assert mismatched.calls == []
    assert store.load().latest_attempt.generation == 1


def test_refresher_wall_clock_timeout_seals_failure_and_stops_more_reads(tmp_path):
    ticks = iter((0.0, 0.1, 1.1, 1.2))
    client = RecordingDeepcoinClient()

    outcome = _refresh(
        client,
        _store(tmp_path / "snapshot.json"),
        wall_clock_timeout_seconds=1.0,
        monotonic_factory=lambda: next(ticks),
    )

    assert outcome.execution_status == "COMPLETED"
    assert outcome.snapshot_outcome == "FAILURE"
    assert outcome.failure_code == "wall_clock_timeout"
    assert client.calls == ["read_positions"]


def test_refresher_overlap_is_sealed_without_any_exchange_read(tmp_path):
    path = tmp_path / "snapshot.json"
    owner_store = _store(path)
    overlapping_store = _store(path)
    client = RecordingDeepcoinClient()

    with owner_store.try_refresh_lease(
        uid_scope_hash=UID_HASH,
        observed_at=NOW,
    ) as owner_lease:
        assert owner_lease.acquired is True

        outcome = _refresh(client, overlapping_store)

        assert outcome.execution_status == "COMPLETED"
        assert outcome.snapshot_outcome == "FAILURE"
        assert outcome.failure_code == "refresh_overlap"
        assert outcome.generation == 0
        assert client.calls == []

    sealed = owner_store.load()
    assert sealed.latest_attempt is not None
    assert sealed.latest_attempt.failure_code == "refresh_overlap"
    assert sealed.last_success is None


def test_refresher_injects_one_absolute_deadline_into_each_http_read(tmp_path):
    clock = _AdvancingClock(100.0)
    http_client = _TimeoutCapturingHttpClient(clock)
    transport = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="monitor-key",
            api_secret="secret",
            passphrase="pass",
            timeout_seconds=15.0,
        ),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=lambda seconds: clock.advance(seconds),
        read_only=True,
    )

    outcome = refresh_production_monitor_snapshot(
        client=ReadOnlyDeepcoinMonitorClient(transport),
        store=_store(tmp_path / "snapshot.json"),
        now=NOW,
        wall_clock_timeout_seconds=3.0,
        monotonic_factory=clock,
    )

    assert outcome.snapshot_outcome == "SUCCESS"
    assert http_client.timeouts == pytest.approx([3.0, 2.0, 1.0])


def test_refresher_seals_oversized_wire_response_as_snapshot_size_exceeded(tmp_path):
    http_client = _OversizedStreamingHttpClient()
    transport = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="monitor-key",
            api_secret="secret",
            passphrase="pass",
        ),
        http_client=http_client,
        read_only=True,
    )

    outcome = refresh_production_monitor_snapshot(
        client=ReadOnlyDeepcoinMonitorClient(transport),
        store=_store(tmp_path / "snapshot.json"),
        now=NOW,
    )

    assert outcome.execution_status == "COMPLETED"
    assert outcome.snapshot_outcome == "FAILURE"
    assert outcome.failure_code == "snapshot_size_exceeded"
    assert http_client.response.yielded == 2
