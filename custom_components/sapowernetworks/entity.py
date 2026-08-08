"""SA Power Networks base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import SAPowerNetworksDataUpdateCoordinator


class SAPowerNetworksEntity(CoordinatorEntity[SAPowerNetworksDataUpdateCoordinator]):
    """Base entity for SA Power Networks."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SAPowerNetworksDataUpdateCoordinator,
        unique_id_suffix: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="SA Power Networks",
        )
