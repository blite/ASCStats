"""Each app's Competitors and Keywords tabs, and the pages that add and remove them. The
tabs are cached like every page; the manage pages are not, since their forms carry this
session's CSRF token."""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ascstats.stats import dashboard as d
from ascstats.stats.cache import cached_page
from ascstats.stats.models import App
from ascstats.stats.ratings import storefronts
from ascstats.stats.territories import name

from . import competitors as cp
from . import ideas, jobs
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


@ensure_csrf_cookie  # outside the cache: the delete form reads its token from the cookie
@cached_page
def app_keywords(request, apple_id, storefront=None):
    """An app's Keywords tab: where it ranks for each term it tracks, one storefront at a time
    (the first one, the US if tracked, without ``storefront``)."""
    app = _app(apple_id)
    fronts = kw.storefronts(app)
    codes = [f.code for f in fronts]
    if storefront is not None and storefront not in codes:  # say, one the browser remembered
        return redirect("app_keywords", apple_id)  # that the app no longer tracks
    front = storefront or (codes[0] if codes else None)
    context = _tab(app, "keywords") | kw.board(app, front)
    context |= {"fronts": fronts, "front": front, "total": sum(f.count for f in fronts)}
    return render(request, "aso/app_keywords.html", context)


@cached_page
def app_keyword(request, apple_id, keyword_id):
    """One keyword's ranks over time and today's top results (an htmx fragment)."""
    app = _app(apple_id)
    keyword = get_object_or_404(app.keywords, pk=keyword_id)
    return render(request, "aso/_keyword.html", kw.detail(app, keyword))


@require_POST
def app_keywords_delete(request, apple_id):
    """Stop tracking the keywords picked on the Keywords tab, then show the tab again."""
    app = _editable(apple_id)
    picked = [pk for pk in request.POST.getlist("delete") if pk.isdigit()]
    for keyword in app.keywords.filter(pk__in=picked):
        kw.remove(app, keyword)
    front = request.POST.get("storefront", "")
    if app.keywords.filter(storefront=front).exists():  # back to that storefront's tab
        return redirect("app_keywords_front", apple_id, front)
    return redirect("app_keywords", apple_id)


def _ideas(request, app, storefront, found: dict | None, deep: bool = False):
    context = {"app": app, "front": storefront, "deep": deep} | (found or {})
    return render(request, "aso/_ideas.html", context | {"looked": found is not None})


def app_keyword_ideas(request, apple_id, storefront):
    """The Suggestions card: today's look if there was one, else the button that looks. Not
    cached: it carries this session's CSRF token, and the look is kept for the day anyway."""
    app = _editable(apple_id)
    found = ideas.cached(app, storefront, deep=True)
    deep = found is not None
    return _ideas(
        request, app, storefront, found or ideas.cached(app, storefront, deep=False), deep
    )


@require_POST
def app_keyword_ideas_look(request, apple_id, storefront):
    """Ask the store for ideas now: a few seconds, or longer for a deep look."""
    app = _editable(apple_id)
    deep = bool(request.POST.get("deep"))
    return _ideas(request, app, storefront, ideas.look(app, storefront, deep=deep), deep)


@require_POST
def app_keyword_ideas_track(request, apple_id, storefront):
    """Track one suggested term, then show its row as tracked."""
    app = _editable(apple_id)
    term = request.POST.get("term", "")
    try:
        keyword = kw.add(app, term.replace(",", " "), storefront)[0]  # one term, never a list
    except kw.KeywordError as error:
        return render(request, "aso/_idea_tracked.html", {"term": term, "error": str(error)})
    jobs.search_keyword.enqueue(keyword.pk)
    return render(request, "aso/_idea_tracked.html", {"term": keyword.term})


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
            for k in added:
                jobs.search_keyword.enqueue(k.pk)
            terms = ", ".join(f"“{k.term}”" for k in added)
            messages.success(request, f"Tracking {terms}. Searching now: ranks show in a minute.")
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
    return (
        [f"{tab}competitors/", f"{tab}keywords/"]
        + [f"{tab}keywords/{f.code}/" for f in kw.storefronts(app)]
        + [f"{tab}keywords/{k.pk}/" for k in app.keywords.all()]
    )
