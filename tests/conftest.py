"""Global pytest fixtures for sapowernetworks tests."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapowernetworks.const import DOMAIN


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations: Any,
) -> None:
    """Enable custom integrations in tests."""
    return


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mocked config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test User",
        unique_id="test-user",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
        },
    )
