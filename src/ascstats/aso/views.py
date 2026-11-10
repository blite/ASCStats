"""Each app's Competitors and Keywords tabs, and the pages that add and remove them. The
tabs are cached like every page; the manage pages are not, since their forms carry this
session's CSRF token."""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ascstats import store
from ascstats.stats import dashboard as d
from ascstats.stats.cache import cached_page
from ascstats.stats.models import App
from ascstats.stats.ratings import storefronts
from ascstats.stats.territories import name

from . import charts as ch
from . import competitors as cp
from . import finder, idea_apps, jobs, rival_keywords
from . import keywords as kw


def _app(apple_id) -> App:
    app = App.objects.researched().filter(apple_id=apple_id).first()
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


@ensure_csrf_cookie  # outside the cache: the Track buttons read the token from the cookie
@cached_page
def app_competitor(request, apple_id, rival_id):
    """One competitor of an app: its listing, the searches it ranks in, and the history of its
    versions and listing changes."""
    app = _app(apple_id)
    rival = get_object_or_404(app.competitors, apple_id=rival_id)
    context = _tab(app, "competitors") | cp.profile(app, rival)
    return render(request, "aso/app_competitor.html", context)


def app_competitor_stores(request, apple_id, rival_id):
    """The Read all stores button on a competitor's page: POST starts the ``read_stores`` job
    and answers its id; GET ``?job=<id>`` answers whether it finished. The page is cached,
    so the button asks with this session's CSRF token from its cookie."""
    app = _editable(apple_id)
    rival = get_object_or_404(app.competitors, apple_id=rival_id)
    if request.method == "POST":
        try:
            return JsonResponse({"job": cp.start_store_read(rival)})
        except cp.CompetitorError as error:
            return JsonResponse({"error": str(error)}, status=429)
    try:
        result = jobs.read_stores.get_result(request.GET.get("job", ""))
    except Exception:  # an unknown or malformed id: nothing to wait for
        return JsonResponse({"done": True})
    return JsonResponse({"done": result.is_finished, "status": str(result.status)})


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


@require_POST
def app_competitors_follow(request, apple_id):
    """Follow one of a keyword's top results, from the + in its row's menu on the Keywords
    tab. Its name, seller and icon come from the search that listed it; the rest of its
    listing fills in with the next daily snapshot."""
    app = _editable(apple_id)
    keyword = get_object_or_404(app.keywords, pk=request.POST.get("keyword", ""))
    top = keyword.snapshots.order_by("-date").values_list("top", flat=True).first() or []
    rid = request.POST.get("id", "")
    hit = next((r for r in top if str(r["id"]) == rid), None)
    if hit is None:
        return JsonResponse({"error": "That app is not in this keyword's results."}, status=400)
    try:
        rival = cp.add(app, rid, listing=kw.as_listing(hit))
    except cp.CompetitorError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"name": rival.name})


@require_POST
def app_competitors_unfollow(request, apple_id):
    """Stop following a competitor, from Following beside a search result on the Keywords tab.
    Its history is kept, so Track picks up where it left off."""
    app = _editable(apple_id)
    rival = app.competitors.filter(apple_id=request.POST.get("id", "")).first()
    if rival is None:
        return JsonResponse({"error": f"{app.name} doesn't follow that app."}, status=400)
    cp.remove(app, rival)
    return JsonResponse({"name": rival.name})


def idea_new(request):
    """Start an idea: an app not built yet, to research its keywords and competitors."""
    if settings.DEMO_SITE:
        raise Http404("the static demo is read-only")
    if request.method == "POST":
        try:
            idea = idea_apps.create(request.POST.get("name", ""))
        except idea_apps.IdeaError as error:
            messages.error(request, str(error))
        else:
            return redirect("app_keywords_manage", idea.apple_id)
    context = {"page": "idea_new", "typed": request.POST.get("name", "")}
    return render(request, "aso/idea_new.html", context)


def app_make_real(request, apple_id):
    """Turn an idea into the app it became, keeping its keywords and competitors."""
    idea = _editable(apple_id)
    if not idea.is_idea:
        return redirect("app_keywords", apple_id)
    if request.method == "POST":
        try:
            app = idea_apps.make_real(idea, request.POST.get("app", ""))
        except idea_apps.IdeaError as error:
            messages.error(request, str(error))
        else:
            return redirect("app_keywords", app.apple_id)
    context = _tab(idea, "real") | {"typed": request.POST.get("app", "")}
    return render(request, "aso/app_make_real.html", context)


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
    # an idea ranks for nothing, so its tiles would only count zeros
    context["kw_summary"] = kw.summary([app], front) if front and not app.is_idea else None
    context |= {"rk": rival_keywords.board(app, front), "scan_terms": rival_keywords.SCAN_TERMS}
    return render(request, "aso/app_keywords.html", context)


@cached_page
def app_keyword(request, apple_id, keyword_id):
    """One keyword's ranks over time and today's top results (an htmx fragment)."""
    app = _app(apple_id)
    keyword = get_object_or_404(app.keywords, pk=keyword_id)
    return render(request, "aso/_keyword.html", kw.detail(app, keyword) | {"app": app})


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


