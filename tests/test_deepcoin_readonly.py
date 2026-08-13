import pytest

from telegram_kol_research.deepcoin_readonly import (
    DeepcoinOrderBinding,
    DeepcoinReadOnlyAccountState,
    map_deepcoin_open_orders,
    map_deepcoin_positions,
)
from telegram_kol_research.deepcoin_snapshot_authority import (
    DeepcoinSnapshotUnavailable,
)


def test_map_deepcoin_positions_returns_bound_active_positions_only():
    positions = map_deepcoin_positions(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "pos": "1",
            },
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "pos-2",
                "posSide": "short",
                "pos": "0",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "unbound-pos",
                "posSide": "long",
                "pos": "1",
            },
        ],
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
                pos_id="pos-1",
            )
        ],
    )

    assert len(positions) == 1
    assert positions[0].kol_id == "alice"
    assert positions[0].chat_id == 100
    assert positions[0].symbol == "BTC"
    assert positions[0].side == "long"
    assert positions[0].pos_id == "pos-1"


def test_map_deepcoin_positions_splits_multi_position_binding():
    positions = map_deepcoin_positions(
        [
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "short",
                "pos": "4.3",
            },
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "pos-2",
                "posSide": "short",
                "pos": "6.4",
            },
        ],
        bindings=[
            DeepcoinOrderBinding(
                kol_id="kol-a",
                chat_id=100,
                source_message_id=55,
                symbol="ETH",
                side="short",
                pos_id="pos-1,pos-2",
            )
        ],
    )

    assert [position.pos_id for position in positions] == ["pos-1", "pos-2"]
    assert {position.kol_id for position in positions} == {"kol-a"}


def test_map_deepcoin_open_orders_returns_bound_open_orders_only():
    orders = map_deepcoin_open_orders(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "client-1",
                "posSide": "long",
                "state": "live",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "order-2",
                "posSide": "long",
                "state": "filled",
            },
            {
                "instId": "ETH-USDT-SWAP",
                "ordId": "unbound-order",
                "posSide": "short",
                "state": "live",
            },
        ],
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
                client_order_id="client-1",
            )
        ],
    )

    assert len(orders) == 1
    assert orders[0].kol_id == "alice"
    assert orders[0].chat_id == 100
    assert orders[0].source_message_id == 55
    assert orders[0].symbol == "BTC"
    assert orders[0].side == "long"
    assert orders[0].order_id == "client-1"


def test_deepcoin_readonly_account_state_uses_injected_client_and_bindings():
    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "1",
                }
            ]

        def list_open_orders(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-1",
                    "posSide": "long",
                    "state": "live",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    account_state = DeepcoinReadOnlyAccountState(
        client=FakeClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
                pos_id="pos-1",
                order_id="order-1",
            )
        ],
    )

    assert account_state.load_active_positions()[0].pos_id == "pos-1"
    assert account_state.load_open_orders()[0].order_id == "order-1"


def test_trigger_read_exception_is_unavailable_not_false_empty():
    class FailingTriggerClient:
        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            raise RuntimeError("Authorization: Bearer secret")

    account_state = DeepcoinReadOnlyAccountState(
        client=FailingTriggerClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
            )
        ],
    )

    with pytest.raises(DeepcoinSnapshotUnavailable) as raised:
        account_state.load_open_orders()
    assert str(raised.value) == "trigger_orders_pending_unavailable"
    assert "secret" not in repr(raised.value).lower()


def test_missing_trigger_reader_is_unavailable_not_false_empty():
    class MissingTriggerClient:
        def list_open_orders(self):
            return []

    account_state = DeepcoinReadOnlyAccountState(
        client=MissingTriggerClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
            )
        ],
    )

    with pytest.raises(DeepcoinSnapshotUnavailable) as raised:
        account_state.load_open_orders()
    assert str(raised.value) == "trigger_orders_pending_unavailable"


@pytest.mark.parametrize("collection", ["positions", "open_orders"])
def test_primary_collection_error_is_typed_and_redacted(collection):
    class FailingClient:
        def list_positions(self):
            if collection == "positions":
                raise RuntimeError("DC-ACCESS-KEY private")
            return []

        def list_open_orders(self):
            if collection == "open_orders":
                raise RuntimeError("Authorization: Bearer private")
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    account_state = DeepcoinReadOnlyAccountState(
        client=FailingClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
            )
        ],
    )

    call = (
        account_state.load_active_positions
        if collection == "positions"
        else account_state.load_open_orders
    )
    with pytest.raises(DeepcoinSnapshotUnavailable) as raised:
        call()
    assert str(raised.value) == f"{collection}_unavailable"
    assert "private" not in repr(raised.value).lower()


def test_primary_collection_at_limit_without_pagination_proof_is_unavailable():
    class FullPageClient:
        def list_open_orders(self):
            return [{"ordId": f"order-{index}"} for index in range(100)]

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    account_state = DeepcoinReadOnlyAccountState(
        client=FullPageClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
            )
        ],
    )

    with pytest.raises(DeepcoinSnapshotUnavailable) as raised:
        account_state.load_open_orders()
    assert str(raised.value) == "open_orders_unavailable"


@pytest.mark.parametrize(
    ("collection", "expected_code"),
    [
        ("positions", "positions_unavailable"),
        ("open_orders", "open_orders_unavailable"),
        ("trigger_orders", "trigger_orders_pending_unavailable"),
    ],
)
def test_account_state_prefers_raw_reader_and_rejects_hidden_pagination(
    collection, expected_code
):
    class PaginatedClient:
        def read_positions(self):
            if collection == "positions":
                return {"data": [], "nextPageCursor": "page-2"}
            return {"data": []}

        def list_positions(self):
            return []

        def read_open_orders(self):
            if collection == "open_orders":
                return {"data": [], "nextPageCursor": "page-2"}
            return {"data": []}

        def list_open_orders(self):
            return []

        def read_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            if collection == "trigger_orders":
                return {"data": [], "nextPageCursor": "page-2"}
            return {"data": []}

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    account_state = DeepcoinReadOnlyAccountState(
        client=PaginatedClient(),
        bindings=[
            DeepcoinOrderBinding(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
            )
        ],
    )

    call = (
        account_state.load_active_positions
        if collection == "positions"
        else account_state.load_open_orders
    )
    with pytest.raises(DeepcoinSnapshotUnavailable) as raised:
        call()
    assert str(raised.value) == expected_code
