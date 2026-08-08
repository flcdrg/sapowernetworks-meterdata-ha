"""Tests for SA Power Networks diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.sapowernetworks.diagnostics import (
    async_get_config_entry_diagnostics,
)

pytestmark = pytest.mark.asyncio


async def test_config_entry_diagnostics_redacts_sensitive_values(
    hass,
    mock_config_entry,
) -> None:
    """Diagnostics should redact credentials, usernames, and raw NMI values."""
    mock_config_entry.runtime_data = SimpleNamespace(
        data={
            "authenticated": True,
            "nmis": ["20012345678"],
            "rows_imported": 4,
            "interval_statistic_ids": ["sapowernetworks:interval_a"],
            "last_error": "",
        }
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["config_entry"]["data"]["username"] == "<redacted>"
    assert diagnostics["config_entry"]["data"]["password"] == "<redacted>"
    assert diagnostics["coordinator_data"]["nmis"] == "<redacted>"
    assert diagnostics["coordinator_data"]["interval_statistic_ids"] == [
        "sapowernetworks:interval_a"
    ]
    assert diagnostics["coordinator_data"]["rows_imported"] == 4
