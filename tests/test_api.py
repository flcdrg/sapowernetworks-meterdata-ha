"""Tests for SA Power Networks API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    SAPowerNetworksApiClientCommunicationError,
    _merge_nem12_chunks,
    _parse_ver,
    _split_date_range,
)
from custom_components.sapowernetworks.const import DETAILED_REPORT_MAX_RANGE


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
