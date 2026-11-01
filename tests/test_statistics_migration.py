"""Tests for the legacy statistics reset/rebuild migration."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapowernetworks import async_migrate_entry
from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClientCommunicationError,
)
from custom_components.sapowernetworks.config_flow import SAPowerNetworksConfigFlow
from custom_components.sapowernetworks.const import (
    COMBINED_BACKFILL_ATTEMPTS,
    DOMAIN,
    INITIAL_BACKFILL_START,
    STATISTICS_MIGRATION,
    STATISTICS_MIGRATION_CHECK,
    STATISTICS_MIGRATION_COMPLETED_DIGESTS,
    STATISTICS_MIGRATION_DONE,
    STATISTICS_MIGRATION_PENDING,
    STATISTICS_MIGRATION_PENDING_DIGESTS,
)
from custom_components.sapowernetworks.coordinator import (
    SAPowerNetworksDataUpdateCoordinator,
    StatisticsRebuildError,
)
from custom_components.sapowernetworks.statistics_migration import (
    async_create_legacy_statistics_issue,
    get_statistics_rebuild_lock,
    legacy_statistics_issue_id,
)

from .migration_helpers import (
    COORDINATOR_MODULE,
    DIGEST,
    DIGEST2,
    MASKED_NMI,
    NMI,
    NMI2,
    FakeRecorder,
    assignment,
    detailed_csv,
    make_client,
    make_entry,
    summary_csv,
)

pytestmark = pytest.mark.asyncio

INTERVAL_ID = f"{DOMAIN}:{DIGEST}_import_e1"
ACCUMULATED_ID = f"{DOMAIN}:{DIGEST}_accumulated_import"
COMBINED_ID = f"{DOMAIN}:{DIGEST}_combined_import"
OTHER_INTERVAL_ID = f"{DOMAIN}:{DIGEST2}_import_e1"
LEGACY_START = 2.0e9
LEGACY_SUM = 9999.0


def _issue(hass: Any, digest: str = DIGEST) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, legacy_statistics_issue_id(digest)
    )


def _pending_entry(
    hass: Any,
    *,
    unique_id: str = "legacy-user",
    pending: list[str] | None = None,
    **extra: Any,
) -> MockConfigEntry:
    entry = make_entry(
        STATISTICS_MIGRATION_PENDING,
        unique_id=unique_id,
        pending=pending if pending is not None else [DIGEST],
        **extra,
    )
    entry.add_to_hass(hass)
    return entry


# --- async_migrate_entry / config flow -------------------------------------------


async def test_migrate_entry_marks_legacy_entries_for_check(hass) -> None:
    """Version 1.1 entries move to 1.2, keep their data and need a check."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            COMBINED_BACKFILL_ATTEMPTS: ["abc"],
        },
    )
    entry.add_to_hass(hass)
    assert entry.minor_version == 1

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 1
    assert entry.minor_version == 2
    assert entry.data == {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
        COMBINED_BACKFILL_ATTEMPTS: ["abc"],
        STATISTICS_MIGRATION: STATISTICS_MIGRATION_CHECK,
    }


@pytest.mark.parametrize(
    "state",
    [STATISTICS_MIGRATION_DONE, STATISTICS_MIGRATION_PENDING],
)
async def test_migrate_entry_leaves_current_entries_untouched(hass, state) -> None:
    """Entries already on 1.2 keep their migration state."""
    entry = make_entry(state, pending=[DIGEST])
    entry.add_to_hass(hass)
    original = dict(entry.data)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 2
    assert dict(entry.data) == original


