"""Data parsing helpers for SA Power Networks payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

NEM12_HEADER_MIN_FIELDS = 9
NEM12_INTERVAL_MIN_FIELDS = 4
SUMMARY_MIN_FIELDS = 10


@dataclass(frozen=True)
class IntervalReading:
    """Single interval reading from a NEM12 300 record."""

    interval_start: datetime
    interval_end: datetime
    value_kwh: float | None
    quality_method: str


@dataclass(frozen=True)
class SummaryPeriod:
    """Single period from summary-format CSV."""

    nmi: str
    meter_serial: str
    unit: str
    period_start: date
    period_end: date
    import_kwh: float
    export_kwh: float
    third_value_kwh: float
    status: str


def parse_nem12_datetime(value: str) -> datetime | None:
    """Parse supported NEM12 date/datetime formats."""
    value = value.strip()
    formats = {
        8: "%Y%m%d",
        12: "%Y%m%d%H%M",
        14: "%Y%m%d%H%M%S",
    }
    fmt = formats.get(len(value))
    if fmt is None:
        return None
    try:
        return datetime.strptime(value, fmt).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_nem12_content(
    content: str,
) -> dict[tuple[str, str], list[IntervalReading]]:
    """Parse detailed NEM12 content into per-(NMI,suffix) interval readings."""
    parsed: dict[tuple[str, str], list[IntervalReading]] = {}
    current_nmi: str | None = None
    current_suffix: str | None = None
    current_interval_mins: int | None = None

    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(",")
        indicator = parts[0]

        if indicator == "200" and len(parts) >= NEM12_HEADER_MIN_FIELDS:
            current_nmi = parts[1]
            current_suffix = parts[4]
            try:
                current_interval_mins = int(parts[8])
            except ValueError:
                current_interval_mins = None
            if current_nmi and current_suffix:
                parsed.setdefault((current_nmi, current_suffix), [])
            continue

        if (
            indicator == "300"
            and current_nmi
            and current_suffix
            and current_interval_mins
            and current_interval_mins > 0
            and len(parts) >= NEM12_INTERVAL_MIN_FIELDS
        ):
            reading_date = parse_nem12_datetime(parts[1])
            if reading_date is None:
                continue
            num_intervals = 1440 // current_interval_mins
            quality = parts[2 + num_intervals] if len(parts) > 2 + num_intervals else ""
            delta = timedelta(minutes=current_interval_mins)

            for i in range(num_intervals):
                start = reading_date + (i * delta)
                value_text = parts[2 + i] if len(parts) > 2 + i else ""
                value = _parse_float(value_text)
                parsed[(current_nmi, current_suffix)].append(
                    IntervalReading(
                        interval_start=start,
                        interval_end=start + delta,
                        value_kwh=value,
                        quality_method=quality,
                    )
                )

    return parsed


def parse_summary_csv(content: str) -> list[SummaryPeriod]:
    """Parse accumulated summary CSV into normalized period rows."""
    output: list[SummaryPeriod] = []
    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        parts = [segment.strip() for segment in line.split(",")]
        if len(parts) < SUMMARY_MIN_FIELDS:
            continue
        start = _parse_au_date(parts[4])
        end = _parse_au_date(parts[5])
        if start is None or end is None:
            continue

        output.append(
            SummaryPeriod(
                nmi=parts[0],
                meter_serial=parts[1],
                unit=parts[2],
                period_start=start,
                period_end=end,
                import_kwh=_parse_float(parts[6]) or 0.0,
                export_kwh=_parse_float(parts[7]) or 0.0,
                third_value_kwh=_parse_float(parts[8]) or 0.0,
                status=parts[9],
            )
        )

    return output


def _parse_float(value: str) -> float | None:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_au_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()  # noqa: DTZ007
    except ValueError:
        return None
