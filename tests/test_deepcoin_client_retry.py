import json
import traceback

import httpx
import pytest

from telegram_kol_research.deepcoin_client import (
    DeepcoinCredentials,
    DeepcoinDefiniteRejection,
    DeepcoinPreSendUnavailable,
    DeepcoinReadUnavailable,
    DeepcoinRequestOutcomeUnknown,
    DeepcoinRequestScope,
    DeepcoinRestClient,
)
from telegram_kol_research.deepcoin_request_governor import GovernorLease
from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinGovernorDeadlineExceeded,
    DeepcoinGovernorStateError,
)
from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    OutcomeCertainty,
    RequestPriority,
)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _SequentialHttpClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.close_calls = 0

    def request(
        self,
        method,
        request_path,
        content="",
        headers=None,
        timeout=None,
    ):
        self.requests.append(
            {
                "method": method,
                "request_path": request_path,
                "content": content,
                "headers": headers or {},
                "timeout": timeout,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.close_calls += 1


class _CloseFailingHttpClient(_SequentialHttpClient):
    def close(self):
        self.close_calls += 1
        raise RuntimeError("socket cleanup failed")


class _WaitingGovernor:
    def __init__(self, clock, waits):
        self.clock = clock
        self.waits = list(waits)
        self.calls = []

    def enforces(self, method):
        return True

    def acquire(self, *, method, request_path, priority, deadline_monotonic):
        self.calls.append(
            {
                "method": method,
                "request_path": request_path,
                "priority": priority,
                "deadline_monotonic": deadline_monotonic,
            }
        )
        wait = self.waits.pop(0) if self.waits else 0
        self.clock.sleep(wait)
        return GovernorLease(
            uid_scope_hash="a" * 64,
            normalized_path=request_path.split("?", 1)[0],
            waited_ms=round(wait * 1000),
            observed_delay_ms=round(wait * 1000),
        )


class _FailingGovernor:
    def __init__(self, failure):
        self.failure = failure

    def enforces(self, method):
        return True

    def acquire(self, **kwargs):
        raise self.failure


def _response(status=200, *, payload=None, content=None, headers=None):
    request = httpx.Request("GET", "https://api.deepcoin.test/test")
    kwargs = {"request": request, "headers": headers}
    if content is not None:
        kwargs["content"] = content
    else:
        kwargs["json"] = {"code": "0", "data": []} if payload is None else payload
    return httpx.Response(status, **kwargs)


def _timeout():
    return httpx.ReadTimeout(
        "response unavailable",
        request=httpx.Request("GET", "https://api.deepcoin.test/test"),
    )


def _client(http_client, clock, **kwargs):
    kwargs.setdefault("retry_jitter_fn", lambda: 0.0)
    return DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
            timeout_seconds=15,
        ),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        timestamp_factory=lambda: f"ts-{clock():.1f}",
        **kwargs,
    )


def _scope(clock, *, priority=RequestPriority.CRITICAL, seconds=10, recorder=None):
    return DeepcoinRequestScope(
        phase="entry_preflight",
        priority=priority,
        deadline_monotonic=clock() + seconds,
        correlation_id="operation-1",
        attempt_recorder=recorder,
    )


def test_get_timeout_then_success_sends_exactly_two_gets():
    clock = _Clock()
    http_client = _SequentialHttpClient([_timeout(), _response()])
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)):
        assert client.list_positions() == []

    assert [row["method"] for row in http_client.requests] == ["GET", "GET"]
    assert clock.sleeps == [pytest.approx(0.5)]


def test_get_429_honors_bounded_retry_after():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(429, headers={"Retry-After": "2"}), _response()]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)):
        client.list_positions()

    assert len(http_client.requests) == 2
    assert clock.sleeps == [pytest.approx(2.0)]


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "-1", "bad"])
def test_invalid_retry_after_uses_closed_backoff(retry_after):
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(429, headers={"Retry-After": retry_after}), _response()]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)):
        client.list_positions()

    assert clock.sleeps == [pytest.approx(0.5)]


def test_request_timeout_is_bounded_by_remaining_deadline_each_attempt():
    clock = _Clock()
    http_client = _SequentialHttpClient([_timeout(), _response()])
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock, seconds=1)):
        client.list_positions()

    assert [row["timeout"] for row in http_client.requests] == pytest.approx(
        [1.0, 0.5]
    )


