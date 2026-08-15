import base64
from contextlib import contextmanager
import hashlib
import hmac
import json

import httpx
import pytest

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinCredentials
from telegram_kol_research.deepcoin_client import DeepcoinRestClient
from telegram_kol_research.deepcoin_client import DeepcoinReadUnavailable
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.deepcoin_client import DeepcoinRequestScope
from telegram_kol_research.deepcoin_client import DeepcoinTpslWriteLimiter
from telegram_kol_research.deepcoin_client import build_deepcoin_auth_headers
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_client import load_deepcoin_credentials
from telegram_kol_research.deepcoin_client import _raise_for_deepcoin_business_error
from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinRequestGovernor,
    GovernorMode,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []
        self.close_calls = 0

    def request(self, method, request_path, content="", headers=None, timeout=None):
        self.requests.append(
            {
                "method": method,
                "request_path": request_path,
                "content": content,
                "headers": headers or {},
            }
        )
        return _FakeResponse(self.payload)

    def close(self):
        self.close_calls += 1


class _FailingHttpClient:
    def request(self, method, request_path, content="", headers=None, timeout=None):
        request = httpx.Request(method, f"https://api.deepcoin.test{request_path}")
        raise httpx.ReadTimeout("lost response", request=request)


class _HttpStatusClient:
    def __init__(self, status_code):
        self.status_code = status_code

    def request(self, method, request_path, content="", headers=None, timeout=None):
        request = httpx.Request(method, f"https://api.deepcoin.test{request_path}")
        return httpx.Response(
            self.status_code,
            request=request,
            json={"code": str(self.status_code), "msg": "server error"},
        )


class _ChunkedWireResponse:
    status_code = 200
    headers = {}

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.yielded = 0
        self.json_calls = 0

    def iter_raw(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def json(self):
        self.json_calls += 1
        raise AssertionError("bounded monitor response must not call response.json")


class _MonitorStreamingHttpClient:
    def __init__(self, response):
        self.response = response
        self.request_calls = 0
        self.stream_calls = []

    def request(self, *args, **kwargs):
        self.request_calls += 1
        raise AssertionError("bounded monitor response must use streaming HTTP")

    @contextmanager
    def stream(self, method, request_path, content="", headers=None, timeout=None):
        self.stream_calls.append(
            {
                "method": method,
                "request_path": request_path,
                "headers": headers,
                "timeout": timeout,
            }
        )
        yield self.response


class _FakeMonotonicClock:
    def __init__(self, current: float = 100.0):
        self.current = current
        self.sleeps = []

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds

    def advance(self, seconds):
        self.current += seconds


def test_read_only_deepcoin_client_refuses_write_before_http():
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=http_client,
        read_only=True,
    )

    with pytest.raises(DeepcoinClientError, match="read-only"):
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert http_client.requests == []
    assert client.read_positions(inst_id="BTC-USDT-SWAP") == {
        "code": "0",
        "data": [],
    }
    assert [request["method"] for request in http_client.requests] == ["GET"]


def test_monitor_scope_stops_reading_wire_bytes_before_json_decode():
    response = _ChunkedWireResponse(
        [
            b'{"code":"0","data":[',
            b'{"posId":"' + b"x" * 48,
            b'"}]}',
        ]
    )
    http_client = _MonitorStreamingHttpClient(response)
    clock = _FakeMonotonicClock(100.0)
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
        ),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        read_only=True,
    )

    with client.request_scope(
        DeepcoinRequestScope(
            phase="production_monitor_snapshot",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=105.0,
            max_response_bytes=64,
        )
    ):
        with pytest.raises(DeepcoinReadUnavailable) as captured:
            client.read_positions()

    assert captured.value.fact.safe_code == "monitor_response_size_exceeded"
    assert response.yielded == 2
    assert response.json_calls == 0
    assert http_client.request_calls == 0
    assert len(http_client.stream_calls) == 1


def test_monitor_response_limit_cannot_be_used_for_exchange_write():
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"ordId": "unexpected"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    with client.request_scope(
        DeepcoinRequestScope(
            phase="production_monitor_snapshot",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=None,
            max_response_bytes=64,
        )
    ):
        with pytest.raises(DeepcoinClientError, match="only supports GET"):
            client.place_order({"instId": "BTC-USDT-SWAP"})

    assert http_client.requests == []


