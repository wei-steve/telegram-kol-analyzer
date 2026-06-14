import pytest

from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
    describe_session_lock_owner,
    reap_stopped_session_lock_owner,
    release_session_lock_owner,
)


def test_session_lock_prevents_a_second_process_from_using_same_session(tmp_path):
    session_path = tmp_path / "telegram.session"

    with acquire_telegram_session_lock(session_path):
        with pytest.raises(TelegramSessionLockError) as exc_info:
            with acquire_telegram_session_lock(session_path):
                pass

    assert "telegram.session" in str(exc_info.value)
    assert "already in use" in str(exc_info.value)
    assert "owner pid=" in str(exc_info.value)
    assert (tmp_path / "telegram.session.lock").exists()


def test_describe_session_lock_owner_returns_pid_status_and_command(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")

    owner = describe_session_lock_owner(
        session_path,
        process_status=lambda pid: "S",
        process_command=lambda pid: "/repo/.venv/bin/telegram-kol-research web",
    )

    assert owner is not None
    assert owner.pid == 12345
    assert owner.status == "S"
    assert owner.command == "/repo/.venv/bin/telegram-kol-research web"
    assert "pid=12345" in owner.format_for_humans()
    assert "status=S" in owner.format_for_humans()
    assert "telegram-kol-research web" in owner.format_for_humans()


def test_reap_stopped_session_lock_owner_terminates_stopped_project_process(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    calls = []

    def fake_process_status(pid):
        calls.append(("status", pid))
        return "T"

    def fake_process_command(pid):
        calls.append(("command", pid))
        return "/repo/.venv/bin/telegram-kol-research web"

    killed = []

    def fake_kill(pid):
        killed.append(pid)

    assert reap_stopped_session_lock_owner(
        session_path,
        current_command="/repo/.venv/bin/telegram-kol-research web",
        process_status=fake_process_status,
        process_command=fake_process_command,
        kill_process=fake_kill,
    )
    assert killed == [12345]
    assert calls == [("status", 12345), ("command", 12345)]


def test_reap_stopped_session_lock_owner_leaves_running_process_alone(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    killed = []

    assert not reap_stopped_session_lock_owner(
        session_path,
        current_command="/repo/.venv/bin/telegram-kol-research web",
        process_status=lambda pid: "S",
        process_command=lambda pid: "/repo/.venv/bin/telegram-kol-research web",
        kill_process=lambda pid: killed.append(pid),
    )
    assert killed == []


def test_release_session_lock_owner_terminates_matching_project_pid(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    killed = []

    released_owner = release_session_lock_owner(
        session_path,
        expected_pid=12345,
        current_command="/repo/.venv/bin/telegram-kol-research session-release",
        process_status=lambda pid: "S",
        process_command=lambda pid: "/repo/.venv/bin/telegram-kol-research web",
        kill_process=lambda pid: killed.append(pid),
    )

    assert released_owner is not None
    assert released_owner.pid == 12345
    assert killed == [12345]


def test_release_session_lock_owner_allows_python_module_entrypoint(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    killed = []

    released_owner = release_session_lock_owner(
        session_path,
        expected_pid=12345,
        current_command="telegram-kol-research session-release",
        process_status=lambda pid: "S",
        process_command=lambda pid: (
            "/usr/bin/python -m telegram_kol_research.cli web --host 127.0.0.1 --port 8000"
        ),
        kill_process=lambda pid: killed.append(pid),
    )

    assert released_owner is not None
    assert killed == [12345]


def test_release_session_lock_owner_refuses_pid_mismatch(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    killed = []

    assert (
        release_session_lock_owner(
            session_path,
            expected_pid=99999,
            current_command="/repo/.venv/bin/telegram-kol-research session-release",
            process_status=lambda pid: "S",
            process_command=lambda pid: "/repo/.venv/bin/telegram-kol-research web",
            kill_process=lambda pid: killed.append(pid),
        )
        is None
    )
    assert killed == []


def test_release_session_lock_owner_refuses_non_project_process(tmp_path):
    session_path = tmp_path / "telegram.session"
    lock_path = tmp_path / "telegram.session.lock"
    lock_path.write_text("pid=12345\n", encoding="utf-8")
    killed = []

    assert (
        release_session_lock_owner(
            session_path,
            expected_pid=12345,
            current_command="/repo/.venv/bin/telegram-kol-research session-release",
            process_status=lambda pid: "S",
            process_command=lambda pid: "/usr/bin/python unrelated.py",
            kill_process=lambda pid: killed.append(pid),
        )
        is None
    )
    assert killed == []