async def test_migrate_entry_rejects_future_major_version(hass) -> None:
    """Downgrades from a newer major version are refused."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={})
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)


async def test_config_flow_creates_migrated_entries(hass, monkeypatch) -> None:
    """New entries never need the legacy statistics migration."""

    async def _pass_credentials(_self: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        SAPowerNetworksConfigFlow, "_test_credentials", _pass_credentials
    )
    monkeypatch.setattr(
        "custom_components.sapowernetworks.async_setup_entry",
        AsyncMock(return_value=True),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_USERNAME: "new@example.com", CONF_PASSWORD: "secret"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.version == 1
    assert entry.minor_version == 2
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert entry.data[CONF_USERNAME] == "new@example.com"
    assert SAPowerNetworksConfigFlow.MINOR_VERSION == 2


# --- Detection --------------------------------------------------------------------


async def test_check_without_existing_statistics_marks_done(hass, monkeypatch) -> None:
    """Entries without stored statistics for their NMIs skip the migration."""
    entry = make_entry()
    entry.add_to_hass(hass)
    client = make_client(detailed={NMI: detailed_csv()})
    recorder = FakeRecorder({OTHER_INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(
        monkeypatch
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator._async_update_data()

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == []
    assert _issue(hass) is None
    assert result["statistics_import_paused"] is False
    assert INTERVAL_ID in recorder.imported_ids()


async def test_check_with_existing_statistics_pauses_and_raises_issue(
    hass, monkeypatch
) -> None:
    """Existing statistics pause imports and raise a privacy-safe Repairs issue."""
    entry = make_entry()
    entry.add_to_hass(hass)
    client = make_client(detailed={NMI: detailed_csv()})
    recorder = FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(
        monkeypatch
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator._async_update_data()

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == [DIGEST]
    assert result["statistics_import_paused"] is True
    assert result["statistics_paused_nmi_count"] == 1
    client.download_detailed_csv.assert_not_awaited()
    client.download_accumulated_summary_csv.assert_not_awaited()
    assert recorder.imported == []

    issue = _issue(hass)
    assert issue is not None
    assert issue.issue_id == f"legacy_statistics_{DIGEST}"
    assert issue.is_fixable
    assert issue.translation_key == "legacy_statistics"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.data == {"digest": DIGEST}
    assert issue.translation_placeholders == {"nmi": MASKED_NMI}
    assert NMI not in repr(issue)


async def test_pending_issue_recreated_by_new_coordinator(hass, monkeypatch) -> None:
    """A restart re-raises the Repairs issue while the entry is still pending."""
    entry = _pending_entry(hass)
    FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(monkeypatch)
    assert _issue(hass) is None

    client = make_client(detailed={NMI: detailed_csv()})
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)
    await coordinator._async_update_data()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders == {"nmi": MASKED_NMI}
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    client.download_detailed_csv.assert_not_awaited()


async def test_check_without_portal_nmis_stays_check(hass, monkeypatch) -> None:
    """Without NMIs the check cannot run yet, so the state is kept."""
    entry = make_entry()
    entry.add_to_hass(hass)
    FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, make_client([]))

    await coordinator._async_update_data()

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_CHECK
    assert STATISTICS_MIGRATION_PENDING_DIGESTS not in entry.data
    assert _issue(hass) is None


async def test_done_entry_with_existing_statistics_is_not_rechecked(
    hass, monkeypatch
) -> None:
    """Resolved entries keep importing incrementally on top of stored statistics."""
    entry = make_entry(STATISTICS_MIGRATION_DONE)
    entry.add_to_hass(hass)
    recorder = FakeRecorder(
        {INTERVAL_ID: (INITIAL_BACKFILL_START.timestamp(), 10.0)}
    ).install(monkeypatch)
    client = make_client(detailed={NMI: detailed_csv()})
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator._async_update_data()

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert _issue(hass) is None
    assert result["statistics_import_paused"] is False
    assert recorder.rows_for(INTERVAL_ID)[0]["sum"] == 12.5


# --- Pause ------------------------------------------------------------------------


async def test_pending_nmi_paused_while_other_nmi_imports(hass, monkeypatch) -> None:
    """Only the NMI with legacy statistics is paused within one entry."""
    entry = make_entry()
    entry.add_to_hass(hass)
    client = make_client(
        [NMI, NMI2],
        detailed={NMI: detailed_csv(NMI), NMI2: detailed_csv(NMI2)},
    )
    recorder = FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(
        monkeypatch
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator._async_update_data()

    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == [DIGEST]
    assert result["statistics_paused_nmi_count"] == 1
    assert result["nmis"] == [NMI, NMI2]
    downloaded = {call.args[0] for call in client.download_detailed_csv.await_args_list}
    assert downloaded == {NMI2}
    assert recorder.imported
    assert all(
        sid.startswith(f"{DOMAIN}:{DIGEST2}_") for sid in recorder.imported_ids()
    )
    assert _issue(hass) is not None
    assert _issue(hass, DIGEST2) is None


async def test_pending_digest_on_other_entry_pauses_imports(hass, monkeypatch) -> None:
    """A different entry pending on the same NMI pauses this entry's imports too."""
    entry = make_entry(STATISTICS_MIGRATION_DONE, unique_id="first")
    entry.add_to_hass(hass)
    _pending_entry(hass, unique_id="second")
    client = make_client(
        [NMI, NMI2],
        detailed={NMI: detailed_csv(NMI), NMI2: detailed_csv(NMI2)},
    )
    recorder = FakeRecorder().install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator._async_update_data()

    assert result["statistics_import_paused"] is True
    downloaded = {call.args[0] for call in client.download_detailed_csv.await_args_list}
    assert downloaded == {NMI2}
    assert not any(
        sid.startswith(f"{DOMAIN}:{DIGEST}_") for sid in recorder.imported_ids()
    )