def test_get_503_uses_closed_half_one_two_second_backoff():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(503), _response(503), _response(503), _response()]
    )
    client = _client(http_client, clock, retry_jitter_fn=lambda: 0.0)

    with client.request_scope(_scope(clock)):
        client.list_positions()

    assert len(http_client.requests) == 4
    assert clock.sleeps == pytest.approx([0.5, 1.0, 2.0])


def test_fourth_failed_critical_get_stops_inside_parent_deadline():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response(503) for _ in range(4)])
    client = _client(http_client, clock, retry_jitter_fn=lambda: 0.0)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinReadUnavailable
    ) as captured:
        client.list_positions()

    assert len(http_client.requests) == 4
    assert clock() == pytest.approx(3.5)
    assert captured.value.fact.category == ErrorCategory.HTTP_RETRYABLE
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_background_get_uses_at_most_two_attempts_inside_five_seconds():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response(503), _response(503), _response()])
    client = _client(http_client, clock, retry_jitter_fn=lambda: 0.0)

    with client.request_scope(
        _scope(clock, priority=RequestPriority.BACKGROUND, seconds=5)
    ), pytest.raises(DeepcoinReadUnavailable):
        client.list_positions()

    assert len(http_client.requests) == 2
    assert clock.sleeps == [pytest.approx(0.5)]


def test_malformed_get_json_retries_once_then_is_schema_incompatible():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(content=b"{"), _response(content=b"{")]
    )
    facts = []
    client = _client(http_client, clock, retry_jitter_fn=lambda: 0.0)

    with client.request_scope(
        _scope(clock, recorder=facts.append)
    ), pytest.raises(DeepcoinReadUnavailable) as captured:
        client.list_positions()

    assert len(http_client.requests) == 2
    assert captured.value.fact.category == ErrorCategory.SCHEMA_INCOMPATIBLE
    assert captured.value.fact.safe_code == "schema_incompatible"
    assert [fact.ordinal for fact in facts] == [1, 2]
    assert all("{" not in json.dumps(fact.safe_code) for fact in facts)


def test_invalid_list_schema_retries_once_then_succeeds():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [
            _response(payload={"code": "0", "data": {"unexpected": []}}),
            _response(payload={"code": "0", "data": []}),
        ]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)):
        assert client.list_positions() == []

    assert len(http_client.requests) == 2
    assert clock.sleeps == [pytest.approx(0.5)]


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_get_failure_is_not_retried(status):
    clock = _Clock()
    http_client = _SequentialHttpClient([_response(status), _response()])
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinReadUnavailable
    ) as captured:
        client.list_positions()

    assert len(http_client.requests) == 1
    assert captured.value.fact.category == ErrorCategory.AUTH_FAILED


def test_explicit_business_rejection_is_not_retried():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(payload={"code": "50011", "msg": "raw provider detail"}), _response()]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinDefiniteRejection
    ) as captured:
        client.list_positions()

    assert len(http_client.requests) == 1
    assert captured.value.fact.category == ErrorCategory.BUSINESS_REJECTED
    assert "raw provider detail" not in str(captured.value)


def test_malformed_business_code_is_schema_failure_without_payload_leak():
    clock = _Clock()
    secret = "provider-secret-must-not-persist"
    http_client = _SequentialHttpClient(
        [
            _response(payload={"code": {"raw": secret}, "data": []}),
            _response(payload={"code": {"raw": secret}, "data": []}),
        ]
    )
    facts = []
    client = _client(http_client, clock)

    with client.request_scope(
        _scope(clock, recorder=facts.append)
    ), pytest.raises(DeepcoinReadUnavailable) as captured:
        client.list_positions()

    assert captured.value.fact.category == ErrorCategory.SCHEMA_INCOMPATIBLE
    assert secret not in str(captured.value)
    assert secret not in repr(facts)


def test_deeply_nested_post_json_is_unknown_and_never_retried():
    clock = _Clock()
    deep_json = b"[" * 20_000 + b"0" + b"]" * 20_000
    http_client = _SequentialHttpClient(
        [_response(content=deep_json), _response()]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinRequestOutcomeUnknown
    ) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert len(http_client.requests) == 1
    assert captured.value.fact.category == ErrorCategory.SCHEMA_INVALID
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_deeply_nested_get_json_retries_once_then_schema_incompatible():
    clock = _Clock()
    deep_json = b"[" * 20_000 + b"0" + b"]" * 20_000
    http_client = _SequentialHttpClient(
        [_response(content=deep_json), _response(content=deep_json)]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinReadUnavailable
    ) as captured:
        client.list_positions()

    assert len(http_client.requests) == 2
    assert captured.value.fact.category == ErrorCategory.SCHEMA_INCOMPATIBLE