def test_build_deepcoin_auth_headers_signs_timestamp_method_path_and_body():
    credentials = DeepcoinCredentials(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
    )
    body = '{"instId":"BTC-USDT-SWAP"}'

    headers = build_deepcoin_auth_headers(
        credentials=credentials,
        timestamp="2026-06-29T11:00:00.000Z",
        method="POST",
        request_path="/deepcoin/trade/order",
        body=body,
    )

    expected = base64.b64encode(
        hmac.new(
            b"secret",
            b"2026-06-29T11:00:00.000ZPOST/deepcoin/trade/order"
            + body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    assert headers["DC-ACCESS-KEY"] == "key"
    assert headers["DC-ACCESS-SIGN"] == expected
    assert headers["DC-ACCESS-PASSPHRASE"] == "pass"


def test_load_deepcoin_credentials_requires_all_secrets():
    try:
        load_deepcoin_credentials(environ={}, env_file_paths=[])
    except DeepcoinClientError as exc:
        assert "DEEPCOIN_API_KEY" in str(exc)
        assert "DEEPCOIN_API_SECRET" in str(exc)
        assert "DEEPCOIN_API_PASSPHRASE" in str(exc)
    else:
        raise AssertionError("expected missing credentials to fail")


def test_load_deepcoin_credentials_reads_env_values():
    credentials = load_deepcoin_credentials(
        environ={
            "DEEPCOIN_API_KEY": "key",
            "DEEPCOIN_API_SECRET": "secret",
            "DEEPCOIN_API_PASSPHRASE": "pass",
            "DEEPCOIN_BASE_URL": "https://example.test/",
        },
        env_file_paths=[],
    )

    assert credentials.api_key == "key"
    assert credentials.api_secret == "secret"
    assert credentials.passphrase == "pass"
    assert credentials.base_url == "https://example.test"


def test_request_governor_mode_is_wired_only_from_environment(tmp_path):
    state_directory = tmp_path / "governor"
    state_directory.mkdir(mode=0o700)
    state_directory.chmod(0o700)
    client = build_deepcoin_client_from_env(
        environ={
            "DEEPCOIN_API_KEY": "key",
            "DEEPCOIN_API_SECRET": "secret",
            "DEEPCOIN_API_PASSPHRASE": "pass",
            "DEEPCOIN_BASE_URL": "https://api.deepcoin.test",
            "DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_reads",
            "DEEPCOIN_GOVERNOR_STATE_DIR": str(state_directory.resolve()),
        },
        env_file_paths=[],
    )

    assert client._request_governor is not None
    assert client._request_governor.mode == GovernorMode.ENFORCE_READS
    assert client._request_governor._state_directory == state_directory.resolve()


@pytest.mark.parametrize(
    "governor_environment",
    [
        {},
        {"DEEPCOIN_REQUEST_GOVERNOR_MODE": "invalid"},
        {"DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_all"},
    ],
)
def test_request_governor_mode_missing_or_invalid_stays_disabled(
    tmp_path,
    governor_environment,
):
    environment = {
        "DEEPCOIN_API_KEY": "key",
        "DEEPCOIN_API_SECRET": "secret",
        "DEEPCOIN_API_PASSPHRASE": "pass",
        **governor_environment,
    }

    client = build_deepcoin_client_from_env(
        environ=environment,
        env_file_paths=[],
    )

    assert client._request_governor is None


def test_client_order_timeout_preserves_unknown_write_outcome():
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=_FailingHttpClient(),
        timestamp_factory=lambda: "2026-07-15T09:00:00.000Z",
    )

    with pytest.raises(DeepcoinRequestOutcomeUnknown, match="outcome unknown"):
        client.place_order(
            {
                "instId": "BTC-USDT-SWAP",
                "ordType": "market",
                "closePosId": "pos-1",
                "clOrdId": "TM123",
                "sz": "1",
            }
        )


def test_client_order_http_500_preserves_unknown_write_outcome():
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key", api_secret="secret", passphrase="pass"
        ),
        http_client=_HttpStatusClient(500),
        timestamp_factory=lambda: "2026-07-15T09:00:00.000Z",
    )

    with pytest.raises(DeepcoinRequestOutcomeUnknown, match="outcome unknown"):
        client.place_order({"instId": "BTC-USDT-SWAP", "clOrdId": "TM123"})