async def test_locked_digest_is_skipped_and_locks_released(hass, monkeypatch) -> None:
    """A digest locked by a rebuild is skipped; refresh releases its own locks."""
    entry = make_entry(STATISTICS_MIGRATION_DONE)
    entry.add_to_hass(hass)
    client = make_client(
        [NMI, NMI2],
        detailed={NMI: detailed_csv(NMI), NMI2: detailed_csv(NMI2)},
    )
    recorder = FakeRecorder().install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)
    lock = get_statistics_rebuild_lock(hass, DIGEST)
    await lock.acquire()
    try:
        result = await coordinator._async_update_data()
    finally:
        lock.release()

    assert result["statistics_paused_nmi_count"] == 1
    downloaded = {call.args[0] for call in client.download_detailed_csv.await_args_list}
    assert downloaded == {NMI2}
    assert not any(
        sid.startswith(f"{DOMAIN}:{DIGEST}_") for sid in recorder.imported_ids()
    )
    assert not get_statistics_rebuild_lock(hass, DIGEST2).locked()


async def test_sync_failure_releases_digest_locks(hass, monkeypatch) -> None:
    """Digest locks are released when a refresh fails mid-import."""
    entry = make_entry(STATISTICS_MIGRATION_DONE)
    entry.add_to_hass(hass)
    client = make_client()
    client.download_detailed_csv.side_effect = (
        SAPowerNetworksApiClientCommunicationError("boom")
    )
    FakeRecorder().install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert not get_statistics_rebuild_lock(hass, DIGEST).locked()
    assert not coordinator._portal_lock.locked()


# --- Rebuild ----------------------------------------------------------------------


async def test_rebuild_clears_then_reimports_all_streams_from_zero(
    hass, monkeypatch
) -> None:
    """Rebuild waits for the threaded clear, then writes sums starting at zero."""
    entry = _pending_entry(hass, **{COMBINED_BACKFILL_ATTEMPTS: ["stale"]})
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    client = make_client(detailed={NMI: detailed_csv()}, summary={NMI: summary_csv()})
    recorder = FakeRecorder(
        {
            INTERVAL_ID: (LEGACY_START, LEGACY_SUM),
            ACCUMULATED_ID: (LEGACY_START, LEGACY_SUM),
            COMBINED_ID: (LEGACY_START, LEGACY_SUM),
            OTHER_INTERVAL_ID: (LEGACY_START, LEGACY_SUM),
        }
    ).install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    result = await coordinator.async_rebuild_statistics(DIGEST)

    assert client.download_detailed_csv.await_args.args[:2] == (
        NMI,
        INITIAL_BACKFILL_START,
    )
    assert client.download_accumulated_summary_csv.await_args.args[:2] == (
        NMI,
        INITIAL_BACKFILL_START,
    )

    assert recorder.cleared == [sorted([ACCUMULATED_ID, COMBINED_ID, INTERVAL_ID])]
    assert OTHER_INTERVAL_ID in recorder.existing
    assert recorder.on_done_threads
    assert recorder.on_done_threads[0] is not threading.current_thread()
    first_add = next(
        index for index, event in enumerate(recorder.events) if event.startswith("add:")
    )
    assert recorder.events[0] == "clear_requested"
    assert recorder.events.index("clear_done") < first_add

    assert recorder.rows_for(INTERVAL_ID)[0]["sum"] == 2.5
    assert recorder.rows_for(ACCUMULATED_ID)[0]["sum"] == 837.0
    combined_rows = recorder.rows_for(COMBINED_ID)
    assert combined_rows[0]["sum"] == 837.0
    assert combined_rows[-1]["sum"] == 1859.5
    assert all(
        row["sum"] < LEGACY_SUM for _meta, rows in recorder.imported for row in rows
    )

    assert result.digest == DIGEST
    assert result.interval_statistic_ids == (INTERVAL_ID,)
    assert ACCUMULATED_ID in result.accumulated_statistic_ids
    assert result.combined_statistic_ids == (COMBINED_ID,)
    assert result.rows_imported == sum(len(rows) for _m, rows in recorder.imported)

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == []
    assert COMBINED_BACKFILL_ATTEMPTS not in entry.data
    assert _issue(hass) is None
    assert not get_statistics_rebuild_lock(hass, DIGEST).locked()
    assert not coordinator._portal_lock.locked()


