"""Shared configuration defaults for the local research app."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from telegram_kol_research.llm_chat import _load_env_file_values
from telegram_kol_research.management_directives import (
    MULTI_TARGET_LIVE_ACTION_NAMES,
)


READ_ONLY_CAPTURE_PROFILE = frozenset(
    {
        "provider_retry_exhausted",
        "context_worker_exhausted",
        "management_submit_unknown",
        "management_partial_failed",
        "management_recovery_required",
        "severe_protection_incident",
        "monitor_adapter_failure",
        "monitor_audit_incomplete",
        "notification_delivery_failure",
    }
)
_SQLITE_MAX_INTEGER = 2**63 - 1
MULTI_TARGET_CAPTURE_PROFILE = frozenset(
    {
        "management_target_refused",
        "management_target_orchestration_failed",
        "management_target_visibility_exhausted",
        "management_target_drift",
        "management_target_collision",
        "unclassified_operation_failure",
    }
)

RUNTIME_SCANNER_RULE_IDS = frozenset(
    {
        "terminal_lifecycle_exchange_exposure_v1",
        "active_position_missing_protection_v1",
        "cancel_outcome_stale_unknown_v1",
        "tp1_break_even_nonterminal_v1",
        "monitor_incident_ledger_silence_v1",
        "terminal_high_risk_management_without_instruction_v1",
        "verified_replacement_role_gap_v1",
        "management_safety_gate_divergence_v1",
        "admitted_target_item_nonterminal_after_deadline_v1",
        "management_target_batch_state_inconsistent_v1",
        "instruction_execution_contradiction_v1",
    }
)
RUNTIME_SCANNER_DEPLOYABLE_RULE_IDS = frozenset(
    {
        "active_position_missing_protection_v1",
        "cancel_outcome_stale_unknown_v1",
        "management_safety_gate_divergence_v1",
        "instruction_execution_contradiction_v1",
    }
)


@dataclass(slots=True)
class AppConfig:
    base_dir: Path = Path.cwd()
    data_dir: Path = Path("data")
    session_path: Path = Path("data/telegram.session")
    database_path: Path = Path("data/research.db")


@dataclass(frozen=True, slots=True)
class RuntimeIncidentConfig:
    """Fail-closed feature flags and budgets for runtime incident handling."""

    capture_types: frozenset[str] = frozenset()
    telegram_notifications_enabled: bool = False
    telegram_notification_types: frozenset[str] | None = None
    telegram_notification_after_incident_id: int | None = None
    notification_lease_seconds: float = 120.0
    message_operation_stage1_enabled: bool = False
    message_operation_stage1_after_contract_id: int = _SQLITE_MAX_INTEGER
    message_operation_stage1_max_attempts: int = 5
    message_operation_stage2_enabled: bool = False
    message_operation_stage2_after_handoff_id: int = _SQLITE_MAX_INTEGER
    message_operation_stage2_max_attempts: int = 5
    agent_enabled: bool = False
    agent_incident_types: frozenset[str] | None = None
    message_operation_agent_enabled: bool = False
    message_operation_agent_after_contract_id: int = _SQLITE_MAX_INTEGER
    agent_deployed_code_version: str = "unknown"
    agent_max_tool_steps: int = 4
    agent_max_wall_seconds: float = 45.0
    agent_max_prompt_bytes: int = 16_384
    agent_max_tool_output_bytes: int = 8192
    agent_claim_lease_seconds: float = 120.0
    agent_token_budget_enabled: bool = False
    agent_per_incident_token_limit: int = 65_536
    agent_daily_token_limit: int = 500_000
    agent_max_completion_tokens: int = 4096
    agent_shadow_playbooks: frozenset[str] = frozenset()
    agent_actions_enabled: bool = False
    agent_action_playbooks: frozenset[str] = frozenset()
    agent_action_circuit_threshold: int = 3
    monitor_capture_token: str | None = None
    feature_policy_version: str = "runtime-incident-phase-6-v1"
    prompt_version: str = "runtime-agent-prompt-v8"
    tool_policy_version: str = "runtime-agent-tools-v2"

    def captures(self, incident_type: str) -> bool:
        return incident_type in self.capture_types

    def notifies(self, incident_type: str) -> bool:
        return self.telegram_notifications_enabled and (
            self.telegram_notification_types is None
            or incident_type in self.telegram_notification_types
        )

    def diagnoses(self, incident_type: str) -> bool:
        return self.agent_enabled and (
            self.agent_incident_types is None
            or incident_type in self.agent_incident_types
        )


@dataclass(frozen=True, slots=True)
class RuntimeScannerConfig:
    enabled: bool = False
    shadow_only: bool = True
    rules: frozenset[str] = frozenset()
    interval_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class MessageOperationSupervisorConfig:
    """Fail-closed Phase 8R.5 shadow projection settings."""

    enabled: bool = False
    shadow_only: bool = True
    after_raw_message_id: int = _SQLITE_MAX_INTEGER
    batch_limit: int = 50


MESSAGE_OPERATION_SUPERVISOR_POLICY_STATUSES = frozenset(
    {
        "disabled",
        "valid",
        "invalid_missing_message_operation_failure_capture",
    }
)


def message_operation_supervisor_policy_status(
    supervisor_config: MessageOperationSupervisorConfig,
    runtime_incident_config: RuntimeIncidentConfig,
) -> str:
    """Return the bounded startup policy state for the shadow supervisor."""

    if not supervisor_config.enabled:
        return "disabled"
    if not runtime_incident_config.captures("message_operation_failure"):
        return "invalid_missing_message_operation_failure_capture"
    return "valid"


@dataclass(frozen=True, slots=True)
class MultiTargetManagementConfig:
    """Dormant-by-default projection and live-action policy."""

    projection_enabled: bool = False
    shadow_only: bool = True
    live_actions: frozenset[str] = frozenset()

    def action_is_live(self, action: str) -> bool:
        return (
            self.projection_enabled
            and not self.shadow_only
            and str(action).strip().lower() in MULTI_TARGET_LIVE_ACTION_NAMES
            and str(action).strip().lower() in self.live_actions
        )


def load_multi_target_management_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> MultiTargetManagementConfig:
    paths = (
        [".env", "config/telegram.env"]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(os.environ if environ is None else environ)
    requested_actions = {
        item.strip().lower()
        for item in env.get(
            "TELEGRAM_KOL_MULTI_TARGET_LIVE_ACTIONS", ""
        ).split(",")
        if item.strip()
    }
    return MultiTargetManagementConfig(
        projection_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_MULTI_TARGET_PROJECTION_ENABLED")
        ),
        shadow_only=not (
            str(
                env.get("TELEGRAM_KOL_MULTI_TARGET_SHADOW_ONLY", "true")
            ).strip().lower()
            in {"0", "false", "no", "off"}
        ),
        live_actions=frozenset(
            requested_actions.intersection(MULTI_TARGET_LIVE_ACTION_NAMES)
        ),
    )


def load_runtime_scanner_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> RuntimeScannerConfig:
    paths = ["config/runtime_incident_agent.env"] if env_file_paths is None else env_file_paths
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(os.environ if environ is None else environ)
    requested = {
        item.strip().lower()
        for item in env.get("TELEGRAM_KOL_RUNTIME_SCANNER_RULES", "").split(",")
        if item.strip()
    }
    try:
        interval = float(env.get("TELEGRAM_KOL_RUNTIME_SCANNER_INTERVAL_SECONDS", "60"))
    except (TypeError, ValueError):
        interval = 60.0
    return RuntimeScannerConfig(
        enabled=_enabled_flag(env.get("TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED")),
        shadow_only=not (
            str(env.get("TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY", "true"))
            .strip().lower() in {"0", "false", "no", "off"}
        ),
        rules=frozenset(requested.intersection(RUNTIME_SCANNER_DEPLOYABLE_RULE_IDS)),
        interval_seconds=max(10.0, min(interval, 3600.0)),
    )


def load_message_operation_supervisor_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> MessageOperationSupervisorConfig:
    paths = (
        ["config/runtime_incident_agent.env"]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(os.environ if environ is None else environ)
    try:
        after_raw_message_id = int(
            env[
                "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID"
            ]
        )
    except (KeyError, TypeError, ValueError):
        after_raw_message_id = _SQLITE_MAX_INTEGER
    if not 0 <= after_raw_message_id <= _SQLITE_MAX_INTEGER:
        after_raw_message_id = _SQLITE_MAX_INTEGER
    try:
        batch_limit = int(
            env.get("TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_BATCH_LIMIT", "50")
        )
    except (TypeError, ValueError):
        batch_limit = 50
    return MessageOperationSupervisorConfig(
        enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED")
        ),
        shadow_only=not (
            str(
                env.get(
                    "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY",
                    "true",
                )
            ).strip().lower()
            in {"0", "false", "no", "off"}
        ),
        after_raw_message_id=after_raw_message_id,
        batch_limit=max(1, min(batch_limit, 100)),
    )


def _enabled_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _notification_after_incident_id(env: dict[str, str]) -> int | None:
    key = "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID"
    if key not in env:
        return None
    try:
        value = int(env[key])
    except (TypeError, ValueError):
        return _SQLITE_MAX_INTEGER
    if not 0 <= value <= _SQLITE_MAX_INTEGER:
        return _SQLITE_MAX_INTEGER
    return value


def _message_operation_stage1_after_contract_id(env: dict[str, str]) -> int:
    key = "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_AFTER_CONTRACT_ID"
    try:
        value = int(env[key])
    except (KeyError, TypeError, ValueError):
        return _SQLITE_MAX_INTEGER
    if not 0 <= value <= _SQLITE_MAX_INTEGER:
        return _SQLITE_MAX_INTEGER
    return value


def _message_operation_agent_after_contract_id(env: dict[str, str]) -> int:
    key = "TELEGRAM_KOL_MESSAGE_OPERATION_AGENT_AFTER_CONTRACT_ID"
    try:
        value = int(env[key])
    except (KeyError, TypeError, ValueError):
        return _SQLITE_MAX_INTEGER
    if not 0 <= value <= _SQLITE_MAX_INTEGER:
        return _SQLITE_MAX_INTEGER
    return value


def _message_operation_stage2_after_handoff_id(env: dict[str, str]) -> int:
    key = "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_AFTER_HANDOFF_ID"
    try:
        value = int(env[key])
    except (KeyError, TypeError, ValueError):
        return _SQLITE_MAX_INTEGER
    if not 0 <= value <= _SQLITE_MAX_INTEGER:
        return _SQLITE_MAX_INTEGER
    return value


def _deployed_code_version(env: dict[str, str]) -> str:
    value = str(
        env.get("TELEGRAM_KOL_RUNTIME_AGENT_DEPLOYED_CODE_VERSION", "unknown")
    ).strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else "unknown"


def load_runtime_incident_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
    *,
    environment_only: bool = False,
) -> RuntimeIncidentConfig:
    """Load dormant-by-default runtime incident flags from bounded settings."""

    paths = (
        [".env", "config/telegram.env", "config/runtime_incident_agent.env"]
        if env_file_paths is None
        else env_file_paths
    )
    active_environment = os.environ if environ is None else environ
    if environment_only:
        env: dict[str, str] = {}
    else:
        env = dict(
            _load_env_file_values(
                paths,
                ignore_unreadable_names=frozenset(
                    {"runtime_incident_agent.env"}
                ),
            )
            if paths
            else {}
        )
    env.update(active_environment)
    capture_types = frozenset(
        item.strip().lower()
        for item in env.get(
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", ""
        ).split(",")
        if item.strip()
    )
    telegram_types_key = "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES"
    telegram_notification_types = (
        frozenset(
            item.strip().lower()
            for item in env.get(telegram_types_key, "").split(",")
            if item.strip()
        )
        if telegram_types_key in env
        else None
    )
    agent_shadow_playbooks = frozenset(
        item.strip().lower()
        for item in env.get(
            "TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS", ""
        ).split(",")
        if item.strip()
    )
    agent_types_key = "TELEGRAM_KOL_RUNTIME_AGENT_TYPES"
    agent_incident_types = (
        frozenset(
            item.strip().lower()
            for item in env.get(agent_types_key, "").split(",")
            if item.strip()
        )
        if agent_types_key in env
        else None
    )
    agent_action_playbooks = frozenset(
        item.strip().lower()
        for item in env.get(
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS", ""
        ).split(",")
        if item.strip()
    )
    try:
        lease_seconds = float(
            env.get(
                "TELEGRAM_KOL_RUNTIME_INCIDENT_NOTIFICATION_LEASE_SECONDS",
                "120",
            )
        )
    except (TypeError, ValueError):
        lease_seconds = 120.0
    try:
        message_operation_stage1_max_attempts = int(
            env.get(
                "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_MAX_ATTEMPTS", "5"
            )
        )
    except (TypeError, ValueError):
        message_operation_stage1_max_attempts = 5
    try:
        message_operation_stage2_max_attempts = int(
            env.get(
                "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_MAX_ATTEMPTS", "5"
            )
        )
    except (TypeError, ValueError):
        message_operation_stage2_max_attempts = 5
    try:
        agent_tool_steps = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_STEPS", "4")
        )
    except (TypeError, ValueError):
        agent_tool_steps = 4
    try:
        agent_wall_seconds = float(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_MAX_WALL_SECONDS", "45")
        )
    except (TypeError, ValueError):
        agent_wall_seconds = 45.0
    try:
        agent_prompt_bytes = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_MAX_PROMPT_BYTES", "16384")
        )
    except (TypeError, ValueError):
        agent_prompt_bytes = 16_384
    try:
        agent_tool_output_bytes = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_OUTPUT_BYTES", "8192")
        )
    except (TypeError, ValueError):
        agent_tool_output_bytes = 8192
    try:
        agent_claim_lease_seconds = float(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_CLAIM_LEASE_SECONDS", "120")
        )
    except (TypeError, ValueError):
        agent_claim_lease_seconds = 120.0
    try:
        agent_action_circuit_threshold = int(
            env.get(
                "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_CIRCUIT_THRESHOLD", "3"
            )
        )
    except (TypeError, ValueError):
        agent_action_circuit_threshold = 3
    try:
        agent_per_incident_token_limit = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_PER_INCIDENT_TOKEN_LIMIT", "65536")
        )
    except (TypeError, ValueError):
        agent_per_incident_token_limit = 65_536
    try:
        agent_daily_token_limit = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_DAILY_TOKEN_LIMIT", "500000")
        )
    except (TypeError, ValueError):
        agent_daily_token_limit = 500_000
    try:
        agent_max_completion_tokens = int(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_MAX_COMPLETION_TOKENS", "4096")
        )
    except (TypeError, ValueError):
        agent_max_completion_tokens = 4096
    raw_monitor_capture_token = env.get(
        "TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN"
    )
    monitor_capture_token = (
        raw_monitor_capture_token
        if raw_monitor_capture_token is not None
        and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", raw_monitor_capture_token)
        else None
    )
    return RuntimeIncidentConfig(
        capture_types=capture_types,
        telegram_notifications_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED")
        ),
        telegram_notification_types=telegram_notification_types,
        telegram_notification_after_incident_id=(
            _notification_after_incident_id(env)
        ),
        notification_lease_seconds=max(5.0, min(lease_seconds, 3600.0)),
        message_operation_stage1_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_ENABLED")
        ),
        message_operation_stage1_after_contract_id=(
            _message_operation_stage1_after_contract_id(env)
        ),
        message_operation_stage1_max_attempts=max(
            1, min(message_operation_stage1_max_attempts, 20)
        ),
        message_operation_stage2_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_ENABLED")
        ),
        message_operation_stage2_after_handoff_id=(
            _message_operation_stage2_after_handoff_id(env)
        ),
        message_operation_stage2_max_attempts=max(
            1, min(message_operation_stage2_max_attempts, 20)
        ),
        agent_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED")
        ),
        agent_incident_types=agent_incident_types,
        message_operation_agent_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_MESSAGE_OPERATION_AGENT_ENABLED")
        ),
        message_operation_agent_after_contract_id=(
            _message_operation_agent_after_contract_id(env)
        ),
        agent_deployed_code_version=_deployed_code_version(env),
        agent_max_tool_steps=max(1, min(agent_tool_steps, 4)),
        agent_max_wall_seconds=max(5.0, min(agent_wall_seconds, 120.0)),
        agent_max_prompt_bytes=max(4096, min(agent_prompt_bytes, 32_768)),
        agent_max_tool_output_bytes=max(
            512, min(agent_tool_output_bytes, 32_768)
        ),
        agent_claim_lease_seconds=max(
            5.0, min(agent_claim_lease_seconds, 3600.0)
        ),
        agent_token_budget_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_TOKEN_BUDGET_ENABLED")
        ),
        agent_per_incident_token_limit=max(
            4096, min(agent_per_incident_token_limit, 1_000_000)
        ),
        agent_daily_token_limit=max(
            4096, min(agent_daily_token_limit, 10_000_000)
        ),
        agent_max_completion_tokens=max(
            64, min(agent_max_completion_tokens, 32_768)
        ),
        agent_shadow_playbooks=agent_shadow_playbooks,
        agent_actions_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED")
        ),
        agent_action_playbooks=agent_action_playbooks,
        agent_action_circuit_threshold=max(
            1, min(agent_action_circuit_threshold, 5)
        ),
        monitor_capture_token=monitor_capture_token,
    )
