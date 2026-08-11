"""Tests for SA Power Networks API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Never

import pytest

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientCommunicationError,
    SAPowerNetworksApiClientParseError,
    _is_summary_csv_payload,
    _merge_csv_chunks,
    _merge_nem12_chunks,
    _parse_ver,
    _split_date_range,
)
from custom_components.sapowernetworks.const import (
    ACCUMULATED_REPORT_MAX_RANGE,
    DETAILED_REPORT_MAX_RANGE,
)


def test_extract_redirect_link() -> None:
    """Redirect helper should extract the Salesforce frontdoor URL."""
    body = (
        "window.location.handleRedirect('https://example.test/frontdoor.jsp?sid=123')"
    )
    assert (
        SAPowerNetworksApiClient._extract_redirect_link(body)
        == "https://example.test/frontdoor.jsp?sid=123"
    )


def test_extract_redirect_link_window_location_href() -> None:
    """Redirect helper should extract plain window.location.href redirects."""
    body = 'window.location.href = "/meterdata/secur/frontdoor.jsp?sid=abc"'
    assert (
        SAPowerNetworksApiClient._extract_redirect_link(body)
        == "/meterdata/secur/frontdoor.jsp?sid=abc"
    )


def test_extract_form_action_prefers_html_action() -> None:
    """Form action helper should resolve form action when available."""
    html = '<form id="login" action="/meterdata/CADSiteLogin?refURL=x"></form>'
    fallback = "https://customer.portal.sapowernetworks.com.au/meterdata/CADSiteLogin"
    assert (
        SAPowerNetworksApiClient._extract_form_action(html, fallback)
        == "https://customer.portal.sapowernetworks.com.au/meterdata/CADSiteLogin?refURL=x"
    )


def test_extract_form_action_fallback_when_missing() -> None:
    """Form action helper should fall back to current page URL."""
    fallback = "https://customer.portal.sapowernetworks.com.au/meterdata/CADSiteLogin"
    assert (
        SAPowerNetworksApiClient._extract_form_action("<html></html>", fallback)
        == fallback
    )


def test_extract_hidden_inputs_preserves_values() -> None:
    """Hidden input extraction should preserve values from live-style input tags."""
    html = (
        '<input type="hidden" '
        'name="loginPage:SiteTemplate:siteLogin:loginComponent:loginForm" '
        'value="loginPage:SiteTemplate:siteLogin:loginComponent:loginForm" />'
        '<input type="hidden" '
        'id="com.salesforce.visualforce.ViewState" '
        'name="com.salesforce.visualforce.ViewState" '
        'value="viewstate-value" />'
        '<input type="hidden" '
        'id="com.salesforce.visualforce.ViewStateVersion" '
        'name="com.salesforce.visualforce.ViewStateVersion" '
        'value="1" />'
        '<input type="hidden" '
        'id="com.salesforce.visualforce.ViewStateMAC" '
        'name="com.salesforce.visualforce.ViewStateMAC" '
        'value="mac-value" />'
    )

    hidden = SAPowerNetworksApiClient._extract_hidden_inputs(html)

    assert (
        hidden["loginPage:SiteTemplate:siteLogin:loginComponent:loginForm"]
        == "loginPage:SiteTemplate:siteLogin:loginComponent:loginForm"
    )
    assert hidden["com.salesforce.visualforce.ViewState"] == "viewstate-value"
    assert hidden["com.salesforce.visualforce.ViewStateVersion"] == "1"
    assert hidden["com.salesforce.visualforce.ViewStateMAC"] == "mac-value"


def test_extract_vf_json() -> None:
    """VF helper should extract embedded remoting context JSON."""
    content = (
        "<html><script>Manager.add(new RemotingProviderImpl("
        '{"vf":{"vid":"066XYZ"},"actions":{},"service":"meterdata/apexremote"}'
        "));</script></html>"
    )
    extracted = SAPowerNetworksApiClient._extract_vf_json(content)
    assert (
        extracted
        == '{"vf":{"vid":"066XYZ"},"actions":{},"service":"meterdata/apexremote"}'
    )


def test_extract_vf_json_with_whitespace_before_closure() -> None:
    """VF helper should tolerate whitespace/newlines around the remoting call."""
    content = (
        "<html><script>Manager.add(new RemotingProviderImpl(\n"
        '{"vf":{"vid":"066XYZ"},"actions":{},"service":"meterdata/apexremote"}\n'
        "));</script></html>"
    )
    extracted = SAPowerNetworksApiClient._extract_vf_json(content)
    assert (
        extracted
        == '{"vf":{"vid":"066XYZ"},"actions":{},"service":"meterdata/apexremote"}'
    )


def test_resolve_method_uses_last_action_match() -> None:
    """Method resolver should use the last action containing the method name."""
    data_keys = json.dumps(
        {
            "vf": {"vid": "066AB"},
            "service": "meterdata/apexremote",
            "actions": {
                "FirstController": {
                    "ms": [
                        {
                            "name": "getNmiAssignments",
                            "ns": "",
                            "ver": 35,
                            "csrf": "C1",
                            "authorization": "A1",
                        }
                    ]
                },
                "SecondController": {
                    "ms": [
                        {
                            "name": "getNmiAssignments",
                            "ns": "",
                            "ver": 35,
                            "csrf": "C2",
                            "authorization": "A2",
                        }
                    ]
                },
            },
        }
    )

    method = SAPowerNetworksApiClient._resolve_method_from_json(
        data_keys,
        "getNmiAssignments",
    )
    assert method.action == "SecondController"
    assert method.csrf == "C2"
    assert method.authorization == "A2"


def test_resolve_method_missing_raises() -> None:
    """Resolver should raise auth error for unknown method name."""
    data_keys = '{"vf":{"vid":"x"},"service":"meterdata/apexremote","actions":{}}'
    with pytest.raises(SAPowerNetworksApiClientAuthenticationError):
        SAPowerNetworksApiClient._resolve_method_from_json(
            data_keys,
            "doesNotExist",
        )


def test_parse_ver_whole_decimal_to_int() -> None:
    """Whole decimal ver values should be emitted as int."""
    assert _parse_ver("35.0") == 35


def test_parse_ver_non_whole_decimal_stays_float() -> None:
    """Non-whole decimal ver values should be emitted as float."""
    assert _parse_ver("35.5") == 35.5


def test_split_date_range_uses_contiguous_90_day_blocks() -> None:
    """Long detailed requests should split into contiguous 90-day blocks."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 8, 1, tzinfo=UTC)

    blocks = _split_date_range(start, end, DETAILED_REPORT_MAX_RANGE)

    assert len(blocks) == 3
    assert blocks[0] == (start, start + timedelta(days=90))
    assert blocks[1] == (
        start + timedelta(days=90),
        start + timedelta(days=180),
    )
    assert blocks[2] == (start + timedelta(days=180), end)


