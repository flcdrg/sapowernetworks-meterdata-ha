"""Tests for SA Power Networks API client."""

from __future__ import annotations

import pytest

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClient,
)

pytestmark = pytest.mark.asyncio


async def test_async_get_data_raises_not_implemented() -> None:
    """Test that async_get_data raises NotImplementedError before implementation."""
    client = SAPowerNetworksApiClient(session=None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await client.async_get_data()
