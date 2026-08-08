"""Tests for SA Power Networks sensor entities."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from custom_components.sapowernetworks.sensor import (
    ENTITY_DESCRIPTIONS,
    SAPowerNetworksSensor,
)


def _build_coordinator(mock_config_entry, data: dict) -> SimpleNamespace:
    coordinator = SimpleNamespace()
    coordinator.config_entry = mock_config_entry
    coordinator.data = data
    return coordinator


def test_rows_imported_sensor_exposes_statistic_ids(mock_config_entry) -> None:
    """Rows Imported sensor should expose interval and accumulated statistic IDs."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "rows_imported": 5,
            "interval_statistic_ids": ["sapowernetworks:interval_a"],
            "accumulated_statistic_ids": ["sapowernetworks:accumulated_b"],
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


def test_last_error_sensor_returns_string_without_extra_attributes(
    mock_config_entry,
) -> None:
    """Last Error sensor should expose its string state without debug attributes."""
    coordinator = _build_coordinator(
        mock_config_entry,
        {
            "last_error": "portal timeout",
        },
    )
    entity_description = next(
        description
        for description in ENTITY_DESCRIPTIONS
        if description.key == "last_error"
    )

    sensor = SAPowerNetworksSensor(coordinator, entity_description)

    assert sensor.native_value == "portal timeout"
    assert sensor.extra_state_attributes is None
