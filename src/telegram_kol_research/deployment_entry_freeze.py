from __future__ import annotations

import os
from typing import Mapping


DEPLOYMENT_ENTRY_FROZEN_ENV = "TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN"


class DeploymentEntryFreezeError(ValueError):
    pass


def deployment_entry_admission_frozen(
    environment: Mapping[str, str] | None = None,
) -> bool:
    observed = os.environ if environment is None else environment
    value = str(observed.get(DEPLOYMENT_ENTRY_FROZEN_ENV, "0")).strip()
    if value not in {"0", "1"}:
        raise DeploymentEntryFreezeError(
            f"{DEPLOYMENT_ENTRY_FROZEN_ENV} must be 0 or 1"
        )
    return value == "1"
