"""Phase 5b: 401/50000 recognition, read pacing, and the round read cache.

The three claims these tests pin down, in the order the request path applies
them:

1. ``401`` means rate limited only when the body carries ``code=50000``. Every
   other ``401`` stays an authentication failure, and a rate-limited POST stays
   ``DeepcoinRequestOutcomeUnknown`` and is never retried.
2. The token bucket charges one token per *physical* HTTP request, so a
   paginated ``list_open_orders`` pays per page and a retry pays again.
3. A round read cache serves the repeated reads of one reconcile round, is
   dropped by any write through the same client, and never survives the round.
"""

from __future__ import annotations

import httpx
import pytest

from telegram_kol_research.deepcoin_client import (
    DEEPCOIN_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS,
    DeepcoinClientError,
    DeepcoinCredentials,
    DeepcoinRateLimited,
    DeepcoinRateLimitMetrics,
    DeepcoinReadRateLimiter,
    DeepcoinRequestOutcomeUnknown,
    DeepcoinRestClient,
)


class _FakeMonotonicClock:
    def __init__(self, current: float = 100.0) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class _CountingReadLimiter(DeepcoinReadRateLimiter):
    """A real limiter that also records how many tokens were taken."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.acquisitions = 0

    def acquire(self) -> None:
        self.acquisitions += 1
        super().acquire()


class _ScriptedHttpClient:
    """Returns one scripted ``httpx.Response`` per request, in order."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, str]] = []

    def request(self, method, request_path, content="", headers=None):
        self.requests.append({"method": method, "request_path": request_path})
        response = (
            self._responses[len(self.requests) - 1]
            if len(self.requests) <= len(self._responses)
            else self._responses[-1]
        )
        return response(method, request_path)

    def close(self) -> None:
        return None


def _rate_limited(retry_after: str | None = "1"):
    def build(method, request_path):
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return httpx.Response(
            401,
            request=httpx.Request(method, f"https://api.deepcoin.test{request_path}"),
            json={"code": "50000", "msg": "Trigger the api frequency limiting"},
            headers=headers,
        )

    return build


def _unauthorized():
    def build(method, request_path):
        return httpx.Response(
            401,
            request=httpx.Request(method, f"https://api.deepcoin.test{request_path}"),
            json={"code": "50113", "msg": "Invalid Sign"},
        )

    return build


def _ok(rows=None):
    def build(method, request_path):
        return httpx.Response(
            200,
            request=httpx.Request(method, f"https://api.deepcoin.test{request_path}"),
            json={"code": "0", "data": list(rows or [])},
        )

    return build


def _client(responses, *, clock=None, metrics=None):
    clock = clock or _FakeMonotonicClock()
    return (
        DeepcoinRestClient(
            DeepcoinCredentials(
                api_key="key",
                api_secret="secret",
                passphrase="pass",
                base_url="https://api.deepcoin.test",
            ),
            http_client=_ScriptedHttpClient(responses),
            monotonic_factory=clock,
            sleep_fn=clock.sleep,
            rate_limit_metrics=metrics,
        ),
        clock,
    )


# ── 1. recognition ────────────────────────────────────────────────────────────


def test_401_with_code_50000_is_recognised_as_rate_limiting():
    metrics = DeepcoinRateLimitMetrics()
    client, _ = _client([_rate_limited("1")] * 2, metrics=metrics)

    with pytest.raises(DeepcoinRateLimited) as excinfo:
        client.list_positions()

    assert excinfo.value.retry_after == 1.0
    assert metrics.snapshot()["rate_limited_last_hour"] == 2


def test_other_401_is_not_treated_as_rate_limiting_and_is_not_retried():
    metrics = DeepcoinRateLimitMetrics()
    client, _ = _client([_unauthorized()], metrics=metrics)
    http_client = client._http_client

    with pytest.raises(DeepcoinClientError) as excinfo:
        client.list_positions()

    assert not isinstance(excinfo.value, DeepcoinRateLimited)
    assert len(http_client.requests) == 1
    assert metrics.snapshot()["rate_limited_last_hour"] == 0


def test_401_rate_limit_without_json_body_stays_an_auth_failure():
    def not_json(method, request_path):
        return httpx.Response(
            401,
            request=httpx.Request(method, f"https://api.deepcoin.test{request_path}"),
            content=b"<html>401</html>",
        )

    client, _ = _client([not_json])

    with pytest.raises(DeepcoinClientError) as excinfo:
        client.list_positions()

    assert not isinstance(excinfo.value, DeepcoinRateLimited)


