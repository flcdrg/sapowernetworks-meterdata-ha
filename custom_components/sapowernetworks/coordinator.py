"""DataUpdateCoordinator for SA Power Networks."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_utc_time_change,
)
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
    COMBINED_BACKFILL_ATTEMPTS,
    DOMAIN,
    INITIAL_BACKFILL_START,
    LOCAL_REFRESH_TIME,
    LOGGER,
    STATISTIC_NAME_PREFIX,
    STATISTICS_MIGRATION,
    STATISTICS_MIGRATION_CHECK,
    STATISTICS_MIGRATION_DONE,
    STATISTICS_MIGRATION_PENDING,
    STATISTICS_MIGRATION_PENDING_DIGESTS,
    UTC_REFRESH_TIME,
)
from .parsing import (
    NEM12_TIMEZONE,
    IntervalReading,
    SummaryPeriod,
    UnsupportedEnergyUnitError,
    parse_nem12_content,
    parse_summary_csv,
)
from .privacy import mask_identifier
from .statistics_migration import (
    async_complete_statistics_migration,
    async_create_legacy_statistics_issue,
    completed_statistics_digests,
    entry_pending_statistics_digests,
    entry_statistics_migration_state,
    get_statistics_rebuild_lock,
    pending_statistics_digests,
    statistic_digest,
    statistic_digest_prefix,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


PreparedStatistics = tuple[StatisticMetaData, list[StatisticData]]

STATISTICS_CLEAR_TIMEOUT_SECONDS = 600


class StatisticsRebuildError(Exception):
    """Raised when a legacy statistics rebuild cannot run safely."""


@dataclass(frozen=True)
class StatisticsRebuildResult:
    """Result of rebuilding all statistics for one NMI digest."""

    digest: str
    cleared_statistic_ids: tuple[str, ...] = ()
    interval_statistic_ids: tuple[str, ...] = ()
    accumulated_statistic_ids: tuple[str, ...] = ()
    combined_statistic_ids: tuple[str, ...] = ()
    rows_imported: int = 0


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
            update_interval=None,
        )
        self.config_entry = config_entry
        self.client = client
        # Serializes portal sessions: the client shares cookies and form view state.
        self._portal_lock = asyncio.Lock()
        self._scheduled_refresh_unsubscribers: list[Callable[[], None]] = []

    def async_setup_scheduled_refreshes(self) -> None:
        """Schedule automatic refreshes at fixed local and UTC times."""
        if self._scheduled_refresh_unsubscribers:
            return

        self._scheduled_refresh_unsubscribers.append(
            async_track_time_change(
                self.hass,
                self._async_handle_scheduled_refresh,
                hour=LOCAL_REFRESH_TIME.hour,
                minute=LOCAL_REFRESH_TIME.minute,
                second=0,
            )
        )
        self._scheduled_refresh_unsubscribers.append(
            async_track_utc_time_change(
                self.hass,
                self._async_handle_scheduled_refresh,
                hour=UTC_REFRESH_TIME.hour,
                minute=UTC_REFRESH_TIME.minute,
                second=0,
            )
        )
        LOGGER.debug(
            "Scheduled refresh triggers configured for local %s and UTC %s",
            LOCAL_REFRESH_TIME.isoformat(timespec="minutes"),
            UTC_REFRESH_TIME.isoformat(timespec="minutes"),
        )

    def async_unload(self) -> None:
        """Unsubscribe automatic refresh callbacks."""
        while self._scheduled_refresh_unsubscribers:
            unsubscribe = self._scheduled_refresh_unsubscribers.pop()
            unsubscribe()

    async def _async_handle_scheduled_refresh(self, now: datetime) -> None:
        """Run one scheduled refresh."""
        LOGGER.debug("Running scheduled refresh at %s", now.isoformat())
        await self.async_request_refresh()

    async def async_rebuild_statistics(self, digest: str) -> StatisticsRebuildResult:
        """Clear and rebuild every statistic stream for one NMI digest."""
        async with get_statistics_rebuild_lock(self.hass, digest), self._portal_lock:
            result = await self._async_rebuild_statistics_locked(digest)
            async_complete_statistics_migration(
                self.hass,
                digest,
                reset_backfill_attempts=True,
            )
        return result

    async def _async_rebuild_statistics_locked(
        self,
        digest: str,
    ) -> StatisticsRebuildResult:
        """Download full history, validate it, then replace stored statistics."""
        assignments = await self.client.get_nmi_assignments()
        target = next(
            (item for item in assignments if statistic_digest(item.nmi) == digest),
            None,
        )
        if target is None:
            msg = "The NMI for this statistics rebuild is not assigned to the account"
            raise StatisticsRebuildError(msg)

        masked_nmi = mask_identifier(target.nmi)
        prefix = statistic_digest_prefix(digest)
        existing_ids = sorted(
            statistic_id
            for statistic_id in await self._async_existing_statistic_ids()
            if statistic_id.startswith(prefix)
        )
        fetch_end = datetime.now(tz=UTC)
        LOGGER.info(
            "Rebuilding statistics for %s from %s",
            masked_nmi,
            INITIAL_BACKFILL_START.date().isoformat(),
        )

        has_interval_history = bool(
            self._interval_statistic_ids(target.nmi, set(existing_ids))
        ) or (self._combined_statistic_id(target.nmi) in existing_ids)
        is_accumulated_meter = (
            target.meter_type_description or ""
        ).strip().casefold() == "accumulated"
        interval_parsed: dict[tuple[str, str], list[IntervalReading]] = {}
        if has_interval_history or not is_accumulated_meter:
            raw_csv = await self.client.download_detailed_csv(
                target.nmi,
                INITIAL_BACKFILL_START,
                fetch_end,
            )
            interval_parsed = {
                key: readings
                for key, readings in parse_nem12_content(raw_csv).items()
                if statistic_digest(key[0]) == digest
            }
        raw_csv = await self.client.download_accumulated_summary_csv(
            target.nmi,
            INITIAL_BACKFILL_START,
            fetch_end,
        )
        periods = [
            period
            for period in parse_summary_csv(raw_csv)
            if statistic_digest(period.nmi) == digest
        ]

        pair = await self._async_rebuild_combined_pair(
            digest,
            assignments,
            interval_parsed,
            periods,
            fetch_end,
        )
        interval_prepared = await self._async_prepare_interval_statistics(
            interval_parsed,
            from_scratch=True,
        )
        accumulated_prepared = await self._async_prepare_accumulated_statistics(
            periods,
            from_scratch=True,
        )
        combined_prepared: list[PreparedStatistics] = []
        if pair is not None:
            prepared = await self._async_prepare_combined_statistics(
                pair["combined_nmi"],
                pair["accumulated_points"],
                pair["interval_points"],
                from_scratch=True,
            )
            if prepared is not None:
                combined_prepared.append(prepared)

        self._validate_rebuild_replacement(
            digest,
            existing_ids,
            interval_prepared,
            accumulated_prepared,
            combined_prepared,
        )

        if existing_ids:
            await self._async_clear_statistics(existing_ids)
        interval_result = self._write_statistics(interval_prepared)
        accumulated_result = self._write_statistics(accumulated_prepared)
        combined_result = self._write_statistics(combined_prepared)
        rows_imported = (
            interval_result.rows_imported
            + accumulated_result.rows_imported
            + combined_result.rows_imported
        )
        LOGGER.info(
            "Rebuilt statistics for %s: cleared=%s rows=%s",
            masked_nmi,
            len(existing_ids),
            rows_imported,
        )
        return StatisticsRebuildResult(
            digest=digest,
            cleared_statistic_ids=tuple(existing_ids),
            interval_statistic_ids=interval_result.statistic_ids,
            accumulated_statistic_ids=accumulated_result.statistic_ids,
            combined_statistic_ids=combined_result.statistic_ids,
            rows_imported=rows_imported,
        )

    async def _async_rebuild_combined_pair(
        self,
        digest: str,
        assignments: list[NmiAssignment],
        interval_parsed: dict[tuple[str, str], list[IntervalReading]],
        periods: list[SummaryPeriod],
        fetch_end: datetime,
    ) -> CombinedStreamPair | None:
        """Find the accumulated stream that continues into this digest's intervals."""
        interval_points_by_nmi = {
            nmi: list(points)
            for nmi, points in self._interval_import_points_by_nmi(
                interval_parsed
            ).items()
        }
        if not interval_points_by_nmi:
            return None
        accumulated_points_by_nmi = {
            nmi: list(points)
            for nmi, points in self._summary_import_points_by_nmi(periods).items()
        }

        def _select() -> CombinedStreamPair | None:
            return next(
                (
                    pair
                    for pair in self._select_consecutive_stream_pairs(
                        accumulated_points_by_nmi,
                        interval_points_by_nmi,
                    )
                    if statistic_digest(pair["combined_nmi"]) == digest
                ),
                None,
            )

        pair = _select()
        other_assignments = [
            item for item in assignments if statistic_digest(item.nmi) != digest
        ]
        if pair is not None or not other_assignments:
            return pair

        for assignment in other_assignments:
            raw_csv = await self.client.download_accumulated_summary_csv(
                assignment.nmi,
                INITIAL_BACKFILL_START,
                fetch_end,
            )
            for nmi, points in self._summary_import_points_by_nmi(
                parse_summary_csv(raw_csv)
            ).items():
                if statistic_digest(nmi) != digest:
                    accumulated_points_by_nmi[nmi] = list(points)
        return _select()

    @staticmethod
    def _validate_rebuild_replacement(
        digest: str,
        existing_ids: list[str],
        interval_prepared: list[PreparedStatistics],
        accumulated_prepared: list[PreparedStatistics],
        combined_prepared: list[PreparedStatistics],
    ) -> None:
        """Refuse to clear stored statistics that cannot be fully replaced."""
        prefix = statistic_digest_prefix(digest)
        stream_types = (
            (
                "interval",
                tuple(f"{prefix}{item}_" for item in ("import", "export", "channel")),
                interval_prepared,
            ),
            ("accumulated", (f"{prefix}accumulated_",), accumulated_prepared),
            ("combined", (f"{prefix}combined_import",), combined_prepared),
        )
        missing = [
            name
            for name, prefixes, prepared in stream_types
            if any(statistic_id.startswith(prefixes) for statistic_id in existing_ids)
            and not prepared
        ]
        if missing:
            msg = (
                "Downloaded history has no replacement data for stored "
                f"{', '.join(missing)} statistics; nothing was cleared"
            )
            raise StatisticsRebuildError(msg)
        if not (interval_prepared or accumulated_prepared or combined_prepared):
            msg = "Downloaded history contains no statistics; nothing was cleared"
            raise StatisticsRebuildError(msg)

    async def _async_clear_statistics(self, statistic_ids: list[str]) -> None:
        """Clear recorder statistics and wait for the recorder to finish."""
        loop = self.hass.loop
        future: asyncio.Future[None] = loop.create_future()

        def _resolve() -> None:
            if not future.done():
                future.set_result(None)

        def _on_done() -> None:
            # Called from the recorder thread.
            loop.call_soon_threadsafe(_resolve)

        get_instance(self.hass).async_clear_statistics(
            list(statistic_ids),
            on_done=_on_done,
        )
        try:
            async with asyncio.timeout(STATISTICS_CLEAR_TIMEOUT_SECONDS):
                await future
        except TimeoutError as exception:
            msg = "Timed out waiting for the recorder to clear statistics"
            raise StatisticsRebuildError(msg) from exception

    async def _async_update_data(self) -> Any:
        """Update data via library."""
        async with self._portal_lock:
            return await self._async_update_data_unlocked()

    async def _async_update_data_unlocked(self) -> Any:
        """Refresh portal data while the portal session is held."""
        try:
            LOGGER.debug("Coordinator refresh started")
            assignments = await self.client.get_nmi_assignments()
            LOGGER.debug(
                "Fetched NMI assignments", extra={"nmi_count": len(assignments)}
            )
            await self._async_check_legacy_statistics(assignments)
            active_assignments, held_locks = await self._async_acquire_sync_locks(
                assignments
            )
            try:
                import_stats = await self._async_sync_statistics(active_assignments)
            finally:
                for lock in held_locks:
                    lock.release()
            paused_nmis = [
                item.nmi for item in assignments if item not in active_assignments
            ]
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
                "statistics_import_paused": bool(paused_nmis),
                "statistics_paused_nmi_count": len(paused_nmis),
                "last_error": "",
                "last_sync": datetime.now(tz=UTC),
            }
        except SAPowerNetworksApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except UnsupportedEnergyUnitError as exception:
            raise UpdateFailed(exception) from exception
        except SAPowerNetworksApiClientError as exception:
            raise UpdateFailed(exception) from exception

    async def _async_check_legacy_statistics(
        self,
        assignments: list[NmiAssignment],
    ) -> None:
        """Detect legacy statistics once and keep pending Repairs issues raised."""
        entry = self.config_entry
        if (
            entry_statistics_migration_state(entry) == STATISTICS_MIGRATION_CHECK
            and assignments
        ):
            existing_statistic_ids = await self._async_existing_statistic_ids()
            completed_digests = completed_statistics_digests(self.hass)
            pending_digests = sorted(
                {
                    statistic_digest(assignment.nmi)
                    for assignment in assignments
                    if statistic_digest(assignment.nmi) not in completed_digests
                    if any(
                        statistic_id.startswith(self._statistic_prefix(assignment.nmi))
                        for statistic_id in existing_statistic_ids
                    )
                }
            )
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    STATISTICS_MIGRATION: (
                        STATISTICS_MIGRATION_PENDING
                        if pending_digests
                        else STATISTICS_MIGRATION_DONE
                    ),
                    STATISTICS_MIGRATION_PENDING_DIGESTS: pending_digests,
                },
            )
            if pending_digests:
                LOGGER.warning(
                    "Legacy statistics found for %s NMI(s); statistics import is "
                    "paused until the Repairs issue is resolved",
                    len(pending_digests),
                )

        pending_digests_for_entry = entry_pending_statistics_digests(entry)
        for assignment in assignments:
            digest = statistic_digest(assignment.nmi)
            if digest in pending_digests_for_entry:
                async_create_legacy_statistics_issue(self.hass, digest, assignment.nmi)

    async def _async_acquire_sync_locks(
        self,
        assignments: list[NmiAssignment],
    ) -> tuple[list[NmiAssignment], list[asyncio.Lock]]:
        """Return assignments allowed to import and the digest locks now held."""
        paused_digests = pending_statistics_digests(self.hass)
        held_digests: set[str] = set()
        held_locks: list[asyncio.Lock] = []
        active_assignments: list[NmiAssignment] = []
        for assignment in assignments:
            digest = statistic_digest(assignment.nmi)
            if digest in paused_digests:
                LOGGER.debug(
                    "Statistics import paused for %s pending legacy migration",
                    mask_identifier(assignment.nmi),
                )
                continue
            if digest not in held_digests:
                lock = get_statistics_rebuild_lock(self.hass, digest)
                if lock.locked():
                    LOGGER.debug(
                        "Statistics import skipped for %s while another task is "
                        "writing its statistics",
                        mask_identifier(assignment.nmi),
                    )
                    continue
                # An unlocked asyncio.Lock is acquired without yielding.
                await lock.acquire()
                held_digests.add(digest)
                held_locks.append(lock)
            active_assignments.append(assignment)
        return active_assignments, held_locks

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
                assignment,
                existing_statistic_ids,
            )
            if interval_fetch_start is not None and interval_fetch_start < fetch_end:
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

        backfill_candidates = await self._async_backfill_combined_source_history(
            assignments,
            existing_statistic_ids,
            interval_import_points_by_nmi,
            accumulated_import_points_by_nmi,
            fetch_end,
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

        unmatched_candidates = {
            candidate_key
            for nmi, candidate_key in backfill_candidates.items()
            if nmi not in paired_interval_nmis
        }
        if unmatched_candidates:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    COMBINED_BACKFILL_ATTEMPTS: sorted(
                        set(self.config_entry.data.get(COMBINED_BACKFILL_ATTEMPTS, []))
                        | unmatched_candidates
                    ),
                },
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

    def _interval_import_points_by_nmi(
        self,
        parsed: dict[tuple[str, str], list[IntervalReading]],
    ) -> dict[str, tuple[tuple[datetime, float], ...]]:
        """Aggregate detailed import channels into hourly points by NMI."""
        hourly_totals_by_nmi: dict[str, dict[datetime, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for (nmi, suffix), readings in parsed.items():
            if self._channel_direction(suffix) != "import":
                continue
            for hour_start, hour_value in self._complete_hourly_totals(
                readings
            ).items():
                hourly_totals_by_nmi[nmi][hour_start] += hour_value

        return {
            nmi: tuple(sorted(hourly_totals.items()))
            for nmi, hourly_totals in hourly_totals_by_nmi.items()
        }

    def _summary_import_points_by_nmi(
        self,
        periods: list[SummaryPeriod],
    ) -> dict[str, tuple[tuple[datetime, float], ...]]:
        """Convert summary import periods into timestamped points by NMI."""
        grouped_periods: dict[str, list[SummaryPeriod]] = defaultdict(list)
        for period in periods:
            grouped_periods[period.nmi].append(period)

        return {
            nmi: tuple(
                (
                    self._summary_period_timestamp(period.period_end),
                    period.import_kwh,
                )
                for period in sorted(
                    nmi_periods,
                    key=lambda item: (item.period_end, item.period_start),
                )
            )
            for nmi, nmi_periods in grouped_periods.items()
        }

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

        import_points_by_nmi = self._interval_import_points_by_nmi(parsed)
        result = self._write_statistics(
            await self._async_prepare_interval_statistics(parsed)
        )
        return ImportBatchResult(
            rows_imported=result.rows_imported,
            channels_imported=result.channels_imported,
            statistic_ids=result.statistic_ids,
            import_points_by_nmi=import_points_by_nmi,
            latest_interval_data_point=max(
                (
                    hour_start
                    for hourly_totals in import_points_by_nmi.values()
                    for hour_start, _value in hourly_totals
                ),
                default=None,
            ),
        )

    async def _async_statistic_checkpoint(
        self,
        statistic_id: str,
        *,
        from_scratch: bool,
    ) -> tuple[float | None, float]:
        """Return the import checkpoint, or an empty one when rebuilding."""
        if from_scratch:
            return None, 0.0
        return await self._async_last_statistic_snapshot(statistic_id)

    def _write_statistics(
        self,
        prepared: list[PreparedStatistics],
    ) -> ImportBatchResult:
        """Write prepared statistics batches to the recorder."""
        total_rows = 0
        statistic_ids: list[str] = []
        for metadata, statistics in prepared:
            async_add_external_statistics(self.hass, metadata, statistics)
            total_rows += len(statistics)
            statistic_ids.append(metadata["statistic_id"])
        return ImportBatchResult(
            rows_imported=total_rows,
            channels_imported=len(statistic_ids),
            statistic_ids=tuple(statistic_ids),
            import_points_by_nmi={},
        )

    async def _async_prepare_interval_statistics(
        self,
        parsed: dict[tuple[str, str], list[IntervalReading]],
        *,
        from_scratch: bool = False,
    ) -> list[PreparedStatistics]:
        """Build new hourly statistics for each parsed detailed channel."""
        prepared: list[PreparedStatistics] = []
        for (nmi, suffix), readings in parsed.items():
            hourly_totals = self._complete_hourly_totals(readings)

            statistic_id = self._statistic_id(nmi, suffix)
            last_start_ts, last_sum = await self._async_statistic_checkpoint(
                statistic_id,
                from_scratch=from_scratch,
            )
            statistics = self._build_statistics(
                hourly_totals,
                last_start_ts,
                last_sum,
            )
            if not statistics:
                continue

            prepared.append(
                (self._statistic_metadata(nmi, suffix, statistic_id), statistics)
            )
        return prepared

    async def _async_backfill_combined_source_history(
        self,
        assignments: list[NmiAssignment],
        existing_statistic_ids: set[str],
        interval_points_by_nmi: dict[str, list[tuple[datetime, float]]],
        accumulated_points_by_nmi: dict[str, list[tuple[datetime, float]]],
        fetch_end: datetime,
    ) -> dict[str, str]:
        """Load complete existing source history before first combined import."""
        interval_assignments = [
            assignment
            for assignment in assignments
            if any(
                statistic_id.startswith(
                    f"{self._statistic_prefix(assignment.nmi)}import_"
                )
                for statistic_id in existing_statistic_ids
            )
            and self._combined_statistic_id(assignment.nmi)
            not in existing_statistic_ids
        ]
        accumulated_assignments = [
            assignment
            for assignment in assignments
            if (
                self._summary_statistic_id(assignment.nmi, "accumulated_import")
                in existing_statistic_ids
            )
        ]
        if not interval_assignments or not accumulated_assignments:
            return {}

        accumulated_source_ids = {
            self._summary_statistic_id(assignment.nmi, "accumulated_import")
            for assignment in accumulated_assignments
        }
        candidate_keys: dict[str, str] = {}
        for assignment in interval_assignments:
            source_ids = accumulated_source_ids | {
                statistic_id
                for statistic_id in existing_statistic_ids
                if statistic_id.startswith(
                    f"{self._statistic_prefix(assignment.nmi)}import_"
                )
            }
            candidate_keys[assignment.nmi] = sha256(
                "|".join(sorted(source_ids)).encode("utf-8")
            ).hexdigest()
        previous_attempts = set(
            self.config_entry.data.get(COMBINED_BACKFILL_ATTEMPTS, [])
        )
        interval_assignments = [
            assignment
            for assignment in interval_assignments
            if candidate_keys[assignment.nmi] not in previous_attempts
        ]
        if not interval_assignments:
            LOGGER.debug("Skipping previously unmatched combined backfill candidates")
            return {}

        LOGGER.debug(
            "Backfilling existing source history to initialize combined streams"
        )
        for assignment in interval_assignments:
            raw_csv = await self.client.download_detailed_csv(
                assignment.nmi,
                INITIAL_BACKFILL_START,
                fetch_end,
            )
            for nmi, points in self._interval_import_points_by_nmi(
                parse_nem12_content(raw_csv)
            ).items():
                interval_points_by_nmi[nmi] = list(points)

        for assignment in accumulated_assignments:
            raw_csv = await self.client.download_accumulated_summary_csv(
                assignment.nmi,
                INITIAL_BACKFILL_START,
                fetch_end,
            )
            for nmi, points in self._summary_import_points_by_nmi(
                parse_summary_csv(raw_csv)
            ).items():
                accumulated_points_by_nmi[nmi] = list(points)

        return {
            assignment.nmi: candidate_keys[assignment.nmi]
            for assignment in interval_assignments
        }

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

        import_points_by_nmi = self._summary_import_points_by_nmi(periods)
        result = self._write_statistics(
            await self._async_prepare_accumulated_statistics(periods)
        )
        return ImportBatchResult(
            rows_imported=result.rows_imported,
            channels_imported=result.channels_imported,
            statistic_ids=result.statistic_ids,
            import_points_by_nmi=import_points_by_nmi,
        )

    async def _async_prepare_accumulated_statistics(
        self,
        periods: list[SummaryPeriod],
        *,
        from_scratch: bool = False,
    ) -> list[PreparedStatistics]:
        """Build new accumulated import/export statistics for parsed periods."""
        grouped_periods: dict[str, list[SummaryPeriod]] = {}
        for period in periods:
            grouped_periods.setdefault(period.nmi, []).append(period)

        prepared: list[PreparedStatistics] = []
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
                last_start_ts, last_sum = await self._async_statistic_checkpoint(
                    statistic_id,
                    from_scratch=from_scratch,
                )
                statistics = self._build_summary_statistics(
                    direction_periods,
                    direction,
                    last_start_ts,
                    last_sum,
                )
                if not statistics:
                    continue

                prepared.append(
                    (
                        self._summary_statistic_metadata(
                            nmi,
                            direction,
                            statistic_id,
                        ),
                        statistics,
                    )
                )
        return prepared

    async def _async_sync_combined_import_statistics(
        self,
        nmi: str,
        accumulated_import_points: tuple[tuple[datetime, float], ...],
        interval_import_points: tuple[tuple[datetime, float], ...],
    ) -> ImportBatchResult:
        """Import a combined stream when source streams are consecutive."""
        prepared = await self._async_prepare_combined_statistics(
            nmi,
            accumulated_import_points,
            interval_import_points,
        )
        if prepared is None:
            return ImportBatchResult(import_points_by_nmi={})
        return self._write_statistics([prepared])

    async def _async_prepare_combined_statistics(
        self,
        nmi: str,
        accumulated_import_points: tuple[tuple[datetime, float], ...],
        interval_import_points: tuple[tuple[datetime, float], ...],
        *,
        from_scratch: bool = False,
    ) -> PreparedStatistics | None:
        """Build new combined statistics when source streams are consecutive."""
        combined_points = self._combined_import_points(
            accumulated_import_points,
            interval_import_points,
        )
        if not combined_points:
            return None

        statistic_id = self._combined_statistic_id(nmi)
        last_start_ts, last_sum = await self._async_statistic_checkpoint(
            statistic_id,
            from_scratch=from_scratch,
        )
        statistics = self._build_combined_statistics(
            combined_points,
            last_start_ts,
            last_sum,
        )
        if not statistics:
            return None
        return self._combined_statistic_metadata(nmi, statistic_id), statistics

    @staticmethod
    def _combined_import_points(
        accumulated_import_points: tuple[tuple[datetime, float], ...],
        interval_import_points: tuple[tuple[datetime, float], ...],
    ) -> list[tuple[datetime, float]]:
        """Join consecutive accumulated and interval points into one timeline."""
        if not accumulated_import_points or not interval_import_points:
            return []

        sorted_accumulated = sorted(accumulated_import_points)
        sorted_interval = sorted(interval_import_points)

        accumulated_end = sorted_accumulated[-1][0]
        interval_start = sorted_interval[0][0]
        if interval_start < accumulated_end:
            return []
        if interval_start - accumulated_end > timedelta(days=1):
            return []

        combined_values_by_start: dict[datetime, float] = defaultdict(float)
        for point_start, point_value in sorted_accumulated:
            if point_start <= interval_start:
                combined_values_by_start[point_start] += point_value
        for point_start, point_value in sorted_interval:
            if point_start >= interval_start:
                combined_values_by_start[point_start] += point_value
        return sorted(combined_values_by_start.items())

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

        return self._write_statistics(
            [(self._combined_statistic_metadata(nmi, statistic_id), statistics)]
        )

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
        assignment: NmiAssignment,
        existing_statistic_ids: set[str],
    ) -> datetime | None:
        """Determine interval fetch start, or skip a known summary-only meter."""
        nmi = assignment.nmi
        combined_statistic_id = self._combined_statistic_id(nmi)
        matching_ids = self._interval_statistic_ids(nmi, existing_statistic_ids)
        if combined_statistic_id in existing_statistic_ids:
            matching_ids.add(combined_statistic_id)
        if not matching_ids:
            if (
                assignment.meter_type_description or ""
            ).strip().casefold() == "accumulated":
                LOGGER.debug(
                    "Skipping interval sync for accumulated-only meter %s",
                    mask_identifier(nmi),
                )
                return None
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

    def _interval_statistic_ids(
        self,
        nmi: str,
        existing_statistic_ids: set[str],
    ) -> set[str]:
        """Return existing detailed channel statistic ids for an NMI."""
        statistic_prefix = self._statistic_prefix(nmi)
        interval_prefixes = tuple(
            f"{statistic_prefix}{direction}_"
            for direction in ("import", "export", "channel")
        )
        return {
            statistic_id
            for statistic_id in existing_statistic_ids
            if statistic_id.startswith(interval_prefixes)
        }

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

    @staticmethod
    def _complete_hourly_totals(
        readings: list[IntervalReading],
    ) -> dict[datetime, float]:
        """Aggregate only hours fully covered by valid interval readings."""
        hourly_readings: dict[datetime, list[IntervalReading]] = defaultdict(list)
        for reading in readings:
            hour_start = reading.interval_start.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            hourly_readings[hour_start].append(reading)

        hourly_totals: dict[datetime, float] = {}
        for hour_start, hour_readings in sorted(hourly_readings.items()):
            hour_end = hour_start + timedelta(hours=1)
            coverage_end = hour_start
            hour_total = 0.0
            for reading in sorted(hour_readings, key=lambda item: item.interval_start):
                if (
                    reading.value_kwh is None
                    or reading.interval_start != coverage_end
                    or reading.interval_end > hour_end
                ):
                    break
                hour_total += reading.value_kwh
                coverage_end = reading.interval_end

            if coverage_end == hour_end:
                hourly_totals[hour_start] = hour_total

        return hourly_totals

    def _build_statistics(
        self,
        hourly_totals: dict[datetime, float],
        last_start_ts: float | None,
        running_sum: float,
    ) -> list[StatisticData]:
        """Build hourly recorder statistics, skipping data already present."""
        statistics: list[StatisticData] = []
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
            period_start = self._summary_period_timestamp(period.period_end)
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

    @staticmethod
    def _summary_period_timestamp(period_end: date) -> datetime:
        """Convert a summary period's AEST date boundary to UTC."""
        return datetime.combine(
            period_end,
            datetime.min.time(),
            tzinfo=NEM12_TIMEZONE,
        ).astimezone(UTC)

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
        digest = statistic_digest(nmi)
        channel = self._slugify(self._channel_direction(suffix))
        suffix_slug = self._slugify(suffix)
        return f"{DOMAIN}:{digest}_{channel}_{suffix_slug}"

    def _statistic_prefix(self, nmi: str) -> str:
        """Return the privacy-safe prefix shared by all statistic ids for an NMI."""
        return statistic_digest_prefix(statistic_digest(nmi))

    def _summary_statistic_id(self, nmi: str, direction: str) -> str:
        """Create a privacy-safe deterministic statistic id for accumulated data."""
        digest = statistic_digest(nmi)
        return f"{DOMAIN}:{digest}_{self._slugify(direction)}"

    def _combined_statistic_id(self, nmi: str) -> str:
        """Create a privacy-safe deterministic statistic id for combined imports."""
        digest = statistic_digest(nmi)
        return f"{DOMAIN}:{digest}_combined_import"

    def _summary_statistic_prefix(self, nmi: str) -> str:
        """Return the shared prefix used by accumulated summary statistic ids."""
        digest = statistic_digest(nmi)
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
