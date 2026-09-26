"""The stuck deletion exit: say why, stop shouting, and close by itself.

Production, 2026-09-15 to 09-26 (docs/2026-09-26-silent-stall-case-note.md):
陈哥's group deleted two BTC-long messages, both exits landed in
``recovery_required`` with no ``execution_binding_id``, and the deferral
barrier sealed that group's BTC-long lane for eleven days. Eleven later
messages -- four of them entry strategies -- were held until they expired. The
system did alert: 356933 times, notified once, and the one field carrying the
answer ("why was the lane not released") was refused by the incident summary
vocabulary on every single pass.

Three defects, three sections here:

* 甲 ``release_reason`` has to survive the summary contract.
* 乙 the same unchanged exit must be captured once per interval, not every
  five seconds -- but a changed one must still be captured at once.
* 丙 an exit with no execution credentials, over a lane where every live
  position and resting order belongs to somebody else, must be allowed to
  close. And nothing that already expired may be brought back.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deferred_instruction_recovery import (
    DEFERRED_EXPIRED_REASON,
    DEFERRED_HOLD_REASON,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.source_deletion_exit_timeout import (
    NO_EXCHANGE_FOOTPRINT_REASON,
    POSITION_GONE_REASON,
    STUCK_EXIT_CAPTURE_MIN_INTERVAL,
    build_exchange_absence_reader,
    expire_stuck_source_deletion_exits,
)


NOW = datetime(2026, 9, 26, 9, 46, tzinfo=UTC)
CHAT_ID = -1002337721508
STUCK_SINCE = NOW - timedelta(days=11)


class _Capture(logging.Handler):
    """Records straight off the named logger.

    Not ``caplog``: ``app_logging.configure_application_logging`` sets
    ``propagate = False`` on the ``telegram_kol_research`` logger, so once any
    test in the suite has called it nothing reaches the root handler ``caplog``
    installs. The log-throttle case below passed alone and failed in the full
    run for exactly that reason.
    """

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _captured(logger_name):
    handler = _Capture()
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# --------------------------------------------------------------------------
# 甲: the vocabulary that swallowed the only useful field
# --------------------------------------------------------------------------


def _permissive_incident_config():
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )

    return RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES)
    )


def _capture_stuck_incident(session_factory, **overrides):
    from telegram_kol_research.runtime_incident_adapters import (
        capture_source_deletion_exit_stuck,
    )

    payload = {
        "config": _permissive_incident_config(),
        "deletion_exit_id": 310,
        "state": "recovery_required",
        "reason_code": "exact_lifecycle_missing",
        "timeout_minutes": 120,
        "lane_released": False,
        "release_reason": "exit_has_no_known_position",
        "occurred_at": NOW,
    }
    payload.update(overrides)
    return capture_source_deletion_exit_stuck(session_factory, **payload)


def _incident_rows(session_factory):
    from telegram_kol_research.models import RuntimeIncident

    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "source_deletion_exit_stuck"
            )
            .all()
        )


def test_the_stuck_exit_alert_lands_with_impact_and_release_reason(tmp_path):
    """The detailed summary, not the minimal fallback it used to land on."""

    session_factory = create_session_factory(tmp_path / "incidents.db")

    _capture_stuck_incident(session_factory)

    rows = _incident_rows(session_factory)
    assert len(rows) == 1
    summary = json.loads(rows[0].redacted_summary)
    assert summary["release_reason"] == "exit_has_no_known_position"
    assert summary["impact"] == "lane_still_held_after_timeout"
    assert summary["timeout_minutes"] == 120
    assert summary["operation"] == "deletion_exit_310"
    assert summary["reason_code"] == "exact_lifecycle_missing"


def test_the_notification_a_person_reads_names_the_release_verdict(tmp_path):
    """The row is not the last mile -- the Telegram message is."""

    from telegram_kol_research.system_operator_bot import (
        format_runtime_incident_notification,
    )

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture_stuck_incident(session_factory)

    rendered = format_runtime_incident_notification(_incident_rows(session_factory)[0])
    assert "释放判定: exit_has_no_known_position" in rendered


def test_every_stuck_exit_summary_field_is_inside_the_closed_vocabulary(tmp_path):
    """The A-8c / A-10b failure asserted, since this was its third repeat.

    ``runtime_incidents`` refuses a summary carrying an unknown key and logs
    the refusal instead of raising it, so a missing word costs an alarm and
    nothing turns red. Under the suite the same condition raises, so this test
    fails loudly if the word disappears again.
    """

    from telegram_kol_research.runtime_incidents import _SUMMARY_FIELDS

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture_stuck_incident(session_factory, lane_released=True,
                            release_reason=NO_EXCHANGE_FOOTPRINT_REASON)

    summary = json.loads(_incident_rows(session_factory)[0].redacted_summary)
    assert set(summary) <= set(_SUMMARY_FIELDS)
    assert summary["release_reason"] == NO_EXCHANGE_FOOTPRINT_REASON
    assert summary["impact"] == "lane_released_after_timeout"


# --------------------------------------------------------------------------
# 乙 and 丙 share one fixture: 陈哥's sealed BTC-long lane
# --------------------------------------------------------------------------


def _lane_fixture(
    tmp_path,
    *,
    execution_binding_id=None,
    symbol="BTC",
    side="long",
    with_candidate=True,
):
    """One credential-less stuck exit over a deleted BTC-long message.

    Shaped after exit 310: ``recovery_required`` since 09-15,
    ``exact_lifecycle_missing``, no binding, no known position.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                id=15900,
                chat_id=CHAT_ID,
                message_id=15,
                text="BTC 多单 76000-67300",
                source_status="deleted",
                posted_at=STUCK_SINCE.replace(tzinfo=None),
            )
        )
        if with_candidate:
            session.add(
                SignalCandidate(
                    raw_message_id=15900,
                    symbol=symbol,
                    side=side,
                    review_status="approved",
                )
            )
        session.add(
            TelegramSourceMessageEvent(
                id=9001,
                chat_id=CHAT_ID,
                message_id=15,
                event_type="message_deleted",
                raw_message_id=15900,
                event_fingerprint="a" * 64,
                binding_state="bound",
                occurred_at=STUCK_SINCE.replace(tzinfo=None),
            )
        )
        session.add(
            SourceMessageDeletionExit(
                id=310,
                source_event_id=9001,
                raw_message_id=15900,
                execution_binding_id=execution_binding_id,
                state="recovery_required",
                last_reason="exact_lifecycle_missing",
                updated_at=STUCK_SINCE.replace(tzinfo=None),
                created_at=STUCK_SINCE.replace(tzinfo=None),
            )
        )
        session.commit()
    return session_factory


