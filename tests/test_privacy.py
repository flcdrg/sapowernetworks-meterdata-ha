"""Tests for SA Power Networks privacy helpers."""

from __future__ import annotations

from custom_components.sapowernetworks.privacy import redact_mapping, redact_text


def test_redact_text_masks_tokens_email_and_long_numbers() -> None:
    """Free-text redaction should remove common secrets and PII patterns."""
    text = (
        "authorization=Bearer secret-token csrf=abc123 "
        "cookie=session=value; password=hunter2 "
        "user@example.com 20012345678"
    )

    redacted = redact_text(text)

    assert "secret-token" not in redacted
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "user@example.com" not in redacted
    assert "20012345678" not in redacted
    assert "cookie=<redacted>" in redacted


def test_redact_mapping_recurses_into_nested_lists_and_dicts() -> None:
    """Nested lists and dicts should be redacted recursively."""
    payload = {
        "headers": [
            "authorization=Bearer secret-token",
            {"note": "contact user@example.com"},
        ],
        "rpc": {
            "details": [
                "nmi 20012345678",
                ("csrf=abc123", "plain"),
            ]
        },
        "username": "user@example.com",
    }

    redacted = redact_mapping(payload)

    assert redacted["username"] == "<redacted>"
    assert redacted["headers"][0] == "authorization=<redacted>"
    assert redacted["headers"][1]["note"] == "contact <redacted-email>"
    assert redacted["rpc"]["details"][0] == "nmi *******5678"
    assert redacted["rpc"]["details"][1][0] == "csrf=<redacted>"
    assert redacted["rpc"]["details"][1][1] == "plain"
