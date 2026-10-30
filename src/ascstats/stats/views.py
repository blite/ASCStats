"""The dashboard: a portfolio page, plus a page per app with an htmx-swapped deep-dive."""

from django.http import Http404
from django.shortcuts import render

from . import dashboard as d
from . import funnel as f
from . import ratings as rt
from . import revenue as r
from .cache import cached_page
from .charts import ALL, DEFAULT_PERIOD, PERIODS, Period
from .models import App, IngestionRun

WINDOWS = [30, 90, 180, 365]


def _period(end, days: int | str) -> Period:
    if days == ALL:
        return Period.all(end, d.first_day())
    if days not in PERIODS:
        raise Http404("no such period")
    return Period(end, days)


@cached_page
def dashboard(request, days=DEFAULT_PERIOD):
    end = d.last_day()
    if end is None:
        return render(request, "stats/dashboard.html", {"empty": True, "page": "overview"})
    period = _period(end, days)
    tiles = d.tiles(end, period)
    board = d.scoreboard(end)
    context = {
        "page": "overview",
        "end": end,
        "period": period,
        "tiles": tiles,
        "board": board,
        "calendar": d.pipeline_calendar(end),
        "activity": rt.recent_activity(list(App.objects.active()), end, period.days),
        "app_count": App.objects.active().count(),
        "best": max(
            (r for r in board.releases if r["kind"] == "up"),
            key=lambda r: r["impact"].deltas[1].change_pct,
            default=None,
        ),
        "runs": IngestionRun.objects.select_related("app")[:6],
    }
    return render(request, "stats/dashboard.html", context)


@cached_page
def app_page(request, apple_id):
    app = App.objects.active().filter(apple_id=apple_id).first()
    if app is None:
        raise Http404("no such app")
    end = d.last_day()
    context = {
        "page": "app",
        "end": end,
        "app": app,
        "windows": WINDOWS,
        "revenue": r.app_revenue(app, end) if end else None,
    }
    return render(request, "stats/app.html", context)


@cached_page
def chart(request, apple_id, days):
    if days not in WINDOWS:
        raise Http404("no such window")
    app = App.objects.active().filter(apple_id=apple_id).first()
    end = d.last_day()
    if app is None or end is None:
        return render(request, "stats/_chart.html", {"empty": True})
    return render(request, "stats/_chart.html", d.app_detail(app, end, days))


@cached_page
def reviews(request, state="", apple_id=None):
    if state not in rt.REVIEW_STATES:
        raise Http404("no such review state")
    app = None
    if apple_id is not None:
        app = App.objects.active().filter(apple_id=apple_id).first()
        if app is None:
            raise Http404("no such app")
    context = {
        "page": "reviews",
        "app": app,
        "state": state,
        "apps": App.objects.active(),
        **rt.review_queue(state, app),
    }
    return render(request, "stats/reviews.html", context)


@cached_page
def revenue(request, days=DEFAULT_PERIOD):
    end = d.last_day()
    if end is None:
        return render(request, "stats/revenue.html", {"empty": True, "page": "revenue"})
    period = _period(end, days)
    context = {"page": "revenue", "end": end, "period": period, **r.revenue(end, period)}
    return render(request, "stats/revenue.html", context)


@cached_page
def funnel(request):
    end = f.last_day()
    if end is None:
        return render(request, "stats/funnel.html", {"empty": True, "page": "funnel"})
    context = {
        "page": "funnel",
        "end": end,
        "conversion": f.conversion_table(end),
        "strip": f.leak_strip(end),
    }
    return render(request, "stats/funnel.html", context)


@cached_page
def funnel_app(request, apple_id):
    app = App.objects.active().filter(apple_id=apple_id).first()
    end = f.last_day()
    if app is None or end is None:
        return render(request, "stats/_funnel_app.html", {"empty": True})
    context = {"app": app, "trends": f.step_trends(app, end), "mix": f.source_mix(app, end)}
    return render(request, "stats/_funnel_app.html", context)
