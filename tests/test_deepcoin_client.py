import base64
import hashlib
import hmac

import pytest

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinCredentials
from telegram_kol_research.deepcoin_client import DeepcoinRestClient
from telegram_kol_research.deepcoin_client import build_deepcoin_auth_headers
from telegram_kol_research.deepcoin_client import load_deepcoin_credentials
from telegram_kol_research.deepcoin_client import _raise_for_deepcoin_business_error


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def request(self, method, request_path, content="", headers=None):
        self.requests.append(
            {
                "method": method,
                "request_path": request_path,
                "content": content,
                "headers": headers or {},
            }
        )
        return _FakeResponse(self.payload)


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
    with pytest.raises(DeepcoinClientError, match="invalid list response schema"):
        client.list_position_history(
            inst_id="BTC-USDT-SWAP",
            pos_id="position-1",
        )


def test_deepcoin_list_endpoint_rejects_non_list_data():
    http_client = _CapturingHttpClient({"code": "0", "data": {"unexpected": []}})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
    )

    with pytest.raises(DeepcoinClientError, match="invalid list response schema"):
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