def _suggest(request, app, storefront, found: dict | None, deep: bool = False):
    context = {"app": app, "front": storefront, "deep": deep} | (found or {})
    return render(request, "aso/_suggest.html", context | {"looked": found is not None})


def app_keyword_suggest(request, apple_id, storefront):
    """The Suggestions card: today's look if there was one, else the button that looks. Not
    cached: it carries this session's CSRF token, and the look is kept for the day anyway."""
    app = _editable(apple_id)
    found = finder.cached(app, storefront, deep=True)
    deep = found is not None
    return _suggest(
        request, app, storefront, found or finder.cached(app, storefront, deep=False), deep
    )


@require_POST
def app_keyword_suggest_look(request, apple_id, storefront):
    """Ask the store for suggestions now: a few seconds, or longer for a deep look."""
    app = _editable(apple_id)
    deep = bool(request.POST.get("deep"))
    return _suggest(request, app, storefront, finder.look(app, storefront, deep=deep), deep)


@require_POST
def app_keyword_suggest_track(request, apple_id, storefront):
    """Track one suggested term, then show its row as tracked."""
    app = _editable(apple_id)
    term = request.POST.get("term", "")
    try:
        keyword = kw.add(app, term.replace(",", " "), storefront)[0]  # one term, never a list
    except kw.KeywordError as error:
        return render(request, "aso/_suggest_tracked.html", {"term": term, "error": str(error)})
    jobs.search_keyword.enqueue(keyword.pk)
    return render(request, "aso/_suggest_tracked.html", {"term": keyword.term})


FINISHED_LATELY = 600  # seconds a finished scan is still announced under the form


def app_keyword_scan(request, apple_id, storefront):
    """The scan form above the Competitors' keywords card: GET shows it (and polls while a
    scan runs), POST starts one. Not cached: it carries this session's CSRF token."""
    app = _editable(apple_id)
    error = ""
    if request.method == "POST":
        rival = app.competitors.filter(apple_id=request.POST.get("competitor", "")).first()
        try:
            if rival is None:
                raise rival_keywords.ScanError("Pick one of the apps it follows.")
            rival_keywords.start(app, rival, storefront)
        except rival_keywords.ScanError as error_:
            error = str(error_)
    context = rival_keywords.status(app, storefront)
    # A poll (sent only while a scan ran) announces the scans that finished since
    since = timezone.now() - timedelta(seconds=FINISHED_LATELY)
    context["finished"] = [
        s for _, s in context["rows"]
        if request.GET.get("polled") and s and s.finished_at and s.finished_at >= since
    ]  # fmt: skip
    return render(request, "aso/_scan.html", context | {"error": error})


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


def charts_home(request):
    """/charts/: the category fetched last, else Games in the US store."""
    return redirect("charts", *ch.latest())


@ensure_csrf_cookie  # outside the cache: Fetch and Track read the token from the cookie
@cached_page
def charts(request, storefront, genre, day=None, days=1):
    """The Top charts page: a category's free, paid and grossing charts side by side as
    fetched on one day, each app's movement since an earlier fetch, and what the names share."""
    try:
        context = ch.page(storefront, genre, day, days)
    except ch.ChartError as error:
        raise Http404(str(error)) from None
    return render(request, "aso/charts.html", {"page": "charts", "end": d.last_day()} | context)


@require_POST
def charts_fetch(request, storefront, genre):
    """Fetch (or Refetch): ask the store for the category's three charts while you wait,
    about three seconds; the page reloads to show them."""
    if settings.DEMO_SITE:
        raise Http404("the static demo is read-only")
    try:
        ch.fetch(storefront, genre)
    except ch.ChartError as error:
        raise Http404(str(error)) from None
    except store.StoreError:
        return JsonResponse({"error": "The App Store didn't answer; try again."}, status=502)
    return JsonResponse({"ok": True})


@require_POST
def charts_track(request):
    """Track beside a chart's app: follow it as a competitor of one of your apps or ideas."""
    if settings.DEMO_SITE:
        raise Http404("the static demo is read-only")
    app = App.objects.researched().filter(apple_id=request.POST.get("app", "") or 0).first()
    if app is None:
        return JsonResponse({"error": "Pick one of your apps or ideas."}, status=400)
    try:
        rival = ch.track(app, int(request.POST.get("id", "") or 0))
    except (cp.CompetitorError, ValueError) as error:
        return JsonResponse({"error": str(error) or "No such app."}, status=400)
    return JsonResponse({"name": rival.name, "app": app.name})


def dashboard_tiles(apps: list[App]) -> tuple[str, kw.Summary] | None:
    """The Overview's keyword tiles, across every app; none until some app tracks a keyword."""
    found = kw.summary(apps)
    return ("aso/_keyword_tiles.html", found) if found.tracked else None


def export_paths(app: App) -> list[str]:
    """The pages the static demo writes for one app (``export_site``)."""
    tab = f"app/{app.apple_id}/"
    return (
        [f"{tab}competitors/", f"{tab}keywords/"]
        + [f"{tab}competitors/{c.apple_id}/" for c in app.competitors.all()]
        + [f"{tab}keywords/{f.code}/" for f in kw.storefronts(app)]
        + [f"{tab}keywords/{k.pk}/" for k in app.keywords.all()]
    )
