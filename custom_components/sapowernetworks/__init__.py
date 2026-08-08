"""Custom integration for SA Power Networks meter data."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SAPowerNetworksApiClient
from .const import DOMAIN
from .coordinator import SAPowerNetworksDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .data import SAPowerNetworksConfigEntry

SERVICE_REFRESH = "refresh"
DATA_COORDINATORS = "coordinators"


async def _async_handle_refresh(service_call: ServiceCall) -> None:
    """Refresh all loaded SA Power Networks coordinators."""
    hass = service_call.hass
    domain_data = hass.data.get(DOMAIN, {})
    coordinators: dict[str, SAPowerNetworksDataUpdateCoordinator] = domain_data.get(
        DATA_COORDINATORS,
        {},
    )
    for coordinator in coordinators.values():
        await coordinator.async_request_refresh()


PLATFORMS: list[Platform] = [
    Platform.SENSOR,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SAPowerNetworksConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    coordinators: dict[str, SAPowerNetworksDataUpdateCoordinator] = (
        domain_data.setdefault(
            DATA_COORDINATORS,
            {},
        )
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass=hass,
        config_entry=entry,
        client=SAPowerNetworksApiClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            session=async_get_clientsession(hass),
        ),
    )
    coordinators[entry.entry_id] = coordinator
    entry.runtime_data = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH,
            _async_handle_refresh,
        )

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: SAPowerNetworksConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    coordinators: dict[str, SAPowerNetworksDataUpdateCoordinator] = domain_data.get(
        DATA_COORDINATORS,
        {},
    )
    coordinators.pop(entry.entry_id, None)

    if not coordinators:
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
        hass.data.pop(DOMAIN, None)

    return True
