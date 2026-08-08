"""Sensor platform for SA Power Networks."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)

from .entity import SAPowerNetworksEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import SAPowerNetworksDataUpdateCoordinator
    from .data import SAPowerNetworksConfigEntry

ENTITY_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="authenticated",
        name="Authentication Status",
        icon="mdi:account-check",
    ),
    SensorEntityDescription(
        key="nmi_count",
        name="NMI Count",
        icon="mdi:counter",
    ),
    SensorEntityDescription(
        key="last_sync",
        name="Last Successful Sync",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: SAPowerNetworksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    async_add_entities(
        SAPowerNetworksSensor(
            coordinator=entry.runtime_data,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class SAPowerNetworksSensor(SAPowerNetworksEntity, SensorEntity):
    """SA Power Networks sensor entity."""

    def __init__(
        self,
        coordinator: SAPowerNetworksDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, unique_id_suffix=entity_description.key)
        self.entity_description = entity_description

    @property
    def native_value(self) -> datetime | str | int | bool | None:
        """Return the native value of the sensor."""
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        value = data.get(self.entity_description.key)
        if isinstance(value, (datetime, str, int, bool)):
            return value
        return None
