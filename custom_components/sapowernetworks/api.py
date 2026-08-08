"""SA Power Networks API client."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin

if TYPE_CHECKING:
    import aiohttp

from .const import (
    CAD_DASHBOARD_PATH,
    CAD_REQUEST_METER_DATA_PATH,
    CAD_SITE_LOGIN_PATH,
    DETAILED_REPORT_MAX_RANGE,
    LOGGER,
    PORTAL_BASE_URL,
    PORTAL_COMPANY,
    REPORT_DATA_SET_NEM12,
    REPORT_TYPE_DETAILED_CSV,
    REPORT_TYPE_SUMMARY_CSV,
)
from .privacy import redact_mapping, redact_text


@dataclass(frozen=True)
class SalesforceMethod:
    """Resolved Salesforce remoting method context."""

    action: str
    service: str
    vid: str
    csrf: str
    ns: str
    ver: str
    authorization: str


@dataclass(frozen=True)
class NmiAssignment:
    """NMI metadata returned by SAPN portal."""

    nmi: str
    company: str
    meter_serial_number: str | None
    meter_type_description: str | None
    description: str | None
    is_default: bool


class SAPowerNetworksApiClientError(Exception):
    """Exception to indicate a general API error."""


class SAPowerNetworksApiClientCommunicationError(SAPowerNetworksApiClientError):
    """Exception to indicate a communication error."""


class SAPowerNetworksApiClientAuthenticationError(SAPowerNetworksApiClientError):
    """Exception to indicate an authentication error."""


class SAPowerNetworksApiClientParseError(SAPowerNetworksApiClientError):
    """Exception to indicate unexpected portal page shape."""


class SAPowerNetworksApiClient:
    """SA Power Networks API client."""

    _VIEWSTATE = "com.salesforce.visualforce.ViewState"
    _VIEWSTATE_MAC = "com.salesforce.visualforce.ViewStateMAC"
    _VIEWSTATE_VERSION = "com.salesforce.visualforce.ViewStateVersion"
    _LOGIN_FORM_PREFIX = "loginPage:SiteTemplate:siteLogin:loginComponent:loginForm"
    _HTTP_OK = 200
    _HTTP_UNAUTHORIZED = 401
    _HTTP_FORBIDDEN = 403
    _HTTP_SERVICE_UNAVAILABLE = 503
    _HTTP_BAD_REQUEST = 400
    _DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client."""
        self._username = username
        self._password = password
        self._session = session
        self._is_authenticated = False

    async def test_credentials(self) -> None:
        """Validate account credentials by performing login and assignment fetch."""
        await self._ensure_authenticated(force=True)
        _ = await self.get_nmi_assignments()

    async def async_get_data(self) -> Any:
        """Fetch operational snapshot data from the portal."""
        await self._ensure_authenticated()
        assignments = await self.get_nmi_assignments()
        return {
            "authenticated": True,
            "nmi_count": len(assignments),
            "nmis": [item.nmi for item in assignments],
            "last_sync": datetime.now(tz=UTC),
        }

    async def get_nmis(self) -> list[str]:
        """Get NMI identifiers linked to this account."""
        return [assignment.nmi for assignment in await self.get_nmi_assignments()]

    async def get_nmi_assignments(self) -> list[NmiAssignment]:
        """Get account NMI assignments from dashboard remoting controller."""
        response = await self._data_from_method(
            path=CAD_DASHBOARD_PATH,
            method_name="getNmiAssignments",
            data=None,
        )
        first = self._first_rpc_item(response, "getNmiAssignments")
        result = first.get("result")
        if not isinstance(result, list):
            msg = "No result array in getNmiAssignments"
            raise SAPowerNetworksApiClientParseError(msg)

        assignments: list[NmiAssignment] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            nmi = _string_or_none(item.get("NMI__c"))
            if not nmi:
                continue
            assignments.append(
                NmiAssignment(
                    nmi=nmi,
                    company=_string_or_none(item.get("Company__c")) or PORTAL_COMPANY,
                    meter_serial_number=_string_or_none(
                        item.get("Meter_Serial_Number__c")
                    ),
                    meter_type_description=_string_or_none(
                        item.get("Meter_Type_Desc__c")
                    ),
                    description=_string_or_none(item.get("NMI_Description__c")),
                    is_default=bool(item.get("Default__c", False)),
                )
            )

        return assignments

    async def download_detailed_csv(
        self,
        nmi: str,
        start: datetime,
        end: datetime,
        job_id: int = 0,
    ) -> str:
        """Download raw detailed report CSV for one NMI via remoting."""
        try:
            return await self._download_detailed_csv_single(nmi, start, end, job_id)
        except SAPowerNetworksApiClientAuthenticationError:
            raise
        except SAPowerNetworksApiClientError:
            if end - start <= DETAILED_REPORT_MAX_RANGE:
                raise

        chunks: list[str] = []
        for block_job_id, (block_start, block_end) in enumerate(
            _split_date_range(start, end, DETAILED_REPORT_MAX_RANGE),
        ):
            chunks.append(
                await self._download_detailed_csv_single(
                    nmi,
                    block_start,
                    block_end,
                    block_job_id,
                )
            )
        return _merge_nem12_chunks(chunks)

    async def _download_detailed_csv_single(
        self,
        nmi: str,
        start: datetime,
        end: datetime,
        job_id: int,
    ) -> str:
        """Download one detailed CSV block for one NMI via remoting."""
        request_path = f"{CAD_REQUEST_METER_DATA_PATH}?{urlencode({'selNMI': nmi})}"
        response = await self._data_from_method(
            path=request_path,
            method_name="downloadNMIData",
            data=[
                nmi,
                PORTAL_COMPANY,
                _to_gmt_timestamp(start),
                _to_gmt_timestamp(end),
                REPORT_DATA_SET_NEM12,
                REPORT_TYPE_DETAILED_CSV,
                job_id,
            ],
        )
        return self._extract_results_string(response, "downloadNMIData")

    async def download_accumulated_summary_csv(
        self,
        nmi: str,
        start: datetime,
        end: datetime,
    ) -> str:
        """Download accumulated summary CSV via remoting first, then form fallback."""
        request_path = f"{CAD_REQUEST_METER_DATA_PATH}?{urlencode({'selNMI': nmi})}"

        try:
            response = await self._data_from_method(
                path=request_path,
                method_name="downloadNMIData",
                data=[
                    nmi,
                    PORTAL_COMPANY,
                    _to_gmt_timestamp(start),
                    _to_gmt_timestamp(end),
                    REPORT_DATA_SET_NEM12,
                    REPORT_TYPE_SUMMARY_CSV,
                    0,
                ],
            )
            csv = self._extract_results_string(response, "downloadNMIData")
            if csv.strip():
                return csv
        except SAPowerNetworksApiClientError:
            pass

        return await self._download_accumulated_summary_csv_form(nmi, start, end)

    async def _download_accumulated_summary_csv_form(
        self,
        nmi: str,
        start: datetime,
        end: datetime,
    ) -> str:
        """Download accumulated summary CSV using direct Visualforce form POST."""
        await self._ensure_authenticated()
        page_url = urljoin(
            PORTAL_BASE_URL,
            f"/meterdata/{CAD_REQUEST_METER_DATA_PATH}?{urlencode({'selNMI': nmi})}",
        )
        async with self._session.get(page_url) as response:
            html = await response.text()

        hidden_inputs = self._extract_hidden_inputs(html)
        if (
            self._VIEWSTATE not in hidden_inputs
            or self._VIEWSTATE_MAC not in hidden_inputs
        ):
            msg = "Unable to locate view state tokens for accumulated form POST"
            raise SAPowerNetworksApiClientParseError(msg)

        form_prefix = "j_id0:SiteTemplate:j_id86"
        payload = {
            "meter": "Accumulated",
            f"{form_prefix}:selMeterType": "Accumulated",
            f"{form_prefix}:selReportType": REPORT_TYPE_SUMMARY_CSV,
            f"{form_prefix}:selNMI": nmi,
            f"{form_prefix}:frmDate": start.strftime("%d/%m/%Y"),
            f"{form_prefix}:toDate": end.strftime("%d/%m/%Y"),
            f"{form_prefix}:selNumberStreams": "0",
            self._VIEWSTATE: hidden_inputs[self._VIEWSTATE],
            self._VIEWSTATE_MAC: hidden_inputs[self._VIEWSTATE_MAC],
        }
        if self._VIEWSTATE_VERSION in hidden_inputs:
            payload[self._VIEWSTATE_VERSION] = hidden_inputs[self._VIEWSTATE_VERSION]

        async with self._session.post(
            page_url,
            data=payload,
            headers={
                "Origin": PORTAL_BASE_URL,
                "Referer": page_url,
            },
        ) as response:
            return await response.text()

    async def _ensure_authenticated(self, *, force: bool = False) -> None:
        """Ensure authenticated cookie session exists."""
        if self._is_authenticated and not force:
            return
        await self._perform_login()
        self._is_authenticated = True

    async def _perform_login(self) -> None:
        """Perform SAPN login handshake on Salesforce-backed portal."""
        login_page_url = urljoin(PORTAL_BASE_URL, CAD_SITE_LOGIN_PATH)
        ref = urlencode({"refURL": login_page_url})
        page_url = f"{login_page_url}?{ref}"

        async with self._session.get(
            page_url,
            headers=self._browser_headers(referer=login_page_url),
        ) as response:
            if response.status != self._HTTP_OK:
                msg = f"Login page unavailable: {response.status}"
                raise SAPowerNetworksApiClientCommunicationError(msg)
            login_page = await response.text()

        hidden_inputs = self._extract_hidden_inputs(login_page)
        view_state = hidden_inputs.get(self._VIEWSTATE)
        view_state_mac = hidden_inputs.get(self._VIEWSTATE_MAC)
        view_state_version = hidden_inputs.get(self._VIEWSTATE_VERSION)
        if not view_state or not view_state_mac:
            msg = "Portal view state missing"
            raise SAPowerNetworksApiClientAuthenticationError(msg)

        form: dict[str, str] = {
            **hidden_inputs,
            self._LOGIN_FORM_PREFIX: self._LOGIN_FORM_PREFIX,
            f"{self._LOGIN_FORM_PREFIX}:username": self._username,
            f"{self._LOGIN_FORM_PREFIX}:password": self._password,
            f"{self._LOGIN_FORM_PREFIX}:loginButton": "Login",
            self._VIEWSTATE: view_state,
            self._VIEWSTATE_MAC: view_state_mac,
        }
        if view_state_version:
            form[self._VIEWSTATE_VERSION] = view_state_version

        post_url = self._extract_form_action(login_page, page_url)
        self._debug(
            "Login form submission",
            {
                "post_url": post_url,
                "fields": sorted(form.keys()),
            },
        )

        async with self._session.post(
            post_url,
            data=form,
            headers=self._browser_headers(referer=page_url),
        ) as response:
            if response.status != self._HTTP_OK:
                snippet = redact_text((await response.text()).strip()[:300])
                if response.status in {self._HTTP_UNAUTHORIZED, self._HTTP_FORBIDDEN}:
                    msg = f"Login rejected with status: {response.status}"
                    raise SAPowerNetworksApiClientAuthenticationError(msg)
                if response.status == self._HTTP_SERVICE_UNAVAILABLE:
                    msg = (
                        "Login POST returned 503; likely request-shape mismatch "
                        f"in integration (response snippet: {snippet})"
                    )
                    raise SAPowerNetworksApiClientParseError(msg)
                msg = (
                    f"Login failed with unexpected status: {response.status} "
                    f"(response snippet: {snippet})"
                )
                raise SAPowerNetworksApiClientCommunicationError(msg)
            body = await response.text()

        redirect = self._extract_redirect_link(body)
        if not redirect:
            msg = "Login redirect missing; credentials may be invalid"
            raise SAPowerNetworksApiClientAuthenticationError(msg)

        redirect_url = urljoin(PORTAL_BASE_URL, redirect)
        async with self._session.get(
            redirect_url,
            headers=self._browser_headers(referer=page_url),
        ) as response:
            if response.status >= self._HTTP_BAD_REQUEST:
                msg = f"Redirect follow failed: {response.status}"
                raise SAPowerNetworksApiClientAuthenticationError(msg)

    async def _data_from_method(
        self,
        path: str,
        method_name: str,
        data: list[Any] | None,
    ) -> list[dict[str, Any]]:
        """Resolve method context and invoke one remoting call."""
        await self._ensure_authenticated()
        ctx = await self.resolve_method(path=path, method_name=method_name)
        return await self.invoke_rpc(
            path=path, method_name=method_name, ctx=ctx, data=data
        )

    async def resolve_method(self, path: str, method_name: str) -> SalesforceMethod:
        """Resolve method metadata from Visualforce data keys."""
        json_text = await self._update_methods_raw(path=path)
        return self._resolve_method_from_json(json_text, method_name)

    async def _update_methods_raw(self, path: str) -> str:
        """Load a page and extract embedded Visualforce remoting context JSON."""
        url = urljoin(PORTAL_BASE_URL + "/meterdata/", path)
        try:
            async with self._session.get(url) as response:
                text = await response.text()
        except Exception as exception:
            msg = "Unable to resolve remoting methods"
            raise SAPowerNetworksApiClientCommunicationError(msg) from exception

        vf_json = self._extract_vf_json(text)
        if not vf_json:
            msg = "No remoting data keys found"
            raise SAPowerNetworksApiClientAuthenticationError(msg)
        return vf_json

    async def invoke_rpc(
        self,
        path: str,
        method_name: str,
        ctx: SalesforceMethod,
        data: list[Any] | None,
    ) -> list[dict[str, Any]]:
        """Invoke one Salesforce remoting method."""
        body = self._build_rpc_body(ctx=ctx, method_name=method_name, data=data)
        url = urljoin(PORTAL_BASE_URL + "/", ctx.service)
        referer = urljoin(PORTAL_BASE_URL + "/meterdata/", path)

        payload = json.dumps(body)
        self._debug(
            "RPC request",
            {
                "url": url,
                "method": method_name,
                "body": body,
            },
        )
        async with self._session.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-User-Agent": "Visualforce-Remoting",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": PORTAL_BASE_URL,
                "Referer": referer,
            },
        ) as response:
            response_text = await response.text()

        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as exception:
            msg = "RPC returned non-JSON response"
            raise SAPowerNetworksApiClientParseError(msg) from exception

        if not isinstance(parsed, list):
            msg = "RPC payload is not a list"
            raise SAPowerNetworksApiClientParseError(msg)
        return [item for item in parsed if isinstance(item, dict)]

    @staticmethod
    def _extract_hidden_input_value(html: str, field_id: str) -> str | None:
        pattern = re.compile(
            rf'<input[^>]*id="{re.escape(field_id)}"[^>]*value="([^"]*)"[^>]*>',
            re.IGNORECASE,
        )
        match = pattern.search(html)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_hidden_inputs(html: str) -> dict[str, str]:
        pattern = re.compile(
            r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*>',
            re.IGNORECASE,
        )
        return dict(pattern.findall(html))

    @staticmethod
    def _extract_redirect_link(body: str) -> str | None:
        patterns = (
            r"handleRedirect\('([^']+)'\)",
            r'window\.location\.href\s*=\s*"([^"]+)"',
            r"window\.location\.href\s*=\s*'([^']+)'",
            r'window\.location\s*=\s*"([^"]+)"',
            r"window\.location\s*=\s*'([^']+)'",
        )
        for pattern in patterns:
            match = re.search(pattern, body)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_form_action(html: str, fallback_url: str) -> str:
        match = re.search(r'<form[^>]*action="([^"]+)"', html, re.IGNORECASE)
        if not match:
            return fallback_url
        return urljoin(PORTAL_BASE_URL, match.group(1))

    @staticmethod
    def _extract_vf_json(text: str) -> str | None:
        marker = '{"vf":{"vid":"'
        start = text.find(marker)
        if start < 0:
            return None
        end = text.find('"}));', start)
        if end < 0:
            return None
        return text[start : end + 2]

    @staticmethod
    def _resolve_method_from_json(json_text: str, method_name: str) -> SalesforceMethod:
        try:
            root = json.loads(json_text)
        except json.JSONDecodeError as exception:
            msg = "Unable to parse data keys JSON"
            raise SAPowerNetworksApiClientParseError(msg) from exception

        if not isinstance(root, dict):
            msg = "Data keys root is not object"
            raise SAPowerNetworksApiClientParseError(msg)

        service = _string_or_none(root.get("service"))
        vf = root.get("vf")
        vid = _string_or_none(vf.get("vid")) if isinstance(vf, dict) else None
        actions = root.get("actions")

        if not service or not vid or not isinstance(actions, dict):
            msg = "Missing required data keys"
            raise SAPowerNetworksApiClientParseError(msg)

        resolved: SalesforceMethod | None = None
        for action_name, action_value in actions.items():
            if not isinstance(action_value, dict):
                continue
            methods = action_value.get("ms")
            if not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict):
                    continue
                if _string_or_none(method.get("name")) != method_name:
                    continue
                resolved = SalesforceMethod(
                    action=action_name,
                    service=service,
                    vid=vid,
                    csrf=str(method.get("csrf", "")),
                    ns=str(method.get("ns", "")),
                    ver=str(method.get("ver", "")),
                    authorization=str(method.get("authorization", "")),
                )

        if resolved is None:
            msg = f"Method '{method_name}' not found in data keys"
            raise SAPowerNetworksApiClientAuthenticationError(msg)
        return resolved

    @staticmethod
    def _build_rpc_body(
        ctx: SalesforceMethod,
        method_name: str,
        data: list[Any] | None,
    ) -> dict[str, Any]:
        return {
            "action": ctx.action,
            "method": method_name,
            "type": "rpc",
            "tid": 1,
            "data": data,
            "ctx": {
                "csrf": ctx.csrf,
                "vid": ctx.vid,
                "ns": ctx.ns,
                "ver": _parse_ver(ctx.ver),
                "authorization": ctx.authorization,
            },
        }

    @staticmethod
    def _first_rpc_item(
        payload: list[dict[str, Any]],
        method_name: str,
    ) -> dict[str, Any]:
        if not payload:
            msg = f"Empty RPC payload for {method_name}"
            raise SAPowerNetworksApiClientParseError(msg)
        first = payload[0]
        if _string_or_none(first.get("type")) == "exception":
            message = _string_or_none(first.get("message")) or "Unknown RPC exception"
            msg = f"RPC exception for {method_name}: {message}"
            raise SAPowerNetworksApiClientAuthenticationError(msg)
        return first

    def _extract_results_string(
        self, payload: list[dict[str, Any]], method_name: str
    ) -> str:
        first = self._first_rpc_item(payload, method_name)
        result = first.get("result")
        if isinstance(result, dict):
            results = result.get("results")
            if isinstance(results, str):
                return results
        msg = f"No results string found for {method_name}"
        raise SAPowerNetworksApiClientParseError(msg)

    def _debug(self, message: str, data: dict[str, Any]) -> None:
        safe = redact_mapping({str(key): value for key, value in data.items()})
        LOGGER.debug(redact_text(f"{message}: {safe}"))

    def _browser_headers(self, referer: str) -> dict[str, str]:
        """Build browser-like headers for Salesforce form and redirect flows."""
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-AU,en;q=0.9",
            "Cache-Control": "no-cache",
            "Origin": PORTAL_BASE_URL,
            "Pragma": "no-cache",
            "Referer": referer,
            "User-Agent": self._DEFAULT_USER_AGENT,
        }