# ── 2. bounded retry, GET only ────────────────────────────────────────────────


def test_rate_limited_get_retries_exactly_once_after_waiting_retry_after():
    metrics = DeepcoinRateLimitMetrics()
    client, clock = _client([_rate_limited("1"), _ok([{"posId": "1"}])], metrics=metrics)
    http_client = client._http_client

    rows = client.list_positions()

    assert rows == [{"posId": "1"}]
    assert len(http_client.requests) == 2
    assert clock.sleeps == [1.0]
    assert metrics.snapshot()["retry_after_waits_last_hour"] == 1


def test_retry_wait_is_capped_at_two_seconds():
    client, clock = _client([_rate_limited("30"), _ok()])

    client.list_positions()

    assert clock.sleeps == [DEEPCOIN_RATE_LIMIT_MAX_RETRY_WAIT_SECONDS]


def test_missing_retry_after_falls_back_to_the_measured_one_second_window():
    client, clock = _client([_rate_limited(None), _ok()])

    client.list_positions()

    assert clock.sleeps == [1.0]


def test_a_second_rate_limit_is_raised_rather_than_retried_again():
    client, clock = _client([_rate_limited("1")] * 3)
    http_client = client._http_client

    with pytest.raises(DeepcoinRateLimited):
        client.list_positions()

    assert len(http_client.requests) == 2
    assert clock.sleeps == [1.0]


def test_rate_limited_post_is_unknown_outcome_and_never_retried():
    metrics = DeepcoinRateLimitMetrics()
    client, clock = _client([_rate_limited("1"), _ok()], metrics=metrics)
    http_client = client._http_client

    with pytest.raises(DeepcoinRequestOutcomeUnknown):
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert len(http_client.requests) == 1
    assert clock.sleeps == []
    # The hit is still counted; only the retry is withheld.
    assert metrics.snapshot()["rate_limited_last_hour"] == 1
    assert metrics.snapshot()["retry_after_waits_last_hour"] == 0


# ── 3. the token bucket counts physical requests ──────────────────────────────


def test_read_limiter_paces_at_the_configured_requests_per_second():
    clock = _FakeMonotonicClock(0.0)
    limiter = DeepcoinReadRateLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep, per_second=2
    )

    for _ in range(6):
        limiter.acquire()

    # A capacity-2 bucket serves two immediately, then one every 0.5s.
    assert clock.sleeps == [pytest.approx(0.5)] * 4


def test_writes_do_not_take_read_tokens():
    clock = _FakeMonotonicClock(0.0)
    limiter = DeepcoinReadRateLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep, per_second=1
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=_ScriptedHttpClient([_ok()]),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        read_rate_limiter=limiter,
    )

    for _ in range(4):
        client.cancel_order({"instId": "BTC-USDT-SWAP", "ordId": "1"})

    assert clock.sleeps == []


def test_each_open_orders_page_takes_its_own_token():
    """One logical V2 read spans N pages and must be charged N times.

    Charging the logical call would under-count the true request rate by
    exactly the page count, which is the failure phase 5a's ruling 4 named.
    """

    clock = _FakeMonotonicClock(0.0)
    limiter = _CountingReadLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep, per_second=1
    )

    def page(prefix):
        return [
            {"ordId": f"{prefix}-{index}", "instType": "SWAP"} for index in range(100)
        ]

    http_client = _ScriptedHttpClient(
        [_ok(page("a")), _ok(page("b")), _ok([{"ordId": "tail", "instType": "SWAP"}])]
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        read_rate_limiter=limiter,
    )

    rows = client.list_open_orders()

    assert len(rows) == 201
    assert len(http_client.requests) == 3
    # Three pages, three tokens -- not one token for the logical call.
    assert limiter.acquisitions == 3
    # Capacity 1: the first page is free, each further page waits a full second.
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_a_retry_takes_a_second_token():
    clock = _FakeMonotonicClock(0.0)
    limiter = _CountingReadLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep, per_second=1
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=_ScriptedHttpClient([_rate_limited("1"), _ok()]),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        read_rate_limiter=limiter,
    )

    client.list_positions()

    # Two physical requests, two tokens; the Retry-After wait is on top.
    assert limiter.acquisitions == 2
    assert clock.sleeps == [pytest.approx(1.0)]


