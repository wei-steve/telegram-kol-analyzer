"""Shared configuration defaults for the local research app."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from telegram_kol_research.llm_chat import _load_env_file_values


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

RUNTIME_SCANNER_RULE_IDS = frozenset(
    {
        "terminal_lifecycle_exchange_exposure_v1",
        "active_position_missing_protection_v1",
        "cancel_outcome_stale_unknown_v1",
        "tp1_break_even_nonterminal_v1",
        "monitor_incident_ledger_silence_v1",
    }
)
RUNTIME_SCANNER_DEPLOYABLE_RULE_IDS = frozenset(
    {"cancel_outcome_stale_unknown_v1"}
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
    notification_lease_seconds: float = 120.0
    agent_enabled: bool = False
    agent_incident_types: frozenset[str] | None = None
    agent_max_tool_steps: int = 4
    agent_max_wall_seconds: float = 45.0
    agent_max_prompt_bytes: int = 16_384
    agent_max_tool_output_bytes: int = 8192
    agent_claim_lease_seconds: float = 120.0
    agent_shadow_playbooks: frozenset[str] = frozenset()
    agent_actions_enabled: bool = False
    agent_action_playbooks: frozenset[str] = frozenset()
    agent_action_circuit_threshold: int = 3
    monitor_capture_token: str | None = None
    feature_policy_version: str = "runtime-incident-phase-6-v1"
    prompt_version: str = "runtime-agent-prompt-v7"
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


def _enabled_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        notification_lease_seconds=max(5.0, min(lease_seconds, 3600.0)),
        agent_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED")
        ),
        agent_incident_types=agent_incident_types,
        agent_max_tool_steps=max(1, min(agent_tool_steps, 4)),
        agent_max_wall_seconds=max(5.0, min(agent_wall_seconds, 120.0)),
        agent_max_prompt_bytes=max(4096, min(agent_prompt_bytes, 32_768)),
        agent_max_tool_output_bytes=max(
            512, min(agent_tool_output_bytes, 32_768)
        ),
        agent_claim_lease_seconds=max(
            5.0, min(agent_claim_lease_seconds, 3600.0)
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
