from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from django.core.management import call_command

from ascstats.stats import fx
from ascstats.stats.models import FxDay
from ascstats.stats.revenue import money, revenue

RATES = {
    "amount": 1.0,
    "base": "USD",
    "rates": {"2026-10-09": {"GBP": 0.8, "EUR": 0.9}, "2026-10-12": {"GBP": 0.75, "EUR": 0.88}},
}


def transport():
    def handler(request):
        assert request.url.host == "api.frankfurter.dev"
        assert request.url.params["base"] == "USD"
        return httpx.Response(200, json=RATES)

    return httpx.MockTransport(handler)


@pytest.mark.django_db
def test_fx_load_is_idempotent_and_converts_as_of():
    assert fx.load(date(2026, 10, 9), date(2026, 10, 12), transport()) == 2  # published days
    assert fx.load(date(2026, 10, 9), date(2026, 10, 12), transport()) == 2
    assert FxDay.objects.count() == 2
    friday = FxDay.objects.get(date=date(2026, 10, 9)).rates
    assert friday["USD"] == 1.0
    assert friday["GBP"] == 1.25  # 0.8 GBP per USD, stored as USD per GBP
    convert = fx.Converter()
    assert convert.usd(Decimal("8"), "GBP", date(2026, 10, 9)) == Decimal("10")
    # Saturday uses Friday's rate; Monday has its own
    assert convert.usd(Decimal("8"), "GBP", date(2026, 10, 10)) == Decimal("10")
    assert round(convert.usd(Decimal("3"), "GBP", date(2026, 10, 12)), 6) == Decimal("4")
    assert convert.usd(Decimal("5"), "USD", date(2026, 10, 1)) == Decimal("5")
    assert convert.usd(Decimal("5"), "KZT", date(2026, 10, 12)) is None  # not an ECB currency
    assert convert.usd(Decimal("5"), "GBP", date(2026, 10, 1)) is None  # before any rate


@pytest.mark.django_db
def test_revenue_sums_every_storefront_in_usd(apps):
    call_command("seed_synthetic", "--days", "40")
    end = date.today() - timedelta(days=1)
    rates = {"USD": 1, "GBP": "1.25", "EUR": "1.1", "CAD": "0.73", "AUD": "0.66",
             "JPY": "0.0067", "BRL": "0.2"}  # fmt: skip
    FxDay.objects.create(
        date=end - timedelta(days=60), rates={c: float(r) for c, r in rates.items()}
    )
    data = revenue(end)
    assert data["total30"] > 0
    assert data["unconverted"] == []
    assert data["apps"][0]["share"] >= data["apps"][-1]["share"]
    assert {a["app"].name for a in data["apps"]} == {
        "Synthetic Snake",
        "Synthetic Sudoku",
        "Synthetic Stargazer",
    }
    assert len(data["bars"]) == 52


@pytest.mark.django_db
def test_revenue_page_renders(client, apps):
    call_command("seed_synthetic", "--days", "10")
    page = client.get("/revenue/")
    assert page.status_code == 200
    assert b"Weekly proceeds by app" in page.content
    assert b"Not converted" in page.content  # no rates loaded: shown, not guessed
    assert b'aria-current="page"' in page.content


def test_money():
    assert money(1234.5) == "$1,234.50"
    assert money(-3) == "-$3.00"
    assert money(25000) == "$25,000"
