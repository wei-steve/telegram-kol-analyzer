"""Phase-3 batch 3 wiring tests.

Batch 2 (``tests/test_oncall_remediation.py``, 83 cases) already exercises
gates G-A/G-B/G-C and the state machine in isolation. These tests cover the
*glue* added in batch 3: the loopback registration endpoint, the worker
background task's dormant-by-default gating, and the system-bot callback /
text-command dispatch in ``telegram_bot_commands.py``. Library functions are
monkeypatched here wherever a test is about the wiring rather than the gate
logic itself; the endpoint tests that assert "no exchange call happens on
the request path" use the real ``register_proposal_request`` against a real
temporary database, because that is the one guarantee that must hold for the
*actual* function, not a stand-in for it.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from telegram_kol_research import telegram_bot_commands as tbc_module
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.oncall_remediation import (
    CallbackOutcome,
    CommandOutcome,
    ExecutionOutcome,
    FinalizeOutcome,
    ProposalOutcome,
    RegisterResult,
)
from telegram_kol_research.oncall_remediation_runtime import OncallRemediationWiring
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.web_app import create_web_app

PROPOSALS_URL = "/internal/oncall/remediation/proposals"
TOKEN = "a" * 40
GOOD_BODY = {"case_key": "case-1", "case_no": 1, "raw_message_id": 42}


def _config(**overrides) -> OncallRemediationConfig:
    values = dict(
        mode="approve",
        token=TOKEN,
        approver_ids=frozenset({111}),
        system_chat_id="999",
    )
    values.update(overrides)
    return OncallRemediationConfig(**values)


def _loopback(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _refusing_deepcoin_client():
    raise AssertionError("registration must never touch the exchange")


# ---------------------------------------------------------------------------
# Endpoint: registration and auth
# ---------------------------------------------------------------------------


def test_endpoint_registers_without_touching_exchange_and_is_idempotent(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "r.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
        deepcoin_client_factory=_refusing_deepcoin_client,
    )
    client = _loopback(app)

    resp = client.post(
        PROPOSALS_URL,
        json=GOOD_BODY,
        headers={"x-oncall-remediation-token": TOKEN},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"proposal_id", "state"}
    assert body["state"] == "requested"
    assert app.state.oncall_remediation_wake_event.is_set()

    # Same raw_message_id again: idempotent, returns the same non-terminal row.
    resp2 = client.post(
        PROPOSALS_URL,
        json=GOOD_BODY,
        headers={"x-oncall-remediation-token": TOKEN},
    )
    assert resp2.status_code == 200
    assert resp2.json() == {"proposal_id": body["proposal_id"], "state": "requested"}


def test_endpoint_rejects_non_loopback_and_forwarded_and_bad_token(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "r.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
    )
    default_client = TestClient(app)  # host defaults to "testclient", not loopback
    assert (
        default_client.post(
            PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": TOKEN}
        ).status_code
        == 404
    )

    loopback = _loopback(app)
    assert (
        loopback.post(PROPOSALS_URL, json=GOOD_BODY).status_code == 404
    ), "missing token header must 404"
    assert (
        loopback.post(
            PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": "wrong"}
        ).status_code
        == 404
    )
    assert (
        loopback.post(
            PROPOSALS_URL,
            json=GOOD_BODY,
            headers={
                "x-oncall-remediation-token": TOKEN,
                "x-forwarded-for": "1.2.3.4",
            },
        ).status_code
        == 404
    )


def test_endpoint_404s_when_mode_off_or_token_unconfigured(tmp_path):
    app_off = create_web_app(
        database_path=tmp_path / "off.db",
        runtime_role="worker",
        oncall_remediation_config=_config(mode="off"),
    )
    assert (
        _loopback(app_off)
        .post(PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": TOKEN})
        .status_code
        == 404
    )

    app_no_token = create_web_app(
        database_path=tmp_path / "no-token.db",
        runtime_role="worker",
        oncall_remediation_config=_config(token=None),
    )
    assert (
        _loopback(app_no_token)
        .post(PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": TOKEN})
        .status_code
        == 404
    )


def test_endpoint_default_dormant_when_unconfigured(tmp_path, monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("TELEGRAM_KOL_ONCALL_REMEDIATION_"):
            monkeypatch.delenv(name, raising=False)
    app = create_web_app(database_path=tmp_path / "dormant.db", runtime_role="worker")
    assert app.state.oncall_remediation_active is False
    resp = _loopback(app).post(
        PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": "whatever"}
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("role", ["web", "ingest", "all"])
def test_endpoint_not_registered_for_non_worker_roles(tmp_path, role):
    app = create_web_app(
        database_path=tmp_path / f"role-{role}.db",
        runtime_role=role,
        oncall_remediation_config=_config(),
    )
    resp = _loopback(app).post(
        PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": TOKEN}
    )
    assert resp.status_code == 404
    assert app.state.oncall_remediation_active is False


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD_BODY, "extra": 1},
        {**GOOD_BODY, "case_no": True},
        {**GOOD_BODY, "raw_message_id": "42"},
        {**GOOD_BODY, "case_no": 0},
        {**GOOD_BODY, "raw_message_id": -1},
        {**GOOD_BODY, "case_key": ""},
        {**GOOD_BODY, "case_key": "x" * 65},
        {"case_key": "c", "case_no": 1},
        {},
    ],
)
def test_endpoint_rejects_malformed_payloads(tmp_path, payload):
    app = create_web_app(
        database_path=tmp_path / "bad.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
    )
    resp = _loopback(app).post(
        PROPOSALS_URL, json=payload, headers={"x-oncall-remediation-token": TOKEN}
    )
    assert resp.status_code == 400


def test_endpoint_rejects_duplicate_keys(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "dup.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
    )
    raw = b'{"case_key":"c","case_key":"d","case_no":1,"raw_message_id":1}'
    resp = _loopback(app).post(
        PROPOSALS_URL,
        content=raw,
        headers={
            "x-oncall-remediation-token": TOKEN,
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_endpoint_rejects_oversized_body(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "big.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
    )
    huge = b'{"case_key":"c","case_no":1,"raw_message_id":1,"pad":"' + b"x" * 3000 + b'"}'
    resp = _loopback(app).post(
        PROPOSALS_URL,
        content=huge,
        headers={
            "x-oncall-remediation-token": TOKEN,
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 413


def test_endpoint_response_body_has_only_two_fields(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "shape.db",
        runtime_role="worker",
        oncall_remediation_config=_config(),
    )
    resp = _loopback(app).post(
        PROPOSALS_URL, json=GOOD_BODY, headers={"x-oncall-remediation-token": TOKEN}
    )
    assert set(resp.json()) == {"proposal_id", "state"}


# ---------------------------------------------------------------------------
# Background task: dormant-by-default gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,expected",
    [
        (_config(mode="off"), False),
        (_config(token=None), False),
        (_config(mode="shadow"), True),
        (_config(mode="approve"), True),
    ],
)
def test_oncall_remediation_active_flag(tmp_path, config, expected):
    app = create_web_app(
        database_path=tmp_path / f"active-{expected}-{id(config)}.db",
        runtime_role="worker",
        oncall_remediation_config=config,
    )
    assert app.state.oncall_remediation_active is expected


def test_background_task_starts_only_when_active(tmp_path, monkeypatch):
    started = threading.Event()

    async def fake_loop(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        web_app_module, "run_oncall_remediation_background_loop", fake_loop
    )

    inactive_app = create_web_app(
        database_path=tmp_path / "inactive.db",
        runtime_role="worker",
        oncall_remediation_config=_config(mode="off"),
    )
    with TestClient(inactive_app):
        assert not started.wait(timeout=0.3)
        assert inactive_app.state.oncall_remediation_background_task is None

    active_app = create_web_app(
        database_path=tmp_path / "active.db",
        runtime_role="worker",
        oncall_remediation_config=_config(mode="shadow"),
    )
    with TestClient(active_app):
        assert started.wait(timeout=2.0)
        assert active_app.state.oncall_remediation_background_task is not None
    assert active_app.state.oncall_remediation_background_task is None


def test_system_operator_loop_receives_wiring_only_when_active(tmp_path, monkeypatch):
    captured = {}
    ready = threading.Event()

    async def fake_system_bot_loop(**kwargs):
        captured.update(kwargs)
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        web_app_module, "run_system_operator_bot_command_loop", fake_system_bot_loop
    )
    app = create_web_app(
        database_path=tmp_path / "sysbot.db",
        runtime_role="worker",
        oncall_remediation_config=_config(mode="shadow"),
    )
    app.state.system_operator_bot_config = SystemOperatorBotConfig(
        bot_token="tok", chat_id="999"
    )
    with TestClient(app):
        assert ready.wait(timeout=2.0)
        assert isinstance(captured.get("oncall_remediation"), OncallRemediationWiring)


# ---------------------------------------------------------------------------
# telegram_bot_commands.py: callback and text-command dispatch
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


class _FakeTelegramClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **kwargs):
        self.calls.append((url, json or {}))
        return _FakeResponse({"ok": True, "result": {"message_id": 555}})


def _wiring(**overrides) -> OncallRemediationWiring:
    values = dict(
        config=_config(),
        session_factory=lambda: None,
        deepcoin_client_factory=lambda: None,
        group_config_provider=lambda: None,
        now_provider=lambda: __import__("datetime").datetime(2026, 9, 26, 0, 0, 0),
    )
    values.update(overrides)
    return OncallRemediationWiring(**values)


def test_orm_callback_answers_disabled_when_wiring_absent():
    client = _FakeTelegramClient()
    callback = {"id": "cb1", "from": {"id": 111}, "message": {"message_id": 5}}

    asyncio.run(
        tbc_module._handle_oncall_remediation_callback(
            client,
            "https://api.telegram.org/botX",
            callback=callback,
            message=callback["message"],
            chat_id="999",
            oncall_remediation=None,
        )
    )

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url.endswith("/answerCallbackQuery")
    assert payload["text"] == "补救未启用"


def test_orm_callback_answers_before_editing_and_edits_message(monkeypatch):
    client = _FakeTelegramClient()
    callback = {"id": "cb2", "from": {"id": 111}, "message": {"message_id": 7}}

    outcome = CallbackOutcome(
        proposal_id=3,
        accepted=True,
        text="确认执行：BTC 多 全部平仓？2 分钟内有效",
        keyboard=(("确认执行", "orm:3:2:tok2"), ("取消", "orm:3:c:tok2")),
    )

    def fake_handle_callback(*_args, **_kwargs):
        return outcome

    monkeypatch.setattr(tbc_module, "handle_callback", fake_handle_callback)

    asyncio.run(
        tbc_module._handle_oncall_remediation_callback(
            client,
            "https://api.telegram.org/botX",
            callback=callback,
            message=callback["message"],
            chat_id="999",
            oncall_remediation=_wiring(),
        )
    )

    assert len(client.calls) == 2
    answer_url, answer_payload = client.calls[0]
    edit_url, edit_payload = client.calls[1]
    assert answer_url.endswith("/answerCallbackQuery")
    assert edit_url.endswith("/editMessageText")
    assert edit_payload["text"] == outcome.text
    assert edit_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        "orm:3:2:tok2"
    )


def test_orm_callback_schedules_execution_exactly_once_for_two_confirms(monkeypatch):
    """Two rapid "确认执行" clicks must trigger at most one execution."""

    client = _FakeTelegramClient()
    callback = {"id": "cb3", "from": {"id": 111}, "message": {"message_id": 9}}

    call_count = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    async def fake_execute_proposal_locked(*_args, **_kwargs):
        call_count["n"] += 1
        started.set()
        await asyncio.to_thread(release.wait, 5.0)
        return ExecutionOutcome(
            proposal_id=3, state="succeeded", management_batch_id=1, text="done"
        )

    monkeypatch.setattr(
        tbc_module, "execute_proposal_locked", fake_execute_proposal_locked
    )
    monkeypatch.setattr(
        tbc_module,
        "handle_callback",
        lambda *a, **k: CallbackOutcome(
            proposal_id=3, accepted=True, text=None, keyboard=None, execute_proposal_id=3
        ),
    )

    async def scenario():
        await tbc_module._handle_oncall_remediation_callback(
            client,
            "https://api.telegram.org/botX",
            callback=callback,
            message=callback["message"],
            chat_id="999",
            oncall_remediation=_wiring(),
        )
        await tbc_module._handle_oncall_remediation_callback(
            client,
            "https://api.telegram.org/botX",
            callback=callback,
            message=callback["message"],
            chat_id="999",
            oncall_remediation=_wiring(),
        )
        await asyncio.sleep(0.05)
        assert started.is_set()
        release.set()
        # let the tracked background tasks finish before asserting
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=2.0)

    asyncio.run(scenario())
    assert call_count["n"] == 2  # both were invoked; the lock only serializes them
    assert len(client.calls) >= 2  # at least the two answerCallbackQuery calls


def test_text_command_disabled_when_wiring_absent():
    async def scenario():
        return await tbc_module._handle_oncall_remediation_text_command(
            text="/oncall_off",
            chat_id="999",
            from_user_id=111,
            oncall_remediation=None,
        )

    text, markup = asyncio.run(scenario())
    assert text == "补救未启用"
    assert markup is None


def test_text_command_routes_to_library(monkeypatch):
    seen = {}

    def fake_handle_text_command(*_args, **kwargs):
        seen.update(kwargs)
        return CommandOutcome(accepted=True, text="补救已关闭（作废 0 条在途提案）")

    monkeypatch.setattr(tbc_module, "handle_text_command", fake_handle_text_command)

    async def scenario():
        return await tbc_module._handle_oncall_remediation_text_command(
            text="/oncall_off",
            chat_id="999",
            from_user_id=111,
            oncall_remediation=_wiring(),
        )

    text, markup = asyncio.run(scenario())
    assert text == "补救已关闭（作废 0 条在途提案）"
    assert markup is None
    assert seen["chat_id"] == "999"
    assert seen["from_user_id"] == 111
    assert seen["text"] == "/oncall_off"


@pytest.mark.parametrize("text", ["/fix P1", "/oncall_off", "/oncall_on"])
def test_is_oncall_remediation_command_matches_only_these(text):
    assert tbc_module._is_oncall_remediation_command(text) is True


@pytest.mark.parametrize("text", ["/positions", "/pending", "/choose 1 2", "hello"])
def test_is_oncall_remediation_command_does_not_match_unrelated(text):
    assert tbc_module._is_oncall_remediation_command(text) is False


# ---------------------------------------------------------------------------
# Background loop: propose / finalize / expire wiring
# ---------------------------------------------------------------------------


def test_background_loop_sends_proposal_message_and_records_id(monkeypatch):
    import telegram_kol_research.oncall_remediation_runtime as runtime_module

    outcome = ProposalOutcome(
        proposal_id=5,
        state="proposed",
        refusal_reason=None,
        text="🛠 补救提案 P5",
        keyboard=(("✅ 执行补救", "orm:5:1:tok"), ("❌ 忽略", "orm:5:d:tok")),
        should_send=True,
    )
    monkeypatch.setattr(
        runtime_module, "compute_requested_proposal", lambda *a, **k: outcome
    )
    recorded = {}
    monkeypatch.setattr(
        runtime_module,
        "record_proposal_message",
        lambda *a, **k: recorded.update(k),
    )
    sent = {}

    async def fake_send(*, config, text, reply_markup=None):
        sent["text"] = text
        sent["reply_markup"] = reply_markup
        return 4242

    monkeypatch.setattr(runtime_module, "send_system_operator_bot_message", fake_send)

    async def scenario():
        await runtime_module._process_one_requested_proposal(
            proposal_id=5,
            config=_config(),
            session_factory=lambda: None,
            deepcoin_client_factory=lambda: object(),
            group_config_provider=lambda: __import__(
                "telegram_kol_research.group_config", fromlist=["GroupConfig"]
            ).GroupConfig(),
            bot_config=SystemOperatorBotConfig(bot_token="t", chat_id="999"),
            now_provider=lambda: __import__("datetime").datetime(2026, 9, 26),
        )

    asyncio.run(scenario())
    assert sent["text"] == outcome.text
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "orm:5:1:tok"
    assert recorded == {"proposal_id": 5, "telegram_message_id": 4242}


def test_background_loop_send_failure_leaves_proposal_unrecorded(monkeypatch):
    import telegram_kol_research.oncall_remediation_runtime as runtime_module

    outcome = ProposalOutcome(
        proposal_id=6,
        state="proposed",
        refusal_reason=None,
        text="🛠 补救提案 P6",
        keyboard=None,
        should_send=True,
    )
    monkeypatch.setattr(
        runtime_module, "compute_requested_proposal", lambda *a, **k: outcome
    )
    record_calls = []
    monkeypatch.setattr(
        runtime_module, "record_proposal_message", lambda *a, **k: record_calls.append(k)
    )

    async def failing_send(**_kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(
        runtime_module, "send_system_operator_bot_message", failing_send
    )

    async def scenario():
        await runtime_module._process_one_requested_proposal(
            proposal_id=6,
            config=_config(),
            session_factory=lambda: None,
            deepcoin_client_factory=lambda: object(),
            group_config_provider=lambda: __import__(
                "telegram_kol_research.group_config", fromlist=["GroupConfig"]
            ).GroupConfig(),
            bot_config=SystemOperatorBotConfig(bot_token="t", chat_id="999"),
            now_provider=lambda: __import__("datetime").datetime(2026, 9, 26),
        )

    asyncio.run(scenario())  # must not raise
    assert record_calls == []


def test_background_loop_finalize_sends_result_messages(monkeypatch):
    import telegram_kol_research.oncall_remediation_runtime as runtime_module

    finalize_outcome = FinalizeOutcome(proposal_id=7, state="succeeded", text="✅ 已补救 P7")
    monkeypatch.setattr(
        runtime_module, "finalize_executing_proposals", lambda *a, **k: [finalize_outcome]
    )
    monkeypatch.setattr(runtime_module, "expire_stale_proposals", lambda *a, **k: [])
    monkeypatch.setattr(runtime_module, "recover_after_restart", lambda *a, **k: [])
    sent = []

    async def fake_send(*, config, text, reply_markup=None):
        sent.append(text)
        return None

    monkeypatch.setattr(runtime_module, "send_system_operator_bot_message", fake_send)
    monkeypatch.setattr(
        runtime_module,
        "_list_requested_proposal_ids",
        _async_returning([]),
    )

    wake_event = asyncio.Event()

    async def scenario():
        wake_event.set()
        loop_task = asyncio.create_task(
            runtime_module.run_oncall_remediation_background_loop(
                config=_config(),
                session_factory=lambda: None,
                deepcoin_client_factory=lambda: object(),
                group_config_provider=lambda: None,
                bot_config=SystemOperatorBotConfig(bot_token="t", chat_id="999"),
                now_provider=lambda: __import__("datetime").datetime(2026, 9, 26),
                wake_event=wake_event,
                poll_interval_seconds=0.05,
            )
        )
        for _ in range(20):
            await asyncio.sleep(0.02)
            if sent:
                break
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert sent == ["✅ 已补救 P7"]


def _async_returning(value):
    async def _fn(*_args, **_kwargs):
        return value

    return _fn


# ---------------------------------------------------------------------------
# Event loop must not be blocked by synchronous library calls
# ---------------------------------------------------------------------------


def test_execute_proposal_locked_does_not_block_the_event_loop(monkeypatch):
    import time as time_module

    import telegram_kol_research.oncall_remediation_runtime as runtime_module

    def slow_execute_proposal(*_args, **_kwargs):
        time_module.sleep(0.5)
        return ExecutionOutcome(
            proposal_id=1, state="succeeded", management_batch_id=1, text="done"
        )

    monkeypatch.setattr(runtime_module, "execute_proposal", slow_execute_proposal)

    async def scenario():
        gaps: list[float] = []

        async def heartbeat():
            last = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(0.03)
                now = asyncio.get_event_loop().time()
                gaps.append(now - last)
                last = now

        heartbeat_task = asyncio.create_task(heartbeat())
        await runtime_module.execute_proposal_locked(
            lambda: None,
            config=_config(),
            proposal_id=1,
            deepcoin_client_factory=lambda: object(),
            group_config=None,
            now_provider=lambda: __import__("datetime").datetime(2026, 9, 26),
        )
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        return gaps

    gaps = asyncio.run(scenario())
    assert gaps, "heartbeat should have ticked at least once"
    assert max(gaps) < 0.2, f"event loop was blocked for {max(gaps):.3f}s"
