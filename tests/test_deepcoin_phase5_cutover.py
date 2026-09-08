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
