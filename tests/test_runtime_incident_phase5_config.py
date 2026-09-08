import logging

import pytest

from telegram_kol_research import config as config_module
from telegram_kol_research.config import load_runtime_incident_config
from telegram_kol_research.env_file_readability import (
    reset_unreadable_config_files,
    unreadable_config_files,
)


class _CollectingHandler(logging.Handler):
    """Listen on the module logger itself.

    ``configure_application_logging`` sets ``propagate = False`` on the package
    logger for the life of the process, so ``caplog`` -- which listens on the
    root logger -- sees nothing from this package once any test has called it.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_multi_target_projection_defaults_dormant():
    assert hasattr(config_module, "load_multi_target_management_config")
    config = config_module.load_multi_target_management_config(
        environ={},
        env_file_paths=[],
    )

    assert config.projection_enabled is False
    assert config.shadow_only is True
    assert config.live_actions == frozenset()


def test_multi_target_live_actions_are_closed_and_normalized():
    assert hasattr(config_module, "load_multi_target_management_config")
    config = config_module.load_multi_target_management_config(
        environ={
            "TELEGRAM_KOL_MULTI_TARGET_PROJECTION_ENABLED": "true",
            "TELEGRAM_KOL_MULTI_TARGET_SHADOW_ONLY": "false",
            "TELEGRAM_KOL_MULTI_TARGET_LIVE_ACTIONS": (
                " partial_take_profit,EXIT_FULL,add_position "
            ),
        },
        env_file_paths=[],
    )

    assert config.projection_enabled is True
    assert config.shadow_only is False
    assert config.live_actions == frozenset({"partial_take_profit"})


def test_multi_target_live_gate_cannot_bypass_action_graduation():
    config = config_module.MultiTargetManagementConfig(
        projection_enabled=True,
        shadow_only=False,
        live_actions=frozenset({"partial_take_profit", "exit_full"}),
    )

    assert config.action_is_live("partial_take_profit") is True
    assert config.action_is_live("exit_full") is False


def test_phase5_shadow_playbooks_are_dormant_and_exact_allowlisted():
    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS": (
                " refresh_read_only_exchange_snapshot,"
                "retry_business_instruction,REFRESH_READ_ONLY_EXCHANGE_SNAPSHOT "
            )
        },
        env_file_paths=[],
    )

    assert dormant.agent_shadow_playbooks == frozenset()
    assert enabled.agent_shadow_playbooks == frozenset(
        {
            "refresh_read_only_exchange_snapshot",
            "retry_business_instruction",
        }
    )
    assert dormant.feature_policy_version == "runtime-incident-phase-6-v1"


def test_phase6_action_authority_is_dormant_bounded_and_exact_allowlisted():
    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS": (
                " refresh_read_only_exchange_snapshot,"
                "RETRY_BUSINESS_INSTRUCTION "
            ),
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_CIRCUIT_THRESHOLD": "99",
        },
        env_file_paths=[],
    )

    assert dormant.agent_actions_enabled is False
    assert dormant.agent_action_playbooks == frozenset()
    assert enabled.agent_actions_enabled is True
    assert enabled.agent_action_playbooks == frozenset(
        {
            "refresh_read_only_exchange_snapshot",
            "retry_business_instruction",
        }
    )
    assert enabled.agent_action_circuit_threshold == 5


def test_message_operation_agent_gate_is_dormant_and_watermarked():
    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_AGENT_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_AGENT_AFTER_CONTRACT_ID": "27",
            "TELEGRAM_KOL_RUNTIME_AGENT_DEPLOYED_CODE_VERSION": "abc123",
        },
        env_file_paths=[],
    )

    assert dormant.message_operation_agent_enabled is False
    assert dormant.message_operation_agent_after_contract_id == 2**63 - 1
    assert enabled.message_operation_agent_enabled is True
    assert enabled.message_operation_agent_after_contract_id == 27
    assert enabled.agent_deployed_code_version == "abc123"


def test_runtime_config_uses_systemd_environment_when_secret_file_is_unreadable(
    tmp_path,
    monkeypatch,
):
    secret_file = tmp_path / "runtime_incident_agent.env"
    secret_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(secret_file):
            raise PermissionError("root-owned secret")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    config = load_runtime_incident_config(
        environ={"TELEGRAM_KOL_RUNTIME_AGENT_ENABLED": "true"},
        env_file_paths=[secret_file],
    )

    assert config.agent_enabled is True


def test_runtime_config_does_not_hide_unreadable_non_secret_config(
    tmp_path,
    monkeypatch,
):
    """The principle is kept; the mechanism that carried it is not.

    This used to require ``PermissionError`` to propagate, so that an
    unreadable non-secret config could never be silently ignored. The
    principle stands -- an operator must find out -- but raising was the wrong
    carrier for it. These loaders run once per message, so from 2026-09-04 the
    worker (running as ``telegram-kol-worker``, against a ``root`` 0600
    ``config/telegram.env``) failed the message instead of reporting the
    permission mistake: the job failed and its attempt landed
    ``authoritative_execution_outcome_unknown``.

    Visibility now rides on two carriers that cost a message nothing: one
    warning per path per process, and the path in the deployment-identity
    health endpoint as ``unreadable_config_files``. Loading continues.
    """

    config_file = tmp_path / "telegram.env"
    config_file.write_text("SECRET_KEY=must-not-be-read\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(config_file):
            raise PermissionError("unexpected permissions")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    reset_unreadable_config_files()
    handler = _CollectingHandler()
    logger = logging.getLogger("telegram_kol_research.llm_chat")
    logger.addHandler(handler)

    try:
        # Loading continues rather than raising.
        config = load_runtime_incident_config(
            environ={},
            env_file_paths=[config_file],
        )
        # Once per path per process, not once per message.
        load_runtime_incident_config(environ={}, env_file_paths=[config_file])
    finally:
        logger.removeHandler(handler)
        reset_unreadable_config_files_after = unreadable_config_files()

    assert config is not None
    assert [str(config_file)] == list(reset_unreadable_config_files_after)
    warnings = [
        message for message in handler.messages if str(config_file) in message
    ]
    assert len(warnings) == 1
    assert not any("must-not-be-read" in message for message in handler.messages)


def test_runtime_config_prefers_complete_systemd_environment_over_unreadable_defaults(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "telegram.env"
    config_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(config_file):
            raise PermissionError("root-owned unrelated config")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", "false")

    config = load_runtime_incident_config(
        env_file_paths=[config_file],
        environment_only=True,
    )

    assert config.agent_enabled is False


def test_runtime_config_partial_environment_keeps_file_backed_capture_settings(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "telegram.env"
    config_file.write_text(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES="
        "worker_retry_exhausted\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", "false")
    monkeypatch.delenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", raising=False
    )

    config = load_runtime_incident_config(env_file_paths=[config_file])

    assert config.captures("worker_retry_exhausted") is True
