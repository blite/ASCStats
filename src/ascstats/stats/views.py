"""The dashboard: a portfolio page, plus a page per app with an htmx-swapped deep-dive."""

from django.conf import settings
from django.contrib.auth import logout
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from . import dashboard as d
from . import extensions
from . import funnel as f
from . import rating_trends as rtr
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
    apps = list(App.objects.active())
    context = {
        "page": "overview",
        "end": end,
        "period": period,
        "tiles": tiles,
        "board": board,
        "calendar": d.pipeline_calendar(end),
        "activity": rt.recent_activity(apps, end, period.days),
        "app_count": len(apps),
        "more_tiles": [t for hook in extensions.dashboard_tiles if (t := hook(apps))],
        "best": max(
            (r for r in board.releases if r["kind"] == "up"),
            key=lambda r: r["impact"].deltas[1].change_pct,
            default=None,
        ),
        "runs": IngestionRun.objects.select_related("app")[:6],
    }
    return render(request, "stats/dashboard.html", context)


def _app(apple_id) -> App:
    app = App.objects.active().filter(apple_id=apple_id).first()
    if app is None:
        raise Http404("no such app")
    return app


@cached_page
def app_page(request, apple_id):
    """An app's Stats tab: downloads, revenue and the store funnel together."""
    app = _app(apple_id)
    end = d.last_day()
    context = {
        "page": "app",
        "tab": "stats",
        "end": end,
        "app": app,
        "windows": WINDOWS,
        "tiles": d.tiles(end, Period(end, 30), app=app)[:2] if end else [],
        "revenue": r.app_revenue(app, end) if end else None,
        "funnel": f.app_funnel(app, funnel_end) if (funnel_end := f.last_day()) else None,
    }
    return render(request, "stats/app.html", context)


@cached_page
def app_ratings(request, apple_id):
    """An app's Ratings tab: totals by country, and a chart of one country (or all)."""
    app = _app(apple_id)
    context = {
        "page": "app",
        "tab": "ratings",
        "end": d.last_day(),
        "app": app,
        "ratings": rtr.countries(app),
        "windows": rtr.WINDOW_LABELS,
        "default_window": rtr.DEFAULT_WINDOW,
    }
    return render(request, "stats/app_ratings.html", context)


@cached_page
def app_ratings_chart(request, apple_id, country, window):
    if window not in rtr.WINDOWS:
        raise Http404("no such time frame")
    if country != rtr.ALL_COUNTRIES and not (len(country) == 2 and country.isalpha()):
        raise Http404("no such storefront")
    app = _app(apple_id)
    return render(request, "stats/_ratings_chart.html", rtr.history(app, country.lower(), window))


@cached_page
def app_reviews(request, apple_id, state=""):
    """An app's Reviews tab: the review queue for that app alone."""
    if state not in rt.REVIEW_STATES:
        raise Http404("no such review state")
    app = _app(apple_id)
    context = {
        "page": "app",
        "tab": "reviews",
        "end": d.last_day(),
        "app": app,
        "state": state,
        **rt.review_queue(state, app),
    }
    return render(request, "stats/app_reviews.html", context)


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
def reviews(request, state=""):
    """Every app's reviews in one queue; one app's are on its Reviews tab."""
    if state not in rt.REVIEW_STATES:
        raise Http404("no such review state")
    context = {"page": "reviews", "state": state, **rt.review_queue(state)}
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
def app_funnel(request, apple_id):
    """The Stats tab's funnel over time: weekly step rates and the traffic sources."""
    app = App.objects.active().filter(apple_id=apple_id).first()
    end = f.last_day()
    if app is None or end is None:
        return render(request, "stats/_funnel_app.html", {"empty": True})
    context = {"app": app, "trends": f.step_trends(app, end), "mix": f.source_mix(app, end)}
    return render(request, "stats/_funnel_app.html", context)


def arrange_apps(request):
    """Re-order the sidebar's apps and hide the ones not worth a row. Uncached: it carries a
    CSRF token. Each button posts one move; a drag posts the whole order."""
    apps = App.objects.active()
    if request.method == "POST":
        listed = list(apps.filter(is_hidden=False).values_list("pk", flat=True))
        if order := request.POST.getlist("order"):
            App.objects.arrange([pk for pk in order if pk.isdigit()])
            return HttpResponse(status=204)
        for step, field in ((-1, "up"), (1, "down")):
            if (pk := request.POST.get(field, "")).isdigit() and int(pk) in listed:
                i = listed.index(int(pk))
                if 0 <= i + step < len(listed):
                    listed[i], listed[i + step] = listed[i + step], listed[i]
                    App.objects.arrange(listed)
        for field, hidden in (("hide", True), ("show", False)):
            if (pk := request.POST.get(field, "")).isdigit():
                app = get_object_or_404(apps, pk=pk)
                app.is_hidden = hidden
                if not hidden:  # back at the end of the list, where it is easy to find
                    App.objects.arrange([*listed, app.pk])
                app.save(update_fields=["is_hidden"])
        return redirect("arrange_apps")
    context = {
        "page": "apps",
        "listed": apps.filter(is_hidden=False),
        "hidden": apps.filter(is_hidden=True),
    }
    return render(request, "stats/arrange_apps.html", context)


def sign_out(request):
    """GET asks, POST signs out. The sidebar links here instead of posting a form itself,
    because cached pages cannot carry a per-session CSRF token."""
    if request.method == "POST":
        logout(request)
        return redirect(settings.LOGIN_URL)
    return render(request, "stats/logout.html")