def test_deepcoin_business_error_checks_nested_scode():
    try:
        _raise_for_deepcoin_business_error(
            {"code": "0", "data": {"sCode": "36", "sMsg": "InsufficientMoney"}}
        )
    except DeepcoinClientError as exc:
        assert "36" in str(exc)
        assert "InsufficientMoney" in str(exc)
    else:
        raise AssertionError("expected nested sCode failure")


def test_pending_trigger_read_exposes_raw_response_for_completeness_audit():
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"ordId": "tp-1"}], "nextCursor": "next-page"}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-07-15T09:00:00.000Z",
    )

    response = client.read_trigger_orders_pending(inst_id="BTC-USDT-SWAP")

    assert response == {
        "code": "0",
        "data": [{"ordId": "tp-1"}],
        "nextCursor": "next-page",
    }
    assert http_client.requests[0]["request_path"].startswith(
        "/deepcoin/trade/trigger-orders-pending?"
    )
    assert "limit=100" in http_client.requests[0]["request_path"]


def test_cancel_position_sltp_uses_official_contract_and_shared_write_limiter():
    clock = _FakeMonotonicClock(0.0)
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"ordId": "tp-1", "sCode": "0"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-07-15T09:00:00.000Z",
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    for index in range(16):
        if index % 2:
            client.cancel_position_sltp(
                {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "ordId": f"tp-{index}"}
            )
        else:
            client.set_position_sltp(
                {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-1", "slTriggerPx": "64000"}
            )

    cancel = http_client.requests[1]
    assert cancel["request_path"] == "/deepcoin/trade/cancel-position-sltp"
    assert json.loads(cancel["content"]) == {
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "ordId": "tp-1"
    }
    assert clock.sleeps == [pytest.approx(1.0)]


def test_cancel_position_sltp_rejects_nested_business_error():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {"code": "0", "data": [{"ordId": "tp-1", "sCode": "51000", "sMsg": "rejected"}]}
        ),
    )
    with pytest.raises(DeepcoinClientError, match="51000"):
        client.cancel_position_sltp(
            {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "ordId": "tp-1"}
        )


def test_unchecked_position_write_adapter_methods_use_exact_endpoints():
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"ordId": "write-1", "sCode": "0"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    client._set_position_sltp_unchecked(
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "slTriggerPx": "62000",
        }
    )
    client._cancel_position_sltp_unchecked(
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "ordId": "stop-1",
        }
    )
    client._place_position_close_unchecked(
        {
            "instId": "BTC-USDT-SWAP",
            "closePosId": "pos-1",
            "ordType": "market",
            "sz": "1",
        }
    )

    assert [row["request_path"] for row in http_client.requests] == [
        "/deepcoin/trade/set-position-sltp",
        "/deepcoin/trade/cancel-position-sltp",
        "/deepcoin/trade/order",
    ]


def test_two_clients_share_one_injected_tpsl_credential_limiter():
    clock = _FakeMonotonicClock(0.0)
    limiter = DeepcoinTpslWriteLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep
    )
    credentials = DeepcoinCredentials(api_key="uid-key", api_secret="secret", passphrase="pass")
    clients = [
        DeepcoinRestClient(
            credentials,
            http_client=_CapturingHttpClient({"code": "0", "data": [{"ordId": "x", "sCode": "0"}]}),
            tpsl_rate_limiter=limiter,
        )
        for _ in range(2)
    ]
    for index in range(16):
        clients[index % 2].set_position_sltp(
            {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "slTriggerPx": "1"}
        )
    assert clock.sleeps == [pytest.approx(1.0)]


def test_two_default_clients_share_process_limiter_by_credential_uid():
    credentials = DeepcoinCredentials(
        api_key="unique-default-uid", api_secret="secret", passphrase="pass",
        base_url="https://api.deepcoin.test",
    )
    first = DeepcoinRestClient(credentials)
    second = DeepcoinRestClient(credentials)
    assert first._tpsl_rate_limiter is second._tpsl_rate_limiter


