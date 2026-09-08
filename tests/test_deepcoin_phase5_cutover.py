"""Phase 5: the entry limit leg moves to the ordinary order, and what guards it.

Three things become true at the cutover and each is checked here on its own:

* the position an ordinary entry opened is claimed only when the identity
  equation *and* all three confirmations hold, and an ``unverified`` leg is one
  no automatic action may touch;
* a new entry is not submitted at all while the WebSocket observation cannot
  vouch for it, and the refusal names the stream state;
* a leg carrying real trigger semantics still goes to ``trigger-order``, which
  is also what makes rolling back to the previous SHA a complete rollback.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import (
    DeepcoinCredentials,
    DeepcoinDefiniteRejection,
    DeepcoinRestClient,
)
from telegram_kol_research.deepcoin_entry_admission import (
    DeepcoinEntryAdmissionBlocked,
    ROLES_REQUIRING_WS_OBSERVATION,
    require_ws_observation_permits_new_entry,
    set_entry_admission_inbox_provider,
    set_entry_admission_runtime_role,
    ws_observation_admits_new_entry,
)
from telegram_kol_research.deepcoin_limit_entry import (
    build_deepcoin_limit_entry_payload,
    limit_leg_requires_trigger_order,
)
from telegram_kol_research.deepcoin_ordinary_entry_binding import (
    ATTRIBUTION_UNVERIFIED,
    ATTRIBUTION_VERIFIED,
    resolve_ordinary_entry_attribution,
)
from telegram_kol_research.deepcoin_ws_stream_state import (
    WS_STATE_HEALTHY,
    DeepcoinWsStreamStateMachine,
)
from telegram_kol_research.models import DeepcoinWsEvent
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_verified_position_ownership,
)
from telegram_kol_research.recovery_live_submit import (
    MARKET_FILL_ATTRIBUTION_INCIDENT_TYPE,
    _require_ws_observation_or_raise,
    build_deepcoin_trigger_order_payload,
)

ORD_ID = "1001125145471184"
INST_ID = "ETH-USDT-SWAP"


# --------------------------------------------------------------------------
# The identity equation and its triple confirmation
# --------------------------------------------------------------------------


def _position_frame(session_factory, *, pos_id, size="0.2"):
    payload = json.dumps(
        {
            "result": [
                {"table": "Position", "data": {"PI": str(pos_id), "Po": str(size)}}
            ]
        },
        sort_keys=True,
    )
    with session_factory() as session:
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="Position",
                action="PushPosition",
                position_id=str(pos_id),
                received_at=datetime(2026, 9, 7, 12, 0),
                received_ms=1_788_000_000_000,
                raw_payload=payload,
                payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            )
        )
        session.commit()


class _PositionsClient:
    def __init__(self, rows, *, fail=False):
        self.rows = rows
        self.fail = fail
        self.calls = 0

    def list_positions(self, *, inst_id=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("positions unreadable")
        return self.rows


def _resolve(session_factory, client, *, size="0.2", side="long", attempts=1):
    return resolve_ordinary_entry_attribution(
        session_factory,
        deepcoin_client=client,
        ord_id=ORD_ID,
        inst_id=INST_ID,
        expected_position_side=side,
        expected_size=size,
        attempts=attempts,
        sleep=lambda _seconds: None,
    )


def test_all_three_confirmations_together_make_one_verified_attribution(tmp_path):
    session_factory = create_session_factory(tmp_path / "attribution.db")
    _position_frame(session_factory, pos_id=ORD_ID)
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "0.2"}]
    )

    outcome = _resolve(session_factory, client)

    assert (outcome.pos_id, outcome.status) == (ORD_ID, ATTRIBUTION_VERIFIED)
    assert outcome.reason == ""
    assert outcome.evidence["equation"] == "split_pos_id_equals_ordinary_ord_id"
    assert outcome.is_verified is True


def test_without_the_stream_frame_the_equation_alone_claims_nothing(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-frame.db")
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "0.2"}]
    )

    outcome = _resolve(session_factory, client)

    assert (outcome.pos_id, outcome.status) == (None, ATTRIBUTION_UNVERIFIED)
    assert outcome.reason == "no_ws_position_frame_for_pos_id"
    # And no REST read was spent on a question its answer could not settle --
    # the read quota is shared with the protection loops.
    assert client.calls == 0


def test_a_position_frame_that_never_opened_is_not_an_opened_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "zero-size.db")
    # ``PI`` says an id was allocated; only a non-zero ``Po`` says the position
    # actually opened. Cells 3, 6a, 6c and v all produced the former alone.
    _position_frame(session_factory, pos_id=ORD_ID, size="0")
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "0.2"}]
    )

    outcome = _resolve(session_factory, client)

    assert outcome.reason == "ws_position_never_opened"
    assert outcome.pos_id is None


def test_rest_must_corroborate_the_position_the_stream_named(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-rest.db")
    _position_frame(session_factory, pos_id=ORD_ID)
    client = _PositionsClient([{"posId": "some-other-position", "instId": INST_ID}])

    outcome = _resolve(session_factory, client)

    assert outcome.reason == "rest_pos_id_not_confirmed_by_rest"
    assert outcome.pos_id is None


def test_a_direction_that_disagrees_refuses_rather_than_claims(tmp_path):
    session_factory = create_session_factory(tmp_path / "wrong-side.db")
    _position_frame(session_factory, pos_id=ORD_ID)
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "short", "pos": "0.2"}]
    )

    outcome = _resolve(session_factory, client, side="long")

    assert outcome.reason == "rest_pos_side_mismatch"
    assert outcome.pos_id is None


def test_a_position_larger_than_the_order_is_not_this_order_s_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "wrong-size.db")
    _position_frame(session_factory, pos_id=ORD_ID, size="5")
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "5"}]
    )

    outcome = _resolve(session_factory, client, size="0.2")

    assert outcome.reason == "rest_pos_size_mismatch"
    assert outcome.pos_id is None


def test_a_partial_fill_is_still_this_order_s_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial.db")
    _position_frame(session_factory, pos_id=ORD_ID, size="0.1")
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "0.1"}]
    )

    outcome = _resolve(session_factory, client, size="0.2")

    assert (outcome.pos_id, outcome.status) == (ORD_ID, ATTRIBUTION_VERIFIED)


def test_an_unreadable_rest_read_is_unknown_and_never_zero(tmp_path):
    session_factory = create_session_factory(tmp_path / "unreadable.db")
    _position_frame(session_factory, pos_id=ORD_ID)
    client = _PositionsClient([], fail=True)

    outcome = _resolve(session_factory, client)

    assert outcome.reason == "rest_positions_unreadable"
    assert outcome.pos_id is None


def test_retries_are_for_timing_and_stop_at_the_first_complete_answer(tmp_path):
    session_factory = create_session_factory(tmp_path / "timing.db")
    _position_frame(session_factory, pos_id=ORD_ID)
    client = _PositionsClient(
        [{"posId": ORD_ID, "instId": INST_ID, "posSide": "long", "pos": "0.2"}]
    )

    outcome = _resolve(session_factory, client, attempts=5)

    assert outcome.is_verified is True
    assert client.calls == 1
    assert outcome.evidence["attempts_used"] == 1


# --------------------------------------------------------------------------
# An unverified attribution authorises nothing
# --------------------------------------------------------------------------


def _leg(session_factory, *, attribution_status, pos_id=ORD_ID, order_id=ORD_ID):
    from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="k",
            chat_id=1,
            message_id=2,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                leg_index=1,
                purpose="entry",
                order_kind="limit",
                venue="deepcoin",
                order_id=order_id,
                pos_id=pos_id,
                status="active",
                attribution_status=attribution_status,
            )
        )
        session.commit()


@pytest.mark.parametrize("status", [ATTRIBUTION_UNVERIFIED, "unassigned"])
def test_an_unverified_leg_authorises_no_automatic_position_action(tmp_path, status):
    session_factory = create_session_factory(tmp_path / f"gate-{status}.db")
    _leg(session_factory, attribution_status=status)

    with session_factory() as session:
        with pytest.raises(PositionAttributionError, match="not_verified"):
            require_verified_position_ownership(
                session, venue="deepcoin", pos_id=ORD_ID
            )


def test_a_verified_leg_is_the_only_thing_that_does(tmp_path):
    session_factory = create_session_factory(tmp_path / "gate-verified.db")
    _leg(session_factory, attribution_status=ATTRIBUTION_VERIFIED)

    with session_factory() as session:
        leg = require_verified_position_ownership(
            session, venue="deepcoin", pos_id=ORD_ID
        )
    assert leg.pos_id == ORD_ID


# --------------------------------------------------------------------------
# The stream has to vouch before a new entry is submitted
# --------------------------------------------------------------------------


class _Inbox:
    def __init__(self, *, state, gaps, resync="converged"):
        self.state_machine = DeepcoinWsStreamStateMachine(
            now_provider=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
            monotonic_ms_provider=lambda: 0,
        )
        self.state_machine.state = state
        self.state_machine.last_resync_outcome = resync
        self._gaps = gaps

    def _open_gap_count(self):
        return self._gaps


@pytest.fixture()
def clean_admission():
    yield
    set_entry_admission_inbox_provider(None)
    set_entry_admission_runtime_role(None)


def test_a_process_that_does_not_run_the_stream_is_not_gated_by_it(clean_admission):
    set_entry_admission_runtime_role("web")
    set_entry_admission_inbox_provider(None)

    assert ws_observation_admits_new_entry() == (True, "")
    _require_ws_observation_or_raise()


@pytest.mark.parametrize("role", sorted(ROLES_REQUIRING_WS_OBSERVATION))
def test_the_role_that_runs_the_stream_needs_an_inbox_to_enter(clean_admission, role):
    set_entry_admission_runtime_role(role)
    set_entry_admission_inbox_provider(None)

    assert ws_observation_admits_new_entry() == (False, "ws_inbox_unavailable")
    with pytest.raises(DeepcoinEntryAdmissionBlocked, match="ws_inbox_unavailable"):
        require_ws_observation_permits_new_entry()


@pytest.mark.parametrize(
    ("state", "gaps", "resync", "reason"),
    [
        ("disconnected", 0, "converged", "disconnected"),
        ("connecting", 0, "converged", "connecting"),
        ("resyncing", 0, "converged", "resyncing"),
        (WS_STATE_HEALTHY, 1, "converged", "open_gap"),
        (WS_STATE_HEALTHY, None, "converged", "gap_state_unknown"),
        (WS_STATE_HEALTHY, 0, "diverged", "no_converged_resync"),
    ],
)
def test_every_incomplete_observation_refuses_and_names_itself(
    clean_admission, state, gaps, resync, reason
):
    set_entry_admission_runtime_role("worker")
    set_entry_admission_inbox_provider(
        lambda: _Inbox(state=state, gaps=gaps, resync=resync)
    )

    assert ws_observation_admits_new_entry() == (False, reason)


def test_a_blocked_entry_is_not_submitted_and_carries_its_reason(clean_admission):
    set_entry_admission_runtime_role("worker")
    set_entry_admission_inbox_provider(
        lambda: _Inbox(state=WS_STATE_HEALTHY, gaps=2, resync="converged")
    )

    # Pausing is "do not submit", never "submit and then cancel", and the reason
    # travels with the refusal instead of the intention being dropped silently.
    from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError

    with pytest.raises(
        RecoveryLiveSubmitError, match="ws_observation_blocked_new_entry:open_gap"
    ):
        _require_ws_observation_or_raise()


def test_a_converged_healthy_stream_is_the_only_thing_that_permits(clean_admission):
    set_entry_admission_runtime_role("worker")
    set_entry_admission_inbox_provider(
        lambda: _Inbox(state=WS_STATE_HEALTHY, gaps=0, resync="converged")
    )

    assert ws_observation_admits_new_entry() == (True, "")
    _require_ws_observation_or_raise()


# --------------------------------------------------------------------------
# The soft rejection that looks like success, and the rollback boundary
# --------------------------------------------------------------------------


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DuplicateActionHttpClient:
    def __init__(self):
        self.requests = []

    def request(self, method, request_path, content="", headers=None):
        self.requests.append((method, request_path))
        # The exact shape the experiment drew from cells 6b and 6d: HTTP 200,
        # outer ``code`` 0, and the rejection only inside ``data[].sCode``.
        return _FakeResponse(
            {
                "code": "0",
                "msg": "",
                "data": [{"ordId": "", "sCode": "14", "sMsg": "DuplicateAction"}],
            }
        )

    def close(self):
        pass


def test_an_outer_code_zero_carrying_scode_14_is_a_rejection_not_a_success():
    http_client = _DuplicateActionHttpClient()
    client = DeepcoinRestClient(
        credentials=DeepcoinCredentials(
            api_key="k", api_secret="s", passphrase="p"
        ),
        http_client=http_client,
    )

    with pytest.raises(DeepcoinDefiniteRejection, match="14: DuplicateAction"):
        client.place_order(
            {
                "instId": INST_ID,
                "tdMode": "cross",
                "mrgPosition": "split",
                "side": "buy",
                "posSide": "long",
                "ordType": "limit",
                "px": "1800",
                "sz": "0.2",
                "slTriggerPx": "1700",
            }
        )
    # A definite rejection is one request, not a retry loop.
    assert len(http_client.requests) == 1


def test_the_rollback_target_s_behaviour_is_still_reachable_for_conditional_legs():
    """Rolling back is a complete rollback because nothing else moved.

    The pre-cutover path -- ``build_deepcoin_trigger_order_payload`` followed by
    ``trigger_order`` -- is untouched by this phase and is still the path every
    leg carrying trigger semantics takes. So deploying the pre-deploy SHA
    restores the previous behaviour for the migrated legs too, with no runtime
    switch to set and no state to unwind.
    """

    draft = {
        "instrument_id": INST_ID,
        "margin_mode": "cross",
        "position_mode": "split",
        "stop_loss": 1700.0,
    }
    conditional = {
        "order_type": "limit",
        "side": "buy",
        "position_side": "long",
        "price": 1800.0,
        "quantity": 0.2,
        "trigger_price": 1850.0,
        "client_order_id": "TK1",
    }

    assert limit_leg_requires_trigger_order(conditional, draft) is not None
    legacy = build_deepcoin_trigger_order_payload(draft, conditional)
    assert legacy["orderType"] == "limit"
    assert legacy["triggerPrice"] == "1800.0"
    assert legacy["triggerPxType"] == "last"
    assert legacy["slOrdPx"] == "-1"
    assert legacy["clOrdId"] == "TK1"

    plain = {**conditional}
    plain.pop("trigger_price")
    assert limit_leg_requires_trigger_order(plain, draft) is None
    migrated = build_deepcoin_limit_entry_payload(
        draft,
        plain,
        margin_mode="cross",
        position_mode="split",
        stop_loss=draft["stop_loss"],
    )
    # The two payloads share no vocabulary beyond the order's economics, which
    # is why one endpoint accepts each and neither is a rename of the other.
    assert "clOrdId" not in migrated
    assert "triggerPrice" not in migrated
    assert "slOrdPx" not in migrated
    assert migrated["ordType"] == "limit"
    assert migrated["px"] == "1800.0"


# --------------------------------------------------------------------------
# The phase 5a guard has to recognise what phase 5 submits
# --------------------------------------------------------------------------


def test_the_open_order_guard_recognises_the_new_limit_entry_as_ours(tmp_path):
    """Phase 5a's guard was written before this order existed; it has to fit.

    ``list_open_orders`` sees a live regular order for the first time now, and
    the guard only lets a cancellation reach a row this system recorded as a
    regular order. A migrated entry is recorded with ``order_kind='limit'`` and
    its exchange ``ordId``, and it carries no ``clOrdId`` at all -- so the match
    has to come from the order id alone, and a stranger's row must still be
    refused rather than swept in by the shared empty client id.
    """

    from telegram_kol_research.open_order_action_guard import guard_regular_open_orders

    session_factory = create_session_factory(tmp_path / "guard.db")
    _leg(session_factory, attribution_status="unassigned", pos_id=None)

    guarded = guard_regular_open_orders(
        session_factory,
        rows=[
            {"ordId": ORD_ID, "instId": INST_ID, "state": "live"},
            {"ordId": "someone-elses-order", "instId": INST_ID, "state": "live"},
            {"ordId": "", "clOrdId": "", "instId": INST_ID, "state": "live"},
        ],
        action="cancel_entry_order",
        instrument_id=INST_ID,
    )

    assert [row["ordId"] for row in guarded.allowed] == [ORD_ID]
    assert {row["ordId"] for row in guarded.blocked} == {"someone-elses-order", ""}


# --------------------------------------------------------------------------
# A market fill nobody can attribute has to be heard about, not just recorded
# --------------------------------------------------------------------------


def test_the_unverified_market_fill_type_survives_a_hand_edited_whitelist():
    """The environment's whitelist may not silence this one.

    Production's ``TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`` is a hand-kept
    comma list on a server. A type that means "a position may be sitting there
    with no stop" cannot depend on someone having remembered to add it.
    ``telegram_notifications_enabled`` is still the one deliberate way to send
    nothing.
    """

    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        load_runtime_incident_config,
    )

    assert MARKET_FILL_ATTRIBUTION_INCIDENT_TYPE in ALWAYS_NOTIFIED_INCIDENT_TYPES

    config = load_runtime_incident_config(
        {
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": (
                "management_partial_failed,severe_protection_incident"
            ),
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "1",
        },
        environment_only=True,
    )
    assert MARKET_FILL_ATTRIBUTION_INCIDENT_TYPE in config.telegram_notification_types
    # And the types that were listed are still there.
    assert "management_partial_failed" in config.telegram_notification_types

    # Absent key keeps meaning "every type", not "only this one".
    assert (
        load_runtime_incident_config({}, environment_only=True)
        .telegram_notification_types
        is None
    )
    # And the key present but empty keeps meaning capture-only. That is someone
    # typing "notify nothing", not someone forgetting a type, so it is honoured
    # as written -- the guarantee here is against omission, not against choice.
    assert (
        load_runtime_incident_config(
            {"TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": ""},
            environment_only=True,
        ).telegram_notification_types
        == frozenset()
    )


def test_an_unattributable_market_fill_records_a_critical_incident(tmp_path):
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.recovery_live_submit import (
        _record_market_fill_attribution_incident,
    )

    session_factory = create_session_factory(tmp_path / "incident.db")

    _record_market_fill_attribution_incident(
        session_factory,
        order_id=ORD_ID,
        candidate_pos_id="candidate-pos-9",
        inst_id=INST_ID,
        position_side="long",
        size=0.2,
        reason="rest_pos_id_not_confirmed_by_rest",
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
    assert incident.incident_type == MARKET_FILL_ATTRIBUTION_INCIDENT_TYPE
    assert incident.severity == "critical"
    summary = json.loads(incident.redacted_summary)
    assert summary["reason_code"] == "rest_pos_id_not_confirmed_by_rest"
    assert summary["containment"] == "protection_not_attached_position_unverified"

    # Everything a person needs to open the exchange and look has to be in the
    # message they receive, not behind a query they would have to know to run.
    # The order id is the incident's own source record; the rest ride in
    # ``impact``, and both are fields the notification formatter prints.
    from telegram_kol_research.system_operator_bot import (
        format_runtime_incident_notification,
    )

    text = format_runtime_incident_notification(incident)
    assert f"deepcoin_entry_order:{ORD_ID}" in text
    assert "pos=candidate-pos-9" in text
    assert f"inst={INST_ID}" in text
    assert "side=long" in text
    assert "sz=0.2" in text
    assert "market_entry_may_be_filled_without_stop" in text
    assert "critical" in text


def test_the_same_unattributable_order_does_not_become_a_second_incident(tmp_path):
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.recovery_live_submit import (
        _record_market_fill_attribution_incident,
    )

    session_factory = create_session_factory(tmp_path / "incident-twice.db")
    for _ in range(2):
        _record_market_fill_attribution_incident(
            session_factory,
            order_id=ORD_ID,
            candidate_pos_id=None,
            inst_id=INST_ID,
            position_side="long",
            size=0.2,
            reason="no_ws_position_frame_for_pos_id",
            occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        rows = session.query(RuntimeIncident).all()
    # One order, one alert. Coalescing is what keeps a repeated pass from
    # burying the operator, and the fingerprint is the order id for that reason.
    assert len(rows) == 1


def test_recording_the_incident_never_breaks_an_entry_that_already_reached_the_exchange():
    from telegram_kol_research.recovery_live_submit import (
        _record_market_fill_attribution_incident,
    )

    def _broken_session_factory():
        raise RuntimeError("database unavailable")

    # No exception escapes: the order is already on the exchange, and raising
    # here would replace a reported problem with an unreported one.
    _record_market_fill_attribution_incident(
        _broken_session_factory,
        order_id=ORD_ID,
        candidate_pos_id=None,
        inst_id=INST_ID,
        position_side="long",
        size=0.2,
        reason="rest_positions_unreadable",
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )


def test_a_summary_the_bounds_checker_refuses_still_produces_an_alert(tmp_path):
    """The alert must not be lost to a formatting rule.

    ``redacted_summary`` is checked for anything that looks like leaked secret
    material, and a long unbroken identifier can trip that heuristic. If the
    detailed summary is ever refused, the fallback one -- which carries no
    interpolated values at all -- still records the incident, and the order id
    is in ``source_record_id`` regardless.
    """

    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.recovery_live_submit import (
        _record_market_fill_attribution_incident,
    )

    session_factory = create_session_factory(tmp_path / "bounds-fallback.db")
    _record_market_fill_attribution_incident(
        session_factory,
        order_id=ORD_ID,
        # An id long and mixed enough to read as an opaque secret.
        candidate_pos_id="Ab3" + "xY7z_9Q2-K4mN8pR" * 3,
        inst_id=INST_ID,
        position_side="long",
        size=0.2,
        reason="rest_pos_side_mismatch",
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
    assert incident.severity == "critical"
    assert incident.source_record_id == ORD_ID
    summary = json.loads(incident.redacted_summary)
    assert summary["impact"] == "market_entry_may_be_filled_without_stop"
    assert summary["reason_code"] == "rest_pos_side_mismatch"
