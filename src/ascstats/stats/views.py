"""The dashboard: a portfolio page, plus an htmx-swapped deep-dive per app."""

from django.shortcuts import render

from . import dashboard as d
from . import funnel as f
from . import revenue as r
from .models import App, IngestionRun

WINDOWS = [30, 90, 180, 365]


def dashboard(request):
    end = d.last_day()
    if end is None:
        return render(request, "stats/dashboard.html", {"empty": True, "page": "overview"})
    tiles = d.tiles(end)
    board = d.scoreboard(end)
    apps = [lane.app for lane in board.lanes] or list(App.objects.filter(is_active=True))
    context = {
        "page": "overview",
        "end": end,
        "tiles": tiles,
        "board": board,
        "calendar": d.pipeline_calendar(end),
        "apps": apps,
        "windows": WINDOWS,
        "app_count": App.objects.filter(is_active=True).count(),
        "best": max(
            (r for r in board.releases if r["kind"] == "up"),
            key=lambda r: r["impact"].deltas[1].change_pct,
            default=None,
        ),
        "runs": IngestionRun.objects.select_related("app")[:6],
    }
    return render(request, "stats/dashboard.html", context)


def chart(request):
    app = App.objects.filter(apple_id=request.GET.get("app"), is_active=True).first()
    end = d.last_day()
    if app is None or end is None:
        return render(request, "stats/_chart.html", {"empty": True})
    days = int(request.GET.get("days", 90))
    days = days if days in WINDOWS else 90
    return render(request, "stats/_chart.html", d.app_detail(app, end, days))


def revenue(request):
    end = d.last_day()
    if end is None:
        return render(request, "stats/revenue.html", {"empty": True, "page": "revenue"})
    return render(request, "stats/revenue.html", {"page": "revenue", **r.revenue(end)})


def funnel(request):
    end = f.last_day()
    if end is None:
        return render(request, "stats/funnel.html", {"empty": True, "page": "funnel"})
    context = {"page": "funnel", "end": end, "strip": f.leak_strip(end)}
    return render(request, "stats/funnel.html", context)


def funnel_app(request):
    app = App.objects.filter(apple_id=request.GET.get("app"), is_active=True).first()
    end = f.last_day()
    if app is None or end is None:
        return render(request, "stats/_funnel_app.html", {"empty": True})
    context = {"app": app, "trends": f.step_trends(app, end), "mix": f.source_mix(app, end)}
    return render(request, "stats/_funnel_app.html", context)
