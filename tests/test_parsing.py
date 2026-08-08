"""Tests for SAPN detailed and summary parsing helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.sapowernetworks.parsing import (
    parse_nem12_content,
    parse_summary_csv,
)


def test_parse_nem12_thirty_minute_row_count() -> None:
    """Thirty-minute intervals should produce 48 readings for one 300 row."""
    values = ",".join("1.0" for _ in range(48))
    sample = "\n".join(
        [
            "100,NEM12,202506081149,SAPN,NEMMCO",
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,30,",
            f"300,20260620,{values},A,,,20260621000000,",
            "900",
        ]
    )

    parsed = parse_nem12_content(sample)
    key = ("20012345678", "E1")
    assert key in parsed
    assert len(parsed[key]) == 48
    assert parsed[key][0].interval_start == datetime(2026, 6, 20, 0, 0, tzinfo=UTC)
    assert parsed[key][47].interval_start == datetime(2026, 6, 20, 23, 30, tzinfo=UTC)


def test_parse_nem12_five_minute_row_count() -> None:
    """Five-minute intervals should produce 288 readings for one 300 row."""
    values = ",".join("0.1" for _ in range(288))
    sample = "\n".join(
        [
            "200,20012345678,E1B1,E1,E1,,METER1,KWH,05,",
            f"300,20260620,{values},A,,,20260621000000,",
            "900",
        ]
    )

    parsed = parse_nem12_content(sample)
    key = ("20012345678", "E1")
    assert key in parsed
    assert len(parsed[key]) == 288


def test_parse_summary_csv_rows() -> None:
    """Summary parser should map each row to a normalized period object."""
    sample = (
        "20012345678,1180281,kWh,A,12/09/2024,12/12/2024,837.000,0.000,1006.000,NN\n"
        "20012345678,1180281,kWh,A,12/12/2024,18/03/2025,1020.000,0.000,1165.000,NN"
    )

    periods = parse_summary_csv(sample)
    assert len(periods) == 2
    assert periods[0].nmi == "20012345678"
    assert periods[0].import_kwh == 837.0
    assert periods[0].export_kwh == 0.0
    assert periods[0].third_value_kwh == 1006.0
    assert periods[0].status == "NN"
