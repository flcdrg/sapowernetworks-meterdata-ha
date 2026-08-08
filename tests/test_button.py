"""Tests for SA Power Networks button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.sapowernetworks.button import (
    ENTITY_DESCRIPTIONS,
    SAPowerNetworksButton,
)

pytestmark = pytest.mark.asyncio


async def test_refresh_button_presses_coordinator(mock_config_entry) -> None:
    """Pressing the refresh button should request a coordinator refresh."""
    coordinator = AsyncMock()
    coordinator.config_entry = mock_config_entry

    button = SAPowerNetworksButton(
        coordinator=coordinator,
        entity_description=ENTITY_DESCRIPTIONS[0],
    )

    await button.async_press()

    coordinator.async_request_refresh.assert_awaited_once()
