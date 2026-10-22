from datetime import date, timedelta

import pytest
from django.core.management import call_command

from ascstats.stats import funnel
from ascstats.stats.models import ConversionMetric, FunnelDay

END = date(2026, 10, 18)
STEP = {s.key: s for s in funnel.STEPS}


def test_wilson_and_poisson_intervals_behave_at_small_counts():
    lo, hi = funnel.wilson(0, 10)
    assert lo == 0
    assert 0.1 < hi < 0.3  # zero successes still has an upper bound
    lo, hi = funnel.wilson(50, 100)
    assert lo < 0.5 < hi
    assert hi - lo < 0.2
    lo, hi = funnel.poisson_rate(30, 20)  # purchases per download can exceed 1
    assert lo < 1.5 < hi


def test_shrinkage_stops_a_fluke_from_leading():
    fluke = funnel.Rate(2, 3, STEP["install"], prior=0.04)
    steady = funnel.Rate(400, 10000, STEP["install"], prior=0.04)
    assert fluke.raw > 0.6
    assert fluke.shrunk < 0.06  # pulled almost all the way to the portfolio
    assert abs(steady.shrunk - steady.raw) < 0.001
    assert fluke.low_volume
    assert not steady.low_volume


def _days(app, start, n, imp, dl, iap=0, units=None):
    ConversionMetric.objects.bulk_create(
        ConversionMetric(app=app, date=start + timedelta(days=i), unique_impressions=imp,
                         unique_page_views=imp // 4, first_time_downloads=dl,
                         units=units if units is not None else dl, iap_units=iap)
        for i in range(n)
    )  # fmt: skip


@pytest.mark.django_db
def test_leak_strip_ranks_the_app_that_fell_below_its_usual_rate(apps):
    healthy, leaking, tiny = apps
    window = END - timedelta(days=funnel.WINDOW - 1)
    before = window - timedelta(days=funnel.REFERENCE)
    _days(healthy, before, funnel.REFERENCE + funnel.WINDOW, imp=1000, dl=40)
    _days(leaking, before, funnel.REFERENCE, imp=1000, dl=50)  # usual 5%
    _days(leaking, window, funnel.WINDOW, imp=1000, dl=25)  # now 2.5%
    _days(tiny, window, funnel.WINDOW, imp=1, dl=0)
    strip = funnel.leak_strip(END)
    first = strip["rows"][0]
    assert first.app == leaking
    assert first.worst == "install"
    install = first.cells["install"]
    assert install.verdict == "below"
    assert 600 < first.lost < 850  # ~1000 x 30 x (5% - 2.5%), a little less after shrinkage
    assert {r.app: r.cells["install"].verdict for r in strip["rows"]}[healthy] == "usual"
    assert {r.app: r.cells["install"].verdict for r in strip["rows"]}[tiny] == "thin"


@pytest.mark.django_db
def test_weeks_without_data_are_gaps_not_zeros(apps):
    app = apps[0]
    _days(app, END - timedelta(days=80), 28, imp=500, dl=20)  # 4 weeks of data
    # ...then about 4 weeks with nothing published, then 3 more weeks of data
    _days(app, END - timedelta(days=20), 21, imp=500, dl=20)
    install = next(p for p in funnel.step_trends(app, END)["panels"] if p["step"].key == "install")
    assert len(install["lines"]) == 2  # two separate runs, not one line dipping to zero
    assert all(d["y"] < install["base"] for d in install["dots"])


@pytest.mark.django_db
def test_source_mix_shares_add_up_and_thin_sources_fold(apps):
    app = apps[0]
    rows = []
    for i in range(14):
        day = END - timedelta(days=i)
        # web referrer brings impressions but no downloads: too thin to have its own line
        for source, imp, dl in [("App Store search", 600, 30), ("App Store browse", 400, 8),
                                ("Web referrer", 20, 0)]:  # fmt: skip
            rows.append(FunnelDay(app=app, date=day, source_type=source, impressions=imp,
                                  unique_impressions=imp, first_time_downloads=dl))  # fmt: skip
    FunnelDay.objects.bulk_create(rows)
    mix = funnel.source_mix(app, END)
    sources = [line["src"] for line in mix["lines"]]
    assert "Web referrer" not in sources
    assert "Other" in sources
    assert mix["folded"] == ["Web referrer"]
    rates = {line["src"]: line["latest"] for line in mix["lines"]}
    assert rates["App Store search"] == "5.0%"
    assert rates["App Store browse"] == "2.0%"
    assert round(sum(s["share"] for s in mix["stacks"])) == 100


@pytest.mark.django_db
def test_funnel_pages_render_on_synthetic_data(client, apps):
    call_command("seed_synthetic", "--days", "60")
    page = client.get("/funnel/")
    assert page.status_code == 200
    assert b"Where&#x27;s the leak" in page.content or b"Where's the leak" in page.content
    detail = client.get("/funnel/app/1000000001/")
    assert b"step rates by week" in detail.content
    assert b"how each source converts" in detail.content
