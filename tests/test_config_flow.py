"""Tests for SA Power Networks config flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientCommunicationError,
    SAPowerNetworksApiClientParseError,
)
from custom_components.sapowernetworks.config_flow import SAPowerNetworksConfigFlow
from custom_components.sapowernetworks.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_user_flow_shows_form(hass) -> None:
    """Test that the user config flow shows a form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_maps_auth_error(hass, monkeypatch) -> None:
    """Auth failures should map to the auth error key."""

    async def _fail_auth(self, username: str, password: str) -> None:
        msg = "bad credentials"
        raise SAPowerNetworksApiClientAuthenticationError(msg)

    monkeypatch.setattr(SAPowerNetworksConfigFlow, "_test_credentials", _fail_auth)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth"}


async def test_user_flow_maps_portal_error(hass, monkeypatch) -> None:
    """Portal/request-shape failures should map to the portal error key."""

    async def _fail_portal(self, username: str, password: str) -> None:
        msg = "unexpected portal page"
        raise SAPowerNetworksApiClientParseError(msg)

    monkeypatch.setattr(
        SAPowerNetworksConfigFlow,
        "_test_credentials",
        _fail_portal,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "portal"}


async def test_user_flow_maps_connection_error(hass, monkeypatch) -> None:
    """Connection failures should map to the connection error key."""

    async def _fail_connection(
        self,
        username: str,
        password: str,
    ) -> None:
        msg = "network unreachable"
        raise SAPowerNetworksApiClientCommunicationError(msg)

    monkeypatch.setattr(
        SAPowerNetworksConfigFlow,
        "_test_credentials",
        _fail_connection,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "connection"}


async def test_user_flow_aborts_when_username_already_configured(
    hass,
    monkeypatch,
) -> None:
    """A second flow for the same username should abort as already configured."""

    async def _pass_credentials(self, username: str, password: str) -> None:
        return None

    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test User",
        unique_id="user@example.com",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
        },
    )
    existing_entry.add_to_hass(hass)

    monkeypatch.setattr(
        SAPowerNetworksConfigFlow,
        "_test_credentials",
        _pass_credentials,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_USERNAME: "User@Example.com", CONF_PASSWORD: "secret"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
