"""Tests for SA Power Networks API client."""

from __future__ import annotations

import json

import pytest

from custom_components.sapowernetworks.api import (
    SAPowerNetworksApiClient,
    SAPowerNetworksApiClientAuthenticationError,
    _parse_ver,
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