def _parse_ver(value: str) -> int | float | str:
    """Parse Salesforce ver value similarly to browser behavior."""
    try:
        as_float = float(value)
    except ValueError:
        return value
    if math.isfinite(as_float) and as_float.is_integer():
        return int(as_float)
    return as_float


def _to_gmt_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _split_date_range(
    start: datetime,
    end: datetime,
    max_range: timedelta,
) -> list[tuple[datetime, datetime]]:
    """Split a time range into contiguous blocks no larger than max_range."""
    blocks: list[tuple[datetime, datetime]] = []
    block_start = start
    while block_start < end:
        block_end = min(block_start + max_range, end)
        blocks.append((block_start, block_end))
        if block_end >= end:
            break
        block_start = block_end
    return blocks


def _merge_nem12_chunks(chunks: list[str]) -> str:
    """Merge chunked NEM12 CSV responses while de-duplicating repeated wrappers."""
    header: str | None = None
    footer: str | None = None
    body_lines: list[str] = []
    seen_lines: set[str] = set()

    for chunk in chunks:
        for raw_line in chunk.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("100,"):
                header = header or line
                continue
            if line.startswith("900"):
                footer = footer or line
                continue
            if line in seen_lines:
                continue
            seen_lines.add(line)
            body_lines.append(line)

    merged: list[str] = []
    if header:
        merged.append(header)
    merged.extend(body_lines)
    if footer:
        merged.append(footer)
    return "\n".join(merged)
