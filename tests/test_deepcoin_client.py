import base64
import hashlib
import hmac

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinCredentials
from telegram_kol_research.deepcoin_client import build_deepcoin_auth_headers
from telegram_kol_research.deepcoin_client import load_deepcoin_credentials


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