def test_deepcoin_client_reuses_owned_http_connection_and_closes_once(monkeypatch):
    created_clients = []

    def build_http_client(*, base_url, timeout):
        client = _CapturingHttpClient({"code": "0", "data": []})
        client.base_url = base_url
        client.timeout = timeout
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_client.httpx.Client",
        build_http_client,
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
            timeout_seconds=7,
        )
    )

    with client:
        client.list_positions()
        client.list_open_orders()
    client.close()

    assert len(created_clients) == 1
    assert len(created_clients[0].requests) == 2
    assert created_clients[0].close_calls == 1


def test_deepcoin_client_reuses_owned_connection_until_explicit_close(
    monkeypatch,
):
    created_clients = []

    def build_http_client(*, base_url, timeout):
        client = _CapturingHttpClient({"code": "0", "data": []})
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_client.httpx.Client",
        build_http_client,
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass")
    )

    client.list_positions()
    client.list_open_orders()
    client.close()

    assert len(created_clients) == 1
    assert created_clients[0].close_calls == 1


def test_deepcoin_client_does_not_close_injected_http_client():
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    client.list_positions()
    client.close()

    assert http_client.close_calls == 0


def test_tpsl_limiter_enforces_450_per_minute_with_fake_clock():
    clock = _FakeMonotonicClock(0.0)
    limiter = DeepcoinTpslWriteLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep,
        per_second=10_000, per_minute=450,
    )
    for _ in range(451):
        limiter.acquire()
    assert clock.sleeps == [pytest.approx(60.0)]


def test_deepcoin_client_lists_order_and_trigger_history_with_swap_query():
    http_client = _CapturingHttpClient(
        {
            "code": "0",
            "data": [
                {"ordId": "order-1", "clOrdId": "client-1"},
            ],
        }
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-06-30T00:00:00.000Z",
    )

    order_history = client.list_order_history(inst_id="ETH-USDT-SWAP")
    trigger_history = client.list_trigger_order_history(inst_id="ETH-USDT-SWAP")
    open_orders = client.list_open_orders(inst_id="ETH-USDT-SWAP")
    trade_fills = client.list_trade_fills(inst_id="ETH-USDT-SWAP")

    assert order_history == [{"ordId": "order-1", "clOrdId": "client-1"}]
    assert trigger_history == [{"ordId": "order-1", "clOrdId": "client-1"}]
    assert open_orders == [{"ordId": "order-1", "clOrdId": "client-1"}]
    assert trade_fills == [{"ordId": "order-1", "clOrdId": "client-1"}]
    assert [
        request["request_path"]
        for request in http_client.requests
    ] == [
        "/deepcoin/trade/orders-history?instType=SWAP&instId=ETH-USDT-SWAP",
        "/deepcoin/trade/trigger-orders-history?instType=SWAP&instId=ETH-USDT-SWAP",
        "/deepcoin/trade/orders-pending?instType=SWAP&instId=ETH-USDT-SWAP",
        "/deepcoin/trade/fills?instType=SWAP&instId=ETH-USDT-SWAP",
    ]


def test_exact_history_readers_send_ord_id_and_bounded_limit():
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    client.read_order_history(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )
    client.read_trade_fills(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )
    client.read_trigger_order_history(
        inst_id="BTC-USDT-SWAP", order_id="owned-stop", limit=100
    )

    paths = [request["request_path"] for request in http_client.requests]
    assert all("ordId=owned-stop" in path for path in paths)
    assert all("limit=100" in path for path in paths)


def test_exact_history_readers_reject_invalid_identity_before_http():
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )
    readers = (
        client.read_order_history,
        client.read_trade_fills,
        client.read_trigger_order_history,
    )

    for reader in readers:
        for invalid_order_id in ("   ", "owned/stop"):
            with pytest.raises(DeepcoinClientError, match="order id"):
                reader(
                    inst_id="BTC-USDT-SWAP",
                    order_id=invalid_order_id,
                    limit=100,
                )
        for invalid_limit in (0, 101, True, "100"):
            with pytest.raises(DeepcoinClientError, match="limit"):
                reader(
                    inst_id="BTC-USDT-SWAP",
                    order_id="owned-stop",
                    limit=invalid_limit,
                )

    assert http_client.requests == []


