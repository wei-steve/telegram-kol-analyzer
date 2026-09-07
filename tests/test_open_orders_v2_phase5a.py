"""Phase 5a: list_open_orders on V2 orders-pending, and the regular-order guard."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import (
    DEEPCOIN_OPEN_ORDER_V1_TO_V2_FIELDS,
    DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT,
    DEEPCOIN_ORDERS_PENDING_V2_PATH,
    DeepcoinClientError,
    DeepcoinCredentials,
    DeepcoinRestClient,
)
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.open_order_action_guard import (
    REGULAR_ORDER_LEG_KINDS,
    guard_regular_open_orders,
    is_regular_order_leg_kind,
)
from telegram_kol_research.runtime_incidents import RuntimeIncident

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _PagingHttpClient:
    """Serve one payload per request and record the exact request paths."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests: list[str] = []

    def request(self, method, request_path, content="", headers=None):
        self.requests.append(request_path)
        if not self._payloads:
            raise AssertionError(f"unexpected extra request: {request_path}")
        return _FakeResponse(self._payloads.pop(0))

    def close(self):
        return None


def _client(http_client):
    return DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        timestamp_factory=lambda: "2026-09-07T12:00:00.000Z",
    )


def _swap_row(ord_id: str, *, inst_id: str = "ETH-USDT-SWAP") -> dict[str, str]:
    return {
        "instType": "SWAP",
        "instId": inst_id,
        "ordId": ord_id,
        "clOrdId": f"client-{ord_id}",
        "ordType": "limit",
        "side": "buy",
        "posSide": "long",
        "state": "live",
        "sz": "1.8",
        "px": "2329",
        "cTime": "1788433332000",
    }


def _page(count: int, *, start: int = 0) -> list[dict[str, str]]:
    return [_swap_row(f"order-{start + index}") for index in range(count)]


# --- endpoint and request shape -------------------------------------------


def test_list_open_orders_calls_v2_with_one_based_index_and_max_limit():
    http_client = _PagingHttpClient([{"code": "0", "data": _page(2)}])
    orders = _client(http_client).list_open_orders(inst_id="ETH-USDT-SWAP")

    assert [row["ordId"] for row in orders] == ["order-0", "order-1"]
    assert http_client.requests == [
        f"{DEEPCOIN_ORDERS_PENDING_V2_PATH}"
        f"?instId=ETH-USDT-SWAP&index=1&limit={DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT}"
    ]


def test_list_open_orders_omits_inst_id_when_not_given():
    http_client = _PagingHttpClient([{"code": "0", "data": []}])
    assert _client(http_client).list_open_orders() == []
    assert http_client.requests == [
        f"{DEEPCOIN_ORDERS_PENDING_V2_PATH}"
        f"?index=1&limit={DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT}"
    ]


# --- pagination -------------------------------------------------------------


def test_list_open_orders_pages_until_a_short_page():
    full = _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)
    second = _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT, start=1000)
    tail = _page(3, start=2000)
    http_client = _PagingHttpClient(
        [
            {"code": "0", "data": full},
            {"code": "0", "data": second},
            {"code": "0", "data": tail},
        ]
    )

    orders = _client(http_client).list_open_orders(inst_id="ETH-USDT-SWAP")

    assert len(orders) == 2 * DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT + 3
    assert [request.split("index=")[1].split("&")[0] for request in
            http_client.requests] == ["1", "2", "3"]


def test_list_open_orders_stops_on_an_exactly_empty_page():
    full = _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)
    http_client = _PagingHttpClient(
        [{"code": "0", "data": full}, {"code": "0", "data": []}]
    )

    orders = _client(http_client).list_open_orders()

    assert len(orders) == DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT
    assert len(http_client.requests) == 2


def test_list_open_orders_raises_when_the_cursor_does_not_advance():
    full = _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)
    http_client = _PagingHttpClient(
        [{"code": "0", "data": full}, {"code": "0", "data": list(full)}]
    )

    with pytest.raises(DeepcoinClientError, match="did not advance"):
        _client(http_client).list_open_orders()


# --- fail closed: never a partial result ------------------------------------


class _FailOnSecondPageHttpClient:
    def __init__(self, first_payload):
        self._first_payload = first_payload
        self.calls = 0

    def request(self, method, request_path, content="", headers=None):
        self.calls += 1
        if self.calls == 1:
            return _FakeResponse(self._first_payload)
        raise httpx.ReadTimeout(
            "lost response",
            request=httpx.Request(method, f"https://api.deepcoin.test{request_path}"),
        )

    def close(self):
        return None


def test_a_failed_page_raises_instead_of_returning_the_pages_already_read():
    http_client = _FailOnSecondPageHttpClient(
        {"code": "0", "data": _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)}
    )

    with pytest.raises(DeepcoinClientError):
        _client(http_client).list_open_orders()

    assert http_client.calls == 2


def test_a_malformed_page_raises_instead_of_returning_a_partial_result():
    http_client = _PagingHttpClient(
        [
            {"code": "0", "data": _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)},
            {"code": "0", "data": "not-a-list"},
        ]
    )

    with pytest.raises(DeepcoinClientError, match="invalid list response schema"):
        _client(http_client).list_open_orders()


def test_a_business_error_page_raises_instead_of_returning_a_partial_result():
    http_client = _PagingHttpClient(
        [
            {"code": "0", "data": _page(DEEPCOIN_ORDERS_PENDING_V2_PAGE_LIMIT)},
            {"code": "50000", "msg": "too many requests", "data": []},
        ]
    )

    with pytest.raises(DeepcoinClientError):
        _client(http_client).list_open_orders()


