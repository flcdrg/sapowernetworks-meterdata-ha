"""Tests for SAPN detailed and summary parsing helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.sapowernetworks.parsing import (
    UnsupportedEnergyUnitError,
    parse_nem12_content,
    parse_nem12_datetime,
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
    assert parsed[key][0].interval_start == datetime(2026, 6, 19, 14, 0, tzinfo=UTC)
    assert parsed[key][47].interval_start == datetime(2026, 6, 20, 13, 30, tzinfo=UTC)


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


@pytest.mark.parametrize(
    ("unit", "reading_value", "expected_kwh"),
    [
        ("Wh", "1000.0", 1.0),
        ("KWH", "1.25", 1.25),
        ("MWh", "0.5", 500.0),
    ],
)
def test_parse_nem12_normalizes_energy_units_to_kwh(
    unit: str,
    reading_value: str,
    expected_kwh: float,
) -> None:
    """NEM12 interval values should be normalized using their declared unit."""
    values = [reading_value, reading_value] + ["" for _ in range(46)]
    sample = "\n".join(
        [
            f"200,20012345678,E1B1,E1,E1,,METER1,{unit},30,",
            f"300,20260620,{','.join(values)},A,,,20260621000000,",
            "900",
        ]
    )

    readings = parse_nem12_content(sample)[("20012345678", "E1")]

    assert readings[0].value_kwh == expected_kwh


def test_parse_nem12_rejects_non_energy_unit_for_kwh_statistics() -> None:
    """Reactive energy must not be mislabeled as kWh."""
    sample = "\n".join(
        [
            "200,20012345678,E1B1,E1,E1,,METER1,kVArh,30,",
            f"300,20260620,{','.join('1.0' for _ in range(48))},A,,,20260621000000,",
            "900",
        ]
    )

    with pytest.raises(UnsupportedEnergyUnitError, match="kVArh"):
        parse_nem12_content(sample)


def test_parse_nem12_datetime_converts_fixed_aest_to_utc() -> None:
    """NEM12 timestamps use AEST year-round, including during daylight saving."""
    assert parse_nem12_datetime("20260620") == datetime(2026, 6, 19, 14, 0, tzinfo=UTC)
    assert parse_nem12_datetime("202606201230") == datetime(
        2026, 6, 20, 2, 30, tzinfo=UTC
    )
    assert parse_nem12_datetime("20261220123045") == datetime(
        2026, 12, 20, 2, 30, 45, tzinfo=UTC
    )


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


def test_parse_summary_csv_normalizes_energy_values_to_kwh() -> None:
    """Summary totals should be normalized using the report's declared unit."""
    sample = "20012345678,1180281,MWh,A,12/09/2024,12/12/2024,1.5,0.25,2.5,NN"

    period = parse_summary_csv(sample)[0]

    assert period.unit == "MWh"
    assert period.import_kwh == 1500.0
    assert period.export_kwh == 250.0
    assert period.third_value_kwh == 2500.0


def test_parse_summary_csv_rejects_non_energy_unit_for_kwh_statistics() -> None:
    """Reactive energy must not be mislabeled as kWh."""
    sample = "20012345678,1180281,kVArh,A,12/09/2024,12/12/2024,1.5,0.25,2.5,NN"

    with pytest.raises(UnsupportedEnergyUnitError, match="kVArh"):
        parse_summary_csv(sample)
