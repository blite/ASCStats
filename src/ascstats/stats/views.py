"""One server-rendered page: pick an app and a window, htmx swaps in an SVG chart."""

from datetime import timedelta

from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from .models import (
    DOWNLOAD_ROWS,
    MAC_OS,
    App,
    AppVersion,
    ConversionMetric,
    IngestionRun,
    SalesRow,
)
from .versions import version_impact

WIDTH, HEIGHT, PAD = 720, 260, 36


def dashboard(request):
    apps = App.objects.filter(is_active=True)
    last_runs = IngestionRun.objects.select_related("app")[:8]
    return render(request, "stats/dashboard.html", {"apps": apps, "runs": last_runs})


def chart(request):
    app = App.objects.filter(apple_id=request.GET.get("app")).first()
    days = int(request.GET.get("days", 30))
    if app is None:
        return render(request, "stats/_chart.html", {"empty": True})
    end = timezone.now().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    units = dict(
        SalesRow.objects.filter(DOWNLOAD_ROWS, app=app, report_date__range=(start, end))
        .values_list("report_date")
        .annotate(Sum("units"))
    )
    conversion = dict(
        ConversionMetric.objects.filter(app=app, date__range=(start, end)).values_list(
            "date", "conversion_rate"
        )
    )
    dates = [start + timedelta(days=i) for i in range(days)]
    peak = max([*units.values(), 1])
    step = (WIDTH - 2 * PAD) / len(dates)
    bars, line = [], []
    for i, day in enumerate(dates):
        x = PAD + i * step
        value = units.get(day, 0)
        h = (HEIGHT - 2 * PAD) * value / peak
        bars.append({"x": round(x + 1, 1), "y": round(HEIGHT - PAD - h, 1),
                     "w": round(max(step - 2, 1), 1), "h": round(h, 1),
                     "label": f"{day}: {value} units"})  # fmt: skip
        rate = conversion.get(day)
        if rate is not None:
            y = HEIGHT - PAD - (HEIGHT - 2 * PAD) * min(rate, 1)
            line.append(f"{x + step / 2:.1f},{y:.1f}")
    releases = [
        {
            "x": round(PAD + (v.first_seen - start).days * step, 1),
            "label": ("Mac " if v.platform == MAC_OS else "v") + v.version,
        }
        for v in AppVersion.objects.filter(app=app, first_seen__range=(start + timedelta(1), end))
    ]
    impacts = sorted(
        (i for i in version_impact(app) if i.before_days),
        key=lambda i: i.released_on,
        reverse=True,
    )[:8]
    context = {
        "releases": releases,
        "impacts": impacts,
        "app": app,
        "bars": bars,
        "line": " ".join(line),
        "peak": peak,
        "total": sum(units.values()),
        "start": start,
        "end": end,
        "width": WIDTH,
        "height": HEIGHT,
        "pad": PAD,
        "baseline": HEIGHT - PAD,
    }
    return render(request, "stats/_chart.html", context)