def _other_group_binding(session_factory, *, pos_id="pos-383", order_id="ord-383"):
    """米娅's BTC long: the position that really was on the account."""

    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=383,
                venue="deepcoin",
                strategy_instance_id="strategy-383",
                kol_id="9",
                chat_id=-1001111111111,
                message_id=77,
                symbol="BTC",
                side="long",
                status="open",
                pos_id=pos_id,
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=901,
                execution_binding_id=383,
                venue="deepcoin",
                purpose="entry",
                leg_index=1,
                status="active",
                order_id=order_id,
                pos_id=pos_id,
                attribution_status="verified",
                strategy_instance_id="strategy-383",
            )
        )
        session.commit()


def _reader(*, positions=(), orders=(), broken=False):
    def positions_loader():
        if broken:
            raise RuntimeError("deepcoin down")
        return list(positions)

    return build_exchange_absence_reader(
        positions_loader=positions_loader,
        resting_orders_loader=lambda: list(orders),
    )


def _sweep(session_factory, *, now=NOW, captured=None, reader=None):
    return expire_stuck_source_deletion_exits(
        session_factory,
        now=now,
        timeout_minutes=120,
        exchange_reader=reader if reader is not None else _reader(),
        capture=(
            (lambda **kwargs: captured.append(kwargs))
            if captured is not None
            else (lambda **kwargs: None)
        ),
    )


def _exit_state(session_factory, exit_id=310):
    with session_factory() as session:
        row = session.get(SourceMessageDeletionExit, exit_id)
        return row.state, row.last_reason


