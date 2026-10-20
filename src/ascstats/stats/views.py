"""The dashboard: a portfolio page, plus an htmx-swapped deep-dive per app."""

from django.shortcuts import render

from . import dashboard as d
from .models import App, IngestionRun

WINDOWS = [30, 90, 180, 365]


def dashboard(request):
    end = d.last_day()
    if end is None:
        return render(request, "stats/dashboard.html", {"empty": True})
    tiles = d.tiles(end)
    board = d.scoreboard(end)
    apps = [lane.app for lane in board.lanes] or list(App.objects.filter(is_active=True))
    context = {
        "end": end,
        "tiles": tiles,
        "board": board,
        "calendar": d.pipeline_calendar(end),
        "apps": apps,
        "windows": WINDOWS,
        "app_count": len(apps),
        "best": max(
            (r for r in board.releases if r["kind"] == "up"),
            key=lambda r: r["impact"].deltas[1].change_pct,
            default=None,
        ),
        "runs": IngestionRun.objects.select_related("app")[:6],
    }
    return render(request, "stats/dashboard.html", context)


def chart(request):
    app = App.objects.filter(apple_id=request.GET.get("app")).first()
    end = d.last_day()
    if app is None or end is None:
        return render(request, "stats/_chart.html", {"empty": True})
    days = int(request.GET.get("days", 90))
    days = days if days in WINDOWS else 90
    return render(request, "stats/_chart.html", d.app_detail(app, end, days))