async def test_rebuild_resolves_every_entry_sharing_the_digest(
    hass, monkeypatch
) -> None:
    """All entries tracking the digest are resolved; unrelated entries are not."""
    entry = _pending_entry(hass, unique_id="first")
    other_pending = _pending_entry(
        hass,
        unique_id="second",
        pending=[DIGEST, DIGEST2],
        **{COMBINED_BACKFILL_ATTEMPTS: ["x"]},
    )
    loaded_sharer = make_entry(
        STATISTICS_MIGRATION_DONE,
        unique_id="third",
        **{COMBINED_BACKFILL_ATTEMPTS: ["y"]},
    )
    loaded_sharer.add_to_hass(hass)
    loaded_sharer.runtime_data = SimpleNamespace(data={"nmis": [NMI]})
    unrelated = make_entry(
        STATISTICS_MIGRATION_DONE,
        unique_id="fourth",
        **{COMBINED_BACKFILL_ATTEMPTS: ["z"]},
    )
    unrelated.add_to_hass(hass)
    unrelated.runtime_data = SimpleNamespace(data={"nmis": [NMI2]})
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    async_create_legacy_statistics_issue(hass, DIGEST2, NMI2)
    FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(monkeypatch)
    client = make_client(detailed={NMI: detailed_csv()})
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    await coordinator.async_rebuild_statistics(DIGEST)

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert other_pending.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert other_pending.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == [DIGEST2]
    assert COMBINED_BACKFILL_ATTEMPTS not in other_pending.data
    assert COMBINED_BACKFILL_ATTEMPTS not in loaded_sharer.data
    assert COMBINED_BACKFILL_ATTEMPTS not in unrelated.data
    assert DIGEST in unrelated.data[STATISTICS_MIGRATION_COMPLETED_DIGESTS]
    assert _issue(hass) is None
    assert _issue(hass, DIGEST2) is not None


async def test_unloaded_entry_recognizes_a_completed_digest(hass, monkeypatch) -> None:
    """An entry loaded later will not flag already-rebuilt NMI statistics again."""
    entry = _pending_entry(hass, unique_id="first")
    later_entry = make_entry(unique_id="later")
    later_entry.add_to_hass(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass,
        entry,
        make_client(detailed={NMI: detailed_csv()}),
    )

    await coordinator.async_rebuild_statistics(DIGEST)
    assert DIGEST in later_entry.data[STATISTICS_MIGRATION_COMPLETED_DIGESTS]

    later_coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass,
        later_entry,
        make_client(detailed={NMI: detailed_csv()}),
    )
    await later_coordinator._async_update_data()

    assert later_entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert later_entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == []
    assert _issue(hass) is None


async def _assert_rebuild_left_pending(
    hass: Any,
    entry: MockConfigEntry,
    recorder: FakeRecorder,
    coordinator: SAPowerNetworksDataUpdateCoordinator,
) -> None:
    assert recorder.cleared == []
    assert recorder.imported == []
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == [DIGEST]
    assert _issue(hass) is not None
    assert not get_statistics_rebuild_lock(hass, DIGEST).locked()
    assert not coordinator._portal_lock.locked()


