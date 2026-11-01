"""Tests for the legacy statistics Repairs flow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.sapowernetworks import repairs
from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClientCommunicationError,
)
from custom_components.sapowernetworks.const import (
    DOMAIN,
    STATISTICS_MIGRATION,
    STATISTICS_MIGRATION_DONE,
    STATISTICS_MIGRATION_PENDING,
    STATISTICS_MIGRATION_PENDING_DIGESTS,
)
from custom_components.sapowernetworks.repairs import (
    LegacyStatisticsRepairFlow,
    async_create_fix_flow,
    find_statistics_coordinator,
)
from custom_components.sapowernetworks.statistics_migration import (
    async_create_legacy_statistics_issue,
    legacy_statistics_issue_id,
)

from .migration_helpers import (
    DIGEST,
    MASKED_NMI,
    NMI,
    FakeRecorder,
    detailed_csv,
    make_client,
    make_entry,
)

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
INTERVAL_ID = f"{DOMAIN}:{DIGEST}_import_e1"
ISSUE_ID = legacy_statistics_issue_id(DIGEST)
LEGACY_SUM = 9999.0


async def _setup_legacy_entry(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncMock,
) -> tuple[MockConfigEntry, FakeRecorder]:
    """Load a 1.2 entry needing the check; first refresh raises the issue."""
    recorder = FakeRecorder({INTERVAL_ID: (2.0e9, LEGACY_SUM)}).install(monkeypatch)
    monkeypatch.setattr(
        "custom_components.sapowernetworks.SAPowerNetworksApiClient",
        Mock(return_value=client),
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert await async_setup_component(hass, "repairs", {})
    return entry, recorder


def _issue(hass: Any) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


async def _start_flow(hass: Any) -> dict[str, Any]:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": ISSUE_ID})


async def _configure(hass: Any, flow_id: str, user_input: Any = None) -> Any:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_configure(flow_id, user_input)


@pytest.mark.asyncio
async def test_setup_raises_issue_and_flow_shows_menu(hass, monkeypatch) -> None:
    """Loading a legacy entry raises the issue; the fix flow offers both options."""
    entry, recorder = await _setup_legacy_entry(hass, monkeypatch, make_client())

    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert recorder.imported == []
    issue = _issue(hass)
    assert issue is not None
    assert issue.is_fixable

    result = await _start_flow(hass)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["rebuild", "keep"]
    assert result["description_placeholders"] == {"nmi": MASKED_NMI}


@pytest.mark.asyncio
async def test_keep_resolves_issue_and_resumes_imports(hass, monkeypatch) -> None:
    """Keeping statistics resolves the entry and imports continue from the sum."""
    client = make_client(detailed={NMI: detailed_csv()})
    entry, recorder = await _setup_legacy_entry(hass, monkeypatch, client)
    recorder.existing[INTERVAL_ID] = (0.0, LEGACY_SUM)
    flow = await _start_flow(hass)

    form = await _configure(hass, flow["flow_id"], {"next_step_id": "keep"})
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "keep"
    assert form["description_placeholders"] == {"nmi": MASKED_NMI}
    done = await _configure(hass, flow["flow_id"], {})

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert entry.data[STATISTICS_MIGRATION_PENDING_DIGESTS] == []
    assert _issue(hass) is None
    assert recorder.cleared == []

    await entry.runtime_data.async_refresh()

    assert entry.runtime_data.data["statistics_import_paused"] is False
    assert recorder.rows_for(INTERVAL_ID)[0]["sum"] == LEGACY_SUM + 2.5
    assert _issue(hass) is None


@pytest.mark.asyncio
async def test_rebuild_success_completes_flow(hass, monkeypatch) -> None:
    """A successful rebuild shows progress, then finishes and removes the issue."""
    client = make_client(detailed={NMI: detailed_csv()})
    entry, recorder = await _setup_legacy_entry(hass, monkeypatch, client)
    flow = await _start_flow(hass)

    form = await _configure(hass, flow["flow_id"], {"next_step_id": "rebuild"})
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "rebuild"
    assert form["errors"] == {}
    progress = await _configure(hass, flow["flow_id"], {})
    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    assert progress["progress_action"] == "rebuild"
    assert progress["description_placeholders"] == {"nmi": MASKED_NMI}

    await hass.async_block_till_done(wait_background_tasks=True)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    if manager.async_progress_by_handler(DOMAIN):
        result = await _configure(hass, flow["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not manager.async_progress_by_handler(DOMAIN)

    assert recorder.cleared == [[INTERVAL_ID]]
    assert recorder.rows_for(INTERVAL_ID)[0]["sum"] == 2.5
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_DONE
    assert _issue(hass) is None


@pytest.mark.asyncio
async def test_rebuild_failure_returns_form_with_error(
    hass, monkeypatch, caplog
) -> None:
    """A failed rebuild shows rebuild_failed and keeps the issue pending."""
    client = make_client()
    entry, recorder = await _setup_legacy_entry(hass, monkeypatch, client)
    client.download_detailed_csv.side_effect = (
        SAPowerNetworksApiClientCommunicationError(f"portal failed for NMI {NMI}")
    )
    flow = await _start_flow(hass)
    await _configure(hass, flow["flow_id"], {"next_step_id": "rebuild"})
    progress = await _configure(hass, flow["flow_id"], {})
    if progress["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done(wait_background_tasks=True)
        manager = repairs_flow_manager(hass)
        assert manager is not None
        current = manager.async_get(flow["flow_id"])
        assert current["step_id"] == "rebuild_failed"
        form = await _configure(hass, flow["flow_id"])
    else:
        form = progress

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "rebuild_failed"
    assert form["errors"] == {"base": "rebuild_failed"}
    assert client.download_detailed_csv.await_count == 1
    assert recorder.cleared == []
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert _issue(hass) is not None
    assert NMI not in caplog.text
    assert MASKED_NMI in caplog.text


@pytest.mark.asyncio
async def test_rebuild_without_loaded_coordinator_shows_error(hass) -> None:
    """Without a loaded entry the flow reports rebuild_failed instead of crashing."""
    entry = make_entry(STATISTICS_MIGRATION_PENDING, pending=[DIGEST])
    entry.add_to_hass(hass)
    async_create_legacy_statistics_issue(hass, DIGEST, NMI)
    assert find_statistics_coordinator(hass, DIGEST) is None
    flow = await async_create_fix_flow(hass, ISSUE_ID, {"digest": DIGEST})
    assert isinstance(flow, LegacyStatisticsRepairFlow)
    flow.hass = hass
    flow.issue_id = ISSUE_ID
    await flow.async_step_rebuild()
    form = await flow.async_step_rebuild({})

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "rebuild"
    assert form["errors"] == {"base": "rebuild_failed"}
    assert entry.data[STATISTICS_MIGRATION] == STATISTICS_MIGRATION_PENDING
    assert _issue(hass) is not None


@pytest.mark.asyncio
async def test_fix_flow_factory_and_missing_issue_placeholders(hass) -> None:
    """The factory reads the digest; a missing issue yields empty placeholders."""
    flow = await async_create_fix_flow(hass, ISSUE_ID, {"digest": DIGEST})
    assert isinstance(flow, LegacyStatisticsRepairFlow)
    assert flow._digest == DIGEST

    flow.hass = hass
    flow.issue_id = ISSUE_ID
    menu = await flow.async_step_init()
    assert menu["description_placeholders"] == {"nmi": ""}

    empty = await async_create_fix_flow(hass, ISSUE_ID, None)
    assert isinstance(empty, LegacyStatisticsRepairFlow)
    assert empty._digest == ""


def test_repairs_translation_keys_exist() -> None:
    """Every step, menu option, progress action and error used has a string."""
    source = Path(repairs.__file__).read_text(encoding="utf-8")
    fix_flow = json.loads(
        (INTEGRATION_DIR / "translations" / "en.json").read_text(encoding="utf-8")
    )["issues"]["legacy_statistics"]
    assert "{nmi}" in fix_flow["title"]
    fix_flow = fix_flow["fix_flow"]

    step_ids = set(re.findall(r'(?<!next_)step_id="(\w+)"', source))
    menu_options = {
        option
        for group in re.findall(r"menu_options=\[([^\]]*)\]", source)
        for option in re.findall(r'"(\w+)"', group)
    }
    progress_actions = set(re.findall(r'progress_action="(\w+)"', source))
    errors = set(re.findall(r'"base": "(\w+)"', source))

    assert step_ids == {"init", "rebuild", "rebuild_failed", "keep"}
    assert menu_options == {"rebuild", "keep"}
    assert progress_actions == {"rebuild"}
    assert errors == {"rebuild_failed"}
    for step_id in step_ids:
        assert fix_flow["step"][step_id]["title"]
        assert fix_flow["step"][step_id]["description"]
    assert set(fix_flow["step"]["init"]["menu_options"]) == menu_options
    for option in menu_options:
        assert hasattr(LegacyStatisticsRepairFlow, f"async_step_{option}")
    for action in progress_actions:
        assert fix_flow["progress"][action]
    for error in errors:
        assert fix_flow["error"][error]
