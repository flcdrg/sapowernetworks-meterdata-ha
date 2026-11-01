"""Diagnostics support for SA Power Networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .privacy import redact_mapping
from .statistics_migration import (
    entry_pending_statistics_digests,
    entry_statistics_migration_state,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SAPowerNetworksConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: SAPowerNetworksConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = config_entry.runtime_data
    coordinator_data = coordinator.data if isinstance(coordinator.data, dict) else {}
    update_interval_seconds: float | None = None
    if coordinator.update_interval is not None:
        update_interval_seconds = coordinator.update_interval.total_seconds()

    last_exception_type: str | None = None
    if coordinator.last_exception is not None:
        last_exception_type = type(coordinator.last_exception).__name__

    return {
        "config_entry": redact_mapping(config_entry.as_dict()),
        "coordinator_data": redact_mapping(coordinator_data),
        "coordinator_state": {
            "last_update_success": coordinator.last_update_success,
            "last_exception_type": last_exception_type,
            "has_data": coordinator.data is not None,
            "update_interval_seconds": update_interval_seconds,
        },
        "statistics_migration": {
            "state": entry_statistics_migration_state(config_entry),
            "pending": bool(entry_pending_statistics_digests(config_entry)),
            "pending_count": len(entry_pending_statistics_digests(config_entry)),
        },
    }
