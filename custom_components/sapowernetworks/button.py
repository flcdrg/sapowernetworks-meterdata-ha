"""Button platform for SA Power Networks."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .const import LOGGER
from .entity import SAPowerNetworksEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SAPowerNetworksDataUpdateCoordinator
    from .data import SAPowerNetworksConfigEntry

ENTITY_DESCRIPTIONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="refresh_meter_data",
        name="Refresh Meter Data",
        icon="mdi:refresh",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: SAPowerNetworksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    async_add_entities(
        SAPowerNetworksButton(
            coordinator=entry.runtime_data,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class SAPowerNetworksButton(SAPowerNetworksEntity, ButtonEntity):
    """SA Power Networks button entity."""

    @property
    def available(self) -> bool:
        """Disable while manual refresh is running, then re-enable afterward."""
        return not self._is_refreshing

    def __init__(
        self,
        coordinator: SAPowerNetworksDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button entity."""
        super().__init__(coordinator, unique_id_suffix=entity_description.key)
        self.entity_description = entity_description
        self._is_refreshing = False

    async def async_press(self) -> None:
        """Trigger an immediate coordinator refresh."""
        started = time.monotonic()
        self._is_refreshing = True
        LOGGER.info("Manual refresh requested from integration button")
        if self.hass is not None:
            self.async_write_ha_state()
        try:
            await self.coordinator.async_request_refresh()
            duration_seconds = time.monotonic() - started
            LOGGER.info(
                "Manual refresh completed in %.2fs (success=%s)",
                duration_seconds,
                self.coordinator.last_update_success,
            )
        except Exception:
            duration_seconds = time.monotonic() - started
            LOGGER.exception(
                "Manual refresh failed after %.2fs",
                duration_seconds,
            )
            raise
        finally:
            self._is_refreshing = False
            if self.hass is not None:
                self.async_write_ha_state()
