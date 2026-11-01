"""Shared state helpers for the legacy statistics reset/rebuild migration."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from .const import (
    COMBINED_BACKFILL_ATTEMPTS,
    DATA_STATISTICS_REBUILD_LOCKS,
    DOMAIN,
    LEGACY_STATISTICS_ISSUE_PREFIX,
    LEGACY_STATISTICS_TRANSLATION_KEY,
    STATISTICS_MIGRATION,
    STATISTICS_MIGRATION_CHECK,
    STATISTICS_MIGRATION_COMPLETED_DIGESTS,
    STATISTICS_MIGRATION_DONE,
    STATISTICS_MIGRATION_PENDING,
    STATISTICS_MIGRATION_PENDING_DIGESTS,
)
from .privacy import mask_identifier

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def statistic_digest(nmi: str) -> str:
    """Return the privacy-safe digest used in all statistic ids for an NMI."""
    return sha256(nmi.encode("utf-8")).hexdigest()[:12]


def statistic_digest_prefix(digest: str) -> str:
    """Return the statistic id prefix shared by all streams for one digest."""
    return f"{DOMAIN}:{digest}_"


def legacy_statistics_issue_id(digest: str) -> str:
    """Return the Repairs issue id for one NMI digest."""
    return f"{LEGACY_STATISTICS_ISSUE_PREFIX}{digest}"


def entry_statistics_migration_state(entry: ConfigEntry) -> str:
    """Return the statistics migration state for a config entry."""
    return str(entry.data.get(STATISTICS_MIGRATION, STATISTICS_MIGRATION_CHECK))


def entry_pending_statistics_digests(entry: ConfigEntry) -> set[str]:
    """Return digests whose statistics import is paused for a config entry."""
    if entry_statistics_migration_state(entry) != STATISTICS_MIGRATION_PENDING:
        return set()
    return {
        str(item) for item in entry.data.get(STATISTICS_MIGRATION_PENDING_DIGESTS, [])
    }


def pending_statistics_digests(hass: HomeAssistant) -> set[str]:
    """Return digests pending a user decision on any config entry."""
    pending: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        pending |= entry_pending_statistics_digests(entry)
    return pending


def completed_statistics_digests(hass: HomeAssistant) -> set[str]:
    """Return digests rebuilt or explicitly kept on any config entry."""
    return {
        str(digest)
        for entry in hass.config_entries.async_entries(DOMAIN)
        for digest in entry.data.get(STATISTICS_MIGRATION_COMPLETED_DIGESTS, [])
    }


def get_statistics_rebuild_lock(hass: HomeAssistant, digest: str) -> asyncio.Lock:
    """Return the shared lock guarding statistics writes for one digest."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[str, asyncio.Lock] = domain_data.setdefault(
        DATA_STATISTICS_REBUILD_LOCKS,
        {},
    )
    return locks.setdefault(digest, asyncio.Lock())


def async_create_legacy_statistics_issue(
    hass: HomeAssistant,
    digest: str,
    nmi: str,
) -> None:
    """Create (or refresh) the fixable Repairs issue for one NMI digest."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        legacy_statistics_issue_id(digest),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=LEGACY_STATISTICS_TRANSLATION_KEY,
        translation_placeholders={"nmi": mask_identifier(nmi)},
        data={"digest": digest},
    )


def entry_shares_statistics_digest(entry: ConfigEntry, digest: str) -> bool:
    """Return whether a config entry is known to track the given digest."""
    if digest in {
        str(item) for item in entry.data.get(STATISTICS_MIGRATION_PENDING_DIGESTS, [])
    }:
        return True
    coordinator = getattr(entry, "runtime_data", None)
    data = getattr(coordinator, "data", None)
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(nmi, str) and statistic_digest(nmi) == digest
        for nmi in data.get("nmis", [])
    )


def async_complete_statistics_migration(
    hass: HomeAssistant,
    digest: str,
    *,
    reset_backfill_attempts: bool = False,
) -> None:
    """Mark one digest resolved on every entry sharing it and delete its issue."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = dict(entry.data)
        completed = {
            str(item) for item in data.get(STATISTICS_MIGRATION_COMPLETED_DIGESTS, [])
        }
        completed.add(digest)
        data[STATISTICS_MIGRATION_COMPLETED_DIGESTS] = sorted(completed)

        shares_digest = entry_shares_statistics_digest(entry, digest)
        if shares_digest:
            pending = [
                str(item)
                for item in data.get(STATISTICS_MIGRATION_PENDING_DIGESTS, [])
                if str(item) != digest
            ]
            if STATISTICS_MIGRATION_PENDING_DIGESTS in data:
                data[STATISTICS_MIGRATION_PENDING_DIGESTS] = pending
            if (
                entry_statistics_migration_state(entry) == STATISTICS_MIGRATION_PENDING
                and not pending
            ):
                data[STATISTICS_MIGRATION] = STATISTICS_MIGRATION_DONE
        if reset_backfill_attempts:
            data.pop(COMBINED_BACKFILL_ATTEMPTS, None)
        if data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=data)

    ir.async_delete_issue(hass, DOMAIN, legacy_statistics_issue_id(digest))
