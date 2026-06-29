"""Deepcoin REST client helpers for authenticated trading requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from telegram_kol_research.telegram_client import _load_env_file_values


DEEPCOIN_BASE_URL = "https://api.deepcoin.com"
DEEPCOIN_PLACE_ORDER_PATH = "/deepcoin/trade/order"
DEEPCOIN_REPLACE_ORDER_SLTP_PATH = "/deepcoin/trade/replace-order-sltp"


class DeepcoinClientError(RuntimeError):
    """Raised when Deepcoin credentials or API responses are invalid."""


@dataclass(slots=True)
class DeepcoinCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    base_url: str = DEEPCOIN_BASE_URL
    timeout_seconds: float = 15.0


class DeepcoinTradingClientProtocol(Protocol):
    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one live order and return the raw Deepcoin response."""

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        """Attach or replace take-profit / stop-loss protection for an open limit order."""


def load_deepcoin_credentials(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> DeepcoinCredentials:
    """Load Deepcoin API credentials from env vars or config env files."""

    env = dict(_load_env_file_values(env_file_paths or [".env", "config/telegram.env"]))
    env.update(environ or os.environ)
    api_key = env.get("DEEPCOIN_API_KEY", "")
    api_secret = env.get("DEEPCOIN_API_SECRET", "")
    passphrase = env.get("DEEPCOIN_API_PASSPHRASE", "")
    missing = [
        name
        for name, value in {
            "DEEPCOIN_API_KEY": api_key,
            "DEEPCOIN_API_SECRET": api_secret,
            "DEEPCOIN_API_PASSPHRASE": passphrase,
        }.items()
        if not value
    ]
    if missing:
        raise DeepcoinClientError(f"missing Deepcoin credentials: {','.join(missing)}")
    return DeepcoinCredentials(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        base_url=env.get("DEEPCOIN_BASE_URL", DEEPCOIN_BASE_URL).rstrip("/"),
        timeout_seconds=float(env.get("DEEPCOIN_TIMEOUT_SECONDS", "15")),
    )


class DeepcoinRestClient:
    """Small authenticated Deepcoin REST client."""

    def __init__(
        self,
        credentials: DeepcoinCredentials,
        *,
        http_client: httpx.Client | None = None,
        timestamp_factory=None,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._timestamp_factory = timestamp_factory or _utc_timestamp_ms

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_PLACE_ORDER_PATH, order_payload)

    def replace_order_sltp(self, protection_payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", DEEPCOIN_REPLACE_ORDER_SLTP_PATH, protection_payload)

    def _request(
        self,
        method: str,
        request_path: str,
        body_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = ""
        if body_payload is not None:
            body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = self._timestamp_factory()
        headers = build_deepcoin_auth_headers(
            credentials=self._credentials,
            timestamp=timestamp,
            method=method,
            request_path=request_path,
            body=body,
        )
        headers["Content-Type"] = "application/json"

        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(
            base_url=self._credentials.base_url,
            timeout=self._credentials.timeout_seconds,
        )
        try:
            response = client.request(method, request_path, content=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise DeepcoinClientError(f"Deepcoin request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DeepcoinClientError("Deepcoin response was not JSON") from exc
        finally:
            if owns_client:
                client.close()

        if str(payload.get("code", "0")) not in {"0", ""}:
            raise DeepcoinClientError(
                f"Deepcoin API error {payload.get('code')}: {payload.get('msg')}"
            )
        return payload


def build_deepcoin_auth_headers(
    *,
    credentials: DeepcoinCredentials,
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
) -> dict[str, str]:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        credentials.api_secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return {
        "DC-ACCESS-KEY": credentials.api_key,
        "DC-ACCESS-SIGN": signature,
        "DC-ACCESS-TIMESTAMP": timestamp,
        "DC-ACCESS-PASSPHRASE": credentials.passphrase,
    }


def build_deepcoin_client_from_env() -> DeepcoinRestClient:
    return DeepcoinRestClient(load_deepcoin_credentials())


def _utc_timestamp_ms() -> str:
    value = datetime.now(UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
