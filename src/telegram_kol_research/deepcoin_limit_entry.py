"""The ordinary-``order`` payload for a plain limit entry leg, and the rule for
deciding which legs may use it.

Phase 5 moves the entry limit leg off ``POST /deepcoin/trade/trigger-order``
onto ``POST /deepcoin/trade/order``. Both the payload shape and the migration
rule come from the controlled live experiment of 2026-09-07, whose evidence is
on the server under
``/var/lib/telegram-kol-cutover-evidence/rest-ws-phase-5/`` (``findings-final.md``,
``findings-retest6.md``, ``findings-retest-1-3-11.md``).

Two results from that experiment are load-bearing here.

**No ``clOrdId``.** Four single-variable cells, same account, same day, nine of
the ten fields byte-identical, each cell at its own price so a price-scoped
dedupe key could not make one cell look like a repeat of another:

===  ==============================  ============  ==========================
cell  shape                           ``clOrdId``  result
===  ==============================  ============  ==========================
6a    one order                       absent        accepted
6b    one order                       present       ``sCode=14 DuplicateAction``
6c    two concurrent, identical       absent        both accepted
6d    two concurrent, identical       present       both ``sCode=14``
===  ==============================  ============  ==========================

So the rejection follows the presence of the field itself: not concurrency
(6b alone was rejected, 6c concurrently was not), not the order's economics
(6c's two orders matched on side, size, price and both protection prices and
were still both accepted), and not the value (three distinct legal values were
all rejected). It is judged per order rather than per batch.

**This is specific to the limit path.** Production market entries carry
``clOrdId`` and 149 of them have been accepted. :func:`build_deepcoin_market_order_payload`
must keep sending it; only the limit payload built here leaves it out.

Because there is no client id, the exchange ``ordId`` in the accepted response
is the only identity this order ever has, and the ``posId`` beside it in that
same response body is the only place REST ever links an order to a position.
Both have to be persisted when the response arrives; nothing can re-derive them
later.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# Exactly the fields the ordinary-order creation contract documents for a limit
# order plus its attached protection, and exactly the set cell 6a submitted and
# had accepted. A field added here without an experiment behind it is a guess.
LIMIT_ENTRY_PAYLOAD_FIELDS = frozenset(
    {
        "instId",
        "tdMode",
        "mrgPosition",
        "side",
        "posSide",
        "ordType",
        "px",
        "sz",
        "tpTriggerPx",
        "slTriggerPx",
    }
)

# Vocabulary that belongs to trigger-order and must never appear on the ordinary
# order. ``slOrdPx``/``tpOrdPx`` in particular: the creation contract does not
# list them, and docs/2026-09-05-order-tpsl-fields-and-test.md records that
# carrying trigger-order's ``slOrdPx=-1`` across is an untested widening.
FORBIDDEN_LIMIT_ENTRY_FIELDS = frozenset(
    {
        "clOrdId",
        "isCrossMargin",
        "orderType",
        "price",
        "productGroup",
        "slOrdPx",
        "tag",
        "tpOrdPx",
        "triggerPrice",
        "triggerPxType",
    }
)

# Substrings that mark a key as carrying trigger semantics. A leg naming any of
# them with a meaningful value keeps using trigger-order.
_TRIGGER_SEMANTIC_TOKENS = (
    "trigger",
    "condition",
    "breakout",
    "pullback",
    "activation",
    "price_source",
    "px_type",
)

# Keys whose value is a price: a trigger price equal to the limit price carries
# no trigger semantics, which is the whole shape of today's limit legs.
_PRICE_KEY_TOKENS = ("price", "px")


class DeepcoinLimitEntryError(ValueError):
    """The leg cannot be expressed as an ordinary limit order."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _same_price(left: Any, right: Any) -> bool:
    left_value, right_value = _decimal(left), _decimal(right)
    return left_value is not None and right_value is not None and left_value == right_value