@pytest.mark.parametrize(
    "outcome",
    [
        _timeout(),
        _response(503),
        _response(content=b"{"),
    ],
)
def test_post_ambiguous_failure_is_unknown_and_never_retried(outcome):
    clock = _Clock()
    http_client = _SequentialHttpClient([outcome, _response()])
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinRequestOutcomeUnknown
    ) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP", "secret": "never-log"})

    assert len(http_client.requests) == 1
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.UNKNOWN
    assert "never-log" not in str(captured.value)


@pytest.mark.parametrize(
    ("outcome", "expected_exception"),
    [
        (_response(503), DeepcoinRequestOutcomeUnknown),
        (
            _response(payload={"code": "50011", "data": []}),
            DeepcoinDefiniteRejection,
        ),
    ],
)
def test_post_terminal_fact_is_not_overwritten_by_retry_jitter_failure(
    outcome,
    expected_exception,
):
    clock = _Clock()
    http_client = _SequentialHttpClient([outcome])

    def fail_jitter():
        raise RuntimeError("jitter contains sensitive diagnostic")

    client = _client(http_client, clock, retry_jitter_fn=fail_jitter)

    with client.request_scope(_scope(clock)), pytest.raises(
        expected_exception
    ) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert len(http_client.requests) == 1
    assert "sensitive diagnostic" not in str(captured.value)


def test_close_error_after_parsed_post_success_does_not_replace_success(monkeypatch):
    clock = _Clock()
    http_client = _CloseFailingHttpClient(
        [_response(payload={"code": "0", "data": {"ordId": "order-1"}})]
    )
    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_client.httpx.Client",
        lambda **kwargs: http_client,
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    result = client.place_order({"instId": "BTC-USDT-SWAP"})

    assert result["data"]["ordId"] == "order-1"
    assert http_client.close_calls == 0
    with pytest.raises(Exception, match="cleanup failed"):
        client.close()


def test_context_manager_close_error_does_not_replace_parsed_post_success(
    monkeypatch,
):
    clock = _Clock()
    http_client = _CloseFailingHttpClient(
        [_response(payload={"code": "0", "data": {"ordId": "order-1"}})]
    )
    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_client.httpx.Client",
        lambda **kwargs: http_client,
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(
            api_key="key",
            api_secret="secret",
            passphrase="pass",
            base_url="https://api.deepcoin.test",
        ),
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    with client:
        result = client.place_order({"instId": "BTC-USDT-SWAP"})

    assert result["data"]["ordId"] == "order-1"
    assert http_client.close_calls == 1


def test_timestamp_and_signature_are_created_after_each_governor_wait():
    clock = _Clock()
    http_client = _SequentialHttpClient([_timeout(), _response()])
    governor = _WaitingGovernor(clock, [1.0, 1.0])
    client = _client(
        http_client,
        clock,
        request_governor=governor,
        retry_jitter_fn=lambda: 0.0,
    )

    with client.request_scope(_scope(clock)):
        client.list_positions()

    assert [
        request["headers"]["DC-ACCESS-TIMESTAMP"]
        for request in http_client.requests
    ] == ["ts-1.0", "ts-2.5"]
    assert len(governor.calls) == 2


def test_each_retry_has_a_distinct_signature_from_post_wait_timestamp():
    clock = _Clock()
    http_client = _SequentialHttpClient([_timeout(), _response()])
    governor = _WaitingGovernor(clock, [1.0, 1.0])
    timestamps = iter(
        [
            "2026-08-12T00:00:01.000Z",
            "2026-08-12T00:00:02.500Z",
        ]
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
        timestamp_factory=lambda: next(timestamps),
        request_governor=governor,
        retry_jitter_fn=lambda: 0.0,
    )

    with client.request_scope(_scope(clock)):
        client.list_positions()

    first, second = http_client.requests
    assert first["headers"]["DC-ACCESS-TIMESTAMP"] != second["headers"][
        "DC-ACCESS-TIMESTAMP"
    ]
    assert first["headers"]["DC-ACCESS-SIGN"] != second["headers"][
        "DC-ACCESS-SIGN"
    ]


def test_retry_after_beyond_remaining_deadline_stops_without_sleep():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(429, headers={"Retry-After": "2"}), _response()]
    )
    client = _client(http_client, clock)

    with client.request_scope(_scope(clock, seconds=1)), pytest.raises(
        DeepcoinReadUnavailable
    ):
        client.list_positions()

    assert len(http_client.requests) == 1
    assert clock.sleeps == []