def test_exact_history_readers_preserve_default_urls_without_filters():
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    client.read_order_history(inst_id="ETH-USDT-SWAP")
    client.read_trade_fills(inst_id="ETH-USDT-SWAP")
    client.read_trigger_order_history(inst_id="ETH-USDT-SWAP")

    assert [request["request_path"] for request in http_client.requests] == [
        "/deepcoin/trade/orders-history?instType=SWAP&instId=ETH-USDT-SWAP",
        "/deepcoin/trade/fills?instType=SWAP&instId=ETH-USDT-SWAP",
        "/deepcoin/trade/trigger-orders-history?instType=SWAP&instId=ETH-USDT-SWAP",
    ]


def test_raw_collection_reader_preserves_pagination_metadata_and_uid_scope_hash():
    payload = {
        "code": "0",
        "data": [],
        "nextPageCursor": "page-2",
    }
    credentials = DeepcoinCredentials(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
    )
    client = DeepcoinRestClient(
        credentials,
        http_client=_CapturingHttpClient(payload),
        timestamp_factory=lambda: "2026-06-30T00:00:00.000Z",
    )

    assert client.read_open_orders(inst_id="ETH-USDT-SWAP") == payload
    assert client.uid_scope_hash == hashlib.sha256(
        f"{credentials.base_url.rstrip('/')}\0{credentials.api_key}".encode()
    ).hexdigest()
    assert credentials.api_key not in client.uid_scope_hash


def test_list_position_history_queries_exact_split_position():
    http_client = _CapturingHttpClient(
        {
            "code": "0",
            "data": [{"posId": "position-1"}],
        }
    )
    credentials = DeepcoinCredentials(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
    )
    timestamp = "2026-06-30T00:00:00.000Z"
    client = DeepcoinRestClient(
        credentials,
        http_client=http_client,
        timestamp_factory=lambda: timestamp,
        position_history_min_interval_seconds=0.0,
    )

    rows = client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )

    request_path = (
        "/deepcoin/account/positions-history?instType=SWAP"
        "&instId=BTC-USDT-SWAP&mrgPosition=split&posId=position-1&limit=100"
    )
    assert rows == [{"posId": "position-1"}]
    assert http_client.requests == [
        {
            "method": "GET",
            "request_path": request_path,
            "content": "",
            "headers": {
                **build_deepcoin_auth_headers(
                    credentials=credentials,
                    timestamp=timestamp,
                    method="GET",
                    request_path=request_path,
                    body="",
                ),
                "Content-Type": "application/json",
            },
        }
    ]

    http_client.payload = {"code": "0", "data": {"posId": "position-1"}}
    with pytest.raises(DeepcoinClientError, match="schema_incompatible"):
        client.list_position_history(
            inst_id="BTC-USDT-SWAP",
            pos_id="position-1",
        )


def test_list_position_history_waits_only_for_remaining_endpoint_interval():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"posId": "position-1"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )
    assert clock.sleeps == []

    clock.advance(0.25)
    http_client.payload = {"code": "0", "data": [{"posId": "position-2"}]}
    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-2",
    )

    assert clock.sleeps == pytest.approx([0.80])
    assert len(http_client.requests) == 2


def test_list_position_history_does_not_sleep_after_full_interval_elapsed():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"posId": "position-1"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )
    clock.advance(1.10)
    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )

    assert clock.sleeps == []


def test_position_history_pacing_does_not_delay_other_endpoints():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_positions()
    client.list_positions()

    assert clock.sleeps == []


def test_deepcoin_list_endpoint_rejects_non_list_data():
    http_client = _CapturingHttpClient({"code": "0", "data": {"unexpected": []}})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    with pytest.raises(DeepcoinClientError, match="schema_incompatible"):
        client.list_positions()


def test_deepcoin_client_lists_swap_symbols_from_market_tickers():
    http_client = _CapturingHttpClient(
        {
            "code": "0",
            "data": [
                {"instId": "ETH-USDT-SWAP"},
                {"instId": "BTC-USDT-SWAP"},
                {"instId": "ETH-USDT-SWAP"},
                {"instId": "BTC-USDT-SPOT"},
                {"instId": ""},
            ],
        }
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-06-30T00:00:00.000Z",
    )

    symbols = client.list_swap_symbols()

    assert symbols == [
        {"symbol": "BTC", "instrument_id": "BTC-USDT-SWAP"},
        {"symbol": "ETH", "instrument_id": "ETH-USDT-SWAP"},
    ]
    assert http_client.requests[-1]["request_path"] == "/deepcoin/market/tickers?instType=SWAP"


