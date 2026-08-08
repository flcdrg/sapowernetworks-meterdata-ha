"""Binary sensor platform for SA Power Networks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import SAPowerNetworksEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SAPowerNetworksDataUpdateCoordinator
    from .data import SAPowerNetworksConfigEntry

ENTITY_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = ()


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: SAPowerNetworksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary_sensor platform."""
    async_add_entities(
        SAPowerNetworksBinarySensor(
            coordinator=entry.runtime_data,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class SAPowerNetworksBinarySensor(SAPowerNetworksEntity, BinarySensorEntity):
    """SA Power Networks binary sensor entity."""

    def __init__(
        self,
        coordinator: SAPowerNetworksDataUpdateCoordinator,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, unique_id_suffix=entity_description.key)
        self.entity_description = entity_description

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary_sensor is on."""
        return None