# --------------------------------------------------------------------------
# 乙: 356933 captures in eleven days
# --------------------------------------------------------------------------


def _held_fixture(tmp_path):
    """A stuck exit that will not be released, so it can be swept repeatedly.

    Deliberately the *old* held shape -- a bound exit whose position is still
    open -- so these cases measure the throttle and nothing else.
    """

    session_factory = _lane_fixture(tmp_path, execution_binding_id=383)
    _other_group_binding(session_factory, pos_id="pos-310", order_id="ord-310")
    return session_factory


def _live_position(pos_id="pos-310"):
    return {"posId": pos_id, "instId": "BTC-USDT-SWAP", "posSide": "long",
            "pos": "5"}


def test_an_unchanged_stuck_exit_is_captured_once_per_interval(tmp_path):
    """Five-second ticks, one alert. This is the 69000-lines-a-day defect."""

    session_factory = _held_fixture(tmp_path)
    captured: list[dict] = []
    reader_rows = [_live_position()]

    for tick in range(6):
        _sweep(
            session_factory,
            now=NOW + timedelta(seconds=5 * tick),
            captured=captured,
            reader=_reader(positions=reader_rows),
        )

    assert len(captured) == 1
    assert captured[0]["release_reason"] == "position_still_open"

    # Just short of the interval: still quiet. One second past it: it speaks.
    _sweep(
        session_factory,
        now=NOW + STUCK_EXIT_CAPTURE_MIN_INTERVAL - timedelta(seconds=1),
        captured=captured,
        reader=_reader(positions=reader_rows),
    )
    assert len(captured) == 1
    result = _sweep(
        session_factory,
        now=NOW + STUCK_EXIT_CAPTURE_MIN_INTERVAL + timedelta(seconds=1),
        captured=captured,
        reader=_reader(positions=reader_rows),
    )
    assert len(captured) == 2
    # The sweep still judged it on every pass; only the alert was throttled.
    assert (result.alerted, result.held, result.captured) == ((310,), (310,), (310,))


def test_a_state_change_is_captured_immediately_despite_the_throttle(tmp_path):
    """A state change is news, and the throttle must not sit on news."""

    session_factory = _held_fixture(tmp_path)
    captured: list[dict] = []
    reader_rows = [_live_position()]

    _sweep(session_factory, captured=captured, reader=_reader(positions=reader_rows))
    assert len(captured) == 1

    with session_factory() as session:
        session.query(SourceMessageDeletionExit).filter(
            SourceMessageDeletionExit.id == 310
        ).update({SourceMessageDeletionExit.state: "recovery_required"})
        session.commit()
    # Same state, same reason, one second later: throttled.
    _sweep(
        session_factory,
        now=NOW + timedelta(seconds=1),
        captured=captured,
        reader=_reader(positions=reader_rows),
    )
    assert len(captured) == 1

    with session_factory() as session:
        session.query(SourceMessageDeletionExit).filter(
            SourceMessageDeletionExit.id == 310
        ).update({SourceMessageDeletionExit.last_reason: "reconcile_incomplete"})
        session.commit()
    _sweep(
        session_factory,
        now=NOW + timedelta(seconds=2),
        captured=captured,
        reader=_reader(positions=reader_rows),
    )
    assert len(captured) == 2
    assert captured[1]["candidate"]["last_reason"] == "reconcile_incomplete"


def test_a_release_is_never_throttled(tmp_path):
    """The lane reopening is the one thing the throttle may not swallow."""

    session_factory = _lane_fixture(tmp_path, execution_binding_id=383)
    _other_group_binding(session_factory, pos_id="pos-310", order_id="ord-310")
    captured: list[dict] = []
    ours = {
        "posId": "pos-310",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": "3",
    }

    _sweep(session_factory, captured=captured, reader=_reader(positions=[ours]))
    assert len(captured) == 1 and captured[0]["lane_released"] is False

    # One second later -- deep inside the throttle window -- the position is gone.
    result = _sweep(
        session_factory,
        now=NOW + timedelta(seconds=1),
        captured=captured,
        reader=_reader(),
    )
    assert result.released == (310,)
    assert len(captured) == 2
    assert captured[1]["lane_released"] is True