def test_deepcoin_client_lists_swap_instruments_with_validator_fields_untouched():
    instrument = {
        "instType": "SWAP",
        "instId": "SOL-USDT-SWAP",
        "ctVal": "1",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.001",
        "state": "live",
    }
    http_client = _CapturingHttpClient({"code": "0", "data": [instrument]})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-08-08T00:00:00.000Z",
    )

    instruments = client.list_swap_instruments()

    assert instruments == [instrument]
    assert http_client.requests[-1]["request_path"] == (
        "/deepcoin/market/instruments?instType=SWAP"
    )


def test_deepcoin_client_list_swap_instruments_rejects_non_list_data():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {"code": "0", "data": {"unexpected": []}}
        ),
    )

    with pytest.raises(DeepcoinClientError, match="schema_incompatible"):
        client.list_swap_instruments()


def test_deepcoin_client_list_swap_instruments_rejects_api_error():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {"code": "50011", "msg": "instrument service unavailable", "data": []}
        ),
    )

    with pytest.raises(
        DeepcoinClientError,
        match="Deepcoin request rejected: 50011",
    ):
        client.list_swap_instruments()


def test_get_ticker_quote_returns_structured_last_price_evidence():
    http_client = _CapturingHttpClient(
        {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "last": "64688.6",
                    "lastPx": "64687.1",
                    "ts": "1785663259000",
                },
            ],
        }
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    quote = client.get_ticker_quote(inst_id="BTC-USDT-SWAP")
    assert quote is not None
    assert quote.pop("observed_at") == "2026-08-02T09:34:19+00:00"
    assert quote == {
        "instrument_id": "BTC-USDT-SWAP",
        "price": "64688.6",
        "price_field": "last",
    }
    assert client.get_ticker_price(inst_id="BTC-USDT-SWAP") == 64688.6


def test_get_ticker_quote_falls_back_to_last_px_when_last_is_missing():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "lastPx": "64688.7",
                        "markPx": "64680",
                        "ts": "1785663259000",
                    },
                ],
            }
        ),
    )

    quote = client.get_ticker_quote(inst_id="BTC-USDT-SWAP")
    assert quote is not None
    assert quote.pop("observed_at") == "2026-08-02T09:34:19+00:00"
    assert quote == {
        "instrument_id": "BTC-USDT-SWAP",
        "price": "64688.7",
        "price_field": "lastPx",
    }


def test_get_ticker_quote_rejects_duplicate_target_instruments():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {
                "code": "0",
                "data": [
                    {"instId": "BTC-USDT-SWAP", "last": "64688.6"},
                    {"instId": "btc-usdt-swap", "last": "64688.7"},
                ],
            }
        ),
    )

    with pytest.raises(DeepcoinClientError, match="duplicate ticker rows"):
        client.get_ticker_quote(inst_id="BTC-USDT-SWAP")


def test_get_ticker_quote_rejects_missing_exchange_timestamp():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient({
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "last": "64688.6"}],
        }),
    )

    with pytest.raises(DeepcoinClientError, match="ticker timestamp missing"):
        client.get_ticker_quote(inst_id="BTC-USDT-SWAP")


@pytest.mark.parametrize(
    ("ticker", "error"),
    [
        ({"markPx": "64688.6"}, "ticker price missing"),
        ({"last": "not-a-price"}, "invalid ticker last"),
        ({"last": "0"}, "invalid ticker last"),
        ({"last": "-1"}, "invalid ticker last"),
        ({"last": "NaN"}, "invalid ticker last"),
        ({"last": "Infinity"}, "invalid ticker last"),
        ({"lastPx": "NaN"}, "invalid ticker lastPx"),
        ({"last": "not-a-price", "lastPx": "64688.6"}, "invalid ticker last"),
    ],
)
def test_get_ticker_quote_rejects_missing_or_unsafe_price_evidence(ticker, error):
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {
                "code": "0",
                "data": [{"instId": "BTC-USDT-SWAP", **ticker}],
            }
        ),
    )

    with pytest.raises(DeepcoinClientError, match=error):
        client.get_ticker_quote(inst_id="BTC-USDT-SWAP")


