"""Tests for SA Power Networks button platform."""

from __future__ import annotations

import asyncio
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


async def test_refresh_button_stays_available_when_coordinator_unavailable(
    mock_config_entry,
) -> None:
    """Refresh button should remain available to recover from transient failures."""
    coordinator = AsyncMock()
    coordinator.config_entry = mock_config_entry
    coordinator.last_update_success = False

    button = SAPowerNetworksButton(
        coordinator=coordinator,
        entity_description=ENTITY_DESCRIPTIONS[0],
    )

    assert button.available is True


async def test_refresh_button_is_unavailable_while_refresh_running(
    mock_config_entry,
) -> None:
    """Refresh button should disable while a manual refresh is in progress."""
    coordinator = AsyncMock()
    coordinator.config_entry = mock_config_entry
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_refresh() -> None:
        started.set()
        await release.wait()

    coordinator.async_request_refresh.side_effect = _fake_refresh

    button = SAPowerNetworksButton(
        coordinator=coordinator,
        entity_description=ENTITY_DESCRIPTIONS[0],
    )

    task = asyncio.create_task(button.async_press())
    await started.wait()
    assert button.available is False
    release.set()
    await task
    assert button.available is True


async def test_refresh_button_reenables_when_refresh_errors(
    mock_config_entry,
) -> None:
    """Refresh button should be re-enabled after refresh exceptions."""
    coordinator = AsyncMock()
    coordinator.config_entry = mock_config_entry
    coordinator.async_request_refresh.side_effect = RuntimeError("refresh failed")

    button = SAPowerNetworksButton(
        coordinator=coordinator,
        entity_description=ENTITY_DESCRIPTIONS[0],
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        await button.async_press()

    assert button.available is True
