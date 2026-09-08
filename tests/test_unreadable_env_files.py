"""An env file this process cannot read must not fail a message.

Production ran the opposite from 2026-09-04 to 2026-09-08: the worker runs as
``telegram-kol-worker`` while ``config/telegram.env`` is ``root`` 0600, so
every authoritative message that reached ``load_multi_target_management_config``
raised ``PermissionError`` out of the env loader, failed its processing job,
and left its attempt on ``authoritative_execution_outcome_unknown``.
"""

import inspect
import logging
import os

import pytest

from telegram_kol_research import config as config_module
from telegram_kol_research import llm_chat, telegram_client
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.env_file_readability import (
    reset_unreadable_config_files,
    unreadable_config_files,
)
from telegram_kol_research.runtime_deployment_identity import (
    build_runtime_deployment_identity,
)


@pytest.fixture(autouse=True)
def _clean_readability_record():
    """The record is process-local, so each case has to start from empty."""

    reset_unreadable_config_files()
    yield
    reset_unreadable_config_files()

# The repository holds two independent loaders with the same name and the same
# job. Both are covered here, because fixing one leaves the other raising.
LOADERS = (
    ("llm_chat", llm_chat),
    ("telegram_client", telegram_client),
)


class _Records(logging.Handler):
    """Collect one logger's records without relying on propagation.

    ``configure_application_logging`` sets ``propagate = False`` on the package
    logger for the life of the process, so once any test has called it
    ``caplog`` -- which listens on the root logger -- sees nothing from this
    package. Listening on the module's own logger is order independent.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _warnings_from(module):
    handler = _Records()
    logger = logging.getLogger(module.__name__)
    logger.addHandler(handler)
    return handler, logger


def _write_unreadable(path):
    path.write_text("SECRET_KEY=must-not-be-read\n", encoding="utf-8")
    path.chmod(0o000)
    if os.access(path, os.R_OK):
        # Root ignores the mode bits, so the case under test cannot be built.
        pytest.skip("this user can read a 0000 file; cannot exercise the case")
    return path


@pytest.mark.parametrize("name,module", LOADERS, ids=[n for n, _ in LOADERS])
def test_an_unreadable_file_is_skipped_and_loading_continues(
    name, module, tmp_path
):
    unreadable = _write_unreadable(tmp_path / "unreadable.env")
    readable = tmp_path / "readable.env"
    readable.write_text("WANTED=yes\n", encoding="utf-8")
    handler, logger = _warnings_from(module)

    try:
        values = module._load_env_file_values([unreadable, readable])
    finally:
        logger.removeHandler(handler)

    # The readable file after it still loads: the loop continues, not aborts.
    assert values == {"WANTED": "yes"}
    assert "SECRET_KEY" not in values
    # The path is named so an operator can fix it; the content never is.
    assert any(str(unreadable) in message for message in handler.messages)
    assert not any("must-not-be-read" in message for message in handler.messages)


@pytest.mark.parametrize("name,module", LOADERS, ids=[n for n, _ in LOADERS])
def test_an_unreadable_file_alone_yields_no_values_and_does_not_raise(
    name, module, tmp_path
):
    unreadable = _write_unreadable(tmp_path / "only.env")

    assert module._load_env_file_values([unreadable]) == {}


def test_the_production_call_survives_an_unreadable_telegram_env(
    tmp_path, monkeypatch
):
    """The exact chain that failed: config -> llm_chat loader -> PermissionError."""

    unreadable = _write_unreadable(tmp_path / "telegram.env")
    monkeypatch.setenv("TELEGRAM_KOL_MULTI_TARGET_LIVE_ACTIONS", "")

    loaded = config_module.load_multi_target_management_config(
        env_file_paths=[unreadable]
    )

    assert loaded is not None


def test_a_readable_file_is_still_read(tmp_path):
    readable = tmp_path / "readable.env"
    readable.write_text("A=1\n# comment\n\nB = \"2\"\n", encoding="utf-8")

    for _, module in LOADERS:
        assert module._load_env_file_values([readable]) == {"A": "1", "B": "2"}


def test_neither_loader_takes_a_per_file_exemption_any_more():
    """One rule for every file, so no caller can be the one left raising.

    ``llm_chat`` used to fail open only for a filename an earlier caller had
    opted into, which is why ``config/telegram.env`` still raised in
    production. A parameter that lets one caller keep the old behaviour would
    let this recur, so there is no longer one.
    """

    for _, module in LOADERS:
        signature = inspect.signature(module._load_env_file_values)
        assert "ignore_unreadable_names" not in signature.parameters


def _record_env_reads(monkeypatch, seen):
    def record(real):
        def wrapper(env_file_paths=None, *args, **kwargs):
            seen.extend(
                os.fspath(path)
                for path in (
                    real.__defaults__[0]
                    if env_file_paths is None
                    else env_file_paths
                )
                or []
            )
            return real(env_file_paths, *args, **kwargs)

        return wrapper

    # ``config`` imports the loader by name, so it needs its own patch.
    for module in (llm_chat, config_module, telegram_client):
        monkeypatch.setattr(
            module,
            "_load_env_file_values",
            record(module._load_env_file_values),
        )


@pytest.mark.parametrize(
    "runtime_role,reads_telegram_env",
    [
        # ``all`` is the single-process development mode and still reads it.
        # Its presence here is what proves the recorder is live, so the split
        # roles below cannot pass vacuously.
        ("all", True),
        ("worker", False),
        ("web", False),
        ("ingest", False),
    ],
)
def test_only_the_single_process_role_reads_the_telegram_env_file(
    runtime_role, reads_telegram_env, tmp_path, monkeypatch
):
    """The worker must not read it at startup nor once per message.

    Startup was already role aware -- every split role passes an empty path
    list. The per-message read came from ``reanalyze`` omitting the config
    startup had already resolved, which sent
    ``apply_authoritative_mimo_payload`` back to the module default
    ``[".env", "config/telegram.env"]``.
    """

    seen: list[str] = []
    _record_env_reads(monkeypatch, seen)

    web_app_module.create_web_app(
        database_path=tmp_path / f"{runtime_role}.db",
        runtime_role=runtime_role,
    )

    hits = [path for path in seen if "telegram.env" in path]
    assert bool(hits) is reads_telegram_env, seen


def test_reanalyze_hands_down_the_config_startup_already_resolved():
    """The one missing argument that caused the per-message read.

    ``_run_authoritative_processor`` always passed it; the context-reanalysis
    twin did not, and that is the call that failed in production.
    """

    source = inspect.getsource(
        web_app_module._run_context_resolution_worker_for_app
    )
    reanalyze = source[source.index("    def reanalyze("):]
    reanalyze = reanalyze[: reanalyze.index("    def is_eligible(")]

    assert "multi_target_management_config=" in reanalyze


def test_the_warning_fires_once_per_path_no_matter_how_many_messages(tmp_path):
    """These loaders run once per message; warning every time buries the fact."""

    unreadable = _write_unreadable(tmp_path / "telegram.env")
    handler, logger = _warnings_from(llm_chat)

    try:
        for _ in range(5):
            llm_chat._load_env_file_values([unreadable])
    finally:
        logger.removeHandler(handler)

    assert len([m for m in handler.messages if str(unreadable) in m]) == 1


def test_health_reports_every_unreadable_path_it_skipped(tmp_path):
    """Not raising must not become not telling anyone.

    The deployment-identity endpoint is contractually I/O-free, so the paths
    ride in a process-local set that costs a memory read to report.
    """

    first = _write_unreadable(tmp_path / "telegram.env")
    second = _write_unreadable(tmp_path / "llm.env")

    llm_chat._load_env_file_values([first])
    telegram_client._load_env_file_values([second])

    assert unreadable_config_files() == tuple(sorted([str(first), str(second)]))
    identity = build_runtime_deployment_identity(
        runtime_role="worker",
        module_path=__file__,
        expected_commit="",
        expected_manifest_sha256="",
        tasks={},
    )
    assert identity["health"]["unreadable_config_files"] == [
        *sorted([str(first), str(second)])
    ]


def test_health_reports_nothing_when_every_file_was_readable(tmp_path):
    readable = tmp_path / "readable.env"
    readable.write_text("A=1\n", encoding="utf-8")

    llm_chat._load_env_file_values([readable])

    identity = build_runtime_deployment_identity(
        runtime_role="worker",
        module_path=__file__,
        expected_commit="",
        expected_manifest_sha256="",
        tasks={},
    )
    assert identity["health"]["unreadable_config_files"] == []