def test_merge_nem12_chunks_deduplicates_wrapper_records() -> None:
    """Chunked NEM12 responses should merge with one header/footer and unique rows."""
    chunk_one = (
        "100,NEM12,202506081149,SAPN,NEMMCO\n"
        "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,\n"
        "300,20260620,1.0,1.5,A\n"
        "900"
    )
    chunk_two = (
        "100,NEM12,202506081149,SAPN,NEMMCO\n"
        "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,\n"
        "300,20260621,2.0,2.5,A\n"
        "900"
    )

    merged = _merge_nem12_chunks([chunk_one, chunk_two])

    assert merged.count("100,NEM12,202506081149,SAPN,NEMMCO") == 1
    assert merged.count("200,20012345678,E1B1,E1,E1,,METER1,KWH,30,") == 1
    assert "300,20260620,1.0,1.5,A" in merged
    assert "300,20260621,2.0,2.5,A" in merged
    assert merged.endswith("900")


def test_merge_csv_chunks_deduplicates_summary_rows() -> None:
    """Summary CSV chunks should merge unique rows while preserving order."""
    chunk_one = (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
    )
    chunk_two = (
        "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN\n"
        "20012345678,1180281,kWh,A,18/03/2025,08/08/2025,999.000,10.000,1100.000,NN"
    )

    merged = _merge_csv_chunks([chunk_one, chunk_two])

    assert (
        merged.count(
            "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
        )
        == 1
    )
    assert (
        "20012345678,1180281,kWh,A,18/03/2025,08/08/2025,999.000,10.000,1100.000,NN"
        in merged
    )


def test_is_summary_csv_payload_rejects_nem12_content() -> None:
    """Detailed NEM12 payloads must not be accepted as accumulated summary CSV."""
    payload = "200,20012345678,E1B1,E1,E1,,METER1,KWH,05,\n300,20260620,1.0,1.5,A\n900"
    assert _is_summary_csv_payload(payload) is False


