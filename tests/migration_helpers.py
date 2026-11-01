"""Shared fakes for legacy statistics migration and Repairs tests."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapowernetworks.api import NmiAssignment
from custom_components.sapowernetworks.const import (
    DOMAIN,
    STATISTICS_MIGRATION,
    STATISTICS_MIGRATION_CHECK,
    STATISTICS_MIGRATION_PENDING_DIGESTS,
)
from custom_components.sapowernetworks.statistics_migration import statistic_digest

if TYPE_CHECKING:
    import pytest

COORDINATOR_MODULE = "custom_components.sapowernetworks.coordinator"

NMI = "20012345678"
NMI2 = "20098765432"
DIGEST = statistic_digest(NMI)
DIGEST2 = statistic_digest(NMI2)
MASKED_NMI = "*******5678"


class FakeRecorder:
    """Recorder double that stores statistics and clears them from a thread."""

    def __init__(
        self,
        existing: dict[str, tuple[float, float]] | None = None,
        *,
        clear_delay: float | None = 0.05,
    ) -> None:
        """Initialize the Recorder test double."""
        self.existing: dict[str, tuple[float, float]] = dict(existing or {})
        self.events: list[str] = []
        self.imported: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        self.cleared: list[list[str]] = []
        self.on_done_threads: list[threading.Thread] = []
        self.clear_delay = clear_delay

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        """Run an executor job inline."""
        return target(*args)

    def async_clear_statistics(
        self,
        statistic_ids: list[str],
        *,
        on_done: Any,
    ) -> None:
        """Clear statistics and invoke the completion callback from another thread."""
        self.events.append("clear_requested")
        self.cleared.append(list(statistic_ids))
        if self.clear_delay is None:
            return

        def _complete() -> None:
            self.on_done_threads.append(threading.current_thread())
            for statistic_id in statistic_ids:
                self.existing.pop(statistic_id, None)
            self.events.append("clear_done")
            on_done()

        threading.Timer(self.clear_delay, _complete).start()

    def imported_ids(self) -> list[str]:
        """Return IDs passed to external-statistics imports."""
        return [metadata["statistic_id"] for metadata, _rows in self.imported]

    def rows_for(self, statistic_id: str) -> list[dict[str, Any]]:
        """Return imported rows for one statistic ID."""
        for metadata, rows in self.imported:
            if metadata["statistic_id"] == statistic_id:
                return rows
        return []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeRecorder:
        """Patch coordinator Recorder helpers with this fake."""

        async def _list_ids(_hass: Any) -> list[dict[str, str]]:
            return [{"statistic_id": item} for item in self.existing]

        def _add(_hass: Any, metadata: dict[str, Any], statistics: list[dict]) -> None:
            self.events.append(f"add:{metadata['statistic_id']}")
            self.imported.append((metadata, list(statistics)))

        def _last(
            _hass: Any,
            _count: int,
            statistic_id: str,
            *_args: Any,
        ) -> dict[str, list[dict[str, float]]]:
            if statistic_id not in self.existing:
                return {}
            start, total = self.existing[statistic_id]
            return {statistic_id: [{"start": start, "sum": total}]}

        monkeypatch.setattr(f"{COORDINATOR_MODULE}.async_list_statistic_ids", _list_ids)
        monkeypatch.setattr(f"{COORDINATOR_MODULE}.async_add_external_statistics", _add)
        monkeypatch.setattr(f"{COORDINATOR_MODULE}.get_last_statistics", _last)
        monkeypatch.setattr(f"{COORDINATOR_MODULE}.get_instance", lambda _hass: self)
        return self


def assignment(nmi: str = NMI, meter_type: str = "Interval") -> NmiAssignment:
    """Build a synthetic NMI assignment."""
    return NmiAssignment(
        nmi=nmi,
        company="SAPN",
        meter_serial_number="METER1",
        meter_type_description=meter_type,
        description="Synthetic",
        is_default=True,
    )


def detailed_csv(nmi: str = NMI, yyyymmdd: str = "20250318") -> str:
    """Build a small synthetic NEM12 CSV."""
    values = ["1.0", "1.5"] + ["" for _ in range(46)]
    return "\n".join(
        [
            f"200,{nmi},E1B1,E1,E1,,METER1,KWH,30,",
            f"300,{yyyymmdd},{','.join(values)},A,,,{yyyymmdd}235959,",
            "900",
        ]
    )


def summary_csv(nmi: str = NMI) -> str:
    """Build synthetic accumulated-summary history."""
    return (
        f"{nmi},1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        f"{nmi},1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
    )


def make_client(
    nmis: list[str] | None = None,
    *,
    detailed: dict[str, str] | None = None,
    summary: dict[str, str] | None = None,
) -> AsyncMock:
    """Return an API client double keyed by NMI."""
    client = AsyncMock()
    client.get_nmi_assignments.return_value = [
        assignment(nmi) for nmi in (nmis if nmis is not None else [NMI])
    ]
    detailed = detailed or {}
    summary = summary or {}

    async def _detailed(nmi: str, *_args: Any) -> str:
        return detailed.get(nmi, "")

    async def _summary(nmi: str, *_args: Any) -> str:
        return summary.get(nmi, "")

    client.download_detailed_csv.side_effect = _detailed
    client.download_accumulated_summary_csv.side_effect = _summary
    return client


def make_entry(
    state: str = STATISTICS_MIGRATION_CHECK,
    *,
    unique_id: str = "legacy-user",
    pending: list[str] | None = None,
    **extra: Any,
) -> MockConfigEntry:
    """Build a config entry with the selected migration state."""
    data: dict[str, Any] = {
        CONF_USERNAME: f"{unique_id}@example.com",
        CONF_PASSWORD: "secret",
        STATISTICS_MIGRATION: state,
        **extra,
    }
    if pending is not None:
        data[STATISTICS_MIGRATION_PENDING_DIGESTS] = pending
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        minor_version=2,
        data=data,
    )