def test_the_summary_log_line_is_throttled_with_the_captures(tmp_path):
    """Otherwise the noise only moves from two lines a tick to one."""

    session_factory = _held_fixture(tmp_path)
    reader_rows = [_live_position()]
    with _captured(
        "telegram_kol_research.source_deletion_exit_timeout"
    ) as records:
        for tick in range(4):
            _sweep(
                session_factory,
                now=NOW + timedelta(seconds=5 * tick),
                reader=_reader(positions=reader_rows),
            )

    summary_lines = [
        record
        for record in records
        if record.getMessage().startswith("source deletion exits stuck alerted=")
    ]
    assert len(summary_lines) == 1


# --------------------------------------------------------------------------
# 丙: an exit holding nothing must be allowed to let go
# --------------------------------------------------------------------------


def test_a_credentialless_exit_over_a_fully_attributed_lane_is_released(tmp_path):
    """The production shape: our own footprint is empty, the lane is not ours.

    米娅's binding 383 owns the only BTC long on the account, so nothing in
    this lane can be the orphan exit 310 was supposed to close.
    """

    session_factory = _lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    captured: list[dict] = []

    result = _sweep(
        session_factory,
        captured=captured,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-383",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "3",
                }
            ],
            orders=[
                {
                    "ordId": "ord-383",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                }
            ],
        ),
    )

    assert (result.released, result.held) == ((310,), ())
    assert _exit_state(session_factory) == ("succeeded", NO_EXCHANGE_FOOTPRINT_REASON)
    # Released is not "nothing happened": the alert still fires, once.
    assert captured[0]["lane_released"] is True
    assert captured[0]["release_reason"] == NO_EXCHANGE_FOOTPRINT_REASON


def _unattributed_position():
    """A BTC long on the account that no execution leg claims."""

    return {"posId": "pos-orphan", "instId": "BTC-USDT-SWAP", "posSide": "long",
            "pos": "5"}


def test_an_unattributed_position_in_the_lane_keeps_it_sealed(tmp_path):
    """The safety boundary. An unclaimed BTC long could be our own orphan."""

    session_factory = _lane_fixture(tmp_path)
    _other_group_binding(session_factory)

    result = _sweep(
        session_factory,
        reader=_reader(positions=[_unattributed_position()]),
    )

    assert (result.released, result.held) == ((), (310,))
    assert _exit_state(session_factory) == (
        "recovery_required",
        "exact_lifecycle_missing",
    )


def test_an_unattributed_resting_order_in_the_lane_keeps_it_sealed(tmp_path):
    """Same boundary for a resting order nobody claims."""

    session_factory = _lane_fixture(tmp_path)

    result = _sweep(
        session_factory,
        reader=_reader(
            orders=[
                {"ordId": "ord-orphan", "instId": "BTC-USDT-SWAP", "posSide": "long"}
            ]
        ),
    )

    assert (result.released, result.held) == ((), (310,))


def test_another_lane_does_not_keep_this_one_sealed(tmp_path):
    """A BTC short and an ETH long are different lanes and prove nothing here."""

    session_factory = _lane_fixture(tmp_path)

    result = _sweep(
        session_factory,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-short",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "pos": "4",
                },
                {
                    "posId": "pos-eth",
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "pos": "4",
                },
            ]
        ),
    )

    assert (result.released, result.held) == ((310,), ())


def test_a_failed_exchange_read_never_releases_a_lane(tmp_path):
    """"Unknown" must never be spent as proof."""

    session_factory = _lane_fixture(tmp_path)
    captured: list[dict] = []

    result = _sweep(session_factory, captured=captured, reader=_reader(broken=True))

    assert (result.released, result.held) == ((), (310,))
    assert captured[0]["release_reason"] == "lane_read_failed"
    assert _exit_state(session_factory) == (
        "recovery_required",
        "exact_lifecycle_missing",
    )


