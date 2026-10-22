"""Tests for SA Power Networks integration setup."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.sapowernetworks import (
    DATA_COORDINATORS,
    SERVICE_REFRESH,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.sapowernetworks.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_entries_have_isolated_sessions_closed_on_unload(
    hass,
    mock_config_entry,
    monkeypatch,
) -> None:
    """Each entry owns its portal cookies and releases its session on unload."""
    client_factory = Mock()
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksApiClient",
        client_factory,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksDataUpdateCoordinator.async_config_entry_first_refresh",
        AsyncMock(),
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="second-user",
        data={CONF_USERNAME: "second@example.com", CONF_PASSWORD: "second-secret"},
    )
    mock_config_entry.add_to_hass(hass)
    portal_url = URL("https://customer.portal.sapowernetworks.com.au")
    shared_session = async_get_clientsession(hass)
    shared_session.cookie_jar.update_cookies({"sid": "shared"}, portal_url)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    first_session = client_factory.call_args.kwargs["session"]
    assert first_session is not shared_session
    assert not first_session.cookie_jar.filter_cookies(portal_url)
    first_session.cookie_jar.update_cookies({"sid": "first"}, portal_url)

    second_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second_entry.entry_id)
    second_session = client_factory.call_args.kwargs["session"]
    assert second_session is not first_session
    assert second_session is not shared_session
    assert not second_session.cookie_jar.filter_cookies(portal_url)
    second_session.cookie_jar.update_cookies({"sid": "second"}, portal_url)
    assert first_session.cookie_jar.filter_cookies(portal_url)["sid"].value == "first"
    assert shared_session.cookie_jar.filter_cookies(portal_url)["sid"].value == "shared"

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert first_session.closed
    assert not second_session.closed
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)

    assert await hass.config_entries.async_unload(second_entry.entry_id)
    assert second_session.closed
    assert not shared_session.closed
    assert not hass.services.has_service(DOMAIN, SERVICE_REFRESH)


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
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
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

    unload_result = await async_unload_entry(hass, mock_config_entry)
    assert unload_result is True


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
