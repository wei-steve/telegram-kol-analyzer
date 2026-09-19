"""The watcher process: configuration, main loop, heartbeat, watchdog, CLI."""

from __future__ import annotations

import json
import os
import socket
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oncall_test_support import NOW, ProductionFixture, build_open_position_case
from telegram_kol_research.oncall_service import (
    MODE_DRY_RUN,
    MODE_NOTIFY,
    MODE_OFF,
    OncallConfig,
    build_worker_health_probe,
    load_oncall_config,
    notify_systemd,
    run_oncall_watch,
    write_heartbeat,
)


@pytest.fixture
def production(tmp_path) -> ProductionFixture:
    return ProductionFixture(tmp_path / "research.db")


def watch(production, tmp_path, *, config, sender=None, dry_run=False, clock=None, once=True):
    moments = iter([NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)])
    return run_oncall_watch(
        database_path=production.path,
        state_path=tmp_path / "state.db",
        once=once,
        dry_run=dry_run,
        config=config,
        clock=clock or (lambda: next(moments)),
        sleeper=lambda _seconds: None,
        sender=sender,
    )


# ----------------------------------------------------------------- config


def test_the_watcher_is_off_unless_the_environment_says_otherwise():
    assert load_oncall_config({}).mode == MODE_OFF
    assert load_oncall_config({"TELEGRAM_KOL_ONCALL_MODE": "nonsense"}).mode == MODE_OFF
    assert load_oncall_config({"TELEGRAM_KOL_ONCALL_MODE": "notify"}).mode == MODE_NOTIFY
    assert load_oncall_config({}).daily_alert_cap == 30


def test_off_exits_immediately_without_touching_anything(production, tmp_path):
    summary = watch(production, tmp_path, config=OncallConfig(mode=MODE_OFF))

    assert summary == {"mode": MODE_OFF, "rounds": 0, "stopped": "mode_off"}
    assert not (tmp_path / "state.db").exists()


def test_being_off_still_answers_the_systemd_readiness_protocol(
    production, tmp_path, monkeypatch
):
    """A dormant unit must look dormant, not like a failed start."""

    monkeypatch.chdir(tmp_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind("n.sock")
    server.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "n.sock")
    try:
        watch(production, tmp_path, config=OncallConfig(mode=MODE_OFF))
        assert server.recv(64) == b"READY=1"
    finally:
        server.close()


def test_notify_without_a_token_or_chat_id_cannot_send():
    assert OncallConfig(mode=MODE_NOTIFY).can_send is False
    assert OncallConfig(mode=MODE_NOTIFY, bot_token="t", chat_id="1").can_send is True
    assert OncallConfig(mode=MODE_DRY_RUN, bot_token="t", chat_id="1").can_send is False


# ------------------------------------------------------------------- loop


def test_one_round_detects_a_case_and_sends_one_alert(production, tmp_path):
    watch(production, tmp_path, config=OncallConfig(mode=MODE_NOTIFY), sender=lambda _t: None)
    build_open_position_case(production)
    sent: list[str] = []

    summary = watch(
        production,
        tmp_path,
        config=OncallConfig(mode=MODE_NOTIFY),
        sender=sent.append,
    )

    assert summary["rounds"] == 1
    assert len(sent) == 1
    assert "值守提醒" in sent[0]


def test_a_case_that_recovers_by_itself_produces_exactly_two_messages(
    production, tmp_path
):
    watch(production, tmp_path, config=OncallConfig(mode=MODE_NOTIFY), sender=lambda _t: None)
    built = build_open_position_case(production)
    sent: list[str] = []
    watch(production, tmp_path, config=OncallConfig(mode=MODE_NOTIFY), sender=sent.append)

    production.set_item_status(
        built["item_id"],
        status="succeeded",
        result={"status": "succeeded", "submitted": True},
    )
    watch(
        production,
        tmp_path,
        config=OncallConfig(mode=MODE_NOTIFY),
        sender=sent.append,
        clock=lambda: NOW + timedelta(minutes=3),
    )
    watch(
        production,
        tmp_path,
        config=OncallConfig(mode=MODE_NOTIFY),
        sender=sent.append,
        clock=lambda: NOW + timedelta(minutes=4),
    )

    assert len(sent) == 2
    assert "值守提醒" in sent[0]
    assert "已自行恢复" in sent[1]