def test_get_ticker_quote_returns_none_when_target_instrument_is_absent():
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {
                "code": "0",
                "data": [{"instId": "ETH-USDT-SWAP", "last": "3500"}],
            }
        ),
    )

    assert client.get_ticker_quote(inst_id="BTC-USDT-SWAP") is None
    assert client.get_ticker_price(inst_id="BTC-USDT-SWAP") is None


class _RecordingRequestGovernor:
    def __init__(self, *, enforces_requests=True):
        self.calls = []
        self.enforces_requests = enforces_requests

    def enforces(self, method):
        return self.enforces_requests

    def acquire(self, *, method, request_path, priority, deadline_monotonic):
        self.calls.append(
            {
                "method": method,
                "request_path": request_path,
                "priority": priority,
                "deadline_monotonic": deadline_monotonic,
            }
        )


class _ForbiddenLegacyLimiter:
    def acquire(self):
        raise AssertionError("legacy TPSL limiter must not double-charge")


class _RecordingLegacyLimiter:
    def __init__(self):
        self.acquire_calls = 0

    def acquire(self):
        self.acquire_calls += 1


def test_injected_governor_receives_every_get_request():
    governor = _RecordingRequestGovernor()
    clock = _FakeMonotonicClock()
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient({"code": "0", "data": []}),
        request_governor=governor,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    client.list_positions(inst_id="BTC-USDT-SWAP")

    assert governor.calls == [
        {
            "method": "GET",
            "request_path": (
                "/deepcoin/account/positions?instType=SWAP&instId=BTC-USDT-SWAP"
            ),
            "priority": "normal",
            "deadline_monotonic": 110.0,
        }
    ]


def test_governed_tpsl_writer_is_charged_once_without_legacy_limiter():
    governor = _RecordingRequestGovernor()
    clock = _FakeMonotonicClock()
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {"code": "0", "data": [{"ordId": "stop-1", "sCode": "0"}]}
        ),
        request_governor=governor,
        tpsl_rate_limiter=_ForbiddenLegacyLimiter(),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    client.set_position_sltp(
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "slTriggerPx": "62000",
        }
    )

    assert governor.calls == [
        {
            "method": "POST",
            "request_path": "/deepcoin/trade/set-position-sltp",
            "priority": "normal",
            "deadline_monotonic": 110.0,
        }
    ]


@pytest.mark.parametrize(
    "mode",
    [
        GovernorMode.DISABLED,
        GovernorMode.TELEMETRY,
        GovernorMode.ENFORCE_READS,
    ],
)
def test_non_post_enforcing_governor_keeps_legacy_tpsl_limiter(tmp_path, mode):
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key=f"uid-{mode}",
        mode=mode,
        state_directory=tmp_path,
    )
    legacy_limiter = _RecordingLegacyLimiter()
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=_CapturingHttpClient(
            {"code": "0", "data": [{"ordId": "stop-1", "sCode": "0"}]}
        ),
        request_governor=governor,
        tpsl_rate_limiter=legacy_limiter,
    )

    client.set_position_sltp(
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "slTriggerPx": "62000",
        }
    )

    assert legacy_limiter.acquire_calls == 1


def test_deepcoin_client_finds_historical_orders_by_exchange_or_client_id():
    http_client = _CapturingHttpClient(
        {
            "code": "0",
            "data": [
                {"ordId": "order-1", "clOrdId": "client-1"},
                {"ordId": "order-2", "clOrdId": "client-2"},
            ],
        }
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-06-30T00:00:00.000Z",
    )

    by_order_id = client.get_order_history_by_id(
        inst_id="ETH-USDT-SWAP",
        order_id="order-2",
    )
    by_client_order_id = client.get_trigger_order_history_by_id(
        inst_id="ETH-USDT-SWAP",
        client_order_id="client-1",
    )

    assert by_order_id == {"ordId": "order-2", "clOrdId": "client-2"}
    assert by_client_order_id == {"ordId": "order-1", "clOrdId": "client-1"}
