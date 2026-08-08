"""DataUpdateCoordinator for SA Power Networks."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    NmiAssignment,
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientError,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INITIAL_BACKFILL_START,
    LOGGER,
    STATISTIC_NAME_PREFIX,
)
from .parsing import (
    IntervalReading,
    SummaryPeriod,
    parse_nem12_content,
    parse_summary_csv,
)
from .privacy import mask_identifier

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import SAPowerNetworksConfigEntry


@dataclass(frozen=True)
class SyncImportStats:
    """Import counters for one coordinator refresh."""

    rows_imported: int = 0
    channels_imported: int = 0
    interval_rows_imported: int = 0
    interval_channels_imported: int = 0
    accumulated_rows_imported: int = 0
    accumulated_channels_imported: int = 0


class SAPowerNetworksDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: SAPowerNetworksConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SAPowerNetworksConfigEntry,
        client: SAPowerNetworksApiClient,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.config_entry = config_entry
        self.client = client

    async def _async_update_data(self) -> Any:
        """Update data via library."""
        try:
            assignments = await self.client.get_nmi_assignments()
            import_stats = await self._async_sync_statistics(assignments)
            return {
                "authenticated": True,
                "nmi_count": len(assignments),
                "nmis": [item.nmi for item in assignments],
                "rows_imported": import_stats.rows_imported,
                "channels_imported": import_stats.channels_imported,
                "interval_rows_imported": import_stats.interval_rows_imported,
                "interval_channels_imported": import_stats.interval_channels_imported,
                "accumulated_rows_imported": import_stats.accumulated_rows_imported,
                "accumulated_channels_imported": (
                    import_stats.accumulated_channels_imported
                ),
                "last_error": "",
                "last_sync": datetime.now(tz=UTC),
            }
        except SAPowerNetworksApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except SAPowerNetworksApiClientError as exception:
            raise UpdateFailed(exception) from exception

    async def _async_sync_statistics(
        self,
        assignments: list[NmiAssignment],
    ) -> SyncImportStats:
        """Download, parse, and import interval and accumulated statistics."""
        total_rows = 0
        total_channels = 0
        interval_rows = 0
        interval_channels = 0
        accumulated_rows = 0
        accumulated_channels = 0
        existing_statistic_ids = await self._async_existing_statistic_ids()

        for assignment in assignments:
            interval_fetch_start = await self._async_fetch_start(
                assignment.nmi,
                existing_statistic_ids,
            )
            fetch_end = datetime.now(tz=UTC)
            if interval_fetch_start < fetch_end:
                (
                    rows_imported,
                    channels_imported,
                ) = await self._async_sync_interval_statistics(
                    assignment,
                    interval_fetch_start,
                    fetch_end,
                )
                total_rows += rows_imported
                total_channels += channels_imported
                interval_rows += rows_imported
                interval_channels += channels_imported

            summary_fetch_start = await self._async_accumulated_fetch_start(
                assignment.nmi,
                existing_statistic_ids,
            )
            if summary_fetch_start < fetch_end:
                (
                    rows_imported,
                    channels_imported,
                ) = await self._async_sync_accumulated_statistics(
                    assignment,
                    summary_fetch_start,
                    fetch_end,
                )
                total_rows += rows_imported
                total_channels += channels_imported
                accumulated_rows += rows_imported
                accumulated_channels += channels_imported

        return SyncImportStats(
            rows_imported=total_rows,
            channels_imported=total_channels,
            interval_rows_imported=interval_rows,
            interval_channels_imported=interval_channels,
            accumulated_rows_imported=accumulated_rows,
            accumulated_channels_imported=accumulated_channels,
        )

    async def _async_sync_interval_statistics(
        self,
        assignment: NmiAssignment,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> tuple[int, int]:
        """Import detailed interval statistics for one NMI."""
        raw_csv = await self.client.download_detailed_csv(
            assignment.nmi,
            fetch_start,
            fetch_end,
        )
        parsed = parse_nem12_content(raw_csv)

        total_rows = 0
        total_channels = 0
        for (nmi, suffix), readings in parsed.items():
            statistic_id = self._statistic_id(nmi, suffix)
            last_start_ts, last_sum = await self._async_last_statistic_snapshot(
                statistic_id
            )
            statistics = self._build_statistics(
                readings,
                last_start_ts,
                last_sum,
            )
            if not statistics:
                continue

            async_add_external_statistics(
                self.hass,
                self._statistic_metadata(nmi, suffix, statistic_id),
                statistics,
            )
            total_rows += len(statistics)
            total_channels += 1

        return total_rows, total_channels

    async def _async_sync_accumulated_statistics(
        self,
        assignment: NmiAssignment,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> tuple[int, int]:
        """Import accumulated summary statistics for one NMI."""
        raw_csv = await self.client.download_accumulated_summary_csv(
            assignment.nmi,
            fetch_start,
            fetch_end,
        )
        periods = parse_summary_csv(raw_csv)

        total_rows = 0
        total_channels = 0
        grouped_periods: dict[str, list[SummaryPeriod]] = {}
        for period in periods:
            grouped_periods.setdefault(period.nmi, []).append(period)

        for nmi, nmi_periods in grouped_periods.items():
            for direction, direction_periods in (
                ("accumulated_import", nmi_periods),
                (
                    "accumulated_export",
                    [period for period in nmi_periods if period.export_kwh > 0],
                ),
            ):
                if not direction_periods:
                    continue

                statistic_id = self._summary_statistic_id(nmi, direction)
                last_start_ts, last_sum = await self._async_last_statistic_snapshot(
                    statistic_id
                )
                statistics = self._build_summary_statistics(
                    direction_periods,
                    direction,
                    last_start_ts,
                    last_sum,
                )
                if not statistics:
                    continue

                async_add_external_statistics(
                    self.hass,
                    self._summary_statistic_metadata(
                        nmi,
                        direction,
                        statistic_id,
                    ),
                    statistics,
                )
                total_rows += len(statistics)
                total_channels += 1

        return total_rows, total_channels

    async def _async_sync_accumulated_statistics_for_nmi(
        self,
        nmi: str,
        nmi_periods: list[SummaryPeriod],
    ) -> tuple[int, int]:
        """Build and import accumulated streams for one parsed NMI."""
        total_rows = 0
        total_channels = 0
        for direction, direction_periods in (
            ("accumulated_import", nmi_periods),
            (
                "accumulated_export",
                [period for period in nmi_periods if period.export_kwh > 0],
            ),
        ):
            if not direction_periods:
                continue

            statistic_id = self._summary_statistic_id(nmi, direction)
            last_start_ts, last_sum = await self._async_last_statistic_snapshot(
                statistic_id
            )
            statistics = self._build_summary_statistics(
                direction_periods,
                direction,
                last_start_ts,
                last_sum,
            )
            if not statistics:
                continue

            async_add_external_statistics(
                self.hass,
                self._summary_statistic_metadata(
                    nmi,
                    direction,
                    statistic_id,
                ),
                statistics,
            )
            total_rows += len(statistics)
            total_channels += 1

        return total_rows, total_channels

    async def _async_existing_statistic_ids(self) -> set[str]:
        """Return existing recorder statistic ids for this integration."""
        metadata = await async_list_statistic_ids(self.hass)
        return {
            item["statistic_id"]
            for item in metadata
            if isinstance(item.get("statistic_id"), str)
            and item["statistic_id"].startswith(f"{DOMAIN}:")
        }

    async def _async_fetch_start(
        self,
        nmi: str,
        existing_statistic_ids: set[str],
    ) -> datetime:
        """Determine the earliest missing window start for an NMI."""
        matching_ids = {
            statistic_id
            for statistic_id in existing_statistic_ids
            if statistic_id.startswith(self._statistic_prefix(nmi))
        }
        if not matching_ids:
            return INITIAL_BACKFILL_START

        latest_datetimes = []
        for statistic_id in matching_ids:
            last_start_ts, _last_sum = await self._async_last_statistic_snapshot(
                statistic_id
            )
            if last_start_ts is None:
                continue
            latest_datetimes.append(datetime.fromtimestamp(last_start_ts, tz=UTC))

        if not latest_datetimes:
            return INITIAL_BACKFILL_START
        return min(latest_datetimes)

    async def _async_accumulated_fetch_start(
        self,
        nmi: str,
        existing_statistic_ids: set[str],
    ) -> datetime:
        """Determine fetch start for accumulated summary streams for an NMI."""
        matching_ids = {
            statistic_id
            for statistic_id in existing_statistic_ids
            if statistic_id.startswith(self._summary_statistic_prefix(nmi))
        }
        if not matching_ids:
            return INITIAL_BACKFILL_START

        latest_datetimes = []
        for statistic_id in matching_ids:
            last_start_ts, _last_sum = await self._async_last_statistic_snapshot(
                statistic_id
            )
            if last_start_ts is None:
                continue
            latest_datetimes.append(datetime.fromtimestamp(last_start_ts, tz=UTC))

        if not latest_datetimes:
            return INITIAL_BACKFILL_START
        return min(latest_datetimes)

    async def _async_last_statistic_snapshot(
        self,
        statistic_id: str,
    ) -> tuple[float | None, float]:
        """Return last stored timestamp and cumulative sum for one statistic id."""
        convert_units = True
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            convert_units,
            {"sum"},
        )
        records = result.get(statistic_id)
        if not records:
            return None, 0.0

        record = records[0]
        last_start_ts = record.get("start")
        if not isinstance(last_start_ts, float):
            last_start_ts = None
        return last_start_ts, float(record.get("sum") or 0.0)

    def _build_statistics(
        self,
        readings: list[IntervalReading],
        last_start_ts: float | None,
        running_sum: float,
    ) -> list[StatisticData]:
        """Build hourly recorder statistics, skipping data already present."""
        statistics: list[StatisticData] = []
        hourly_totals: dict[datetime, float] = defaultdict(float)
        for reading in sorted(readings, key=lambda item: item.interval_start):
            if reading.value_kwh is None:
                continue

            hour_start = reading.interval_start.replace(
                minute=0, second=0, microsecond=0
            )
            hourly_totals[hour_start] += reading.value_kwh

        for hour_start, hour_value in sorted(hourly_totals.items()):
            hour_start_ts = hour_start.timestamp()
            if last_start_ts is not None and hour_start_ts <= last_start_ts:
                continue

            running_sum += hour_value
            statistics.append(
                StatisticData(
                    start=hour_start,
                    state=hour_value,
                    sum=running_sum,
                )
            )

        return statistics

    def _build_summary_statistics(
        self,
        periods: list[SummaryPeriod],
        direction: str,
        last_start_ts: float | None,
        running_sum: float,
    ) -> list[StatisticData]:
        """Build accumulated summary statistics for one direction stream."""
        statistics: list[StatisticData] = []
        for period in sorted(
            periods, key=lambda item: (item.period_end, item.period_start)
        ):
            period_start = datetime.combine(
                period.period_end, datetime.min.time(), tzinfo=UTC
            )
            period_start_ts = period_start.timestamp()
            if last_start_ts is not None and period_start_ts <= last_start_ts:
                continue

            value_kwh = period.import_kwh
            if direction == "accumulated_export":
                value_kwh = period.export_kwh

            running_sum += value_kwh
            statistics.append(
                StatisticData(
                    start=period_start,
                    state=value_kwh,
                    sum=running_sum,
                )
            )

        return statistics

    def _statistic_metadata(
        self,
        nmi: str,
        suffix: str,
        statistic_id: str,
    ) -> StatisticMetaData:
        """Build recorder metadata for one NMI/channel stream."""
        direction = self._channel_direction(suffix)
        masked_nmi = mask_identifier(nmi)
        channel_label = suffix.upper()
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"{STATISTIC_NAME_PREFIX} {direction.title()} "
                f"{masked_nmi} {channel_label}"
            ),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    def _summary_statistic_metadata(
        self,
        nmi: str,
        direction: str,
        statistic_id: str,
    ) -> StatisticMetaData:
        """Build recorder metadata for accumulated summary streams."""
        masked_nmi = mask_identifier(nmi)
        label = direction.replace("_", " ").title()
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{STATISTIC_NAME_PREFIX} {label} {masked_nmi}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    def _statistic_id(self, nmi: str, suffix: str) -> str:
        """Create a privacy-safe deterministic statistic id for one channel."""
        digest = sha256(nmi.encode("utf-8")).hexdigest()[:12]
        channel = self._slugify(self._channel_direction(suffix))
        suffix_slug = self._slugify(suffix)
        return f"{DOMAIN}:{digest}_{channel}_{suffix_slug}"

    def _statistic_prefix(self, nmi: str) -> str:
        """Return the privacy-safe prefix shared by all statistic ids for an NMI."""
        digest = sha256(nmi.encode("utf-8")).hexdigest()[:12]
        return f"{DOMAIN}:{digest}_"

    def _summary_statistic_id(self, nmi: str, direction: str) -> str:
        """Create a privacy-safe deterministic statistic id for accumulated data."""
        digest = sha256(nmi.encode("utf-8")).hexdigest()[:12]
        return f"{DOMAIN}:{digest}_{self._slugify(direction)}"

    def _summary_statistic_prefix(self, nmi: str) -> str:
        """Return the shared prefix used by accumulated summary statistic ids."""
        digest = sha256(nmi.encode("utf-8")).hexdigest()[:12]
        return f"{DOMAIN}:{digest}_accumulated_"

    @staticmethod
    def _channel_direction(suffix: str) -> str:
        """Infer an import/export direction from a NEM12 suffix."""
        upper = suffix.upper()
        if upper.startswith("B"):
            return "export"
        if upper.startswith("E"):
            return "import"
        return "channel"

    @staticmethod
    def _slugify(value: str) -> str:
        """Create a statistic-id-safe fragment."""
        lowered = value.strip().lower()
        return re.sub(r"[^a-z0-9_]+", "_", lowered).strip("_") or "unknown"
