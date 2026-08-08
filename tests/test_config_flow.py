"""Tests for SA Power Networks config flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

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
