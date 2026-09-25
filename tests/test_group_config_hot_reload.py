"""甲 (2026-09-25): the group switches save, and every process notices.

Two failures were paired in production and they must stay paired in the tests.
The web role could not write ``config/groups.yaml`` at all, and nothing ever
re-read it -- so a fix for the first alone would have produced the worse
outcome: a page saying 自动交易 已关闭 over a worker still trading on the copy
it parsed at startup. Every reload test below therefore asserts what happens to
``app.state.group_config`` itself, not merely what the function returned.
"""

import asyncio
import contextlib
import errno
import logging
import os

import pytest
from fastapi.testclient import TestClient

import telegram_kol_research.web_app as web_app_module
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.web_app import (
    _format_group_config_write_check_for_log,
    _group_config_write_check,
    _refresh_group_config_from_disk,
    _run_group_config_reload_loop,
    create_web_app,
)


NOTIFY_ONLY_YAML = (
    "groups:\n"
    "  - chat_title: Demo Group\n"
    "    chat_id: 77\n"
    "    ai_strategy_enabled: false\n"
    "    trading_mode: notify_only\n"
)
AUTO_TRADE_YAML = (
    "groups:\n"
    "  - chat_title: Demo Group\n"
    "    chat_id: 77\n"
    "    ai_strategy_enabled: true\n"
    "    trading_mode: auto_trade\n"
)


def _app(tmp_path, *, yaml_text: str = NOTIFY_ONLY_YAML, runtime_role: str = "all"):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role=runtime_role,
        group_config=load_group_config(config_path),
        group_config_path=config_path,
    )
    return app, config_path


@contextlib.contextmanager
def _collect_web_app_logs():
    """Collect this module's own log lines.

    A handler on the package logger rather than ``caplog``:
    ``configure_application_logging`` sets ``propagate = False`` on
    ``telegram_kol_research``, so nothing these paths log ever reaches the root
    handler pytest installs.
    """

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    target = logging.getLogger("telegram_kol_research.web_app")
    handler = _Collect(level=logging.INFO)
    prior_level = target.level
    prior_disable = logging.root.manager.disable
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    logging.disable(logging.NOTSET)
    try:
        yield lines
    finally:
        target.removeHandler(handler)
        target.setLevel(prior_level)
        logging.disable(prior_disable)


def _count_parses(monkeypatch) -> list[str]:
    """Record every ``load_group_config`` the reload path performs."""

    parsed: list[str] = []
    real = web_app_module.load_group_config

    def _counting(path):
        parsed.append(str(path))
        return real(path)

    monkeypatch.setattr(web_app_module, "load_group_config", _counting)
    return parsed


def _rewrite(config_path, text: str) -> None:
    """Write new content and make sure the stat signature really moved.

    A same-length rewrite inside one filesystem timestamp tick is exactly the
    case the signature must still catch, and on a coarse-grained clock the test
    would otherwise pass for the wrong reason.
    """

    config_path.write_text(text, encoding="utf-8")
    stat_result = os.stat(config_path)
    os.utime(
        config_path,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
    )


def test_an_unchanged_file_is_not_reparsed(tmp_path, monkeypatch):
    app, _ = _app(tmp_path)
    parsed = _count_parses(monkeypatch)
    before = app.state.group_config

    assert _refresh_group_config_from_disk(app) == "unchanged"

    assert parsed == []
    assert app.state.group_config is before


def test_a_changed_file_replaces_the_configuration_as_one_object(tmp_path):
    app, config_path = _app(tmp_path)
    before = app.state.group_config
    assert before.groups[0].trading_mode == "notify_only"

    _rewrite(config_path, AUTO_TRADE_YAML)

    assert _refresh_group_config_from_disk(app) == "reloaded"
    assert app.state.group_config is not before
    assert app.state.group_config.groups[0].trading_mode == "auto_trade"
    assert app.state.group_config.groups[0].ai_strategy_enabled is True
    # The old object is still intact: readers holding a reference see the whole
    # previous configuration rather than a half-updated one.
    assert before.groups[0].trading_mode == "notify_only"


def test_a_parse_failure_keeps_the_loaded_copy_and_retries_next_tick(tmp_path):
    app, config_path = _app(tmp_path, yaml_text=AUTO_TRADE_YAML)
    before = app.state.group_config
    baseline = app.state.group_config_stat_signature

    # What a reader can actually see while ``write_text`` is in progress.
    _rewrite(config_path, "groups:\n  - chat_title: Demo Group\n    chat_id: [")

    assert _refresh_group_config_from_disk(app) == "parse_failed"
    assert app.state.group_config is before
    assert app.state.group_config.groups[0].trading_mode == "auto_trade"
    # Baseline untouched, so the next tick looks again instead of accepting the
    # broken file as the new normal.
    assert app.state.group_config_stat_signature == baseline

    _rewrite(config_path, NOTIFY_ONLY_YAML)
    assert _refresh_group_config_from_disk(app) == "reloaded"
    assert app.state.group_config.groups[0].trading_mode == "notify_only"


