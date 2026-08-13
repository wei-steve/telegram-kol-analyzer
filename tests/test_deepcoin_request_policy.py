import pytest

from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    OutcomeCertainty,
    RequestPriority,
    classify_business_failure,
    classify_http_failure,
    classify_schema_failure,
    classify_transport_failure,
    normalize_request_path,
    request_profile,
)


def test_pending_tpsl_uses_safe_five_per_second_profile():
    profile = request_profile(
        "GET",
        "/deepcoin/trade/trigger-orders-pending?instType=SWAP&limit=100",
    )

    assert profile.per_second == 4
    assert profile.per_minute == 120
    assert profile.background_per_second == 2
    assert profile.background_per_minute == 60
    assert profile.min_interval_seconds == 0


def test_query_variants_normalize_to_one_endpoint_path():
    assert normalize_request_path(
        "/deepcoin/trade/trigger-orders-pending?limit=100&instId=BTC-USDT-SWAP"
    ) == normalize_request_path(
        "/deepcoin/trade/trigger-orders-pending?instId=ETH-USDT-SWAP&limit=50"
    )
    assert normalize_request_path(
        "https://api.deepcoin.com/deepcoin/trade/trigger-orders-pending?limit=100"
    ) == "/deepcoin/trade/trigger-orders-pending"


@pytest.mark.parametrize(
    ("method", "path", "per_second", "per_minute"),
    [
        ("GET", "/deepcoin/account/positions", 8, 240),
        ("GET", "/deepcoin/trade/orders-pending", 8, 240),
        ("POST", "/deepcoin/trade/order", 12, 360),
        ("POST", "/deepcoin/trade/set-position-sltp", 12, 360),
    ],
)
def test_documented_profiles_keep_twenty_percent_headroom(
    method,
    path,
    per_second,
    per_minute,
):
    profile = request_profile(method, path)

    assert profile.per_second == per_second
    assert profile.per_minute == per_minute
    assert profile.background_per_second <= per_second
    assert profile.background_per_minute <= per_minute


def test_position_history_uses_explicit_strict_minimum_interval():
    profile = request_profile(
        "GET",
        "/deepcoin/account/positions-history?instType=SWAP",
    )

    assert profile.per_second == 1
    assert profile.per_minute == 48
    assert profile.min_interval_seconds == pytest.approx(1.25)


def test_unknown_endpoint_fails_closed_to_strict_profile():
    profile = request_profile("PATCH", "/deepcoin/private/future-endpoint")

    assert profile.per_second == 1
    assert profile.per_minute == 48
    assert profile.background_per_second == 1
    assert profile.background_per_minute == 24
    assert profile.min_interval_seconds == pytest.approx(1.25)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_safe_get_retryable_http_status_keeps_read_outcome_unknown(status):
    fact = classify_http_failure(method="GET", status_code=status)

    expected_category = (
        ErrorCategory.RATE_LIMITED
        if status == 429
        else ErrorCategory.HTTP_RETRYABLE
    )
    assert fact.category == expected_category
    assert fact.retryable is True
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN
    assert fact.http_status == status


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_http_failure_is_not_retryable(status):
    fact = classify_http_failure(method="GET", status_code=status)

    assert fact.category == ErrorCategory.AUTH_FAILED
    assert fact.retryable is False
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_ordinary_get_client_error_is_not_retryable():
    fact = classify_http_failure(method="GET", status_code=422)

    assert fact.category == ErrorCategory.BUSINESS_REJECTED
    assert fact.retryable is False
    assert fact.outcome_certainty == OutcomeCertainty.REJECTED


def test_post_http_failure_is_unknown_and_never_retryable():
    fact = classify_http_failure(method="POST", status_code=429)

    assert fact.category == ErrorCategory.RATE_LIMITED
    assert fact.retryable is False
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_get_transport_timeout_is_retryable():
    fact = classify_transport_failure(
        method="GET",
        sent=True,
        code="read_timeout",
    )

    assert fact.category == ErrorCategory.TRANSPORT_TIMEOUT
    assert fact.retryable is True
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_post_transport_failure_after_send_is_unknown_and_not_retryable():
    fact = classify_transport_failure(
        method="POST",
        sent=True,
        code="read_timeout",
    )

    assert fact.category == ErrorCategory.TRANSPORT_TIMEOUT
    assert fact.retryable is False
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_post_local_pre_send_failure_is_the_only_retryable_not_sent_case():
    fact = classify_transport_failure(
        method="POST",
        sent=False,
        code="governor_deadline",
    )

    assert fact.category == ErrorCategory.TRANSPORT_TIMEOUT
    assert fact.retryable is True
    assert fact.outcome_certainty == OutcomeCertainty.NOT_SENT


def test_get_schema_failure_retries_once_then_becomes_incompatible():
    first = classify_schema_failure(method="GET", occurrence=1)
    second = classify_schema_failure(method="GET", occurrence=2)

    assert first.category == ErrorCategory.SCHEMA_INVALID
    assert first.retryable is True
    assert first.outcome_certainty == OutcomeCertainty.UNKNOWN
    assert second.category == ErrorCategory.SCHEMA_INCOMPATIBLE
    assert second.retryable is False
    assert second.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_post_schema_failure_is_unknown_without_retry():
    fact = classify_schema_failure(method="POST", occurrence=1)

    assert fact.category == ErrorCategory.SCHEMA_INVALID
    assert fact.retryable is False
    assert fact.outcome_certainty == OutcomeCertainty.UNKNOWN


def test_explicit_business_error_is_rejected_without_message_parsing():
    first = classify_business_failure(method="POST", exchange_code="51000")
    second = classify_business_failure(method="POST", exchange_code="different")

    assert first.category == ErrorCategory.BUSINESS_REJECTED
    assert first.outcome_certainty == OutcomeCertainty.REJECTED
    assert first.retryable is False
    assert second.category == first.category
    assert second.outcome_certainty == first.outcome_certainty


def test_request_priorities_are_closed_values():
    assert {item.value for item in RequestPriority} == {
        "critical",
        "normal",
        "background",
    }
