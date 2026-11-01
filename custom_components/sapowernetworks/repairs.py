"""Repairs flows for SA Power Networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, LOGGER
from .coordinator import SAPowerNetworksDataUpdateCoordinator
from .privacy import redact_text
from .statistics_migration import (
    async_complete_statistics_migration,
    entry_shares_statistics_digest,
)

if TYPE_CHECKING:
    import asyncio

    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult


def find_statistics_coordinator(
    hass: HomeAssistant,
    digest: str,
) -> SAPowerNetworksDataUpdateCoordinator | None:
    """Return a loaded coordinator for a config entry tracking the digest."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        if not isinstance(coordinator, SAPowerNetworksDataUpdateCoordinator):
            continue
        if entry_shares_statistics_digest(entry, digest):
            return coordinator
    return None


class LegacyStatisticsRepairFlow(RepairsFlow):
    """Let the user rebuild or keep legacy statistics for one NMI."""

    def __init__(self, digest: str) -> None:
        """Initialize the flow."""
        self._digest = digest
        self._rebuild_task: asyncio.Task[Any] | None = None
        self._errors: dict[str, str] = {}
        self._failure_transition = False

    def _placeholders(self) -> dict[str, str]:
        """Return description placeholders copied from the issue."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        if issue is None or not issue.translation_placeholders:
            return {"nmi": ""}
        return dict(issue.translation_placeholders)

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        """Offer rebuild or keep options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["rebuild", "keep"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_rebuild(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Confirm, then clear and rebuild statistics in the background."""
        if self._rebuild_task is None:
            if user_input is None:
                return self.async_show_form(
                    step_id="rebuild",
                    data_schema=vol.Schema({}),
                    errors=self._errors,
                    description_placeholders=self._placeholders(),
                )
            coordinator = find_statistics_coordinator(self.hass, self._digest)
            if coordinator is None:
                LOGGER.error("No loaded config entry can rebuild these statistics")
                return self.async_show_form(
                    step_id="rebuild",
                    data_schema=vol.Schema({}),
                    errors={"base": "rebuild_failed"},
                    description_placeholders=self._placeholders(),
                )
            self._rebuild_task = self.hass.async_create_task(
                coordinator.async_rebuild_statistics(self._digest),
                f"{DOMAIN} statistics rebuild",
            )

        if not self._rebuild_task.done():
            return self.async_show_progress(
                step_id="rebuild",
                progress_action="rebuild",
                progress_task=self._rebuild_task,
                description_placeholders=self._placeholders(),
            )

        task, self._rebuild_task = self._rebuild_task, None
        try:
            task.result()
        except Exception as exception:  # noqa: BLE001
            message = redact_text(str(exception)) or type(exception).__name__
            LOGGER.error(
                "Statistics rebuild failed; stored statistics kept: %s", message
            )
            self._errors = {"base": "rebuild_failed"}
            self._failure_transition = True
            return self.async_show_progress_done(next_step_id="rebuild_failed")

        self._errors = {}
        return self.async_show_progress_done(next_step_id="rebuild_complete")

    async def async_step_rebuild_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Show the failure and allow the user to retry explicitly."""
        if self._failure_transition:
            self._failure_transition = False
            user_input = None
        if user_input is not None:
            return await self.async_step_rebuild(user_input)
        return self.async_show_form(
            step_id="rebuild_failed",
            data_schema=vol.Schema({}),
            errors=self._errors,
            description_placeholders=self._placeholders(),
        )

    async def async_step_rebuild_complete(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        """Finish the flow after a successful rebuild."""
        return self.async_create_entry(data={})

    async def async_step_keep(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Confirm keeping existing statistics and resume imports."""
        if user_input is None:
            return self.async_show_form(
                step_id="keep",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(),
            )
        async_complete_statistics_migration(self.hass, self._digest)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001
    issue_id: str,  # noqa: ARG001
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a fix flow for a legacy statistics issue."""
    digest = str((data or {}).get("digest", ""))
    return LegacyStatisticsRepairFlow(digest)
