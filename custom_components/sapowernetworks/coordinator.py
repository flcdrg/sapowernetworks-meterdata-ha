"""DataUpdateCoordinator for SA Power Networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientError,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SAPowerNetworksConfigEntry


class SAPowerNetworksDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: SAPowerNetworksConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SAPowerNetworksConfigEntry,
        client: SAPowerNetworksApiClient,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.config_entry = config_entry
        self.client = client

    async def _async_update_data(self) -> Any:
        """Update data via library."""
        try:
            return await self.client.async_get_data()
        except SAPowerNetworksApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except SAPowerNetworksApiClientError as exception:
            raise UpdateFailed(exception) from exception
