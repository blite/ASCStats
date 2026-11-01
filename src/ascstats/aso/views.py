"""Each app's Competitors and Keywords tabs, and the pages that add and remove them. The
tabs are cached like every page; the manage pages are not, since their forms carry this
session's CSRF token."""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ascstats.stats import dashboard as d
from ascstats.stats.cache import cached_page
from ascstats.stats.models import App
from ascstats.stats.ratings import storefronts
from ascstats.stats.territories import name

from . import competitors as cp
from . import keywords as kw


def _app(apple_id) -> App:
    app = App.objects.active().filter(apple_id=apple_id).first()
    if app is None:
        raise Http404("no such app")
    return app


def _tab(app: App, tab: str) -> dict:
    return {"page": "app", "tab": tab, "end": d.last_day(), "app": app}


def _editable(apple_id) -> App:
    if settings.DEMO_SITE:
        raise Http404("the static demo is read-only")
    return _app(apple_id)


@cached_page
def app_competitors(request, apple_id):
    """An app's Competitors tab: it next to each app it follows."""
    app = _app(apple_id)
    return render(request, "aso/app_competitors.html", _tab(app, "competitors") | cp.compare(app))


def app_competitors_manage(request, apple_id):
    app = _editable(apple_id)
    if request.method == "POST":
        if request.POST.get("remove"):
            rival = get_object_or_404(app.competitors, apple_id=request.POST["remove"])
            cp.remove(app, rival)
            messages.success(request, f"Stopped following {rival.name}.")
            return redirect("app_competitors_manage", apple_id)
        try:
            rival = cp.add(app, request.POST.get("app", ""))
        except cp.CompetitorError as error:
            messages.error(request, str(error))
        else:
            later = "" if rival.snapshots.exists() else " Its ratings start with the next snapshot."
            messages.success(request, f"Following {rival.name}.{later}")
            return redirect("app_competitors_manage", apple_id)
    context = _tab(app, "competitors") | {
        "rivals": app.competitors.all(),
        "max": cp.MAX_PER_APP,
        "typed": request.POST.get("app", ""),
    }
    return render(request, "aso/app_competitors_manage.html", context)


@cached_page
def app_keywords(request, apple_id):
    """An app's Keywords tab: where it ranks for each term it tracks."""
    app = _app(apple_id)
    return render(request, "aso/app_keywords.html", _tab(app, "keywords") | kw.board(app))


@cached_page
def app_keyword(request, apple_id, keyword_id):
    """One keyword's ranks over time and today's top results (an htmx fragment)."""
    app = _app(apple_id)
    keyword = get_object_or_404(app.keywords, pk=keyword_id)
    return render(request, "aso/_keyword.html", kw.detail(app, keyword))


def app_keywords_manage(request, apple_id):
    app = _editable(apple_id)
    if request.method == "POST":
        if request.POST.get("remove"):
            keyword = get_object_or_404(app.keywords, pk=request.POST["remove"])
            kw.remove(app, keyword)
            messages.success(request, f"Stopped tracking “{keyword.term}”.")
            return redirect("app_keywords_manage", apple_id)
        try:
            added = kw.add(app, request.POST.get("terms", ""), request.POST.get("storefront", ""))
        except kw.KeywordError as error:
            messages.error(request, str(error))
        else:
            terms = ", ".join(f"“{k.term}”" for k in added)
            messages.success(request, f"Tracking {terms}. Ranks start with the next daily search.")
            return redirect("app_keywords_manage", apple_id)
    fronts = storefronts(timezone.localdate())
    context = _tab(app, "keywords") | {
        "keywords": app.keywords.all(),
        "max": kw.MAX_PER_APP,
        "storefronts": sorted(((f, name(f)) for f in fronts), key=lambda f: (f[0] != "us", f[1])),
        "typed": request.POST.get("terms", ""),
        "picked": request.POST.get("storefront", "us"),
    }
    return render(request, "aso/app_keywords_manage.html", context)


def export_paths(app: App) -> list[str]:
    """The pages the static demo writes for one app (``export_site``)."""
    tab = f"app/{app.apple_id}/"
    return [f"{tab}competitors/", f"{tab}keywords/"] + [
        f"{tab}keywords/{k.pk}/" for k in app.keywords.all()
    ]
