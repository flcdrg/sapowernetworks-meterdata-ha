"""Tests for SA Power Networks coordinator."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.sapowernetworks.api import NmiAssignment
from custom_components.sapowernetworks.const import DOMAIN
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
    assert not imported
    called_start = client.download_detailed_csv.await_args.args[1]
    assert called_start == datetime(2026, 6, 20, 1, 0, tzinfo=UTC)


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
    assert by_statistic_id[export_stat_id][0]["sum"] == 12.5


async def test_coordinator_advances_accumulated_fetch_start(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Accumulated sync should request data after the latest imported summary period."""
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

    called_start = client.download_accumulated_summary_csv.await_args.args[1]
    assert called_start == datetime(2025, 10, 16, 0, 0, tzinfo=UTC)


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
