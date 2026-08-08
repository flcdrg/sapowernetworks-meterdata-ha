"""Diagnostics support for SA Power Networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .privacy import redact_mapping

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

    return {
        "config_entry": redact_mapping(config_entry.as_dict()),
        "coordinator_data": redact_mapping(coordinator_data),
    }
