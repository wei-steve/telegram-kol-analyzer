"""Deepcoin contract specification helpers.

The module is intentionally offline-only. Runtime code can inject a provider
that obtains specs from a public API, cache, or manually verified config.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from threading import Lock
from typing import Any
from typing import Callable
from typing import Protocol
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from telegram_kol_research.deepcoin_contract_spec_cache import (
        DeepcoinContractSpecSnapshot,
    )


@dataclass(frozen=True)
class DeepcoinContractSpec:
    """Verified contract sizing rules for a Deepcoin swap instrument."""

    instrument_id: str
    contract_value: float
    quantity_step: float
    min_quantity: float
    price_tick: float

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        if self.contract_value <= 0:
            raise ValueError("contract_value must be positive")
        if self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive")
        if self.min_quantity <= 0:
            raise ValueError("min_quantity must be positive")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")

    def to_dict(self) -> dict[str, float | str]:
        values = asdict(self)
        values["instrument_id"] = self.instrument_id.upper()
        values["contract_value"] = float(self.contract_value)
        values["quantity_step"] = float(self.quantity_step)
        values["min_quantity"] = float(self.min_quantity)
        values["price_tick"] = float(self.price_tick)
        return values


class DeepcoinContractSpecProvider(Protocol):
    """Provider interface for verified contract specs."""

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        """Return a verified spec for an instrument, if available."""


@dataclass(frozen=True)
class StaticDeepcoinContractSpecProvider:
    """In-memory provider backed by manually verified contract specs."""

    specs_by_instrument_id: dict[str, DeepcoinContractSpec]

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        return self.specs_by_instrument_id.get(instrument_id.upper())


@dataclass(frozen=True)
class DeepcoinContractSpecProviderMetadata:
    """Bounded health metadata for a refreshable contract-spec provider."""

    last_success_at: datetime | None
    expires_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class DeepcoinContractSpecLookup:
    """Explicit lookup result; absence is never overloaded as unsupported."""

    instrument_id: str
    reason: str
    venue_state: str | None = None
    contract_spec: DeepcoinContractSpec | None = None


@dataclass(frozen=True)
class DeepcoinContractSpecComparison:
    """Last shadow comparison without changing execution authority."""

    instrument_id: str
    matches: bool
    static_spec: DeepcoinContractSpec | None
    authoritative_spec: DeepcoinContractSpec | None
    error: str | None = None


class RolloutDeepcoinContractSpecProvider:
    """Select static or authoritative specs through an explicit rollout mode."""

    def __init__(
        self,
        *,
        static_provider: DeepcoinContractSpecProvider,
        authoritative_provider: DeepcoinContractSpecProvider,
        mode_loader: Callable[[], str],
    ) -> None:
        self.static_provider = static_provider
        self.authoritative_provider = authoritative_provider
        self._mode_loader = mode_loader
        self.last_comparison: DeepcoinContractSpecComparison | None = None

    @property
    def mode(self) -> str:
        try:
            value = self._mode_loader()
        except Exception:
            return "static"
        return value if value in {"static", "shadow", "live"} else "static"

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        mode = self.mode
        if mode == "live":
            return self.authoritative_provider.get_contract_spec(instrument_id)

        static_spec = self.static_provider.get_contract_spec(instrument_id)
        if mode == "shadow":
            comparison_error = None
            try:
                authoritative_spec = self.authoritative_provider.get_contract_spec(
                    instrument_id
                )
            except Exception as exc:
                authoritative_spec = None
                comparison_error = f"shadow_compare_failed:{type(exc).__name__}"
            self.last_comparison = DeepcoinContractSpecComparison(
                instrument_id=str(instrument_id).strip().upper(),
                matches=(
                    comparison_error is None
                    and static_spec == authoritative_spec
                ),
                static_spec=static_spec,
                authoritative_spec=authoritative_spec,
                error=comparison_error,
            )
        return static_spec


class RefreshableDeepcoinContractSpecProvider:
    """Cache-backed provider with coalesced, bounded refresh attempts."""

    def __init__(
        self,
        *,
        cache_path: str | Path,
        instrument_loader: Callable[[], list[dict[str, Any]]],
        ttl: timedelta,
        now_provider: Callable[[], datetime] | None = None,
        refresh_lock_timeout_seconds: float = 5.0,
        max_error_length: int = 256,
    ) -> None:
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if refresh_lock_timeout_seconds <= 0:
            raise ValueError("refresh_lock_timeout_seconds must be positive")
        if isinstance(max_error_length, bool) or max_error_length <= 0:
            raise ValueError("max_error_length must be positive")
        self._cache_path = Path(cache_path)
        self._instrument_loader = instrument_loader
        self._ttl = ttl
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._refresh_lock_timeout_seconds = refresh_lock_timeout_seconds
        self._max_error_length = max_error_length
        self._refresh_lock = Lock()
        self._refresh_generation = 0
        self._last_refresh_result = False
        self._snapshot: DeepcoinContractSpecSnapshot | None = None
        self._last_success_at: datetime | None = None
        self._last_success_expires_at: datetime | None = None
        self._last_error: str | None = None
        self._reload_locked()

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    @property
    def snapshot(self) -> DeepcoinContractSpecSnapshot | None:
        return self._snapshot

    @property
    def metadata(self) -> DeepcoinContractSpecProviderMetadata:
        return DeepcoinContractSpecProviderMetadata(
            last_success_at=self._last_success_at,
            expires_at=self._last_success_expires_at,
            last_error=self._last_error,
        )

    def get_contract_spec(self, instrument_id: str) -> DeepcoinContractSpec | None:
        lookup = self.lookup_contract_spec(instrument_id)
        return lookup.contract_spec if lookup.reason == "available" else None

    def lookup_contract_spec(self, instrument_id: str) -> DeepcoinContractSpecLookup:
        normalized_id = str(instrument_id).strip().upper()
        snapshot = self._snapshot
        if snapshot is None:
            return DeepcoinContractSpecLookup(
                instrument_id=normalized_id,
                reason="contract_spec_sync_unavailable",
            )
        now = _provider_now(self._now_provider)
        if now < snapshot.fetched_at:
            return DeepcoinContractSpecLookup(
                instrument_id=normalized_id,
                reason="contract_spec_invalid",
            )
        if now >= snapshot.expires_at:
            return DeepcoinContractSpecLookup(
                instrument_id=normalized_id,
                reason="contract_spec_stale",
            )
        capability = snapshot.capabilities_by_instrument_id.get(normalized_id)
        if capability is None:
            return DeepcoinContractSpecLookup(
                instrument_id=normalized_id,
                reason="venue_instrument_unsupported",
            )
        if capability.state != "live":
            return DeepcoinContractSpecLookup(
                instrument_id=normalized_id,
                reason="venue_instrument_not_live",
                venue_state=capability.state,
            )
        return DeepcoinContractSpecLookup(
            instrument_id=normalized_id,
            reason="available",
            venue_state=capability.state,
            contract_spec=capability.contract_spec,
        )

    def reload(self) -> bool:
        """Reload an atomically published cache, clearing authority on failure."""

        acquired = self._refresh_lock.acquire(
            timeout=self._refresh_lock_timeout_seconds
        )
        if not acquired:
            self._record_error(
                TimeoutError("contract spec reload lock timed out"),
                operation="cache_reload",
            )
            return False
        try:
            return self._reload_locked()
        finally:
            self._refresh_lock.release()

    def _reload_locked(self) -> bool:
        """Reload while holding the provider's sole mutation lock."""

        from telegram_kol_research.deepcoin_contract_spec_cache import (
            load_deepcoin_contract_spec_snapshot,
        )

        try:
            snapshot = load_deepcoin_contract_spec_snapshot(
                self._cache_path,
                now=_provider_now(self._now_provider),
            )
        except (OSError, ValueError) as exc:
            self._snapshot = None
            self._record_error(exc, operation="cache_reload")
            return False
        self._accept_snapshot(snapshot)
        return True

    def refresh(self) -> bool:
        """Refresh once; waiters share the completed in-flight attempt."""

        generation = self._refresh_generation
        acquired = self._refresh_lock.acquire(
            timeout=self._refresh_lock_timeout_seconds
        )
        if not acquired:
            self._record_error(
                TimeoutError("contract spec refresh lock timed out"),
                operation="refresh_lock",
            )
            return False
        try:
            if self._refresh_generation != generation:
                return self._last_refresh_result
            result = self._refresh_locked()
            self._last_refresh_result = result
            self._refresh_generation += 1
            return result
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self) -> bool:
        from telegram_kol_research.deepcoin_contract_spec_cache import (
            publish_deepcoin_contract_spec_snapshot,
            validate_deepcoin_instrument_snapshot,
        )

        try:
            now = _provider_now(self._now_provider)
            rows = self._instrument_loader()
            snapshot = validate_deepcoin_instrument_snapshot(
                rows,
                fetched_at=now,
                ttl=self._ttl,
            )
            publish_deepcoin_contract_spec_snapshot(
                self._cache_path,
                snapshot,
                now=now,
            )
        except Exception as exc:
            self._record_error(exc, operation="refresh")
            # A refresh failure cannot invalidate a still-fresh snapshot that
            # was already fully validated and atomically published.
            return False
        self._accept_snapshot(snapshot)
        return True

    def _accept_snapshot(self, snapshot: DeepcoinContractSpecSnapshot) -> None:
        self._snapshot = snapshot
        self._last_success_at = snapshot.fetched_at
        self._last_success_expires_at = snapshot.expires_at
        self._last_error = None

    def _record_error(self, error: Exception, *, operation: str) -> None:
        # Exception messages from HTTP clients can contain signed URLs,
        # headers, or response bodies. Health metadata needs only a bounded
        # category; detailed diagnostics belong in access-controlled logs.
        rendered = f"{operation}_failed:{type(error).__name__}"
        self._last_error = rendered[: self._max_error_length]


def _provider_now(now_provider: Callable[[], datetime]) -> datetime:
    value = now_provider()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("provider clock must return a timezone-aware datetime")
    if value.utcoffset() is None:
        raise ValueError("provider clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def load_deepcoin_contract_specs(
    config_path: str | Path,
    *,
    required: bool = True,
) -> StaticDeepcoinContractSpecProvider:
    """Load verified Deepcoin contract specs from YAML."""

    path = Path(config_path)
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return StaticDeepcoinContractSpecProvider(specs_by_instrument_id={})

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = [
        _parse_contract_spec(contract_data)
        for contract_data in raw_data.get("contracts", [])
    ]
    return StaticDeepcoinContractSpecProvider(
        specs_by_instrument_id={spec.instrument_id.upper(): spec for spec in specs}
    )


def _parse_contract_spec(raw_data: dict[str, Any]) -> DeepcoinContractSpec:
    return DeepcoinContractSpec(
        instrument_id=str(raw_data["instrument_id"]).upper(),
        contract_value=float(raw_data["contract_value"]),
        quantity_step=float(raw_data["quantity_step"]),
        min_quantity=float(raw_data["min_quantity"]),
        price_tick=float(raw_data["price_tick"]),
    )
