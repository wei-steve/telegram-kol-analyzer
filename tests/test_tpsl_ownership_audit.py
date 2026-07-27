from types import SimpleNamespace

from telegram_kol_research.tpsl_ownership_audit import (
    build_tpsl_ownership_audit,
)


def _position(pos_id: str, *, side: str = "long") -> dict[str, str]:
    return {
        "posId": pos_id,
        "instId": "BTC-USDT-SWAP",
        "posSide": side,
        "pos": "1",
    }


def _order(
    order_id: str,
    *,
    pos_id: str | None = None,
    side: str = "long",
    price: str = "60000",
    size: str = "0",
    created_at: str = "1000",
) -> dict[str, str]:
    row = {
        "ordId": order_id,
        "triggerOrderType": "TPSL",
        "instId": "BTC-USDT-SWAP",
        "posSide": side,
        "sz": size,
        "slTriggerPrice": price,
        "cTime": created_at,
    }
    if pos_id is not None:
        row["posId"] = pos_id
    return row


def _ledger(order_id: str, pos_id: str, *, status: str = "verified"):
    return SimpleNamespace(
        venue="deepcoin",
        order_id=order_id,
        pos_id=pos_id,
        status=status,
        purpose="stop_loss",
    )


def test_account_audit_classifies_every_pending_tpsl_without_guessing():
    positions = [
        _position("pos-a"),
        _position("pos-b"),
        _position("pos-c", side="short"),
    ]
    pending = [
        _order("owned-a"),
        _order("owned-b", price="61000", size="7", created_at="999999"),
        _order("owned-c", side="short"),
        _order("manual-1"),
        _order("conflict-1", pos_id="pos-b"),
    ]
    ledger = [
        _ledger("owned-a", "pos-a"),
        _ledger("owned-b", "pos-b"),
        _ledger("owned-c", "pos-c"),
        _ledger("conflict-1", "pos-a"),
        _ledger("old-1", "pos-a"),
    ]

    report = build_tpsl_ownership_audit(
        positions=positions,
        pending_orders=pending,
        ledger_rows=ledger,
    )

    assert report.live_position_count == 3
    assert report.pending_tpsl_count == 5
    assert report.owned_pending_count == 3
    assert report.owned_pending_order_ids == ("owned-a", "owned-b", "owned-c")
    assert report.unowned_pending_order_ids == ("manual-1",)
    assert report.conflicts[0].order_id == "conflict-1"
    assert report.conflicts[0].reason == "exchange_position_conflicts_with_ledger"
    assert report.stale_ledger_order_ids == ("old-1",)
    assert report.exchange_write_count == 0
    assert (
        report.owned_pending_count
        + len(report.unowned_pending_order_ids)
        + len(report.conflicts)
        == report.pending_tpsl_count
    )


def test_account_audit_does_not_use_price_size_side_or_time_as_ownership():
    report = build_tpsl_ownership_audit(
        positions=[_position("pos-a")],
        pending_orders=[
            _order(
                "manual-similar",
                price="60000",
                size="1",
                created_at="1000",
            )
        ],
        ledger_rows=[],
    )

    assert report.owned_pending_count == 0
    assert report.unowned_pending_order_ids == ("manual-similar",)
    assert report.conflicts == ()
