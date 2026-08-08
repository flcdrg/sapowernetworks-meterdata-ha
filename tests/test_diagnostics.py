"""Tests for SA Power Networks diagnostics."""

from __future__ import annotations

from datetime import timedelta
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
    mock_config_entry.runtime_data.last_update_success = True
    mock_config_entry.runtime_data.last_exception = None
    mock_config_entry.runtime_data.update_interval = timedelta(hours=24)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["config_entry"]["data"]["username"] == "<redacted>"
    assert diagnostics["config_entry"]["data"]["password"] == "<redacted>"
    assert diagnostics["coordinator_data"]["nmis"] == "<redacted>"
    assert diagnostics["coordinator_data"]["interval_statistic_ids"] == [
        "sapowernetworks:interval_a"
    ]
    assert diagnostics["coordinator_data"]["rows_imported"] == 4
    assert diagnostics["coordinator_state"] == {
        "last_update_success": True,
        "last_exception_type": None,
        "has_data": True,
        "update_interval_seconds": 86400.0,
    }


async def test_config_entry_diagnostics_exposes_exception_type_only(
    hass,
    mock_config_entry,
) -> None:
    """Diagnostics should expose only the exception class name, not raw messages."""
    mock_config_entry.runtime_data = SimpleNamespace(data={"last_error": "failed"})
    mock_config_entry.runtime_data.last_update_success = False
    mock_config_entry.runtime_data.last_exception = RuntimeError(
        "raw nmi 20012345678 should not be copied"
    )
    mock_config_entry.runtime_data.update_interval = None

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["coordinator_state"]["last_update_success"] is False
    assert diagnostics["coordinator_state"]["last_exception_type"] == "RuntimeError"
    assert diagnostics["coordinator_state"]["update_interval_seconds"] is None
