"""Pure construction and validation for exact-position backup stop triggers."""

from __future__ import annotations

from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR


class BackupStopError(ValueError):
    """Raised when an exact-position backup stop would be unsafe or invalid."""


def calculate_backup_stop_price(
    *,
    primary_stop: str | float | Decimal,
    side: str,
    price_tick: str | float | Decimal,
    buffer_bps: str | float | Decimal = 50,
) -> str:
    """Move a stop farther into loss, rounded conservatively to contract tick."""

    normalized_side = _side(side)
    primary = _positive_decimal(primary_stop, label="primary stop")
    tick = _positive_decimal(price_tick, label="price tick")
    buffer = _positive_decimal(buffer_bps, label="backup-stop buffer")
    ratio = buffer / Decimal("10000")
    candidate = primary * (Decimal("1") - ratio if normalized_side == "long" else Decimal("1") + ratio)
    rounding = ROUND_FLOOR if normalized_side == "long" else ROUND_CEILING
    return _decimal_text((candidate / tick).to_integral_value(rounding=rounding) * tick)


def build_backup_stop_trigger_payload(
    *,
    instrument_id: str,
    side: str,
    margin_mode: str,
    pos_id: str,
    primary_stop: str | float | Decimal,
    backup_stop: str | float | Decimal,
    liquidation_price: str | float | Decimal,
    size: str | float | Decimal,
    client_order_id: str,
) -> dict[str, str]:
    """Build one conditional market close bound to exactly one split position."""

    normalized_side = _side(side)
    normalized_instrument = str(instrument_id or "").strip().upper()
    normalized_pos_id = str(pos_id or "").strip()
    normalized_client_id = str(client_order_id or "").strip()
    normalized_margin = str(margin_mode or "").strip().lower()
    if not normalized_instrument or not normalized_pos_id or not normalized_client_id:
        raise BackupStopError("instrument, exact position, and client order id are required")
    if normalized_margin not in {"cross", "isolated"}:
        raise BackupStopError("margin mode must be cross or isolated")
    primary = _positive_decimal(primary_stop, label="primary stop")
    backup = _positive_decimal(backup_stop, label="backup stop")
    liquidation = _positive_decimal(liquidation_price, label="liquidation price")
    close_size = _positive_decimal(size, label="close size")
    if normalized_side == "long":
        if backup >= primary:
            raise BackupStopError("backup stop must be on the long risk side of primary stop")
        if backup <= liquidation:
            raise BackupStopError("backup stop must remain safely before long liquidation")
        close_side = "sell"
    else:
        if backup <= primary:
            raise BackupStopError("backup stop must be on the short risk side of primary stop")
        if backup >= liquidation:
            raise BackupStopError("backup stop must remain safely before short liquidation")
        close_side = "buy"
    return {
        "instId": normalized_instrument,
        "productGroup": "Swap",
        "side": close_side,
        "posSide": normalized_side,
        "mrgPosition": "split",
        "tdMode": normalized_margin,
        "closePosId": normalized_pos_id,
        "orderType": "market",
        "sz": _decimal_text(close_size),
        "triggerPrice": _decimal_text(backup),
        "triggerPxType": "last",
        "price": "-1",
        "clOrdId": normalized_client_id,
    }


def _side(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"long", "short"}:
        raise BackupStopError("side must be long or short")
    return normalized


def _positive_decimal(value: str | float | Decimal, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BackupStopError(f"{label} must be positive") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BackupStopError(f"{label} must be positive")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