def test_is_summary_csv_payload_accepts_summary_content() -> None:
    """Accumulated summary payloads should be accepted when rows parse cleanly."""
    payload = (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,12.500,1165.000,NN"
    )
    assert _is_summary_csv_payload(payload) is True


@pytest.mark.asyncio
async def test_download_detailed_csv_falls_back_to_chunked_requests(
    monkeypatch,
) -> None:
    """Large detailed requests should fall back to 90-day chunk downloads."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 8, 1, tzinfo=UTC)
    calls: list[tuple[datetime, datetime, int]] = []
    chunk_payloads = [
        (
            "100,NEM12,202506081149,SAPN,NEMMCO\n"
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,\n"
            "300,20260620,1.0,1.5,A\n"
            "900"
        ),
        (
            "100,NEM12,202506081149,SAPN,NEMMCO\n"
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,\n"
            "300,20260621,2.0,2.5,A\n"
            "900"
        ),
        (
            "100,NEM12,202506081149,SAPN,NEMMCO\n"
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,\n"
            "300,20260622,3.0,3.5,A\n"
            "900"
        ),
    ]

    async def _fake_download_single(
        _nmi: str,
        block_start: datetime,
        block_end: datetime,
        job_id: int,
    ) -> str:
        calls.append((block_start, block_end, job_id))
        if len(calls) == 1:
            msg = "request too large"
            raise SAPowerNetworksApiClientCommunicationError(msg)
        return chunk_payloads[len(calls) - 2]

    monkeypatch.setattr(client, "_download_detailed_csv_single", _fake_download_single)

    merged = await client.download_detailed_csv("20012345678", start, end)

    assert len(calls) == 4
    assert calls[0] == (start, end, 0)
    assert calls[1][0] == start
    assert calls[1][1] == start + timedelta(days=90)
    assert calls[2][0] == start + timedelta(days=90)
    assert calls[3][1] == end
    assert merged.count("300,") == 3
    assert merged.count("200,20012345678,E1B1,E1,E1,,METER1,KWH,30,") == 1


@pytest.mark.asyncio
async def test_download_accumulated_summary_falls_back_when_rpc_returns_nem12(
    monkeypatch,
) -> None:
    """Accumulated summary fetch should fall back when RPC returns detailed NEM12."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    rpc_payload = (
        "200,20012345678,E1B1,E1,E1,,METER1,KWH,05,\n300,20260620,1.0,1.5,A\n900"
    )
    form_payload = (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN"
    )

    async def _fake_data_from_method(
        **_kwargs: Any,
    ) -> list[dict[str, dict[str, str]]]:
        return [{"result": {"results": rpc_payload}}]

    async def _fake_form(*_args: Any, **_kwargs: Any) -> str:
        return form_payload

    monkeypatch.setattr(client, "_data_from_method", _fake_data_from_method)
    monkeypatch.setattr(
        client,
        "_download_accumulated_summary_csv_form",
        _fake_form,
    )

    result = await client.download_accumulated_summary_csv(
        "20012345678",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )

    assert result == form_payload


@pytest.mark.asyncio
async def test_download_accumulated_summary_uses_rpc_when_summary_is_valid(
    monkeypatch,
) -> None:
    """Accumulated summary fetch should keep a valid RPC summary response."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    rpc_payload = (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN"
    )

    async def _fake_data_from_method(
        **_kwargs: Any,
    ) -> list[dict[str, dict[str, str]]]:
        return [{"result": {"results": rpc_payload}}]

    async def _fail_form(*_args: Any, **_kwargs: Any) -> Never:
        msg = "form fallback should not be used"
        raise AssertionError(msg)

    monkeypatch.setattr(client, "_data_from_method", _fake_data_from_method)
    monkeypatch.setattr(
        client,
        "_download_accumulated_summary_csv_form",
        _fail_form,
    )

    result = await client.download_accumulated_summary_csv(
        "20012345678",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )

    assert result == rpc_payload


@pytest.mark.asyncio
async def test_download_accumulated_summary_chunks_form_fallback(
    monkeypatch,
) -> None:
    """Accumulated form fallback should split large ranges into bounded windows."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2027, 1, 1, tzinfo=UTC)
    calls: list[tuple[datetime, datetime]] = []
    chunks = [
        "20012345678,1180281,kWh,A,01/01/2024,31/12/2024,100.000,0.000,100.000,NN",
        "20012345678,1180281,kWh,A,01/01/2025,31/12/2025,200.000,0.000,300.000,NN",
        "20012345678,1180281,kWh,A,01/01/2026,01/01/2027,300.000,0.000,600.000,NN",
    ]

    async def _fake_data_from_method(
        **_kwargs: Any,
    ) -> list[dict[str, dict[str, str]]]:
        return [{"result": {"results": "200,not-summary"}}]

    async def _fake_form(
        _nmi: str,
        block_start: datetime,
        block_end: datetime,
    ) -> str:
        calls.append((block_start, block_end))
        return chunks[len(calls) - 1]

    monkeypatch.setattr(client, "_data_from_method", _fake_data_from_method)
    monkeypatch.setattr(
        client,
        "_download_accumulated_summary_csv_form",
        _fake_form,
    )

    result = await client.download_accumulated_summary_csv(
        "20012345678",
        start,
        end,
    )

    assert len(calls) == 2
    assert calls[0] == (start, start + ACCUMULATED_REPORT_MAX_RANGE)
    assert calls[1] == (start + ACCUMULATED_REPORT_MAX_RANGE, end)
    assert result.count("20012345678") == 2


