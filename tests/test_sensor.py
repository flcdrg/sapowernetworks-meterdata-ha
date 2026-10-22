"""Tests for SA Power Networks sensor entities."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClientCommunicationError,
)
from custom_components.sapowernetworks.const import DOMAIN
from custom_components.sapowernetworks.sensor import (
    ENTITY_DESCRIPTIONS,
    SAPowerNetworksSensor,
)


def _build_coordinator(mock_config_entry, data: dict) -> SimpleNamespace:
    coordinator = SimpleNamespace()
    coordinator.config_entry = mock_config_entry
    coordinator.data = data
    coordinator.last_exception = None
    coordinator.last_update_success = True
    return coordinator


def test_rows_imported_sensor_exposes_statistic_ids(mock_config_entry) -> None:
    """Rows Imported sensor should expose interval and accumulated statistic IDs."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "rows_imported": 5,
            "interval_statistic_ids": ["sapowernetworks:interval_a"],
            "accumulated_statistic_ids": ["sapowernetworks:accumulated_b"],
            "combined_statistic_ids": ["sapowernetworks:combined_c"],
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "rows_imported"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == 5
    assert sensor.extra_state_attributes == {
        "statistic_ids": [
            "sapowernetworks:interval_a",
            "sapowernetworks:accumulated_b",
            "sapowernetworks:combined_c",
        ]
    }


def test_interval_rows_sensor_exposes_only_interval_statistic_ids(
    mock_config_entry,
) -> None:
    """Interval sensor attributes should only include interval statistic IDs."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "interval_rows_imported": 2,
            "interval_statistic_ids": ["sapowernetworks:interval_a"],
            "accumulated_statistic_ids": ["sapowernetworks:accumulated_b"],
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "interval_rows_imported"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == 2
    assert sensor.extra_state_attributes == {
        "statistic_ids": ["sapowernetworks:interval_a"]
    }


def test_last_sync_sensor_returns_datetime_value(mock_config_entry) -> None:
    """Timestamp sensor should expose a datetime native value."""
    sync_time = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "last_sync": sync_time,
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "last_sync"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == sync_time
    assert sensor.extra_state_attributes is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("portal timeout", "Error reported"),
        (
            "Portal error for user@example.com NMI 20012345678 csrf=synthetic-token",
            "Error reported",
        ),
    ],
)
def test_last_error_sensor_returns_string_without_extra_attributes(
    mock_config_entry,
    message: str,
    expected: str,
) -> None:
    """Last Error should redact its data value without exposing debug attributes."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "last_error": message,
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "last_error"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == expected
    assert sensor.extra_state_attributes is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("portal timeout", "Update failed"),
        (
            "Portal error for user@example.com NMI 20012345678 csrf=synthetic-token",
            "Update failed",
        ),
        ("", "RuntimeError"),
    ],
)
def test_last_error_sensor_reports_failure_and_clears_after_recovery(
    mock_config_entry,
    message: str,
    expected: str,
) -> None:
    """Last Error should stay available through failures and clear after recovery."""
    coordinator = _build_coordinator(mock_config_entry, {"last_error": ""})
    coordinator.last_exception = RuntimeError(message)
    coordinator.last_update_success = False
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "last_error"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.available is True
    assert sensor.native_value == expected

    coordinator.last_update_success = True

    assert sensor.available is True
    assert sensor.native_value == ""


async def test_last_error_entity_remains_available_during_failed_refresh(
    hass,
    mock_config_entry,
    monkeypatch,
) -> None:
    """Home Assistant should show the error sensor through failure and recovery."""
    client = AsyncMock()
    client.get_nmi_assignments.return_value = []
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksApiClient",
        Mock(return_value=client),
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.SAPowerNetworksDataUpdateCoordinator._async_existing_statistic_ids",
        AsyncMock(return_value=set()),
    )
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{mock_config_entry.entry_id}_last_error",
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == ""

    client.get_nmi_assignments.side_effect = SAPowerNetworksApiClientCommunicationError(
        "Portal error for user@example.com NMI 20012345678 csrf=synthetic-token"
    )
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "Update failed"

    client.get_nmi_assignments.side_effect = None
    client.get_nmi_assignments.return_value = []
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == ""


def test_feed_lag_sensor_returns_float_value(mock_config_entry) -> None:
    """Feed Lag sensor should expose a numeric lag in hours."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "feed_lag_hours": 9.75,
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "feed_lag_hours"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == 9.75
    assert sensor.extra_state_attributes is None