def test_zero_groups_keeps_the_loaded_copy(tmp_path):
    app, config_path = _app(tmp_path, yaml_text=AUTO_TRADE_YAML)
    before = app.state.group_config
    baseline = app.state.group_config_stat_signature

    _rewrite(config_path, "groups: []\n")

    assert _refresh_group_config_from_disk(app) == "empty"
    # An empty parse must never retire a group's switches: "unknown chat" reads
    # as "not auto_trade" everywhere downstream, which would silently disarm a
    # trading group on a truncated read.
    assert app.state.group_config is before
    assert app.state.group_config.groups[0].trading_mode == "auto_trade"
    assert app.state.group_config_stat_signature == baseline


def test_a_missing_file_keeps_the_loaded_copy(tmp_path):
    app, config_path = _app(tmp_path, yaml_text=AUTO_TRADE_YAML)
    before = app.state.group_config

    config_path.unlink()

    assert _refresh_group_config_from_disk(app) == "stat_failed"
    assert app.state.group_config is before


def test_no_configured_path_means_no_reload(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")

    assert app.state.group_config_path is None
    assert app.state.group_config_stat_signature is None
    assert _refresh_group_config_from_disk(app) == "unconfigured"


def test_the_endpoint_moves_the_baseline_with_the_file(tmp_path, monkeypatch):
    app, config_path = _app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/groups/77/automation",
        json={"chat_title": "Demo Group", "auto_trade_enabled": True},
    )

    assert response.status_code == 200
    assert response.json()["auto_trade_enabled"] is True
    assert app.state.group_config.groups[0].trading_mode == "auto_trade"
    assert load_group_config(config_path).groups[0].trading_mode == "auto_trade"

    # The writing process is already correct, so the poll must not spend a
    # parse re-reading what it just wrote.
    parsed = _count_parses(monkeypatch)
    assert _refresh_group_config_from_disk(app) == "unchanged"
    assert parsed == []


def test_the_endpoint_answers_503_in_chinese_when_the_file_is_read_only(
    tmp_path, monkeypatch
):
    app, config_path = _app(tmp_path)

    def _read_only(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system", str(config_path))

    monkeypatch.setattr(
        web_app_module, "update_group_automation_settings", _read_only
    )
    before = app.state.group_config

    with _collect_web_app_logs() as log_lines:
        response = TestClient(app).post(
            "/api/groups/77/automation",
            json={"chat_title": "Demo Group", "auto_trade_enabled": True},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "群组配置文件不可写（服务器只读挂载或权限），开关未保存"
    )
    # Nothing moved in this process either: the page must not show a switch the
    # server did not save.
    assert app.state.group_config is before
    assert load_group_config(config_path).groups[0].trading_mode == "notify_only"
    # The errno is the server-side fix's address, so it belongs in the log.
    assert any(
        "EROFS" in line and "not saved" in line for line in log_lines
    )


def test_the_reload_loop_picks_up_a_change_and_cancels_cleanly(tmp_path):
    app, config_path = _app(tmp_path)

    async def _exercise():
        task = asyncio.create_task(
            _run_group_config_reload_loop(app, interval_seconds=0.01)
        )
        _rewrite(config_path, AUTO_TRADE_YAML)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if app.state.group_config.groups[0].trading_mode == "auto_trade":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(_exercise())

    assert app.state.group_config.groups[0].trading_mode == "auto_trade"
    assert task.cancelled()


def test_the_web_role_starts_the_poll_and_logs_whether_saving_can_work(tmp_path):
    # The ``web`` role on purpose: it starts no singleton task of its own, so
    # what this test observes is this change's wiring and nothing else.
    app, config_path = _app(tmp_path, runtime_role="web")

    with _collect_web_app_logs() as log_lines:
        with TestClient(app):
            assert app.state.group_config_reload_task is not None
        assert app.state.group_config_reload_task is None

    lines = [
        line for line in log_lines if line.startswith("group_config_write_check ")
    ]
    assert len(lines) == 1
    assert f"path={config_path}" in lines[0]
    assert "exists=true" in lines[0]
    assert "writable=true" in lines[0]
    assert "reason=ok" in lines[0]


def test_the_write_check_names_the_two_independent_reasons(tmp_path):
    config_path = tmp_path / "groups.yaml"

    assert _group_config_write_check(None) == {
        "exists": False,
        "writable": False,
        "reason": "not_configured",
    }
    assert _group_config_write_check(config_path)["reason"] == "missing"

    config_path.write_text(NOTIFY_ONLY_YAML, encoding="utf-8")
    assert _group_config_write_check(config_path) == {
        "exists": True,
        "writable": True,
        "reason": "ok",
    }

    os.chmod(config_path, 0o444)
    try:
        report = _group_config_write_check(config_path)
    finally:
        os.chmod(config_path, 0o644)
    # Production had the mount closed *and* the mode at 0640; the file mode half
    # has to be reported on its own, because opening the mount does not fix it.
    assert report == {
        "exists": True,
        "writable": False,
        "reason": "no_write_permission",
    }
    assert "writable=false" in _format_group_config_write_check_for_log(
        tmp_path / "absent.yaml"
    )