@pytest.mark.asyncio
async def test_ensure_authenticated_reuses_existing_session(monkeypatch) -> None:
    """Ensure auth should skip login POST when shared session is already logged in."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    calls = {"probe": 0, "login": 0}

    async def _fake_probe() -> bool:
        calls["probe"] += 1
        return True

    async def _fake_login() -> None:
        calls["login"] += 1

    monkeypatch.setattr(client, "_session_appears_authenticated", _fake_probe)
    monkeypatch.setattr(client, "_perform_login", _fake_login)

    await client._ensure_authenticated()

    assert calls == {"probe": 1, "login": 0}
    assert client._is_authenticated is True


@pytest.mark.asyncio
async def test_ensure_authenticated_logs_in_when_session_not_authenticated(
    monkeypatch,
) -> None:
    """Ensure auth should fall back to login POST when no valid session exists."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    calls = {"probe": 0, "login": 0}

    async def _fake_probe() -> bool:
        calls["probe"] += 1
        return False

    async def _fake_login() -> None:
        calls["login"] += 1

    monkeypatch.setattr(client, "_session_appears_authenticated", _fake_probe)
    monkeypatch.setattr(client, "_perform_login", _fake_login)

    await client._ensure_authenticated()

    assert calls == {"probe": 1, "login": 1}
    assert client._is_authenticated is True


@pytest.mark.asyncio
async def test_data_from_method_retries_after_parse_failure(monkeypatch) -> None:
    """Remoting call should retry once with forced login after parse-level miss."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )
    ensure_calls: list[bool] = []
    resolve_calls = 0

    async def _fake_ensure_authenticated(*, force: bool = False) -> None:
        ensure_calls.append(force)

    async def _fake_resolve_method(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            msg = "No remoting data keys found"
            raise SAPowerNetworksApiClientParseError(msg)
        return object()

    async def _fake_invoke_rpc(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"result": []}]

    monkeypatch.setattr(client, "_ensure_authenticated", _fake_ensure_authenticated)
    monkeypatch.setattr(client, "resolve_method", _fake_resolve_method)
    monkeypatch.setattr(client, "invoke_rpc", _fake_invoke_rpc)

    payload = await client._data_from_method(
        path="apex/cadenergydashboard",
        method_name="getNmiAssignments",
        data=None,
    )

    assert payload == [{"result": []}]
    assert ensure_calls == [False, True]


@pytest.mark.asyncio
async def test_data_from_method_raises_after_forced_retry_failure(
    monkeypatch,
) -> None:
    """Remoting call should surface parse errors if forced retry still fails."""
    fake_secret = "synthetic-test-value"
    client = SAPowerNetworksApiClient(
        username="user@example.com",
        password=fake_secret,
        session=None,  # type: ignore[arg-type]
    )

    async def _fake_ensure_authenticated(*, force: bool = False) -> None:
        _ = force

    async def _always_fail_resolve(*_args: Any, **_kwargs: Any) -> Any:
        msg = "No remoting data keys found"
        raise SAPowerNetworksApiClientParseError(msg)

    monkeypatch.setattr(client, "_ensure_authenticated", _fake_ensure_authenticated)
    monkeypatch.setattr(client, "resolve_method", _always_fail_resolve)

    with pytest.raises(SAPowerNetworksApiClientParseError):
        await client._data_from_method(
            path="apex/cadenergydashboard",
            method_name="getNmiAssignments",
            data=None,
        )
