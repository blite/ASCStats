"""In-app purchase sales per product, on synthetic data only (the repo holds no real sales)."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db.models import Max, Sum
from django.test import Client

from ascstats.stats import products, synthetic
from ascstats.stats.models import IAP_ROWS, App, FxDay, SalesRow

SNAKE, SUDOKU = 1000000001, 1000000002
NOADS = "SYN-SNAKE-NOADS"
TOKEN = "test-token"


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "40")


def _usd_only():
    """Rates for every synthetic currency, so all proceeds convert."""
    days = SalesRow.objects.values_list("report_date", flat=True).distinct()
    rates = {"USD": 1.0, "GBP": 1.25, "EUR": 1.1, "CAD": 0.73, "AUD": 0.66, "JPY": 0.0067,
             "BRL": 0.2}  # fmt: skip
    FxDay.objects.bulk_create([FxDay(date=d, rates=rates) for d in set(days)])


def _row(app, day, sku, title, units, proceeds="2.09", currency="USD"):
    return SalesRow.objects.create(
        app=app, report_date=day, sku=sku, title=title, product_type="IA1", units=units,
        developer_proceeds=Decimal(proceeds), currency_of_proceeds=currency,
        customer_price=Decimal("2.99"), customer_currency=currency, country_code="US",
        device="iPhone", parent_identifier="SYN-SNAKE",
    )  # fmt: skip


def test_one_product_under_two_titles_is_one_row(seeded):
    _usd_only()
    snake = App.objects.get(apple_id=SNAKE)
    end = SalesRow.objects.filter(app=snake).aggregate(d=Max("report_date"))["d"]
    # Apple sometimes reports the product under its bare SKU instead of its display name
    _row(snake, end, NOADS, NOADS, 3)
    _row(snake, end, NOADS, NOADS, -1)  # and a refund
    _row(snake, end - timedelta(days=2), "SYN-SNAKE-COINS", "SYN-SNAKE-COINS", 2)

    sales = products.iap_sales(snake)
    by_sku = {p.sku: p for p in sales["products"]}
    assert set(by_sku) == {NOADS, "SYN-SNAKE-COINS"}
    noads = by_sku[NOADS]
    assert noads.name == "Remove Ads"  # the real name wins over the bare SKU
    assert noads.titles == ["Remove Ads", NOADS]
    seeded_units = SalesRow.objects.filter(IAP_ROWS, app=snake, sku=NOADS, title="Remove Ads")
    expected = seeded_units.aggregate(n=Sum("units"))["n"] + 3 - 1
    assert noads.units == expected
    assert noads.refunds == 1
    assert noads.last_sale == end
    assert noads.first_sale == seeded_units.order_by("report_date").first().report_date
    assert noads.usd > 0
    assert not noads.unconverted
    assert by_sku["SYN-SNAKE-COINS"].name == "SYN-SNAKE-COINS"  # no other name to use
    assert sales["products"][0].sku == NOADS  # best-selling first
    assert sales["units"] == expected + 2
    assert sales["end"] == end


def test_a_period_counts_only_its_days(seeded):
    snake = App.objects.get(apple_id=SNAKE)
    end = SalesRow.objects.filter(app=snake).aggregate(d=Max("report_date"))["d"]
    week = products.iap_sales(snake, days=7)
    assert week["start"] == end - timedelta(days=6)
    in_week = SalesRow.objects.filter(
        IAP_ROWS, app=snake, report_date__gt=end - timedelta(days=7)
    ).aggregate(n=Sum("units"))["n"]
    assert week["units"] == in_week < products.iap_sales(snake)["units"]
    # Without rates, other currencies are kept apart rather than guessed
    assert week["unconverted"]
    assert "USD" not in week["unconverted"]


def test_an_app_without_purchases(seeded):
    sales = products.iap_sales(App.objects.get(apple_id=SUDOKU))
    assert sales["products"] == []
    assert sales["end"] is None


def test_the_mcp_tool_and_the_app_page(seeded, settings, client):
    _usd_only()
    settings.MCP_TOKEN = TOKEN

    def call(**arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "list_iap_sales", "arguments": arguments}}  # fmt: skip
        response = Client().post("/mcp/", json.dumps(body), content_type="application/json",
                                 headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
        return response.json()["result"]

    result = call(app="snake", days=30)
    assert not result["isError"], result
    data = result["structuredContent"]
    assert data["period"]["days"] == 30
    [noads] = data["products"]
    assert noads["sku"] == NOADS
    assert noads["name"] == "Remove Ads"
    assert noads["units"] == data["total_units"] > 0
    assert noads["proceeds_usd"] > 0
    assert call(app="snake", days=0)["isError"]
    none = call(app="sudoku")["structuredContent"]
    assert none["products"] == []
    assert none["period"] is None

    page = client.get(f"/app/{SNAKE}/").content.decode()
    assert "In-app purchases" in page
    assert NOADS in page
    sudoku = client.get(f"/app/{SUDOKU}/").content.decode()
    assert "In-app purchases</h2>" not in sudoku
    assert synthetic.APPS[1].iap is None
