"""Tests for SA Power Networks coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.sapowernetworks.api import NmiAssignment
from custom_components.sapowernetworks.const import DOMAIN, INITIAL_BACKFILL_START
from custom_components.sapowernetworks.coordinator import (
    SAPowerNetworksDataUpdateCoordinator,
)

pytestmark = pytest.mark.asyncio


class _FakeRecorderInstance:
    """Minimal recorder instance test double."""

    async def async_add_executor_job(
        self,
        target: Any,
        *args: Any,
    ) -> Any:
        return target(*args)


def _sample_detailed_csv() -> str:
    values = ["1.0", "1.5"] + ["" for _ in range(46)]
    joined = ",".join(values)
    return "\n".join(
        [
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,",
            f"300,20260620,{joined},A,,,20260621000000,",
            "900",
        ]
    )


def _sample_detailed_csv_for_nmi_on_day(nmi: str, yyyymmdd: str) -> str:
    values = ["1.0", "1.5"] + ["" for _ in range(46)]
    joined = ",".join(values)
    return "\n".join(
        [
            f"200,{nmi},E1B1,E1,E1,,METER1,KWH,30,",
            f"300,{yyyymmdd},{joined},A,,,{yyyymmdd}235959,",
            "900",
        ]
    )


def _sample_detailed_csv_with_partial_hour() -> str:
    values = ["1.0", "1.5", "2.0"] + ["" for _ in range(45)]
    joined = ",".join(values)
    return "\n".join(
        [
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,",
            f"300,20260620,{joined},A,,,20260621000000,",
            "900",
        ]
    )


def _sample_summary_csv() -> str:
    return (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
    )


def _sample_summary_csv_for_nmi(nmi: str) -> str:
    return (
        f"{nmi},1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        f"{nmi},1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
    )


async def test_coordinator_imports_new_statistics(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should import parsed interval statistics into recorder."""
    client = AsyncMock()
    client.get_nmi_assignments.return_value = [
        NmiAssignment(
            nmi="20012345678",
            company="SAPN",
            meter_serial_number="METER1",
            meter_type_description="Interval",
            description="Synthetic",
            is_default=True,
        )
    ]
    client.download_detailed_csv.return_value = _sample_detailed_csv()
    client.download_accumulated_summary_csv.return_value = ""

    imported: list[tuple[dict, list[dict]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["rows_imported"] == 1
    assert result["channels_imported"] == 1
    assert result["interval_rows_imported"] == 1
    assert result["accumulated_rows_imported"] == 0
    assert len(result["interval_statistic_ids"]) == 1
    assert result["accumulated_statistic_ids"] == []
    assert result["last_error"] == ""
    assert imported
    metadata, statistics = imported[0]
    assert metadata["source"] == DOMAIN
    assert metadata["statistic_id"].startswith(f"{DOMAIN}:")
    assert "20012345678" not in metadata["statistic_id"]
    assert len(statistics) == 1
    assert statistics[0]["state"] == 2.5
    assert statistics[0]["sum"] == 2.5


async def test_interval_sync_defers_incomplete_hour(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Interval sync should defer a partial hour rather than import its subtotal."""
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic",
        is_default=True,
    )
    client = AsyncMock()
    client.download_detailed_csv.return_value = _sample_detailed_csv_with_partial_hour()
    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)
    imported: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        coordinator,
        "_async_last_statistic_snapshot",
        AsyncMock(return_value=(None, 0.0)),
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        lambda _hass, _metadata, statistics: imported.append(list(statistics)),
    )

    result = await coordinator._async_sync_interval_statistics(
        assignment,
        datetime(2026, 6, 20, 14, tzinfo=UTC),
        datetime(2026, 6, 21, tzinfo=UTC),
    )

    expected_hour = datetime(2026, 6, 19, 14, tzinfo=UTC)
    assert result.rows_imported == 1
    assert result.latest_interval_data_point == expected_hour
    assert result.import_points_by_nmi == {assignment.nmi: ((expected_hour, 2.5),)}
    assert len(imported) == 1
    assert imported[0][0]["start"] == expected_hour
    assert imported[0][0]["state"] == 2.5


async def test_coordinator_reports_unsupported_energy_units(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Unsupported units should fail the sync instead of importing mislabeled data."""
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic",
        is_default=True,
    )
    values = ",".join("1.0" for _ in range(48))
    client = AsyncMock()
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = "\n".join(
        [
            f"200,{assignment.nmi},E1B1,E1,E1,,METER1,kVAh,30,",
            f"300,20260620,{values},A,,,20260621000000,",
            "900",
        ]
    )

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    with pytest.raises(UpdateFailed, match="Unsupported energy unit 'kVAh'"):
        await coordinator._async_update_data()

    client.download_accumulated_summary_csv.assert_not_awaited()


async def test_coordinator_imports_when_payload_nmi_differs_from_selector(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should import rows even if payload and selector NMIs differ."""
    client = AsyncMock()
    client.get_nmi_assignments.return_value = [
        NmiAssignment(
            nmi="20012342987",
            company="SAPN",
            meter_serial_number="METER1",
            meter_type_description="Interval",
            description="Synthetic",
            is_default=True,
        )
    ]
    client.download_detailed_csv.return_value = _sample_detailed_csv()
    client.download_accumulated_summary_csv.return_value = ""

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["rows_imported"] == 1
    assert result["channels_imported"] == 1
    assert result["interval_rows_imported"] == 1
    assert result["accumulated_rows_imported"] == 0
    assert len(result["interval_statistic_ids"]) == 1
    assert imported
    metadata, _statistics = imported[0]
    expected_statistic_id = coordinator._statistic_id("20012345678", "E1")
    assert metadata["statistic_id"] == expected_statistic_id


async def test_coordinator_skips_existing_statistics(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should only import intervals after the last recorder point."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = _sample_detailed_csv()
    client.download_accumulated_summary_csv.return_value = ""

    imported: list[tuple[dict, list[dict]]] = []

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)
    statistic_id = coordinator._statistic_id(assignment.nmi, "E1")

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return [{"statistic_id": statistic_id}]

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {
            statistic_id: [
                {
                    "start": 1781913600.0,
                    "sum": 1.0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    result = await coordinator._async_update_data()

    assert result["rows_imported"] == 0
    assert result["channels_imported"] == 0
    assert result["interval_rows_imported"] == 0
    assert result["accumulated_rows_imported"] == 0
    assert result["interval_statistic_ids"] == []
    assert result["accumulated_statistic_ids"] == []
    assert isinstance(result["feed_lag_hours"], float)
    assert isinstance(result["latest_interval_data_point"], datetime)
    assert not imported
    called_start = client.download_detailed_csv.await_args.args[1]
    assert called_start == datetime(2026, 6, 20, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize("meter_type_description", ["Interval", "Accumulated", None])
async def test_interval_fetch_start_excludes_accumulated_watermark(
    hass, mock_config_entry, monkeypatch, meter_type_description: str | None
) -> None:
    """Interval fetching should ignore summary but retain hourly stream watermarks."""
    nmi = "20012345678"
    coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass,
        mock_config_entry,
        AsyncMock(),
    )
    assignment = NmiAssignment(
        nmi=nmi,
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description=meter_type_description,
        description="Synthetic",
        is_default=True,
    )
    interval_start = datetime(2026, 6, 10, 12, tzinfo=UTC)
    later_interval_start = datetime(2026, 6, 12, 10, tzinfo=UTC)
    combined_start = datetime(2026, 6, 9, 12, tzinfo=UTC)
    recorded_starts = {
        coordinator._statistic_id(nmi, "E1"): interval_start.timestamp(),
        coordinator._statistic_id(nmi, "B1"): later_interval_start.timestamp(),
        coordinator._summary_statistic_id(nmi, "accumulated_import"): datetime(
            2024, 12, 11, 14, tzinfo=UTC
        ).timestamp(),
        coordinator._combined_statistic_id(nmi): combined_start.timestamp(),
    }

    async def _fake_last_statistic_snapshot(
        statistic_id: str,
    ) -> tuple[float | None, float]:
        return recorded_starts[statistic_id], 0.0

    monkeypatch.setattr(
        coordinator,
        "_async_last_statistic_snapshot",
        _fake_last_statistic_snapshot,
    )

    fetch_start = await coordinator._async_fetch_start(assignment, set(recorded_starts))

    assert fetch_start == combined_start + timedelta(hours=1)


async def test_coordinator_imports_accumulated_statistics(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should import accumulated summary streams into recorder."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Accumulated",
        description="Synthetic",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = ""
    client.download_accumulated_summary_csv.return_value = _sample_summary_csv()

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["rows_imported"] == 3
    assert result["channels_imported"] == 2
    assert result["interval_rows_imported"] == 0
    assert result["accumulated_rows_imported"] == 3
    assert result["interval_statistic_ids"] == []
    assert len(result["accumulated_statistic_ids"]) == 2
    assert len(imported) == 2

    by_statistic_id = {
        metadata["statistic_id"]: statistics for metadata, statistics in imported
    }
    import_stat_id = coordinator._summary_statistic_id(
        assignment.nmi,
        "accumulated_import",
    )
    export_stat_id = coordinator._summary_statistic_id(
        assignment.nmi,
        "accumulated_export",
    )

    assert import_stat_id in by_statistic_id
    assert export_stat_id in by_statistic_id
    assert by_statistic_id[import_stat_id][0]["sum"] == 837.0
    assert by_statistic_id[import_stat_id][1]["sum"] == 1857.0
    assert by_statistic_id[import_stat_id][0]["start"] == datetime(
        2024, 12, 11, 14, tzinfo=UTC
    )
    assert by_statistic_id[export_stat_id][0]["sum"] == 12.5
    client.download_detailed_csv.assert_not_awaited()


@pytest.mark.parametrize(
    ("meter_type_description", "expect_interval_fetch"),
    [
        ("Accumulated", False),
        (" accumulated ", False),
        ("Interval", True),
        (None, True),
        ("Unknown", True),
    ],
)
async def test_coordinator_advances_accumulated_fetch_start(
    hass,
    mock_config_entry,
    monkeypatch,
    meter_type_description: str | None,
    *,
    expect_interval_fetch: bool,
) -> None:
    """Summary-only meters should not repeat detailed backfills on each refresh."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description=meter_type_description,
        description="Synthetic",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = ""
    client.download_accumulated_summary_csv.return_value = ""

    accumulated_statistic_id = SAPowerNetworksDataUpdateCoordinator(
        hass,
        mock_config_entry,
        client,
    )._summary_statistic_id(assignment.nmi, "accumulated_import")

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return [{"statistic_id": accumulated_statistic_id}]

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {
            accumulated_statistic_id: [
                {
                    "start": 1760486400.0,
                    "sum": 100.0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    called_start = client.download_accumulated_summary_csv.await_args.args[1]
    assert called_start == datetime(2025, 10, 16, 0, 0, tzinfo=UTC)
    assert client.download_accumulated_summary_csv.await_count == 2
    if expect_interval_fetch:
        assert client.download_detailed_csv.await_count == 2
        assert all(
            call.args[1] == INITIAL_BACKFILL_START
            for call in client.download_detailed_csv.await_args_list
        )
    else:
        client.download_detailed_csv.assert_not_awaited()


async def test_coordinator_imports_multiple_nmis(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should create separate statistic streams for each NMI."""
    client = AsyncMock()
    first_assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic One",
        is_default=True,
    )
    second_assignment = NmiAssignment(
        nmi="20098765432",
        company="SAPN",
        meter_serial_number="METER2",
        meter_type_description="Interval",
        description="Synthetic Two",
        is_default=False,
    )
    client.get_nmi_assignments.return_value = [first_assignment, second_assignment]

    interval_payloads = {
        first_assignment.nmi: _sample_detailed_csv(),
        second_assignment.nmi: _sample_detailed_csv().replace(
            "20012345678", "20098765432"
        ),
    }
    summary_payloads = {
        first_assignment.nmi: _sample_summary_csv_for_nmi(first_assignment.nmi),
        second_assignment.nmi: _sample_summary_csv_for_nmi(second_assignment.nmi),
    }

    async def _fake_download_detailed_csv(
        nmi: str,
        _start: datetime,
        _end: datetime,
    ) -> str:
        return interval_payloads[nmi]

    async def _fake_download_accumulated_summary_csv(
        nmi: str,
        _start: datetime,
        _end: datetime,
    ) -> str:
        return summary_payloads[nmi]

    client.download_detailed_csv.side_effect = _fake_download_detailed_csv
    client.download_accumulated_summary_csv.side_effect = (
        _fake_download_accumulated_summary_csv
    )

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["nmi_count"] == 2
    assert result["rows_imported"] == 8
    assert result["channels_imported"] == 6
    assert result["interval_rows_imported"] == 2
    assert result["accumulated_rows_imported"] == 6
    assert len(result["interval_statistic_ids"]) == 2
    assert len(result["accumulated_statistic_ids"]) == 4
    assert len(imported) == 6

    imported_statistic_ids = {metadata["statistic_id"] for metadata, _stats in imported}
    assert (
        coordinator._statistic_id(first_assignment.nmi, "E1") in imported_statistic_ids
    )
    assert (
        coordinator._statistic_id(second_assignment.nmi, "E1") in imported_statistic_ids
    )
    assert (
        coordinator._summary_statistic_id(first_assignment.nmi, "accumulated_import")
        in imported_statistic_ids
    )
    assert (
        coordinator._summary_statistic_id(second_assignment.nmi, "accumulated_import")
        in imported_statistic_ids
    )


async def test_coordinator_imports_accumulated_when_payload_nmi_differs_from_selector(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Accumulated imports should key statistics from parsed payload NMI."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012342987",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Accumulated",
        description="Synthetic",
        is_default=True,
    )
    payload_nmi = "20012345678"
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = ""
    client.download_accumulated_summary_csv.return_value = _sample_summary_csv_for_nmi(
        payload_nmi
    )

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["accumulated_rows_imported"] == 3
    assert result["accumulated_channels_imported"] == 2
    assert len(imported) == 2

    imported_statistic_ids = {metadata["statistic_id"] for metadata, _ in imported}
    assert (
        coordinator._summary_statistic_id(payload_nmi, "accumulated_import")
        in imported_statistic_ids
    )
    assert (
        coordinator._summary_statistic_id(payload_nmi, "accumulated_export")
        in imported_statistic_ids
    )
    assert (
        coordinator._summary_statistic_id(assignment.nmi, "accumulated_import")
        not in imported_statistic_ids
    )


@pytest.mark.parametrize("summary_already_imported", [False, True])
async def test_coordinator_imports_combined_when_streams_are_consecutive(
    hass, mock_config_entry, monkeypatch, *, summary_already_imported: bool
) -> None:
    """Coordinator should add a combined import stream for consecutive meters."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = _sample_detailed_csv_for_nmi_on_day(
        assignment.nmi,
        "20250318",
    )
    client.download_accumulated_summary_csv.return_value = _sample_summary_csv_for_nmi(
        assignment.nmi
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)
    summary_statistic_id = coordinator._summary_statistic_id(
        assignment.nmi, "accumulated_import"
    )
    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        if summary_already_imported:
            return [{"statistic_id": summary_statistic_id}]
        return []

    def _fake_get_last_statistics(
        _hass: Any,
        _count: int,
        statistic_id: str,
        *_args: Any,
    ) -> dict[str, list[dict[str, float]]]:
        if summary_already_imported and statistic_id == summary_statistic_id:
            return {
                statistic_id: [
                    {
                        "start": datetime(2024, 12, 11, 14, tzinfo=UTC).timestamp(),
                        "sum": 837.0,
                    }
                ]
            }
        return {}

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        _fake_get_last_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    result = await coordinator._async_update_data()

    assert client.download_detailed_csv.await_args.args[1] == INITIAL_BACKFILL_START
    assert result["combined_rows_imported"] == 2
    assert result["combined_channels_imported"] == 1
    assert len(result["combined_statistic_ids"]) == 1

    combined_statistic_id = coordinator._combined_statistic_id(assignment.nmi)
    combined_rows = []
    for metadata, statistics in imported:
        if metadata["statistic_id"] == combined_statistic_id:
            combined_rows = statistics
            break

    assert len(combined_rows) == 2
    assert combined_rows[0]["sum"] == 837.0
    assert combined_rows[1]["state"] == 1022.5
    assert combined_rows[1]["sum"] == 1859.5


async def test_combined_import_preserves_accumulated_point_at_handover(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Values at an equal handover timestamp should be combined, not dropped."""
    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass,
        mock_config_entry,
        AsyncMock(),
    )
    boundary = datetime(2025, 3, 18, 14, tzinfo=UTC)
    imported: list[dict[str, Any]] = []
    monkeypatch.setattr(
        coordinator,
        "_async_last_statistic_snapshot",
        AsyncMock(return_value=(None, 0.0)),
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        lambda _hass, _metadata, statistics: imported.extend(statistics),
    )

    result = await coordinator._async_sync_combined_import_statistics(
        "20012345678",
        (
            (datetime(2025, 3, 17, 14, tzinfo=UTC), 837.0),
            (boundary, 1020.0),
        ),
        ((boundary, 2.5),),
    )

    assert result.rows_imported == 2
    assert imported[0]["sum"] == 837.0
    assert imported[1]["start"] == boundary
    assert imported[1]["state"] == 1022.5
    assert imported[1]["sum"] == 1859.5


async def test_coordinator_imports_combined_across_consecutive_nmis(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Coordinator should combine consecutive streams even when NMIs differ."""
    client = AsyncMock()
    old_assignment = NmiAssignment(
        nmi="20011111111",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Accumulated",
        description="Old Meter",
        is_default=False,
    )
    new_assignment = NmiAssignment(
        nmi="20022222222",
        company="SAPN",
        meter_serial_number="METER2",
        meter_type_description="Interval",
        description="New Meter",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [old_assignment, new_assignment]

    async def _fake_download_detailed_csv(
        nmi: str,
        _start: datetime,
        _end: datetime,
    ) -> str:
        if nmi == new_assignment.nmi:
            return _sample_detailed_csv_for_nmi_on_day(new_assignment.nmi, "20250318")
        return ""

    async def _fake_download_accumulated_summary_csv(
        nmi: str,
        _start: datetime,
        _end: datetime,
    ) -> str:
        if nmi == old_assignment.nmi:
            return _sample_summary_csv_for_nmi(old_assignment.nmi)
        return ""

    client.download_detailed_csv.side_effect = _fake_download_detailed_csv
    client.download_accumulated_summary_csv.side_effect = (
        _fake_download_accumulated_summary_csv
    )

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return []

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)

    result = await coordinator._async_update_data()

    assert result["combined_rows_imported"] == 2
    assert result["combined_channels_imported"] == 1
    assert len(result["combined_statistic_ids"]) == 1

    combined_statistic_id = coordinator._combined_statistic_id(new_assignment.nmi)
    assert result["combined_statistic_ids"] == [combined_statistic_id]

    combined_rows = []
    for metadata, statistics in imported:
        if metadata["statistic_id"] == combined_statistic_id:
            combined_rows = statistics
            break

    assert len(combined_rows) == 2
    assert combined_rows[0]["sum"] == 837.0
    assert combined_rows[1]["state"] == 1022.5
    assert combined_rows[1]["sum"] == 1859.5


async def test_coordinator_continues_combined_from_interval_when_gap_grows(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Combined stream should keep advancing from interval points after handoff."""
    client = AsyncMock()
    assignment = NmiAssignment(
        nmi="20012345678",
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description="Interval",
        description="Synthetic",
        is_default=True,
    )
    client.get_nmi_assignments.return_value = [assignment]
    client.download_detailed_csv.return_value = _sample_detailed_csv_for_nmi_on_day(
        assignment.nmi,
        "20260809",
    )
    client.download_accumulated_summary_csv.return_value = _sample_summary_csv_for_nmi(
        assignment.nmi
    )

    imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    mock_config_entry.add_to_hass(hass)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, mock_config_entry, client)
    combined_statistic_id = coordinator._combined_statistic_id(assignment.nmi)

    async def _fake_list_statistic_ids(_hass: Any) -> list[dict[str, str]]:
        return [{"statistic_id": combined_statistic_id}]

    def _fake_add_external_statistics(
        _hass: Any,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.append((metadata, list(statistics)))

    combined_last = datetime(2026, 8, 8, 13, 0, tzinfo=UTC)

    def _fake_get_last_statistics(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        statistic_ids = kwargs.get("statistic_ids")
        if statistic_ids is None and len(_args) >= 3:
            statistic_ids = _args[2]

        if statistic_ids == combined_statistic_id:
            return {
                combined_statistic_id: [
                    {
                        "start": combined_last.timestamp(),
                        "sum": 100.0,
                    }
                ]
            }
        return {}

    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_list_statistic_ids",
        _fake_list_statistic_ids,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.async_add_external_statistics",
        _fake_add_external_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_last_statistics",
        _fake_get_last_statistics,
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.coordinator.get_instance",
        lambda _hass: _FakeRecorderInstance(),
    )

    result = await coordinator._async_update_data()

    assert result["combined_rows_imported"] == 1
    assert result["combined_channels_imported"] == 1
    assert result["combined_statistic_ids"] == [combined_statistic_id]

    combined_rows = []
    for metadata, statistics in imported:
        if metadata["statistic_id"] == combined_statistic_id:
            combined_rows = statistics
            break

    assert len(combined_rows) == 1
    assert combined_rows[0]["sum"] == 102.5
    called_start = client.download_detailed_csv.await_args.args[1]
    assert called_start == combined_last + timedelta(hours=1)
