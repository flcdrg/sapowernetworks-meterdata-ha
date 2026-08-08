"""
Privacy utilities for safe diagnostics.

This module ensures logs and errors never include raw credentials, tokens,
addresses, or full account identifiers.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+(?:\s+[^\s,;]+)?)"),
    re.compile(r"(?i)(csrf\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)([^\n]+)"),
    re.compile(r"(?i)(password\s*[:=]\s*)([^\s,;]+)"),
)

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DIGIT_ID_PATTERN = re.compile(r"\b\d{8,}\b")


def mask_identifier(value: str, visible: int = 4) -> str:
    """Mask an identifier while keeping a few trailing characters for debugging."""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def redact_text(text: str) -> str:
    """Redact token-like and PII-like patterns from free text."""
    redacted = text
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(r"\1<redacted>", redacted)

    redacted = _EMAIL_PATTERN.sub("<redacted-email>", redacted)

    def _mask_digits(match: re.Match[str]) -> str:
        return mask_identifier(match.group(0))

    return _DIGIT_ID_PATTERN.sub(_mask_digits, redacted)


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Redact mapping values recursively for safe debug output."""
    sensitive_keys = {
        "password",
        "authorization",
        "csrf",
        "cookie",
        "token",
        "username",
        "email",
        "nmi",
        "account",
        "address",
    }

    output: dict[str, Any] = {}
    for key, value in data.items():
        lower_key = key.lower()
        if any(sensitive in lower_key for sensitive in sensitive_keys):
            output[key] = "<redacted>"
            continue

        output[key] = _redact_value(value)

    return output


def _redact_value(value: Any) -> Any:
    """Redact arbitrary nested values while preserving safe structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_mapping({str(key): nested for key, nested in value.items()})
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value