def test_backoff_equal_to_remaining_deadline_preserves_sent_unknown_fact():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response(503), _response()])
    facts = []
    client = _client(http_client, clock)

    with client.request_scope(
        _scope(clock, seconds=0.5, recorder=facts.append)
    ), pytest.raises(DeepcoinReadUnavailable) as captured:
        client.list_positions()

    assert len(http_client.requests) == 1
    assert clock.sleeps == []
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.UNKNOWN
    assert captured.value.fact.safe_code == "http_503"
    assert [(fact.ordinal, fact.safe_code) for fact in facts] == [(1, "http_503")]


def test_request_scope_resets_without_leaking_priority_or_deadline():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response(), _response()])
    governor = _WaitingGovernor(clock, [0, 0])
    client = _client(http_client, clock, request_governor=governor)

    with client.request_scope(
        _scope(clock, priority=RequestPriority.BACKGROUND, seconds=5)
    ):
        client.list_positions()
    client.list_positions()

    assert governor.calls[0]["priority"] == RequestPriority.BACKGROUND
    assert governor.calls[0]["deadline_monotonic"] == 5
    assert governor.calls[1]["priority"] == RequestPriority.NORMAL
    assert governor.calls[1]["deadline_monotonic"] == 10


def test_attempt_recorder_failure_after_post_is_unknown_without_resend():
    clock = _Clock()
    http_client = _SequentialHttpClient(
        [_response(payload={"code": "0", "data": {"ordId": "order-1"}})]
    )
    client = _client(http_client, clock)

    def fail_recorder(fact):
        raise RuntimeError("database contains sensitive diagnostic")

    with client.request_scope(
        _scope(clock, recorder=fail_recorder)
    ), pytest.raises(DeepcoinRequestOutcomeUnknown) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert len(http_client.requests) == 1
    assert captured.value.fact.category == ErrorCategory.STATE_CONFLICT
    assert "sensitive diagnostic" not in str(captured.value)
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert "sensitive diagnostic" not in rendered


def test_expired_post_scope_is_typed_not_sent_and_never_calls_http():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response()])
    client = _client(http_client, clock)

    with client.request_scope(
        DeepcoinRequestScope(
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=clock(),
        )
    ), pytest.raises(DeepcoinPreSendUnavailable) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert http_client.requests == []
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.NOT_SENT
    assert captured.value.fact.retryable is False


def test_post_deadline_is_rechecked_after_timestamp_and_signing_before_send():
    clock = _Clock()
    http_client = _SequentialHttpClient([_response()])

    def timestamp_after_deadline():
        clock.now = 11
        return "2026-08-12T00:00:11.000Z"

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
        timestamp_factory=timestamp_after_deadline,
    )

    with client.request_scope(
        DeepcoinRequestScope(
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=10,
        )
    ), pytest.raises(DeepcoinPreSendUnavailable) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert http_client.requests == []
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.NOT_SENT
    assert captured.value.fact.safe_code == "request_deadline_exceeded"
    assert captured.value.fact.retryable is False


@pytest.mark.parametrize(
    ("failure", "expected_category", "expected_code"),
    [
        (
            DeepcoinGovernorStateError("raw corrupt state detail"),
            ErrorCategory.STATE_CONFLICT,
            "governor_state_unavailable",
        ),
        (
            DeepcoinGovernorDeadlineExceeded("raw deadline detail"),
            ErrorCategory.TRANSPORT_TIMEOUT,
            "governor_deadline_exceeded",
        ),
    ],
)
def test_governor_local_refusals_are_not_retryable_or_raw(
    failure,
    expected_category,
    expected_code,
):
    clock = _Clock()
    http_client = _SequentialHttpClient([_response()])
    client = _client(
        http_client,
        clock,
        request_governor=_FailingGovernor(failure),
    )

    with client.request_scope(_scope(clock)), pytest.raises(
        DeepcoinPreSendUnavailable
    ) as captured:
        client.place_order({"instId": "BTC-USDT-SWAP"})

    assert http_client.requests == []
    assert captured.value.fact.category == expected_category
    assert captured.value.fact.safe_code == expected_code
    assert captured.value.fact.retryable is False
    assert captured.value.fact.outcome_certainty == OutcomeCertainty.NOT_SENT
    assert str(failure) not in str(captured.value)
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert str(failure) not in rendered