# --- V1 -> V2 field mapping --------------------------------------------------


def test_v1_to_v2_field_map_covers_every_field_the_codebase_reads():
    consumed_fields = {
        "instType",
        "instId",
        "ordId",
        "clOrdId",
        "px",
        "sz",
        "ordType",
        "side",
        "posSide",
        "tdMode",
        "accFillSz",
        "fillSz",
        "fillPx",
        "avgPx",
        "state",
        "lever",
        "tpTriggerPx",
        "slTriggerPx",
        "uTime",
        "cTime",
    }
    assert consumed_fields <= set(DEEPCOIN_OPEN_ORDER_V1_TO_V2_FIELDS)
    # V2 renames nothing that is consumed, so the mapping is the identity and
    # rows may be handed to callers verbatim.
    assert all(
        source == target
        for source, target in DEEPCOIN_OPEN_ORDER_V1_TO_V2_FIELDS.items()
    )


def test_rows_are_returned_verbatim_including_fields_v1_did_not_document():
    row = _swap_row("order-1") | {"category": "normal", "source": "13"}
    http_client = _PagingHttpClient([{"code": "0", "data": [row]}])

    assert _client(http_client).list_open_orders() == [row]


def test_non_swap_rows_are_dropped_because_v2_has_no_inst_type_parameter():
    spot_row = _swap_row("spot-1") | {"instType": "SPOT", "instId": "ETH-USDT"}
    http_client = _PagingHttpClient(
        [{"code": "0", "data": [_swap_row("order-1"), spot_row]}]
    )

    orders = _client(http_client).list_open_orders()

    assert [row["ordId"] for row in orders] == ["order-1"]


def test_a_row_without_inst_type_is_kept_because_unclassifiable_is_not_absent():
    row = _swap_row("order-1")
    row.pop("instType")
    http_client = _PagingHttpClient([{"code": "0", "data": [row]}])

    assert _client(http_client).list_open_orders() == [row]


# --- guard -------------------------------------------------------------------


def test_regular_order_leg_kinds_exclude_trigger_and_manual_legs():
    assert is_regular_order_leg_kind("market")
    assert is_regular_order_leg_kind("LIMIT")
    assert not is_regular_order_leg_kind("trigger_limit")
    assert not is_regular_order_leg_kind("manual_bind")
    assert not is_regular_order_leg_kind("unknown")
    assert not is_regular_order_leg_kind(None)
    assert REGULAR_ORDER_LEG_KINDS == {"market", "limit"}


_SEEDED = {"message_id": 0}


def _seed_leg(session_factory, *, order_id, client_order_id, order_kind):
    _SEEDED["message_id"] += 1
    message_id = _SEEDED["message_id"]
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            strategy_instance_id=f"strategy-{message_id}",
            kol_id="kol-1",
            chat_id=-100,
            message_id=message_id,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            order_id=order_id,
            client_order_id=client_order_id,
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=int(binding_id),
            strategy_instance_id=f"strategy-{message_id}",
            leg_index=0,
            purpose="entry",
            order_kind=order_kind,
            order_id=order_id,
            client_order_id=client_order_id,
            venue="deepcoin",
            status="open",
        ),
    )


def test_guard_allows_only_system_submitted_regular_order_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-allow.db")
    _seed_leg(
        session_factory,
        order_id="ours-regular",
        client_order_id="client-ours-regular",
        order_kind="market",
    )
    _seed_leg(
        session_factory,
        order_id="ours-trigger",
        client_order_id="client-ours-trigger",
        order_kind="trigger_limit",
    )

    guarded = guard_regular_open_orders(
        session_factory,
        rows=[
            _swap_row("ours-regular"),
            _swap_row("ours-trigger"),
            _swap_row("someone-elses"),
        ],
        action="cancel_entry_order",
        instrument_id="ETH-USDT-SWAP",
        occurred_at=NOW,
    )

    assert [row["ordId"] for row in guarded.allowed] == ["ours-regular"]
    assert [row["ordId"] for row in guarded.blocked] == [
        "ours-trigger",
        "someone-elses",
    ]


def test_guard_records_a_runtime_incident_for_every_blocked_row(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-incident.db")

    guard_regular_open_orders(
        session_factory,
        rows=[_swap_row("foreign-1")],
        action="cancel_pending_entry_legs",
        instrument_id="ETH-USDT-SWAP",
        occurred_at=NOW,
    )

    with session_factory() as session:
        incidents = session.query(RuntimeIncident).all()
        assert len(incidents) == 1
        assert incidents[0].incident_type == "open_order_guard_blocked"
        assert incidents[0].severity == "high"
        assert incidents[0].source_record_id == "foreign-1"


def test_guard_matches_on_client_order_id_when_the_exchange_omits_ord_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-clordid.db")
    _seed_leg(
        session_factory,
        order_id=None,
        client_order_id="client-only",
        order_kind="market",
    )
    row = _swap_row("unrecorded") | {"clOrdId": "client-only"}

    guarded = guard_regular_open_orders(
        session_factory,
        rows=[row],
        action="cancel_entry_order",
        instrument_id="ETH-USDT-SWAP",
        occurred_at=NOW,
    )

    assert guarded.allowed == (row,)
    assert guarded.blocked == ()


def test_guard_on_no_rows_touches_neither_the_database_nor_the_incident_log(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-empty.db")

    guarded = guard_regular_open_orders(
        session_factory,
        rows=[],
        action="cancel_entry_order",
        instrument_id="ETH-USDT-SWAP",
        occurred_at=NOW,
    )

    assert guarded.allowed == ()
    assert guarded.blocked == ()
    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 0
