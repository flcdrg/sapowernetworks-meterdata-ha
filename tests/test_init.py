"""Tests for SA Power Networks integration setup."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.sapowernetworks import (
    DATA_COORDINATORS,
    SERVICE_REFRESH,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.sapowernetworks.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_setup_registers_refresh_service(
    hass,
    mock_config_entry,
    monkeypatch,
) -> None:
    """Setting up an entry should register the manual refresh service."""
    refresh_mock = AsyncMock()

    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksDataUpdateCoordinator.async_config_entry_first_refresh",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksDataUpdateCoordinator.async_request_refresh",
        refresh_mock,
    )

    mock_config_entry.add_to_hass(hass)

    result = await async_setup_entry(hass, mock_config_entry)

    assert result is True
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert mock_config_entry.entry_id in hass.data[DOMAIN][DATA_COORDINATORS]

    await hass.services.async_call(DOMAIN, SERVICE_REFRESH, blocking=True)

    refresh_mock.assert_awaited_once()


async def test_unload_removes_refresh_service_when_last_entry_unloaded(
    hass,
    mock_config_entry,
    monkeypatch,
) -> None:
    """Unloading the last entry should remove the manual refresh service."""
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksDataUpdateCoordinator.async_config_entry_first_refresh",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )

    mock_config_entry.add_to_hass(hass)
    await async_setup_entry(hass, mock_config_entry)

    result = await async_unload_entry(hass, mock_config_entry)

    assert result is True
    assert not hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert DOMAIN not in hass.data