@pytest.mark.parametrize(
    ("existing_ids", "detailed", "summary", "match"),
    [
        ([INTERVAL_ID], "", "", "no replacement data for stored interval"),
        (
            [INTERVAL_ID, ACCUMULATED_ID],
            detailed_csv(),
            "",
            "no replacement data for stored accumulated",
        ),
        (
            [INTERVAL_ID, COMBINED_ID],
            detailed_csv(),
            "",
            "no replacement data for stored combined",
        ),
        ([], "", "", "contains no statistics"),
    ],
)
async def test_rebuild_without_full_replacement_clears_nothing(  # noqa: PLR0913, PLR0917
    hass, monkeypatch, existing_ids, detailed, summary, match
) -> None:
    """Validation failures abort before anything is cleared."""
    entry = _pending_entry(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    client = make_client(detailed={NMI: detailed}, summary={NMI: summary})
    recorder = FakeRecorder(
        dict.fromkeys(existing_ids, (LEGACY_START, LEGACY_SUM))
    ).install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    with pytest.raises(StatisticsRebuildError, match=match):
        await coordinator.async_rebuild_statistics(DIGEST)

    await _assert_rebuild_left_pending(hass, entry, recorder, coordinator)


async def test_rebuild_download_failure_clears_nothing(hass, monkeypatch) -> None:
    """Portal errors during the full download leave stored statistics intact."""
    entry = _pending_entry(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    client = make_client()
    client.download_detailed_csv.side_effect = (
        SAPowerNetworksApiClientCommunicationError("portal down")
    )
    recorder = FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(
        monkeypatch
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    with pytest.raises(SAPowerNetworksApiClientCommunicationError):
        await coordinator.async_rebuild_statistics(DIGEST)

    await _assert_rebuild_left_pending(hass, entry, recorder, coordinator)


async def test_rebuild_for_unassigned_nmi_fails(hass, monkeypatch) -> None:
    """A digest no longer assigned to the account cannot be rebuilt."""
    entry = _pending_entry(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    recorder = FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(
        monkeypatch
    )
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, make_client([NMI2]))

    with pytest.raises(StatisticsRebuildError, match="not assigned"):
        await coordinator.async_rebuild_statistics(DIGEST)

    await _assert_rebuild_left_pending(hass, entry, recorder, coordinator)


async def test_rebuild_clear_timeout_writes_nothing(hass, monkeypatch) -> None:
    """If the recorder never confirms the clear, nothing is written."""
    entry = _pending_entry(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    monkeypatch.setattr(f"{COORDINATOR_MODULE}.STATISTICS_CLEAR_TIMEOUT_SECONDS", 0.05)
    recorder = FakeRecorder(
        {INTERVAL_ID: (LEGACY_START, LEGACY_SUM)},
        clear_delay=None,
    ).install(monkeypatch)
    coordinator = SAPowerNetworksDataUpdateCoordinator(
        hass, entry, make_client(detailed={NMI: detailed_csv()})
    )

    with pytest.raises(StatisticsRebuildError, match="Timed out"):
        await coordinator.async_rebuild_statistics(DIGEST)

    assert recorder.cleared == [[INTERVAL_ID]]
    assert recorder.imported == []
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert _issue(hass) is not None
    assert not get_statistics_rebuild_lock(hass, DIGEST).locked()


async def test_refresh_waits_for_rebuild_portal_session(hass, monkeypatch) -> None:
    """A refresh cannot call the portal while a rebuild is mid-download."""
    entry = _pending_entry(hass)
    FakeRecorder({INTERVAL_ID: (LEGACY_START, LEGACY_SUM)}).install(monkeypatch)
    client = make_client()
    calls: list[str] = []
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def _assignments() -> list[Any]:
        calls.append("get_nmi_assignments")
        return [assignment()]

    async def _detailed(nmi: str, *_args: Any) -> str:
        calls.append("download_detailed_csv")
        if not download_started.is_set():
            download_started.set()
            await release_download.wait()
            calls.append("rebuild_download_released")
        return detailed_csv(nmi)

    client.get_nmi_assignments.side_effect = _assignments
    client.download_detailed_csv.side_effect = _detailed
    coordinator = SAPowerNetworksDataUpdateCoordinator(hass, entry, client)

    rebuild_task = asyncio.create_task(coordinator.async_rebuild_statistics(DIGEST))
    await download_started.wait()
    refresh_task = asyncio.create_task(coordinator._async_update_data())
    for _ in range(10):
        await asyncio.sleep(0)

    assert not refresh_task.done()
    assert calls == ["get_nmi_assignments", "download_detailed_csv"]

    release_download.set()
    await rebuild_task
    refresh_result = await refresh_task

    assert calls[:4] == [
        "get_nmi_assignments",
        "download_detailed_csv",
        "rebuild_download_released",
        "get_nmi_assignments",
    ]
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert refresh_result["statistics_import_paused"] is False
    assert not coordinator._portal_lock.locked()