# ── 4. round read cache ───────────────────────────────────────────────────────


def test_round_cache_serves_repeated_reads_of_the_same_instrument_once():
    client, _ = _client([_ok([{"posId": "p1"}])])
    http_client = client._http_client

    with client.round_read_cache():
        first = client.list_positions(inst_id="BTC-USDT-SWAP")
        second = client.list_positions(inst_id="BTC-USDT-SWAP")
        pending_a = client.list_trigger_orders_pending(inst_id="BTC-USDT-SWAP")
        pending_b = client.read_trigger_orders_pending(inst_id="BTC-USDT-SWAP")
        open_a = client.list_open_orders(inst_id="BTC-USDT-SWAP")
        open_b = client.list_open_orders(inst_id="BTC-USDT-SWAP")

    assert first == second
    assert pending_a == pending_b["data"]
    assert open_a == open_b
    assert [row["request_path"].split("?")[0] for row in http_client.requests] == [
        "/deepcoin/account/positions",
        "/deepcoin/trade/trigger-orders-pending",
        "/deepcoin/trade/v2/orders-pending",
    ]


def test_round_cache_keys_on_the_instrument():
    client, _ = _client([_ok()])
    http_client = client._http_client

    with client.round_read_cache():
        client.list_positions(inst_id="BTC-USDT-SWAP")
        client.list_positions(inst_id="ETH-USDT-SWAP")
        client.list_positions(inst_id="BTC-USDT-SWAP")

    assert len(http_client.requests) == 2


def test_round_cache_does_not_serve_history_or_fills():
    client, _ = _client([_ok()])
    http_client = client._http_client

    with client.round_read_cache():
        client.list_order_history(inst_id="BTC-USDT-SWAP")
        client.list_order_history(inst_id="BTC-USDT-SWAP")
        client.list_trade_fills(inst_id="BTC-USDT-SWAP")
        client.list_trade_fills(inst_id="BTC-USDT-SWAP")

    assert len(http_client.requests) == 4


def test_a_write_drops_the_round_cache_so_a_post_write_read_is_fresh():
    client, _ = _client([_ok()])
    http_client = client._http_client

    with client.round_read_cache():
        client.list_positions(inst_id="BTC-USDT-SWAP")
        client.cancel_order({"instId": "BTC-USDT-SWAP", "ordId": "1"})
        client.list_positions(inst_id="BTC-USDT-SWAP")

    paths = [row["request_path"].split("?")[0] for row in http_client.requests]
    assert paths == [
        "/deepcoin/account/positions",
        "/deepcoin/trade/cancel-order",
        "/deepcoin/account/positions",
    ]


def test_the_cache_expires_with_the_round_and_never_crosses_rounds():
    client, _ = _client([_ok()])
    http_client = client._http_client

    with client.round_read_cache():
        client.list_positions(inst_id="BTC-USDT-SWAP")
    with client.round_read_cache():
        client.list_positions(inst_id="BTC-USDT-SWAP")
    client.list_positions(inst_id="BTC-USDT-SWAP")

    assert len(http_client.requests) == 3


def test_reads_outside_a_round_are_never_cached():
    client, _ = _client([_ok()])
    http_client = client._http_client

    client.list_positions(inst_id="BTC-USDT-SWAP")
    client.list_positions(inst_id="BTC-USDT-SWAP")

    assert len(http_client.requests) == 2


def test_a_cache_hit_takes_no_token():
    clock = _FakeMonotonicClock(0.0)
    limiter = DeepcoinReadRateLimiter(
        monotonic_factory=clock, sleep_fn=clock.sleep, per_second=1
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        http_client=_ScriptedHttpClient([_ok()]),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        read_rate_limiter=limiter,
    )

    with client.round_read_cache():
        for _ in range(5):
            client.list_positions(inst_id="BTC-USDT-SWAP")

    assert clock.sleeps == []


def test_a_cached_row_cannot_be_mutated_through_a_later_reader():
    client, _ = _client([_ok([{"posId": "p1"}])])

    with client.round_read_cache():
        first = client.list_positions(inst_id="BTC-USDT-SWAP")
        first[0]["posId"] = "tampered"
        second = client.list_positions(inst_id="BTC-USDT-SWAP")

    assert second == [{"posId": "p1"}]
