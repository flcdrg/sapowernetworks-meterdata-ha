"""SA Power Networks API client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp


class SAPowerNetworksApiClientError(Exception):
    """Exception to indicate a general API error."""


class SAPowerNetworksApiClientCommunicationError(SAPowerNetworksApiClientError):
    """Exception to indicate a communication error."""


class SAPowerNetworksApiClientAuthenticationError(SAPowerNetworksApiClientError):
    """Exception to indicate an authentication error."""


class SAPowerNetworksApiClient:
    """SA Power Networks API client."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self._session = session

    async def async_get_data(self) -> Any:
        """Fetch data from the API."""
        raise NotImplementedError
