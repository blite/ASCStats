import pytest
from django.contrib.auth.models import User
from django.core.management import call_command


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "5")


def test_health(client, db):
    assert client.get("/api/health").json() == {"ok": True}


def test_apps_endpoint(client, seeded):
    apps = client.get("/api/apps").json()
    assert {a["name"] for a in apps} == {
        "Synthetic Snake",
        "Synthetic Sudoku",
        "Synthetic Stargazer",
    }


def test_daily_sales_endpoint(client, seeded):
    days = client.get("/api/apps/1000000002/sales/daily").json()
    assert len(days) == 5
    assert all(d["units"] > 0 and "USD" in d["proceeds"] for d in days)
    assert client.get("/api/apps/42/sales/daily").status_code == 404


def test_conversion_endpoint(client, seeded):
    rows = client.get("/api/apps/1000000001/conversion").json()
    assert len(rows) == 5
    assert rows[0]["page_views"] > 0


def test_dashboard_and_chart(client, seeded):
    assert b"hx-get" in client.get("/").content
    chart = client.get("/chart/", {"app": 1000000001, "days": 30})
    assert chart.status_code == 200
    assert b"<rect" in chart.content
    assert b"<polyline" in chart.content


def test_admin_lists_synthetic_rows(client, seeded):
    User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.login(username="admin", password="pw")
    for model in (
        "app",
        "salesrow",
        "analyticsrow",
        "conversionmetric",
        "appversion",
        "ingestionrun",
    ):
        assert client.get(f"/admin/stats/{model}/").status_code == 200
