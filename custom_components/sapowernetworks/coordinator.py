"""DataUpdateCoordinator for SA Power Networks."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, TypedDict

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
from homeassistant.helpers.recorder import get_instance
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
    combined_rows_imported: int = 0
    combined_channels_imported: int = 0
    interval_statistic_ids: tuple[str, ...] = ()
    accumulated_statistic_ids: tuple[str, ...] = ()
    combined_statistic_ids: tuple[str, ...] = ()
    latest_interval_data_point: datetime | None = None


@dataclass(frozen=True)
class ImportBatchResult:
    """Import results for one sync sub-step."""

    rows_imported: int = 0
    channels_imported: int = 0
    statistic_ids: tuple[str, ...] = ()
    import_points_by_nmi: dict[str, tuple[tuple[datetime, float], ...]] | None = None
    latest_interval_data_point: datetime | None = None


class CombinedStreamPair(TypedDict):
    """Matched accumulated and interval streams that can be combined."""

    combined_nmi: str
    accumulated_points: tuple[tuple[datetime, float], ...]
    interval_points: tuple[tuple[datetime, float], ...]


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
            LOGGER.debug("Coordinator refresh started")
            assignments = await self.client.get_nmi_assignments()
            LOGGER.debug(
                "Fetched NMI assignments", extra={"nmi_count": len(assignments)}
            )
            import_stats = await self._async_sync_statistics(assignments)
            feed_lag_hours: float | None = None
            if import_stats.latest_interval_data_point is not None:
                lag_seconds = (
                    datetime.now(tz=UTC) - import_stats.latest_interval_data_point
                ).total_seconds()
                feed_lag_hours = round(max(lag_seconds, 0.0) / 3600.0, 2)

            LOGGER.info(
                "Refresh completed: rows=%s channels=%s interval_rows=%s "
                "accumulated_rows=%s combined_rows=%s lag_hours=%s",
                import_stats.rows_imported,
                import_stats.channels_imported,
                import_stats.interval_rows_imported,
                import_stats.accumulated_rows_imported,
                import_stats.combined_rows_imported,
                feed_lag_hours,
            )

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
                "combined_rows_imported": import_stats.combined_rows_imported,
                "combined_channels_imported": import_stats.combined_channels_imported,
                "interval_statistic_ids": list(import_stats.interval_statistic_ids),
                "accumulated_statistic_ids": list(
                    import_stats.accumulated_statistic_ids
                ),
                "combined_statistic_ids": list(import_stats.combined_statistic_ids),
                "latest_interval_data_point": import_stats.latest_interval_data_point,
                "feed_lag_hours": feed_lag_hours,
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
        combined_rows = 0
        combined_channels = 0
        interval_statistic_ids: list[str] = []
        accumulated_statistic_ids: list[str] = []
        combined_statistic_ids: list[str] = []
        latest_interval_data_point: datetime | None = None
        interval_import_points_by_nmi: dict[str, list[tuple[datetime, float]]] = {}
        accumulated_import_points_by_nmi: dict[str, list[tuple[datetime, float]]] = {}
        existing_statistic_ids = await self._async_existing_statistic_ids()
        fetch_end = datetime.now(tz=UTC)

        for assignment in assignments:
            masked_nmi = mask_identifier(assignment.nmi)
            interval_result = ImportBatchResult(import_points_by_nmi={})
            interval_fetch_start = await self._async_fetch_start(
                assignment.nmi,
                existing_statistic_ids,
            )
            if interval_fetch_start < fetch_end:
                LOGGER.debug(
                    "Interval sync window for %s: %s to %s",
                    masked_nmi,
                    interval_fetch_start.isoformat(),
                    fetch_end.isoformat(),
                )
                interval_result = await self._async_sync_interval_statistics(
                    assignment,
                    interval_fetch_start,
                    fetch_end,
                )
                (
                    total_rows,
                    total_channels,
                    interval_rows,
                    interval_channels,
                ) = self._accumulate_batch_result(
                    interval_result,
                    counters=(
                        total_rows,
                        total_channels,
                        interval_rows,
                        interval_channels,
                    ),
                    statistic_ids=interval_statistic_ids,
                )
                if interval_result.latest_interval_data_point is not None and (
                    latest_interval_data_point is None
                    or interval_result.latest_interval_data_point
                    > latest_interval_data_point
                ):
                    latest_interval_data_point = (
                        interval_result.latest_interval_data_point
                    )
                self._merge_import_points(
                    interval_import_points_by_nmi,
                    interval_result.import_points_by_nmi or {},
                )
                LOGGER.debug(
                    "Interval sync result for %s: rows=%s channels=%s",
                    masked_nmi,
                    interval_result.rows_imported,
                    interval_result.channels_imported,
                )

            accumulated_result = ImportBatchResult(import_points_by_nmi={})
            summary_fetch_start = await self._async_accumulated_fetch_start(
                assignment.nmi,
                existing_statistic_ids,
            )
            if summary_fetch_start < fetch_end:
                LOGGER.debug(
                    "Accumulated sync window for %s: %s to %s",
                    masked_nmi,
                    summary_fetch_start.isoformat(),
                    fetch_end.isoformat(),
                )
                accumulated_result = await self._async_sync_accumulated_statistics(
                    assignment,
                    summary_fetch_start,
                    fetch_end,
                )
                (
                    total_rows,
                    total_channels,
                    accumulated_rows,
                    accumulated_channels,
                ) = self._accumulate_batch_result(
                    accumulated_result,
                    counters=(
                        total_rows,
                        total_channels,
                        accumulated_rows,
                        accumulated_channels,
                    ),
                    statistic_ids=accumulated_statistic_ids,
                )
                self._merge_import_points(
                    accumulated_import_points_by_nmi,
                    accumulated_result.import_points_by_nmi or {},
                )
                LOGGER.debug(
                    "Accumulated sync result for %s: rows=%s channels=%s",
                    masked_nmi,
                    accumulated_result.rows_imported,
                    accumulated_result.channels_imported,
                )

        selected_pairs = self._select_consecutive_stream_pairs(
            accumulated_import_points_by_nmi,
            interval_import_points_by_nmi,
        )
        for pair in selected_pairs:
            combined_result = await self._async_sync_combined_import_statistics(
                pair["combined_nmi"],
                pair["accumulated_points"],
                pair["interval_points"],
            )
            (
                total_rows,
                total_channels,
                combined_rows,
                combined_channels,
            ) = self._accumulate_batch_result(
                combined_result,
                counters=(
                    total_rows,
                    total_channels,
                    combined_rows,
                    combined_channels,
                ),
                statistic_ids=combined_statistic_ids,
            )
            LOGGER.debug(
                "Combined sync result for %s: rows=%s channels=%s",
                mask_identifier(pair["combined_nmi"]),
                combined_result.rows_imported,
                combined_result.channels_imported,
            )

        paired_interval_nmis = {str(pair["combined_nmi"]) for pair in selected_pairs}
        for nmi, interval_points in interval_import_points_by_nmi.items():
            if nmi in paired_interval_nmis:
                continue
            combined_result = await self._async_sync_combined_statistics_from_interval(
                nmi,
                tuple(interval_points),
            )
            (
                total_rows,
                total_channels,
                combined_rows,
                combined_channels,
            ) = self._accumulate_batch_result(
                combined_result,
                counters=(
                    total_rows,
                    total_channels,
                    combined_rows,
                    combined_channels,
                ),
                statistic_ids=combined_statistic_ids,
            )
            LOGGER.debug(
                "Interval-only combined continuation for %s: rows=%s channels=%s",
                mask_identifier(nmi),
                combined_result.rows_imported,
                combined_result.channels_imported,
            )

        return SyncImportStats(
            rows_imported=total_rows,
            channels_imported=total_channels,
            interval_rows_imported=interval_rows,
            interval_channels_imported=interval_channels,
            accumulated_rows_imported=accumulated_rows,
            accumulated_channels_imported=accumulated_channels,
            combined_rows_imported=combined_rows,
            combined_channels_imported=combined_channels,
            interval_statistic_ids=tuple(interval_statistic_ids),
            accumulated_statistic_ids=tuple(accumulated_statistic_ids),
            combined_statistic_ids=tuple(combined_statistic_ids),
            latest_interval_data_point=latest_interval_data_point,
        )

    @staticmethod
    def _accumulate_batch_result(
        result: ImportBatchResult,
        counters: tuple[int, int, int, int],
        statistic_ids: list[str],
    ) -> tuple[int, int, int, int]:
        """Accumulate counters and ids from one sync batch result."""
        total_rows, total_channels, category_rows, category_channels = counters
        statistic_ids.extend(result.statistic_ids)
        return (
            total_rows + result.rows_imported,
            total_channels + result.channels_imported,
            category_rows + result.rows_imported,
            category_channels + result.channels_imported,
        )

    @staticmethod
    def _merge_import_points(
        target: dict[str, list[tuple[datetime, float]]],
        source: dict[str, tuple[tuple[datetime, float], ...]],
    ) -> None:
        """Append import points grouped by NMI while preserving order stability."""
        for nmi, points in source.items():
            bucket = target.setdefault(nmi, [])
            bucket.extend(points)

    @staticmethod
    def _select_consecutive_stream_pairs(
        accumulated_by_nmi: dict[str, list[tuple[datetime, float]]],
        interval_by_nmi: dict[str, list[tuple[datetime, float]]],
    ) -> list[CombinedStreamPair]:
        """Match accumulated and interval streams that form one timeline."""
        candidates: list[tuple[int, float, str, str]] = []
        for accumulated_nmi, accumulated_points in accumulated_by_nmi.items():
            if not accumulated_points:
                continue
            accumulated_end = max(point_start for point_start, _ in accumulated_points)
            for interval_nmi, interval_points in interval_by_nmi.items():
                if not interval_points:
                    continue

                interval_start = min(point_start for point_start, _ in interval_points)
                if interval_start < accumulated_end:
                    continue

                gap = interval_start - accumulated_end
                if gap > timedelta(days=1):
                    continue

                same_nmi_rank = 0 if accumulated_nmi == interval_nmi else 1
                candidates.append(
                    (
                        same_nmi_rank,
                        gap.total_seconds(),
                        accumulated_nmi,
                        interval_nmi,
                    )
                )

        pairs: list[CombinedStreamPair] = []
        used_accumulated_nmis: set[str] = set()
        used_interval_nmis: set[str] = set()
        for _rank, _gap_seconds, accumulated_nmi, interval_nmi in sorted(candidates):
            if accumulated_nmi in used_accumulated_nmis:
                continue
            if interval_nmi in used_interval_nmis:
                continue

            pairs.append(
                {
                    "combined_nmi": interval_nmi,
                    "accumulated_points": tuple(accumulated_by_nmi[accumulated_nmi]),
                    "interval_points": tuple(interval_by_nmi[interval_nmi]),
                }
            )
            used_accumulated_nmis.add(accumulated_nmi)
            used_interval_nmis.add(interval_nmi)

        return pairs

    async def _async_sync_interval_statistics(
        self,
        assignment: NmiAssignment,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> ImportBatchResult:
        """Import detailed interval statistics for one NMI."""
        raw_csv = await self.client.download_detailed_csv(
            assignment.nmi,
            fetch_start,
            fetch_end,
        )
        parsed = parse_nem12_content(raw_csv)

        total_rows = 0
        total_channels = 0
        statistic_ids: list[str] = []
        import_hourly_totals_by_nmi: dict[str, dict[datetime, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for (nmi, suffix), readings in parsed.items():
            if self._channel_direction(suffix) == "import":
                for reading in sorted(readings, key=lambda item: item.interval_start):
                    if reading.value_kwh is None:
                        continue
                    hour_start = reading.interval_start.replace(
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    import_hourly_totals_by_nmi[nmi][hour_start] += reading.value_kwh

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
            statistic_ids.append(statistic_id)

        return ImportBatchResult(
            rows_imported=total_rows,
            channels_imported=total_channels,
            statistic_ids=tuple(statistic_ids),
            import_points_by_nmi={
                nmi: tuple(sorted(hourly_totals.items()))
                for nmi, hourly_totals in import_hourly_totals_by_nmi.items()
            },
            latest_interval_data_point=max(
                (
                    hour_start
                    for hourly_totals in import_hourly_totals_by_nmi.values()
                    for hour_start in hourly_totals
                ),
                default=None,
            ),
        )

    async def _async_sync_accumulated_statistics(
        self,
        assignment: NmiAssignment,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> ImportBatchResult:
        """Import accumulated summary statistics for one NMI."""
        raw_csv = await self.client.download_accumulated_summary_csv(
            assignment.nmi,
            fetch_start,
            fetch_end,
        )
        periods = parse_summary_csv(raw_csv)

        total_rows = 0
        total_channels = 0
        statistic_ids: list[str] = []
        import_points_by_nmi: dict[str, list[tuple[datetime, float]]] = {}
        grouped_periods: dict[str, list[SummaryPeriod]] = {}
        for period in periods:
            grouped_periods.setdefault(period.nmi, []).append(period)

        for nmi, nmi_periods in grouped_periods.items():
            import_points_by_nmi[nmi] = [
                (
                    datetime.combine(
                        period.period_end,
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    period.import_kwh,
                )
                for period in sorted(
                    nmi_periods,
                    key=lambda item: (item.period_end, item.period_start),
                )
            ]

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
                statistic_ids.append(statistic_id)

        return ImportBatchResult(
            rows_imported=total_rows,
            channels_imported=total_channels,
            statistic_ids=tuple(statistic_ids),
            import_points_by_nmi={
                nmi: tuple(points) for nmi, points in import_points_by_nmi.items()
            },
        )

    async def _async_sync_combined_import_statistics(
        self,
        nmi: str,
        accumulated_import_points: tuple[tuple[datetime, float], ...],
        interval_import_points: tuple[tuple[datetime, float], ...],
    ) -> ImportBatchResult:
        """Import a combined stream when source streams are consecutive."""
        if not accumulated_import_points or not interval_import_points:
            return ImportBatchResult(import_points_by_nmi={})

        sorted_accumulated = sorted(accumulated_import_points)
        sorted_interval = sorted(interval_import_points)

        accumulated_end = sorted_accumulated[-1][0]
        interval_start = sorted_interval[0][0]
        if interval_start < accumulated_end:
            return ImportBatchResult(import_points_by_nmi={})
        if interval_start - accumulated_end > timedelta(days=1):
            return ImportBatchResult(import_points_by_nmi={})

        combined_points = [
            (point_start, point_value)
            for point_start, point_value in sorted_accumulated
            if point_start < interval_start
        ]
        combined_points.extend(
            (point_start, point_value)
            for point_start, point_value in sorted_interval
            if point_start >= interval_start
        )
        if not combined_points:
            return ImportBatchResult(import_points_by_nmi={})

        statistic_id = self._combined_statistic_id(nmi)
        last_start_ts, last_sum = await self._async_last_statistic_snapshot(
            statistic_id
        )
        statistics = self._build_combined_statistics(
            combined_points,
            last_start_ts,
            last_sum,
        )
        if not statistics:
            return ImportBatchResult(import_points_by_nmi={})

        async_add_external_statistics(
            self.hass,
            self._combined_statistic_metadata(nmi, statistic_id),
            statistics,
        )
        return ImportBatchResult(
            rows_imported=len(statistics),
            channels_imported=1,
            statistic_ids=(statistic_id,),
            import_points_by_nmi={},
        )

    async def _async_sync_combined_statistics_from_interval(
        self,
        nmi: str,
        interval_import_points: tuple[tuple[datetime, float], ...],
    ) -> ImportBatchResult:
        """Continue a combined stream from interval points when already initialized."""
        if not interval_import_points:
            return ImportBatchResult(import_points_by_nmi={})

        statistic_id = self._combined_statistic_id(nmi)
        last_start_ts, last_sum = await self._async_last_statistic_snapshot(
            statistic_id
        )
        if last_start_ts is None:
            return ImportBatchResult(import_points_by_nmi={})

        statistics = self._build_combined_statistics(
            list(interval_import_points),
            last_start_ts,
            last_sum,
        )
        if not statistics:
            return ImportBatchResult(import_points_by_nmi={})

        async_add_external_statistics(
            self.hass,
            self._combined_statistic_metadata(nmi, statistic_id),
            statistics,
        )
        return ImportBatchResult(
            rows_imported=len(statistics),
            channels_imported=1,
            statistic_ids=(statistic_id,),
            import_points_by_nmi={},
        )

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
        return min(latest_datetimes) + timedelta(hours=1)

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
        return min(latest_datetimes) + timedelta(days=1)

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

    def _build_combined_statistics(
        self,
        points: list[tuple[datetime, float]],
        last_start_ts: float | None,
        running_sum: float,
    ) -> list[StatisticData]:
        """Build recorder statistics from mixed accumulated/interval input points."""
        statistics: list[StatisticData] = []
        for point_start, point_value in sorted(points):
            point_start_ts = point_start.timestamp()
            if last_start_ts is not None and point_start_ts <= last_start_ts:
                continue

            running_sum += point_value
            statistics.append(
                StatisticData(
                    start=point_start,
                    state=point_value,
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

    def _combined_statistic_metadata(
        self,
        nmi: str,
        statistic_id: str,
    ) -> StatisticMetaData:
        """Build recorder metadata for combined import streams."""
        masked_nmi = mask_identifier(nmi)
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{STATISTIC_NAME_PREFIX} Combined Import {masked_nmi}",
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

    def _combined_statistic_id(self, nmi: str) -> str:
        """Create a privacy-safe deterministic statistic id for combined imports."""
        digest = sha256(nmi.encode("utf-8")).hexdigest()[:12]
        return f"{DOMAIN}:{digest}_combined_import"

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
