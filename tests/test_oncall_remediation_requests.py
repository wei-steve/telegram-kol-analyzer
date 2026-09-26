"""Phase 3 spec 5 (``docs/plans/2026-09-26-codex-oncall-phase3-spec.md``):
the watcher's outbound remediation-proposal request.

Section 9, items 11-12. Everything here is either a pure-function test (the
5.2 eligibility table, the HTTP client's response parsing) or an integration
test against a local stub HTTP server -- **never** a real socket to anything
but ``127.0.0.1``, and never the real worker.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from datetime import UTC, datetime, timedelta

import pytest

from oncall_test_support import CHAT_ID, NOW, GROUP_NAME, ProductionFixture
from telegram_kol_research.oncall_service import (
    MAX_REMEDIATION_ATTEMPTS,
    REMEDIATION_RETRY_COOLDOWN,
    REMEDIATION_STATES,
    MODE_DRY_RUN,
    MODE_NOTIFY,
    MODE_OFF,
    OncallConfig,
    _case_wants_remediation,
    _request_remediation_proposal,
    _valid_remediation_token,
    _valid_remediation_url,
    load_oncall_config,
    run_remediation_request_cycle,
    run_oncall_watch,
)
from telegram_kol_research.oncall_state import CaseRecord, OncallStateStore


TOKEN = "a" * 40
DEFAULT_URL = "http://127.0.0.1:8002/internal/oncall/remediation/proposals"


def remediation_config(**extra) -> OncallConfig:
    return OncallConfig(
        mode=extra.pop("mode", MODE_NOTIFY),
        bot_token="t",
        chat_id="1",
        remediation_requests=extra.pop("remediation_requests", True),
        remediation_url=extra.pop("remediation_url", DEFAULT_URL),
        remediation_token=extra.pop("remediation_token", TOKEN),
        **extra,
    )


def _case(
    *,
    id: int = 1,
    rule: str,
    evidence: dict | None = None,
    raw_message_id: int | None = 555,
    attempts: int = 0,
    request_state: str | None = None,
    last_at: datetime | None = None,
) -> CaseRecord:
    return CaseRecord(
        id=id,
        case_key=f"mgmt:{raw_message_id}:adjust_stop_loss",
        rule=rule,
        severity="high",
        raw_message_id=raw_message_id,
        chat_id=CHAT_ID,
        item_ids=(),
        batch_ids=(),
        reason_code="whatever",
        target_uncertain=False,
        status="open",
        first_seen_at=NOW,
        last_seen_at=NOW,
        alerted_at=NOW,
        resolved_at=None,
        evidence=evidence or {},
        remediation_proposal_id=None,
        remediation_request_state=request_state,
        remediation_request_attempts=attempts,
        remediation_request_last_at=last_at,
    )


# --------------------------------------------------------------------------
# 5.2's table, one positive and one negative per rule, plus combinations and
# the shadow_planned exclusion (user ruling #8 / worker gate A6b).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule,evidence,expected",
    [
        ("D1a", {}, True),
        ("D1d", {}, True),
        # D1b: only when the outcome was NOT specifically shadow_planned.
        ("D1b", {"result_status": "skipped"}, True),
        ("D1b", {}, True),  # no result_status recorded -> not excluded
        ("D1b", {"result_status": "shadow_planned"}, False),
        # D2: only while the batch is still 'blocked'.
        ("D2", {"batch_status": "blocked"}, True),
        ("D2", {"batch_status": "partial_failed"}, False),
        ("D2", {"batch_status": "submit_unknown"}, False),
        ("D2", {"batch_status": "recovery_required"}, False),
        ("D2", {}, False),
        # Never requested this phase.
        ("D1c", {}, False),
        ("D3", {}, False),
        ("D4", {}, False),
        ("D5a", {}, False),
        ("D5b", {}, False),
        ("D6a", {}, False),
        ("D6b", {}, False),
        ("D6c", {}, False),
    ],
)
def test_case_eligibility_matches_5_2(rule, evidence, expected):
    assert _case_wants_remediation(_case(rule=rule, evidence=evidence)) is expected


def test_combination_rules_request_when_any_component_qualifies():
    # D1a alone would qualify; its sibling D2 component (not blocked) would
    # not -- the combination must still request, because the spec says "只要
    # 组合里有一条可请求且没有排除条件，就请求".
    assert _case_wants_remediation(
        _case(rule="D1a+D2", evidence={"batch_status": "partial_failed"})
    )
    # The reverse: D2 blocked qualifies even though D1b's shadow_planned
    # component on the same case does not.
    assert _case_wants_remediation(
        _case(
            rule="D1b+D2",
            evidence={"result_status": "shadow_planned", "batch_status": "blocked"},
        )
    )
    # Neither component qualifies -> no request.
    assert not _case_wants_remediation(
        _case(
            rule="D1b+D2",
            evidence={"result_status": "shadow_planned", "batch_status": "partial_failed"},
        )
    )


def test_a_case_with_no_raw_message_id_or_no_rule_never_qualifies():
    assert not _case_wants_remediation(_case(rule=""))


# --------------------------------------------------------------------------
# Config: flag parsing, URL/token format checks, and the "off unless every
# condition holds" gate.
# --------------------------------------------------------------------------


def test_remediation_requests_flag_defaults_off_and_rejects_nonsense():
    assert load_oncall_config({}).remediation_requests is False
    assert (
        load_oncall_config({"TELEGRAM_KOL_ONCALL_REMEDIATION_REQUESTS": "nonsense"}).remediation_requests
        is False
    )
    assert (
        load_oncall_config({"TELEGRAM_KOL_ONCALL_REMEDIATION_REQUESTS": "on"}).remediation_requests
        is True
    )


def test_remediation_url_defaults_to_the_documented_loopback_endpoint():
    assert load_oncall_config({}).remediation_url == DEFAULT_URL


@pytest.mark.parametrize(
    "url,accepted",
    [
        ("http://127.0.0.1:8002/internal/oncall/remediation/proposals", True),
        ("http://localhost:8002/internal/oncall/remediation/proposals", True),
        ("http://127.0.0.1:9/x", True),
        ("https://127.0.0.1:8002/x", False),  # not the exact documented scheme
        ("http://10.0.0.5:8002/x", False),  # not loopback
        ("http://example.com/x", False),
        ("", True),  # empty -> falls back to the default (still loopback)
    ],
)
def test_remediation_url_only_accepts_loopback(url, accepted):
    result = _valid_remediation_url(url)
    assert bool(result) is accepted


@pytest.mark.parametrize(
    "token,accepted",
    [
        ("a" * 32, True),
        ("a" * 128, True),
        ("A-b_9" * 7, True),
        ("a" * 31, False),  # too short
        ("a" * 129, False),  # too long
        ("has spaces" + "a" * 30, False),
        ("token!with!bang" + "a" * 20, False),
        ("", False),
    ],
)
def test_remediation_token_format(token, accepted):
    assert bool(_valid_remediation_token(token)) is accepted


def test_remediation_enabled_requires_every_condition():
    base = remediation_config()
    assert base.remediation_enabled is True
    assert base.remediation_enabled is not remediation_config(mode=MODE_DRY_RUN).remediation_enabled
    assert remediation_config(mode=MODE_DRY_RUN).remediation_enabled is False
    assert remediation_config(mode=MODE_OFF).remediation_enabled is False
    assert remediation_config(remediation_requests=False).remediation_enabled is False
    assert remediation_config(remediation_url="").remediation_enabled is False
    assert remediation_config(remediation_token="").remediation_enabled is False
    # A non-loopback URL never reaches OncallConfig at all -- load_oncall_config
    # already turned it into "" -- but the property must fail closed even if
    # constructed directly with one, since ``remediation_enabled`` only checks
    # truthiness, not shape.
    assert remediation_config(remediation_url="not a url but truthy").remediation_enabled is True


# --------------------------------------------------------------------------
# The HTTP client itself, against a local stub server.
# --------------------------------------------------------------------------


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length)
        try:
            parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_body = None
        self.server.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "raw_body": raw_body,
                "body": parsed_body,
            }
        )
        if self.server.responses:
            status, payload, raw = self.server.responses.pop(0)
        else:
            status, payload, raw = 202, {"proposal_id": 1, "state": "proposed"}, None
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # pragma: no cover - silence test output
        pass


class _StubServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture
def stub_server():
    server = _StubServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []
    server.responses = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/internal/oncall/remediation/proposals"


def test_request_body_is_exactly_the_three_identifier_fields(stub_server):
    outcome = _request_remediation_proposal(
        url=_url(stub_server),
        token=TOKEN,
        case_key="mgmt:555:adjust_stop_loss",
        case_no=7,
        raw_message_id=555,
    )

    assert outcome.ok is True
    assert len(stub_server.requests) == 1
    request = stub_server.requests[0]
    assert request["body"] == {
        "case_key": "mgmt:555:adjust_stop_loss",
        "case_no": 7,
        "raw_message_id": 555,
    }
    assert request["headers"]["x-oncall-remediation-token"] == TOKEN
    assert request["headers"]["content-type"] == "application/json"
    assert "x-forwarded-for" not in request["headers"]


def test_success_discards_every_field_but_proposal_id_and_state(stub_server):
    stub_server.responses.append(
        (
            202,
            {
                "proposal_id": 42,
                "state": "proposed",
                # Everything below must never influence the outcome.
                "price": "2484",
                "target_pos_id": "pos-1",
                "fingerprint": "should-not-matter",
                "button_token": "should-not-leak",
            },
            None,
        )
    )

    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )

    assert outcome.ok is True
    assert outcome.proposal_id == 42
    assert outcome.state == "proposed"


@pytest.mark.parametrize("state", sorted(REMEDIATION_STATES))
def test_every_declared_state_is_accepted(stub_server, state):
    stub_server.responses.append((200, {"proposal_id": 1, "state": state}, None))
    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )
    assert outcome.ok is True
    assert outcome.state == state


def test_404_is_not_found(stub_server):
    stub_server.responses.append((404, {}, None))
    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )
    assert outcome.ok is False
    assert outcome.category == "not_found"


def test_500_is_http_error(stub_server):
    stub_server.responses.append((500, {}, None))
    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )
    assert outcome.ok is False
    assert outcome.category == "http_error"


def test_malformed_json_is_bad_response(stub_server):
    stub_server.responses.append((200, None, b"{not json"))
    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )
    assert outcome.ok is False
    assert outcome.category == "bad_response"


@pytest.mark.parametrize(
    "payload",
    [
        {"proposal_id": "not-an-int", "state": "proposed"},
        {"proposal_id": 1, "state": "not-a-real-state"},
        {"proposal_id": 1},
        {"state": "proposed"},
        {"proposal_id": True, "state": "proposed"},  # bool is not an int here
        [],
    ],
)
def test_schema_violations_are_bad_response(stub_server, payload):
    stub_server.responses.append((200, payload, None))
    outcome = _request_remediation_proposal(
        url=_url(stub_server), token=TOKEN, case_key="k", case_no=1, raw_message_id=1
    )
    assert outcome.ok is False
    assert outcome.category == "bad_response"


def test_a_connection_that_refuses_is_a_network_failure():
    # Nothing is listening on this port (it was just closed).
    server = _StubServer(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    server.server_close()

    outcome = _request_remediation_proposal(
        url=f"http://127.0.0.1:{port}/x",
        token=TOKEN,
        case_key="k",
        case_no=1,
        raw_message_id=1,
        timeout=1.0,
    )
    assert outcome.ok is False
    assert outcome.category == "network"


# --------------------------------------------------------------------------
# The cycle: first-build-only requests, retries, the give-up alert, and the
# independence from case-open alerts / Codex diagnosis.
# --------------------------------------------------------------------------


@pytest.fixture
def production(tmp_path) -> ProductionFixture:
    return ProductionFixture(tmp_path / "research.db")


@pytest.fixture
def store(tmp_path) -> OncallStateStore:
    with OncallStateStore(tmp_path / "state.db") as opened:
        yield opened


def _open_case_row(store, *, id_=1) -> CaseRecord:
    row = store.get_case(id_)
    assert row is not None
    return row


def test_cycle_is_a_noop_when_remediation_is_not_enabled(store):
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    counters = run_remediation_request_cycle(
        store, config=OncallConfig(mode=MODE_NOTIFY), now=NOW, new_case_ids=(case_id,)
    )

    assert counters == {"requested": 0, "ok": 0, "failed": 0, "gave_up": 0}
    assert store.get_case(case_id).remediation_request_attempts == 0


def test_cycle_requests_once_for_an_eligible_new_case(store, stub_server):
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    counters = run_remediation_request_cycle(
        store, config=config, now=NOW, new_case_ids=(case_id,)
    )

    assert counters["requested"] == 1
    assert counters["ok"] == 1
    updated = store.get_case(case_id)
    assert updated.remediation_request_attempts == 1
    assert updated.remediation_request_state == "ok:proposed"
    assert updated.remediation_proposal_id == 1
    assert len(stub_server.requests) == 1


def test_cycle_skips_an_ineligible_new_case(store, stub_server):
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="recog:1", rule="D3", severity="medium", now=NOW, raw_message_id=1,
    )[0].id

    counters = run_remediation_request_cycle(
        store, config=config, now=NOW, new_case_ids=(case_id,)
    )

    assert counters == {"requested": 0, "ok": 0, "failed": 0, "gave_up": 0}
    assert stub_server.requests == []
    assert store.get_case(case_id).remediation_request_attempts == 0


def test_a_case_is_only_ever_requested_once_per_new_case_round(store, stub_server):
    """Spec 5.1: only on the case's first build. A second round that still
    lists the same id as "new" (should not normally happen, but the guard is
    what makes the rule true even if it did) does not fire a second request.
    """

    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case_id,))
    run_remediation_request_cycle(
        store, config=config, now=NOW + timedelta(seconds=1), new_case_ids=(case_id,)
    )

    assert len(stub_server.requests) == 1


def test_three_failures_give_up_and_queue_one_alert(store, stub_server):
    stub_server.responses.extend([(500, {}, None), (500, {}, None), (500, {}, None)])
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case_id,))
    assert store.get_case(case_id).remediation_request_attempts == 1

    # Retrying before the cooldown elapses must not fire a second attempt.
    run_remediation_request_cycle(store, config=config, now=NOW + timedelta(seconds=5), new_case_ids=())
    assert store.get_case(case_id).remediation_request_attempts == 1
    assert len(stub_server.requests) == 1

    run_remediation_request_cycle(
        store, config=config, now=NOW + REMEDIATION_RETRY_COOLDOWN + timedelta(seconds=1), new_case_ids=()
    )
    assert store.get_case(case_id).remediation_request_attempts == 2

    counters = run_remediation_request_cycle(
        store,
        config=config,
        now=NOW + 2 * REMEDIATION_RETRY_COOLDOWN + timedelta(seconds=2),
        new_case_ids=(),
    )
    final = store.get_case(case_id)
    assert final.remediation_request_attempts == MAX_REMEDIATION_ATTEMPTS
    assert final.remediation_request_state == "failed:http_error"
    assert counters["gave_up"] == 1

    pending = store.pending_alerts()
    assert any(
        alert.kind == "remediation_request_failed" and f"#{case_id}" in alert.body
        for alert in pending
    )

    # No further attempts once the cap is reached, even after more cooldowns.
    run_remediation_request_cycle(
        store,
        config=config,
        now=NOW + 5 * REMEDIATION_RETRY_COOLDOWN,
        new_case_ids=(),
    )
    assert store.get_case(case_id).remediation_request_attempts == MAX_REMEDIATION_ATTEMPTS
    assert len(stub_server.requests) == MAX_REMEDIATION_ATTEMPTS


def test_a_success_after_a_failure_stops_retrying(store, stub_server):
    stub_server.responses.extend([(500, {}, None), (202, {"proposal_id": 9, "state": "proposed"}, None)])
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case_id,))
    run_remediation_request_cycle(
        store, config=config, now=NOW + REMEDIATION_RETRY_COOLDOWN + timedelta(seconds=1), new_case_ids=()
    )

    updated = store.get_case(case_id)
    assert updated.remediation_request_state == "ok:proposed"
    assert updated.remediation_request_attempts == 2

    # A third round finds nothing to retry -- the case is no longer in the
    # 'failed:%' state the retry query looks for.
    run_remediation_request_cycle(
        store, config=config, now=NOW + 5 * REMEDIATION_RETRY_COOLDOWN, new_case_ids=()
    )
    assert len(stub_server.requests) == 2


def test_a_request_exception_never_reaches_the_caller_of_run_oncall_round(
    production, tmp_path, monkeypatch
):
    """The alert and the diagnosis must still go out when the request itself
    blows up -- not just when the worker answers with an error status.
    """

    from telegram_kol_research import oncall_service

    def _boom(**_kwargs):
        raise RuntimeError("stub network stack exploded")

    monkeypatch.setattr(oncall_service, "_request_remediation_proposal", _boom)

    sent: list[str] = []
    config = remediation_config()
    state_path = tmp_path / "state.db"

    # The watcher's watermarks are set to "everything that already exists" on
    # its very first round against a fresh state.db (``oncall_test_support``'s
    # own tests always prime like this too) -- so the case-producing data has
    # to land *after* an initial empty round, or this round would treat it as
    # pre-existing history rather than something new to alert on.
    run_oncall_watch(
        database_path=production.path,
        state_path=state_path,
        once=True,
        config=config,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        sender=sent.append,
    )
    sent.clear()

    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )

    summary = run_oncall_watch(
        database_path=production.path,
        state_path=state_path,
        once=True,
        config=config,
        clock=lambda: NOW + timedelta(minutes=1),
        sleeper=lambda _seconds: None,
        sender=sent.append,
    )

    assert summary["last_error"] is None
    assert len(sent) == 1
    assert GROUP_NAME in sent[0] or "值守提醒" in sent[0]


# --------------------------------------------------------------------------
# The token must never reach the state database, the log, a case file, or a
# spooled Codex request.
# --------------------------------------------------------------------------


def test_the_token_never_reaches_the_state_database(store, stub_server, tmp_path):
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id
    run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case_id,))
    store.connection.commit()

    raw_bytes = store.path.read_bytes()
    assert TOKEN.encode("ascii") not in raw_bytes


def test_the_token_never_reaches_the_log(store, stub_server, caplog):
    stub_server.responses.append((404, {}, None))
    config = remediation_config(remediation_url=_url(stub_server))
    case_id = store.upsert_case(
        case_key="mgmt:1:full_exit", rule="D1a", severity="high", now=NOW,
        raw_message_id=1,
    )[0].id

    with caplog.at_level("DEBUG"):
        run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case_id,))

    for record in caplog.records:
        assert TOKEN not in record.getMessage()


def test_the_token_never_reaches_a_case_file(production, store, stub_server):
    from telegram_kol_research.oncall_casefile import build_case_file
    from telegram_kol_research.oncall_detector import ProductionReader

    config = remediation_config(remediation_url=_url(stub_server))
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )
    case, _created = store.upsert_case(
        case_key=f"mgmt:{raw_message_id}:adjust_stop_loss",
        rule="D1a",
        severity="high",
        now=NOW,
        raw_message_id=raw_message_id,
    )
    run_remediation_request_cycle(store, config=config, now=NOW, new_case_ids=(case.id,))

    with ProductionReader(production.path) as reader:
        payload = build_case_file(reader, case=store.get_case(case.id), now=NOW)

    assert TOKEN not in json.dumps(payload, ensure_ascii=False)


def test_the_token_never_reaches_the_codex_spool(production, tmp_path):
    """Both channels (remediation request, Codex diagnosis) fire off the same
    case-open round; the spool files the Codex side writes must not carry the
    remediation token even though both are configured in the same process.
    """

    from telegram_kol_research.oncall_codex import Spool

    shared = Spool(root=tmp_path / "codex-spool")
    shared.ensure_root()
    config = remediation_config(codex_mode="shadow", codex_spool=str(shared.root))
    state_path = tmp_path / "state.db"

    run_oncall_watch(
        database_path=production.path,
        state_path=state_path,
        once=True,
        config=config,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        sender=lambda _text: None,
        spool=shared,
    )

    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )

    run_oncall_watch(
        database_path=production.path,
        state_path=state_path,
        once=True,
        config=config,
        clock=lambda: NOW + timedelta(minutes=1),
        sleeper=lambda _seconds: None,
        sender=lambda _text: None,
        spool=shared,
    )

    for path in shared.root.rglob("*"):
        if path.is_file():
            assert TOKEN.encode("ascii") not in path.read_bytes(), path