def test_a_closed_position_row_does_not_keep_the_lane_sealed(tmp_path):
    """A zero-size row is history, not a holding."""

    session_factory = _lane_fixture(tmp_path)

    result = _sweep(
        session_factory,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-closed",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "0",
                }
            ]
        ),
    )

    assert (result.released, result.held) == ((310,), ())


def test_an_exit_whose_lane_cannot_be_named_is_left_sealed(tmp_path):
    """No candidate means no symbol/side, so the footprint cannot be judged."""

    session_factory = _lane_fixture(tmp_path, with_candidate=False)
    captured: list[dict] = []

    result = _sweep(session_factory, captured=captured)

    assert (result.released, result.held) == ((), (310,))
    assert captured[0]["release_reason"] == "lane_identity_unknown"


def test_an_exit_with_a_binding_still_takes_the_position_proof_path(tmp_path):
    """丙 must not touch the exits that have credentials to reason about."""

    session_factory = _lane_fixture(tmp_path, execution_binding_id=383)
    _other_group_binding(session_factory, pos_id="pos-310", order_id="ord-310")
    captured: list[dict] = []

    still_open = _sweep(
        session_factory,
        captured=captured,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-310",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "2",
                }
            ]
        ),
    )
    assert (still_open.released, still_open.held) == ((), (310,))
    assert captured[0]["release_reason"] == "position_still_open"

    gone = _sweep(
        session_factory,
        now=NOW + timedelta(minutes=1),
        captured=captured,
        reader=_reader(),
    )
    assert gone.released == (310,)
    # The old path keeps its own reason, so the two are distinguishable later.
    assert _exit_state(session_factory) == ("succeeded", POSITION_GONE_REASON)


def test_the_lane_read_only_happens_on_a_speaking_pass(tmp_path):
    """The venue's rate limit, not only the log volume.

    The old code answered a credential-less exit from memory and read nothing.
    The lane judgement needs a snapshot, and at five seconds a tick that would
    be 24 REST calls a minute for as long as the exit sits there -- so it is
    gated by the same throttle as the alert. A lane sealed for days can wait
    another half hour.
    """

    session_factory = _lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    reads: list[str] = []

    def fresh_reader():
        def positions_loader():
            reads.append("positions")
            return [_unattributed_position()]

        # The worker builds one reader per tick, so the memoised snapshot does
        # not survive between passes -- only the throttle does.
        return build_exchange_absence_reader(
            positions_loader=positions_loader,
            resting_orders_loader=lambda: [],
        )

    for tick in range(4):
        _sweep(
            session_factory,
            now=NOW + timedelta(seconds=5 * tick),
            reader=fresh_reader(),
        )

    assert len(reads) == 1


# --------------------------------------------------------------------------
# 丙, the line that must not move: an expired message stays expired
# --------------------------------------------------------------------------


def _held_message(session_factory, *, raw_message_id, message_id, reason):
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_message_id,
                chat_id=CHAT_ID,
                message_id=message_id,
                text="BTC 多单 83000-83300",
                source_status="active",
                posted_at=NOW.replace(tzinfo=None),
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=raw_message_id,
                symbol="BTC",
                side="long",
                review_status="approved",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="test",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="agreement",
                automation_status="deferred",
                automation_reason=reason,
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()


def test_releasing_the_lane_resumes_the_waiting_but_never_the_expired(tmp_path):
    """The user's own rule: too much time has passed, do not place the order.

    ``deferred_expired`` is terminal by design -- the resume path keys on
    ``waiting_source_deletion_exit`` and that difference is the whole
    mechanism. This asserts it through the new release, which is the first
    code path that can reach these messages again.
    """

    session_factory = _lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    _held_message(
        session_factory,
        raw_message_id=16010,
        message_id=101,
        reason=DEFERRED_HOLD_REASON,
    )
    _held_message(
        session_factory,
        raw_message_id=16011,
        message_id=102,
        reason=DEFERRED_EXPIRED_REASON,
    )

    result = _sweep(
        session_factory,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-383",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "3",
                }
            ]
        ),
    )

    assert result.released == (310,)
    with session_factory() as session:
        requeued = {
            int(row.raw_message_id)
            for row in session.query(MessageProcessingJob).all()
        }
    assert requeued == {16010}
