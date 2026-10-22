"""Tests for SA Power Networks config flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientCommunicationError,
    SAPowerNetworksApiClientParseError,
)
from custom_components.sapowernetworks.config_flow import SAPowerNetworksConfigFlow
from custom_components.sapowernetworks.const import DOMAIN

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "error",
    [
        None,
        SAPowerNetworksApiClientAuthenticationError,
        SAPowerNetworksApiClientCommunicationError,
        SAPowerNetworksApiClientParseError,
    ],
)
async def test_credentials_use_isolated_temporary_sessions(
    hass, monkeypatch, error
) -> None:
    """Credential checks cannot replace shared cookies and always close sessions."""
    portal_url = URL("https://customer.portal.sapowernetworks.com.au")
    shared_session = async_get_clientsession(hass)
    shared_session.cookie_jar.update_cookies({"sid": "existing"}, portal_url)
    sessions = []

    async def _test_credentials(client) -> None:
        session = client._session
        sessions.append(session)
        assert session is not shared_session
        assert not session.closed
        assert not session.cookie_jar.filter_cookies(portal_url)
        session.cookie_jar.update_cookies({"sid": "flow"}, portal_url)
        if error is not None:
            msg = "credential validation failed"
            raise error(msg)

    monkeypatch.setattr(SAPowerNetworksApiClient, "test_credentials", _test_credentials)
    flow = SAPowerNetworksConfigFlow()
    flow.hass = hass

    for username in ("first@example.com", "second@example.com"):
        if error is None:
            await flow._test_credentials(username, "secret")
        else:
            with pytest.raises(error, match="credential validation failed"):
                await flow._test_credentials(username, "secret")
        assert sessions[-1].closed

    assert sessions[0] is not sessions[1]
    assert not shared_session.closed
    assert (
        shared_session.cookie_jar.filter_cookies(portal_url)["sid"].value == "existing"
    )


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


async def test_reauth_flow_updates_password(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Reauthentication should store a validated replacement password."""
    mock_config_entry.add_to_hass(hass)

    async def _pass_credentials(self, username: str, password: str) -> None:
        assert username == "user@example.com"
        assert password == "updated-secret"

    monkeypatch.setattr(
        SAPowerNetworksConfigFlow, "_test_credentials", _pass_credentials
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": mock_config_entry.entry_id},
        data=mock_config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "updated-secret"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "updated-secret"


async def test_reauth_flow_maps_auth_error(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Reauthentication should report rejected replacement credentials."""
    mock_config_entry.add_to_hass(hass)

    async def _fail_auth(self, username: str, password: str) -> None:
        msg = "bad credentials"
        raise SAPowerNetworksApiClientAuthenticationError(msg)

    monkeypatch.setattr(SAPowerNetworksConfigFlow, "_test_credentials", _fail_auth)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": mock_config_entry.entry_id},
        data=mock_config_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "invalid-secret"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "auth"}
