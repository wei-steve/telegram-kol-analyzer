"""Shared fixtures for the phase-3 remediation end-to-end/replay tests.

Reuses the DB-row builders from ``tests/test_position_management_remediation_scope.py``
(``_persist_strategy``/``_persist_failed_step``) -- those already produce a
lifecycle+binding+verified-leg shape the scoped remediation planner treats as
a chain head, and are the "复用其建数据函数" the spec asks for. This module
adds:

* ``WritableRemediationClient``: the same read surface as
  ``tests.test_position_management_remediation_scope._ReadOnlyClient`` (used
  everywhere the remediation planner reads an exchange snapshot) plus real
  write methods (``place_order``/``cancel_order``/``cancel_trigger_order``/
  ``set_position_sltp``/``cancel_position_sltp``/``get_ticker_quote``) so
  ``execute_management_batch`` can actually submit and settle an order
  against it -- nothing here is a stub of the execution path itself.
* ``build_ready_full_exit_target`` and friends: end-to-end DB setup that
  satisfies both the remediation plan builder (``build_position_management_remediation_plan``)
  AND the deterministic downstream planner (``plan_strategy_management_batch``,
  which additionally requires a ``RecognitionDecision`` row for the
  triggering raw message -- ``strategy_management_planner._load_exact_identity``
  refuses ``authoritative_management_candidate_not_found`` without one).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from telegram_kol_research.models import ExecutionOrderLeg, RecognitionDecision

from tests.test_position_management_remediation_scope import (
    _persist_failed_step,
    _persist_strategy,
    _ReadOnlyClient,
)

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)

#: resolve_management_directive() reads message *text*, not the candidate's
#: structured management_action field -- each intent needs its own realistic
#: Chinese phrasing (mirrors tests/test_oncall_remediation.py's _ACTION_TEXT).
ACTION_TEXT = {
    "full_exit": "{symbol}{side_zh}单全部平仓",
    "partial_take_profit": "{symbol}{side_zh}单止盈一部分",
    "move_stop_to_break_even": "{symbol}{side_zh}单止损移到成本价",
    "adjust_stop_loss": "{symbol}{side_zh}单止损上移",
    "adjust_stop_loss_widen": "{symbol}{side_zh}单止损下移",
    "cancel_entry": "{symbol}{side_zh}单取消入场",
}

_SIDE_ZH = {"long": "多", "short": "空"}


class WritableRemediationClient(_ReadOnlyClient):
    """A same-shaped, but *writable*, fake Deepcoin client.

    Every read method is inherited unchanged from ``_ReadOnlyClient`` (used
    by ``build_position_management_remediation_plan`` and by the downstream
    reconciliation/planning snapshot loader in ``execution_bindings.py``).
    Write methods below are real enough that ``execute_management_batch``
    (``strategy_management_executor.py``) drives a real order lifecycle
    against them -- there is no monkeypatching of the executor itself.
    """

    def __init__(self, positions=None, *, pending=None, quote_price="64000"):
        super().__init__(positions=positions)
        # ``pending`` doubles as both "pending TPSL orders" (protection
        # actions) and the thing list_trigger_orders_pending serves.
        self.pending = list(pending or [])
        self.open_orders_list: list[dict] = []
        self.close_calls: list[dict] = []
        self.cancel_trigger_calls: list[dict] = []
        self.cancel_order_calls: list[dict] = []
        self.set_calls: list[dict] = []
        self.cancel_sltp_calls: list[dict] = []
        self.call_log: list[tuple[str, dict]] = []
        self._close_counter = 0
        self._set_counter = 0
        self.quote = {
            "instrument_id": (self.positions[0]["instId"] if self.positions else "BTC-USDT-SWAP"),
            "price": quote_price,
            "price_field": "last",
            "observed_at": NOW.isoformat(),
        }

    # -- reads -------------------------------------------------------
    def list_positions(self, *, inst_id=None):
        if inst_id is None:
            return list(self.positions)
        return [row for row in self.positions if row.get("instId") == inst_id]

    def list_trigger_orders_pending(self, *, inst_id):
        self.requested_instruments.append(inst_id)
        return [dict(row) for row in self.pending if row.get("instId") == inst_id]

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": self.list_trigger_orders_pending(inst_id=inst_id)}

    def list_open_orders(self, *, inst_id=None):
        return [dict(row) for row in self.open_orders_list]

    def get_ticker_quote(self, *, inst_id):
        if self.quote is None:
            return None
        # The management stop gate checks quote freshness against the real
        # wall clock (management_stop_price_gate.stop_gate_clock), not the
        # test's business "now", so a live quote is stamped when it is read.
        quote = dict(self.quote)
        quote["observed_at"] = datetime.now(UTC).isoformat()
        return quote

    # -- writes -------------------------------------------------------
    def place_order(self, payload):
        self._close_counter += 1
        ord_id = f"close-{self._close_counter}"
        request = dict(payload)
        self.close_calls.append(request)
        self.call_log.append(("place_order", request))
        return {"code": "0", "data": {"ordId": ord_id}}

    def cancel_order(self, payload):
        request = dict(payload)
        self.cancel_order_calls.append(request)
        self.call_log.append(("cancel_order", request))
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def cancel_trigger_order(self, payload):
        request = dict(payload)
        self.cancel_trigger_calls.append(request)
        self.call_log.append(("cancel_trigger_order", request))
        self.pending = [
            row for row in self.pending if str(row.get("ordId")) != str(payload.get("ordId"))
        ]
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def cancel_position_sltp(self, payload):
        request = dict(payload)
        self.cancel_sltp_calls.append(request)
        self.call_log.append(("cancel_position_sltp", request))
        self.pending = [
            row for row in self.pending if str(row.get("ordId")) != str(payload.get("ordId"))
        ]
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def set_position_sltp(self, payload):
        self._set_counter += 1
        request = dict(payload)
        self.set_calls.append(request)
        self.call_log.append(("set_position_sltp", request))
        order_id = f"new-sltp-{self._set_counter}"
        row = {
            "ordId": order_id,
            "instId": payload["instId"],
            "posId": payload["posId"],
            "posSide": payload["posSide"],
            "sz": payload.get("sz", "0"),
        }
        if payload.get("slTriggerPx") not in (None, ""):
            row["slTriggerPx"] = payload["slTriggerPx"]
        elif payload.get("tpTriggerPx") not in (None, ""):
            row["tpTriggerPx"] = payload["tpTriggerPx"]
        self.pending.append(row)
        return {"code": "0", "data": {"ordId": order_id}}


def build_ready_remediation_target(
    session_factory,
    *,
    action_kind="full_exit",
    event_type=None,
    chat_id=88,
    strategy_message_id=200,
    raw_chat_message_id=300,
    symbol="BTC",
    side="long",
    pos_id="pos-a",
    size="1",
    avg_px="64000",
    posted_at=NOW,
    item_status="failed",
    current_stop_loss_text=None,
    action_text=None,
    verified_stop_order_id=None,
    verified_stop_price="62000",
):
    """Build one entered lifecycle + one failed management instruction item.

    Satisfies both ``build_position_management_remediation_plan`` (via the
    reused scope fixtures) and ``plan_strategy_management_batch`` (adds the
    ``RecognitionDecision`` row the deterministic planner separately
    requires for the *same* raw_message_id).

    Also attaches one *verified* stop-loss row to the protection ledger --
    every entered production position carries one, and the deterministic
    planner refuses ``target_protection_not_verified`` for any risk-reducing
    intent other than ``full_exit``/``move_stop_to_break_even`` without it
    (mirrors tests/test_strategy_management_planner.py's partial_take_profit
    branch of ``test_risk_reducing_management_uses_proven_frozen_spec_when_current_spec_is_stale``).
    Pass ``verified_stop_order_id=None`` (the default) to attach one
    automatically; the caller must feed the matching pending-TPSL row into
    the exchange client itself (``client_for(..., pending=[...])`` or
    ``WritableRemediationClient(pending=[...])``) -- returned as element 6.

    Returns ``(raw_id, lifecycle_id, strategy_instance_id, pos_id, symbol,
    side, pending_stop_order)``.
    """

    if event_type is None:
        event_type = "close_signal" if action_kind == "full_exit" else "position_update"

    binding_id, lifecycle_id, strategy_id = _persist_strategy(
        session_factory,
        chat_id=chat_id,
        message_id=strategy_message_id,
        symbol=symbol,
        side=side,
        pos_id=pos_id,
    )
    side_zh = _SIDE_ZH.get(side, "?")
    text = (action_text or ACTION_TEXT.get(action_kind, "{symbol}多单全部平仓")).format(
        symbol=symbol, side_zh=side_zh
    )
    raw_id, candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=posted_at,
        chat_id=chat_id,
        message_id=raw_chat_message_id,
        text=text,
        event_type=event_type,
        management_action=action_kind,
        target_lifecycle_id=lifecycle_id,
        status=item_status,
    )
    with session_factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        if current_stop_loss_text is not None or action_kind.startswith("adjust_stop_loss"):
            from telegram_kol_research.models import SignalCandidate

            candidate = session.get(SignalCandidate, candidate_id)
            candidate.stop_loss_text = current_stop_loss_text or "62500"
            # management_stop_price_gate._evaluate_stop_gate: an explicit
            # stop price is only accepted with provenance
            # "current_message_text" (proves the number came from the KOL's
            # own message, not e.g. a signature/phone number/timestamp that
            # happens to parse as a price) -- otherwise
            # "management_stop_provenance_invalid".
            candidate.stop_price_source = "current_message_text"
            session.add(candidate)
        # ``_persist_strategy`` (test_position_management_remediation_scope)
        # leaves the entry leg's attribution_evidence_json unset -- fine for
        # the remediation plan builder (it only checks attribution_status),
        # but the *deterministic downstream planner*
        # (strategy_management_planner._entry_leg_management_plan /
        # position ownership checks) additionally requires authoritative
        # evidence or refuses "position_ownership_evidence_not_authoritative".
        leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id=pos_id)
            .one()
        )
        leg.attribution_evidence_json = json.dumps(
            {"policy_version": 2, "source": "direct_order_identity"}
        )
        session.add(leg)

        # A normally-entered production binding carries its own order draft
        # (with the contract spec used to size the entry) in payload_json --
        # that is what lets risk-reducing management always resolve a
        # "frozen_binding_draft" contract spec (deepcoin_execution_actions.
        # resolve_existing_position_contract_spec, risk_reducing=True tries
        # this before ever consulting contract_spec_provider). This matters
        # here because oncall_remediation.execute_proposal calls
        # apply_position_management_remediation_action without a
        # contract_spec_provider at all (its default is None) -- so a real
        # remediation execution in production depends entirely on this
        # frozen draft being present, never on a live provider.
        session.commit()

    with session_factory() as session:
        from telegram_kol_research.models import ExecutionBinding as _Binding

        binding = session.get(_Binding, binding_id)
        instrument_id = f"{symbol}-USDT-SWAP"
        draft = {
            "strategy_instance_id": binding.strategy_instance_id,
            "instrument_id": instrument_id,
            "symbol": symbol,
            "position_mode": binding.position_mode,
            "margin_mode": binding.margin_mode,
            "source": {"chat_id": chat_id, "message_id": strategy_message_id},
            "order_legs": [{"position_side": side, "client_order_id": f"entry-{pos_id}"}],
            "contract_spec": {
                "instrument_id": instrument_id,
                "contract_value": 0.001,
                "quantity_step": 1,
                "min_quantity": 1,
                "price_tick": 0.1,
            },
            "contract_spec_snapshot": {
                "source_digest_sha256": "a" * 64,
                "fetched_at": "2026-09-01T00:00:00+00:00",
                "expires_at": "2027-09-01T00:00:00+00:00",
            },
        }
        binding.payload_json = json.dumps({"draft": draft}, sort_keys=True)
        session.add(binding)
        from telegram_kol_research.models import RecoveryOrderConfirmation

        session.add(
            RecoveryOrderConfirmation(
                kol_id=binding.kol_id,
                chat_id=chat_id,
                message_id=strategy_message_id,
                symbol=symbol,
                side=side,
                venue="deepcoin",
                status="ready_confirmed",
                confirmation_payload_json=json.dumps(
                    {
                        "source": {
                            "chat_id": chat_id,
                            "message_id": strategy_message_id,
                            "symbol": symbol,
                            "side": side,
                        },
                        "deepcoin_order_draft": draft,
                    },
                    sort_keys=True,
                ),
                confirmed_at=NOW,
            )
        )
        session.commit()

    stop_order_id = verified_stop_order_id or f"stop-{pos_id}"
    with session_factory() as session:
        from telegram_kol_research.protection_ledger import upsert_protection_ledger_row

        leg_id = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id=pos_id)
            .one()
            .id
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id=strategy_id,
            pos_id=pos_id,
            instrument_id=f"{symbol}-USDT-SWAP",
            side=side,
            order_id=stop_order_id,
            purpose="stop_loss",
            trigger_price=verified_stop_price,
            size_text=size,
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=NOW,
        )
        session.commit()

    pending_stop_order = {
        "instId": f"{symbol}-USDT-SWAP",
        "posSide": side,
        "triggerOrderType": "TPSL",
        "slTriggerPx": verified_stop_price,
        "sz": size,
        "ordId": stop_order_id,
        "posId": pos_id,
        "cTime": "1721000000000",
    }
    return raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop_order


def add_followup_message(
    session_factory,
    *,
    lifecycle_id,
    action_kind,
    chat_id=88,
    message_id,
    symbol="BTC",
    posted_at=NOW,
    item_status="failed",
    event_type=None,
    action_text=None,
    side="long",
):
    """Add a *second* management raw message targeting an existing lifecycle.

    For the "prior batch unresolved" replay shape (spec 9 item 10): the
    first message's own remediation (or its original execution) may still be
    outstanding when a second message arrives for the same lifecycle. Reuses
    ``_persist_failed_step`` directly (bypassing ``_persist_strategy``, which
    would create a *second*, unrelated binding) plus a
    ``RecognitionDecision`` row, exactly like ``build_ready_remediation_target``
    does for the first message.

    Returns ``raw_id``.
    """

    if event_type is None:
        event_type = "close_signal" if action_kind == "full_exit" else "position_update"
    side_zh = _SIDE_ZH.get(side, "?")
    text = (action_text or ACTION_TEXT.get(action_kind, "{symbol}多单全部平仓")).format(
        symbol=symbol, side_zh=side_zh
    )
    raw_id, _candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=posted_at,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        event_type=event_type,
        management_action=action_kind,
        target_lifecycle_id=lifecycle_id,
        status=item_status,
    )
    with session_factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.commit()
    return raw_id


def client_for(symbol, side, pos_id, *, size="1", avg_px="64000", pending=None, quote_price=None):
    return WritableRemediationClient(
        positions=[
            {
                "instId": f"{symbol}-USDT-SWAP",
                "posId": pos_id,
                "posSide": side,
                "pos": size,
                "avgPx": avg_px,
                "cTime": "1000",
                # The remediation plan builder only needs identity/economics
                # fields, but the deterministic downstream planner
                # (strategy_management_planner) additionally requires margin
                # mode / position mode to resolve "target_live_position_mode_unavailable".
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
        ],
        pending=pending,
        quote_price=quote_price or avg_px,
    )