def test_dry_run_detects_the_same_case_and_sends_nothing(production, tmp_path):
    watch(production, tmp_path, config=OncallConfig(mode=MODE_NOTIFY), sender=lambda _t: None)
    build_open_position_case(production)
    sent: list[str] = []

    watch(
        production,
        tmp_path,
        config=OncallConfig(mode=MODE_NOTIFY),
        sender=sent.append,
        dry_run=True,
    )

    assert sent == []
    from telegram_kol_research.oncall_state import OncallStateStore

    with OncallStateStore(tmp_path / "state.db") as store:
        assert len(store.open_cases()) == 1
        rows = store.connection.execute(
            "SELECT kind, status FROM alerts WHERE kind = 'case_open'"
        ).fetchall()
        assert [(row["kind"], row["status"]) for row in rows] == [
            ("case_open", "dry_run")
        ]


def test_a_round_that_explodes_is_logged_and_the_loop_keeps_going(
    production, tmp_path, monkeypatch
):
    import telegram_kol_research.oncall_service as service

    calls = {"count": 0}

    def exploding(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ZeroDivisionError("first round is cursed")
        return None

    monkeypatch.setattr(service, "run_oncall_round", exploding)
    rounds = {"n": 0}

    def sleeper(_seconds):
        rounds["n"] += 1
        if rounds["n"] >= 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_oncall_watch(
            database_path=production.path,
            state_path=tmp_path / "state.db",
            once=False,
            config=OncallConfig(mode=MODE_DRY_RUN),
            clock=lambda: NOW,
            sleeper=sleeper,
        )

    assert calls["count"] >= 2
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["last_error"] is None  # the second round succeeded
    assert heartbeat["round"] >= 2


def test_the_heartbeat_records_the_failing_round(tmp_path):
    path = write_heartbeat(
        tmp_path / "state.db", now=NOW, round_number=7, last_error="ValueError"
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "at": "2026-09-19T06:00:00+00:00",
        "round": 7,
        "last_error": "ValueError",
    }


def test_keyboard_interrupt_is_not_swallowed(production, tmp_path, monkeypatch):
    import telegram_kol_research.oncall_service as service

    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "run_oncall_round", interrupted)

    with pytest.raises(KeyboardInterrupt):
        watch(production, tmp_path, config=OncallConfig(mode=MODE_DRY_RUN))


# -------------------------------------------------------------- watchdog


def test_systemd_is_notified_when_the_socket_is_there(tmp_path, monkeypatch):
    # A relative name keeps the path inside the AF_UNIX ``sun_path`` limit,
    # which pytest's own temporary directory would blow past on macOS.
    monkeypatch.chdir(tmp_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind("n.sock")
    server.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "n.sock")
    try:
        assert notify_systemd("WATCHDOG=1") is True
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()


def test_no_notify_socket_is_not_an_error(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify_systemd("READY=1") is False


def test_a_missing_notify_socket_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "absent.sock"))
    assert notify_systemd("WATCHDOG=1") is False


# ----------------------------------------------------------------- probe


def test_an_empty_worker_health_url_disables_the_check():
    assert build_worker_health_probe("") is None


def test_an_unreachable_worker_health_url_answers_false():
    probe = build_worker_health_probe("http://127.0.0.1:1/api/runtime/loop-health")
    assert probe is not None
    assert probe() is False


# ------------------------------------------------------------------- CLI


def test_the_cli_exposes_oncall_watch_and_honours_mode_off(production, tmp_path, monkeypatch):
    from telegram_kol_research.cli import app

    monkeypatch.delenv("TELEGRAM_KOL_ONCALL_MODE", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "oncall-watch",
            "--database-path",
            str(production.path),
            "--state-path",
            str(tmp_path / "state.db"),
            "--once",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == MODE_OFF


def test_the_cli_runs_one_dry_run_round(production, tmp_path, monkeypatch):
    from telegram_kol_research.cli import app

    monkeypatch.setenv("TELEGRAM_KOL_ONCALL_MODE", "dry_run")
    result = CliRunner().invoke(
        app,
        [
            "oncall-watch",
            "--database-path",
            str(production.path),
            "--state-path",
            str(tmp_path / "state.db"),
            "--once",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rounds"] == 1
    assert (tmp_path / "state.db").exists()
