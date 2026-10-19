import gzip
from datetime import date
from decimal import Decimal

import pytest

from ascstats.stats import synthetic
from ascstats.stats.parsing import (
    DISCOVERY_REPORT,
    DOWNLOADS_REPORT,
    ValidationError,
    parse_analytics,
    parse_sales,
)
from tests.fake_asc import FIXTURES


def test_parse_recorded_sales_report():
    lines = parse_sales((FIXTURES / "sales_2026-10-10.tsv.gz").read_bytes(), date(2026, 10, 10))
    assert len(lines) == 5
    refund = [ln for ln in lines if ln.units < 0]
    assert refund
    assert refund[0].developer_proceeds == Decimal("0.69")
    iap = next(ln for ln in lines if ln.product_type == "IA1")
    assert iap.parent_identifier == "SYN-SNAKE"


def test_sales_rejects_wrong_day_and_missing_columns():
    raw = (FIXTURES / "sales_2026-10-10.tsv.gz").read_bytes()
    with pytest.raises(ValidationError, match="Begin Date"):
        parse_sales(raw, date(2026, 10, 11))
    with pytest.raises(ValidationError, match="missing columns"):
        parse_sales(gzip.compress(b"SKU\tUnits\nX\t1\n"), date(2026, 10, 10))


def test_sales_reports_every_bad_line():
    text = gzip.decompress((FIXTURES / "sales_2026-10-10.tsv.gz").read_bytes()).decode()
    text = text.replace("\t12\t", "\ttwelve\t").replace("\tDE\t", "\tDEU\t")
    with pytest.raises(ValidationError) as exc:
        parse_sales(text.encode(), date(2026, 10, 10))
    assert len(exc.value.problems) == 2


def test_parse_analytics_keeps_extra_columns_as_dimensions():
    (line,) = parse_analytics((FIXTURES / "segment_seg-1.tsv.gz").read_bytes())
    assert line.event == "First-time download"
    assert line.counts == 11
    assert line.unique_counts is None
    assert line.dimensions["Page Type"] == "Product page"
    assert "Counts" not in line.dimensions


def test_synthetic_files_parse_and_are_deterministic():
    day = date(2026, 3, 14)
    assert synthetic.sales_report(day) == synthetic.sales_report(day)
    assert parse_sales(synthetic.sales_report(day), day)
    app = synthetic.APPS[0]
    for name in (DISCOVERY_REPORT, DOWNLOADS_REPORT):
        assert parse_analytics(synthetic.analytics_report(app, name, day))
