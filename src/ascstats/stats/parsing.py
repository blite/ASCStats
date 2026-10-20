"""Parse and validate raw report files. Pure functions: bytes in, typed rows out."""

from __future__ import annotations

import csv
import gzip
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

SALES_REQUIRED = [
    "SKU",
    "Title",
    "Version",
    "Product Type Identifier",
    "Units",
    "Developer Proceeds",
    "Begin Date",
    "Customer Currency",
    "Country Code",
    "Currency of Proceeds",
    "Apple Identifier",
    "Customer Price",
    "Parent Identifier",
    "Device",
]

ANALYTICS_REQUIRED = ["Date", "App Apple Identifier"]
# The count column is named per report: Counts (engagement, downloads), Crashes, Sessions.
COUNT_COLUMNS = {"Counts": "", "Crashes": "Crash", "Sessions": "Session"}
UNIQUE_COLUMNS = ["Unique Counts", "Unique Devices"]
# Columns promoted to typed fields; everything else stays in AnalyticsLine.dimensions.
ANALYTICS_PROMOTED = {
    "Date",
    "App Name",
    "App Apple Identifier",
    "Event",
    "Download Type",
    "Source Type",
    "Device",
    "Territory",
    "Counts",
    "Unique Counts",
    "Crashes",
    "Sessions",
    "Unique Devices",
}

DISCOVERY_REPORT = "App Store Discovery and Engagement Standard"
DOWNLOADS_REPORT = "App Downloads Standard"
CRASHES_REPORT = "App Crashes"
SESSIONS_REPORT = "App Sessions Standard"
ANALYTICS_REPORTS = [DISCOVERY_REPORT, DOWNLOADS_REPORT, CRASHES_REPORT, SESSIONS_REPORT]


class ValidationError(Exception):
    def __init__(self, problems: list[str]):
        shown = problems[:10]
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        super().__init__("; ".join(shown) + more)
        self.problems = problems


@dataclass
class SalesLine:
    apple_identifier: int
    parent_identifier: str
    sku: str
    title: str
    version: str
    product_type: str
    units: int
    developer_proceeds: Decimal
    currency_of_proceeds: str
    customer_price: Decimal
    customer_currency: str
    country_code: str
    device: str
    begin_date: date


@dataclass
class AnalyticsLine:
    date: date
    app_apple_id: int
    event: str
    source_type: str
    device: str
    territory: str
    counts: int
    unique_counts: int | None
    dimensions: dict[str, str] = field(default_factory=dict)


def _read_table(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = gzip.decompress(raw).decode("utf-8-sig") if raw[:2] == b"\x1f\x8b" else raw.decode()
    first_line = text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    return list(reader.fieldnames or []), rows


def parse_sales(raw: bytes, report_date: date) -> list[SalesLine]:
    header, rows = _read_table(raw)
    missing = [c for c in SALES_REQUIRED if c not in header]
    if missing:
        raise ValidationError([f"sales header missing columns: {', '.join(missing)}"])
    lines, problems = [], []
    for n, row in enumerate(rows, start=2):
        try:
            begin = datetime.strptime(row["Begin Date"], "%m/%d/%Y").date()
            line = SalesLine(
                apple_identifier=int(row["Apple Identifier"]),
                parent_identifier=row["Parent Identifier"].strip(),
                sku=row["SKU"].strip(),
                title=row["Title"].strip(),
                version=row["Version"].strip(),
                product_type=row["Product Type Identifier"].strip(),
                units=int(Decimal(row["Units"])),
                developer_proceeds=Decimal(row["Developer Proceeds"]),
                currency_of_proceeds=row["Currency of Proceeds"].strip(),
                customer_price=Decimal(row["Customer Price"]),
                customer_currency=row["Customer Currency"].strip(),
                country_code=row["Country Code"].strip(),
                device=row["Device"].strip(),
                begin_date=begin,
            )
        except (ValueError, InvalidOperation, KeyError) as exc:
            problems.append(f"line {n}: {exc}")
            continue
        if line.begin_date != report_date:
            problems.append(f"line {n}: Begin Date {line.begin_date} != {report_date}")
        if len(line.country_code) != 2:
            problems.append(f"line {n}: bad country code {line.country_code!r}")
        lines.append(line)
    if problems:
        raise ValidationError(problems)
    return lines


def parse_analytics(raw: bytes) -> list[AnalyticsLine]:
    header, rows = _read_table(raw)
    missing = [c for c in ANALYTICS_REQUIRED if c not in header]
    count_column = next((c for c in COUNT_COLUMNS if c in header), None)
    if count_column is None:
        missing.append(" or ".join(COUNT_COLUMNS))
    if missing:
        raise ValidationError([f"analytics header missing columns: {', '.join(missing)}"])
    unique_column = next((c for c in UNIQUE_COLUMNS if c in header), None)
    lines, problems = [], []
    for n, row in enumerate(rows, start=2):
        try:
            unique = (row.get(unique_column) or "").strip() if unique_column else ""
            line = AnalyticsLine(
                date=date.fromisoformat(row["Date"].strip()),
                app_apple_id=int(row["App Apple Identifier"]),
                event=(
                    row.get("Event") or row.get("Download Type") or COUNT_COLUMNS[count_column]
                ).strip(),
                source_type=(row.get("Source Type") or "").strip(),
                device=(row.get("Device") or "").strip(),
                territory=(row.get("Territory") or "").strip(),
                counts=int(row[count_column]),
                unique_counts=int(unique) if unique else None,
                dimensions={
                    k: v
                    for k, v in row.items()
                    if k not in ANALYTICS_PROMOTED and v not in ("", None)
                },
            )
        except (ValueError, KeyError) as exc:
            problems.append(f"line {n}: {exc}")
            continue
        if line.counts < 0:
            problems.append(f"line {n}: negative Counts {line.counts}")
        lines.append(line)
    if problems:
        raise ValidationError(problems)
    return lines
