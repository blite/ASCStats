import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.stats import fx


@pytest.fixture
def no_fx(monkeypatch):
    monkeypatch.setattr(fx, "load", lambda start, end: 0)  # tests never touch the network


@pytest.mark.django_db
def test_export_writes_every_page_under_the_base_path(tmp_path, no_fx):
    out = tmp_path / "site"
    call_command("export_site", "--out", str(out), "--base-path", "/ASCStats/", "--days", "40")
    for path in ("", "7d/", "1d/", "revenue/", "revenue/1d/", "funnel/",
                 "chart/1000000001/365/", "funnel/app/1000000003/"):  # fmt: skip
        assert (out / path / "index.html").is_file(), path
    assert (out / "404.html").is_file()
    assert (out / ".nojekyll").is_file()
    home = (out / "index.html").read_text()
    assert "data through Jun 29, 2026" in home
    assert "A static demo with synthetic data" in home
    assert "/admin/" not in home  # nothing live to link to
    assert "/api/docs" not in home
    links = re.findall(r'(?:href|hx-get)="(/[^"]*)"', home)
    assert links
    assert all(url.startswith("/ASCStats/") for url in links)
    assert 'hx-get="/ASCStats/chart/{app}/{days}/"' in home


@pytest.mark.django_db
def test_export_refuses_anything_but_a_fresh_synthetic_database(tmp_path, no_fx, settings):
    out = tmp_path / "site"
    call_command("seed_synthetic", "--days", "2")
    with pytest.raises(CommandError, match="empty database"):
        call_command("export_site", "--out", str(out))
    settings.ASC_SOURCE = "asc"
    with pytest.raises(CommandError, match="only publishes synthetic data"):
        call_command("export_site", "--out", str(out))
    assert not out.exists()  # nothing written