def limit_leg_requires_trigger_order(
    leg: dict[str, Any],
    draft: dict[str, Any] | None = None,
) -> str | None:
    """Return why this leg must stay on trigger-order, or ``None`` to migrate.

    Phase 5 migrates only the entry limit leg whose trigger price is identical
    to its limit price and which therefore carries no trigger semantics at all
    -- which is every limit leg the current draft builder produces, since
    :func:`build_deepcoin_trigger_order_payload` sets ``triggerPrice`` to the
    limit price unconditionally.

    A leg with a real breakout or pullback condition, or one that selects a
    ``last``/``mark``/``index`` price source, keeps its trigger-order path and
    its separate parent/child attribution flow. The check is deliberately
    written so that a leg shape nobody has seen yet refuses rather than
    migrates: an unrecognised key naming a trigger concept is a reason to stay,
    not a reason to guess.
    """

    order_type = str(leg.get("order_type") or "").lower()
    if order_type != "limit":
        return "not_a_plain_limit_leg"

    price = leg.get("price")
    if _decimal(price) is None or _decimal(price) <= 0:
        return "leg_price_not_usable_as_limit_price"

    sources: list[tuple[str, dict[str, Any]]] = [("leg", leg)]
    if isinstance(draft, dict):
        sources.append(("draft", draft))
    for source_name, source in sources:
        for key, value in source.items():
            lowered = str(key).lower()
            if not any(token in lowered for token in _TRIGGER_SEMANTIC_TOKENS):
                continue
            if value in (None, "", [], {}, 0):
                continue
            if any(token in lowered for token in _PRICE_KEY_TOKENS) and _same_price(
                value, price
            ):
                # triggerPrice == price is the identity case being migrated.
                continue
            return f"{source_name}_field_{lowered}_carries_trigger_semantics"
    return None


def build_deepcoin_limit_entry_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
    *,
    margin_mode: str,
    position_mode: str,
    stop_loss: float | int | str,
    take_profit: float | int | str | None = None,
) -> dict[str, Any]:
    """Build the ordinary-order payload for one plain limit entry leg.

    ``margin_mode`` and ``position_mode`` arrive already normalised to
    Deepcoin's vocabulary so this module never has to reimplement that mapping.
    ``clOrdId`` is absent on purpose; see the module docstring.
    """

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or isinstance(quantity, bool) or quantity <= 0:
        raise DeepcoinLimitEntryError("non_positive_quantity")
    price = leg.get("price")
    if not isinstance(price, int | float) or isinstance(price, bool) or price <= 0:
        raise DeepcoinLimitEntryError("non_positive_price")
    stop_loss_value = _decimal(stop_loss)
    if stop_loss_value is None or stop_loss_value <= 0:
        raise DeepcoinLimitEntryError("missing_stop_loss_for_protection")

    position_side = str(leg["position_side"]).lower()
    if position_side not in {"long", "short"}:
        raise DeepcoinLimitEntryError("unsupported_position_side")
    side = str(leg["side"]).lower()
    if side not in {"buy", "sell"}:
        raise DeepcoinLimitEntryError("unsupported_side")

    payload: dict[str, Any] = {
        "instId": str(draft["instrument_id"]),
        "tdMode": margin_mode,
        "mrgPosition": position_mode,
        "side": side,
        "posSide": position_side,
        "ordType": "limit",
        "px": str(price),
        "sz": str(quantity),
        "slTriggerPx": str(stop_loss),
    }
    # The stop is required; the take profit is optional and, when present, must
    # sit on the profitable side of the entry. A protection price on the wrong
    # side would arm immediately, so refuse rather than send it.
    price_value = _decimal(price)
    if (position_side == "long" and stop_loss_value >= price_value) or (
        position_side == "short" and stop_loss_value <= price_value
    ):
        raise DeepcoinLimitEntryError("stop_loss_on_wrong_side_of_entry")
    take_profit_value = _decimal(take_profit)
    if take_profit is not None and take_profit_value is None:
        raise DeepcoinLimitEntryError("invalid_take_profit_for_protection")
    if take_profit_value is not None:
        if take_profit_value <= 0:
            raise DeepcoinLimitEntryError("invalid_take_profit_for_protection")
        if (position_side == "long" and take_profit_value <= price_value) or (
            position_side == "short" and take_profit_value >= price_value
        ):
            raise DeepcoinLimitEntryError("take_profit_on_wrong_side_of_entry")
        payload["tpTriggerPx"] = str(take_profit)

    unexpected = set(payload) - LIMIT_ENTRY_PAYLOAD_FIELDS
    if unexpected:
        raise DeepcoinLimitEntryError(
            "unexpected_limit_entry_fields:" + ",".join(sorted(unexpected))
        )
    return payload
