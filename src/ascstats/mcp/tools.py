"""What the MCP server lets an agent ask: your apps, their competitors, one competitor in
depth, which languages the market supports, the keywords you track, and which in-app
purchases sell best. Every tool only
reads; the one live request is ``localization_report``'s Lookup of your own app.

Each tool takes plain JSON arguments and answers a JSON-ready dict. An app is named by its
Apple ID or by a part of its name, so an agent can say "Cloudless Cam"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.db.models import Max, Sum

from ascstats import store
from ascstats.aso import competitors as cp
from ascstats.aso import keywords as kw
from ascstats.aso import rival_keywords
from ascstats.aso.models import Competitor, CompetitorSnapshot
from ascstats.stats import products as iap
from ascstats.stats.models import DOWNLOAD_ROWS, App, RatingSnapshot, SalesRow
from ascstats.stats.territories import name as country_name

DOWNLOAD_DAYS = 90  # your downloads by country, over this many days
RELEASES = 10  # a competitor's latest releases in get_competitor
DESCRIPTION_CHARS = 4000

# The language(s) people read in each storefront, as Lookup's ``languageCodesISO2A`` writes
# them. A storefront with two splits its weight between them; one not listed counts as
# "other" (English is the App Store's fallback there).
STORE_LANGUAGES = {
    "ae": ["AR"],
    "ar": ["ES"],
    "at": ["DE"],
    "au": ["EN"],
    "be": ["NL", "FR"],
    "bg": ["BG"],
    "bh": ["AR"],
    "bo": ["ES"],
    "br": ["PT"],
    "by": ["RU"],
    "ca": ["EN", "FR"],
    "ch": ["DE", "FR", "IT"],
    "cl": ["ES"],
    "cn": ["ZH"],
    "co": ["ES"],
    "cr": ["ES"],
    "cy": ["EL"],
    "cz": ["CS"],
    "de": ["DE"],
    "dk": ["DA"],
    "do": ["ES"],
    "dz": ["AR", "FR"],
    "ec": ["ES"],
    "ee": ["ET"],
    "eg": ["AR"],
    "es": ["ES"],
    "fi": ["FI"],
    "fr": ["FR"],
    "gb": ["EN"],
    "gr": ["EL"],
    "gt": ["ES"],
    "hk": ["ZH"],
    "hn": ["ES"],
    "hr": ["HR"],
    "hu": ["HU"],
    "id": ["ID"],
    "ie": ["EN"],
    "il": ["HE"],
    "in": ["EN", "HI"],
    "iq": ["AR"],
    "is": ["IS"],
    "it": ["IT"],
    "jo": ["AR"],
    "jp": ["JA"],
    "kr": ["KO"],
    "kw": ["AR"],
    "kz": ["RU", "KK"],
    "lb": ["AR"],
    "lt": ["LT"],
    "lu": ["FR", "DE"],
    "lv": ["LV"],
    "ly": ["AR"],
    "ma": ["AR", "FR"],
    "md": ["RO"],
    "mx": ["ES"],
    "my": ["MS"],
    "ni": ["ES"],
    "nl": ["NL"],
    "no": ["NB"],
    "nz": ["EN"],
    "om": ["AR"],
    "pa": ["ES"],
    "pe": ["ES"],
    "ph": ["EN"],
    "pl": ["PL"],
    "pt": ["PT"],
    "py": ["ES"],
    "qa": ["AR"],
    "ro": ["RO"],
    "rs": ["SR"],
    "ru": ["RU"],
    "sa": ["AR"],
    "se": ["SV"],
    "sg": ["EN"],
    "si": ["SL"],
    "sk": ["SK"],
    "sv": ["ES"],
    "th": ["TH"],
    "tn": ["AR", "FR"],
    "tr": ["TR"],
    "tw": ["ZH"],
    "ua": ["UK"],
    "us": ["EN"],
    "uy": ["ES"],
    "uz": ["UZ", "RU"],
    "ve": ["ES"],
    "vn": ["VI"],
    "ye": ["AR"],
    "za": ["EN"],
}
# Lookup's codes for the same language, folded together
SAME_LANGUAGE = {"NO": "NB", "IW": "HE", "IN": "ID"}


class ToolError(Exception):
    """A tool was asked something it cannot answer: shown to the agent as the tool's result."""


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    run: Any

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.properties,
                "required": self.required,
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        }


APP_ARG = {
    "app": {
        "type": "string",
        "description": 'Your app: its Apple ID, or a part of its name ("Cloudless Cam").',
    }
}
COMPETITOR_ARG = {
    "competitor": {
        "type": "string",
        "description": "One of the app's competitors: its Apple ID, or a part of its name.",
    }
}


def _app(text) -> App:
    text = str(text or "").strip()
    apps = App.objects.researched()
    if text.lstrip("-").isdigit():
        found = apps.filter(apple_id=int(text)).first()
        if found:
            return found
        raise ToolError(f"No app with Apple ID {text}. list_apps names them.")
    matches = list(apps.filter(name__icontains=text)) if text else []
    exact = [a for a in matches if a.name.lower() == text.lower()]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]
    if not matches:
        raise ToolError(f"No app matches {text!r}. list_apps names them.")
    names = ", ".join(f"{a.name} ({a.apple_id})" for a in matches)
    raise ToolError(f"{text!r} matches several apps: {names}. Give the Apple ID.")


def _competitor(app: App, text) -> Competitor:
    text = str(text or "").strip()
    rivals = app.competitors.all()
    if text.isdigit():
        found = rivals.filter(apple_id=int(text)).first()
        if found:
            return found
        raise ToolError(f"{app.name} follows no competitor with Apple ID {text}.")
    matches = list(rivals.filter(name__icontains=text)) if text else []
    exact = [c for c in matches if c.name.lower() == text.lower()]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]
    if not matches:
        raise ToolError(f"{app.name} follows no competitor matching {text!r}.")
    names = ", ".join(f"{c.name} ({c.apple_id})" for c in matches)
    raise ToolError(f"{text!r} matches several competitors: {names}. Give the Apple ID.")


def _language(code: str) -> str:
    code = code.upper()
    return SAME_LANGUAGE.get(code, code)


def _languages(codes) -> list[str]:
    return sorted({_language(c) for c in codes or []})


def _named(codes) -> list[dict]:
    return [{"code": c, "name": cp.language_name(c)} for c in codes]


def _storefront_ratings(snaps) -> dict[str, dict]:
    """{storefront: {count, average, date}} from each storefront's latest fresh snapshot."""
    return {
        front: {"count": count, "average": round(stars / count, 2) if count else None, "date": on}
        for front, (on, count, stars) in cp._latest(snaps).items()
    }


# --- the tools --------------------------------------------------------------------------------


def list_apps() -> dict:
    apps = App.objects.researched().order_by("is_idea", "sort_order", "name")
    return {
        "apps": [
            {
                "apple_id": a.apple_id,
                "name": a.name,
                "idea": a.is_idea,  # research only: not on the App Store
                "primary_genre": a.primary_genre,
                "competitors": a.competitors.count(),
                "keywords": a.keywords.count(),
            }
            for a in apps
        ]
    }


def list_competitors(app: str) -> dict:
    found = _app(app)
    compared = cp.compare(found)
    you, rivals = compared["members"][0], compared["members"][1:]
    stores_by_rival: dict[int, int] = {}
    for competitor_id, _front in (
        CompetitorSnapshot.objects.filter(
            competitor__in=[m.competitor for m in rivals], count__gt=0
        )
        .values_list("competitor_id", "storefront")
        .distinct()
    ):
        stores_by_rival[competitor_id] = stores_by_rival.get(competitor_id, 0) + 1
    return {
        "app": {
            "apple_id": found.apple_id,
            "name": found.name,
            "idea": found.is_idea,
            "us_ratings": you.count,
            "us_average": you.average,
            "version": you.version,
        },
        "ratings_as_of": compared["end"],
        "note": "Busiest first: new US ratings in the last 30 days, then US ratings in all. "
        "languages are the languages the app itself supports (Lookup's languageCodesISO2A), "
        "not which store listings are translated. accessibility is what the developer "
        "declares on the store page: [] none declared, null not read yet.",
        "competitors": [
            {
                "apple_id": m.apple_id,
                "name": m.name,
                "seller": m.seller,
                "subtitle": m.subtitle,
                "price": m.price,
                "primary_genre": m.competitor.primary_genre,
                "us_ratings": m.count,
                "us_average": round(m.average, 2) if m.average is not None else None,
                "new_ratings_30d": m.new,
                "version": m.version,
                "version_released_on": m.version_on,
                "on_store_since": m.competitor.released_on,
                "languages": _languages(m.competitor.languages),
                "accessibility": m.competitor.accessibility,
                "storefronts_with_ratings": stores_by_rival.get(m.competitor.pk, 0),
                "store_url": m.store_url,
            }
            for m in rivals
        ],
    }


def get_competitor(app: str, competitor: str) -> dict:
    found = _app(app)
    rival = _competitor(found, competitor)
    by_store = cp.stores(found, rival)
    placings = rival_keywords.ranks(found, rival)["placings"]
    releases = list(rival.releases.order_by("-released_on", "-first_seen")[:RELEASES])
    changes = [
        {
            "date": g["date"],
            "version": g["version"],
            "fields": [
                {k: i[k] for k in ("field", "added", "removed", "gained", "dropped") if k in i}
                | (
                    {"before": i["change"].before, "after": i["change"].after}
                    if i["field"] in ("name", "subtitle", "price", "primary_genre", "seller")
                    else {}
                )
                for i in g["items"]
            ],
        }
        for g in cp._grouped(rival.changes.all())
    ]
    return {
        "app": {"apple_id": found.apple_id, "name": found.name},
        "competitor": {
            "apple_id": rival.apple_id,
            "name": rival.name,
            "seller": rival.seller,
            "subtitle": rival.subtitle,
            "price": rival.price,
            "primary_genre": rival.primary_genre,
            "version": rival.version,
            "version_released_on": rival.version_released_on,
            "on_store_since": rival.released_on,
            "store_url": rival.store_url,
            "languages": _named(_languages(rival.languages)),
            "accessibility": rival.accessibility,
            "description": rival.description[:DESCRIPTION_CHARS],
            "screenshots": len(rival.screenshots),
        },
        "ratings_by_storefront": [
            {
                "storefront": r.code,
                "country": r.name,
                "ratings": r.count,
                "average": round(r.average, 2) if r.average is not None else None,
                "as_of": r.on,
                "your_ratings": r.you_count,
            }
            for r in by_store["rows"]
        ],
        "not_sold_in": [r.code for r in by_store["missing"]],
        "releases": [
            {
                "version": r.version,
                "released_on": r.released_on or r.first_seen,
                "notes": r.notes,
            }
            for r in releases
        ],
        "listing_changes": changes,
        "search_ranks": [
            {
                "term": p.keyword.term,
                "storefront": p.keyword.storefront,
                "rank": p.rank,
                "your_rank": p.you,
                "popularity": p.scores.popularity,
                "difficulty": p.scores.difficulty,
                "you_track_it": p.tracked,
            }
            for p in placings
        ],
    }


def _your_languages(app: App) -> tuple[list[str] | None, str | None]:
    """Your app's languages from its live US listing, or None and why not."""
    if app.is_idea:
        return None, "an idea has no listing yet"
    try:
        with store.client() as http:
            found = store.lookup([app.apple_id], "us", http)
    except store.StoreError as error:
        return None, f"the App Store did not answer: {error}"
    listing = next((r for r in found if r.get("trackId") == app.apple_id), None)
    if listing is None:
        return None, "the US store has no listing for it"
    return _languages(listing.get("languageCodesISO2A")), None


def _your_downloads(app: App) -> tuple[dict[str, int], date | None]:
    end = SalesRow.objects.filter(app=app).aggregate(d=Max("report_date"))["d"]
    if end is None:
        return {}, None
    rows = (
        SalesRow.objects.filter(
            DOWNLOAD_ROWS, app=app, report_date__gt=end - timedelta(days=DOWNLOAD_DAYS)
        )
        .values("country_code")
        .annotate(units=Sum("units"))
    )
    return {r["country_code"].lower(): r["units"] for r in rows if r["units"]}, end


def localization_report(app: str) -> dict:
    found = _app(app)
    rivals = list(found.competitors.all())
    if not rivals:
        raise ToolError(f"{found.name} follows no competitors yet.")
    yours, why = _your_languages(found)
    downloads, sales_end = _your_downloads(found)
    your_ratings = _storefront_ratings(RatingSnapshot.objects.filter(app=found))

    # Each competitor's ratings in each storefront: the size of the market there
    market: dict[str, dict] = {}
    for rival in rivals:
        for front, r in _storefront_ratings(
            CompetitorSnapshot.objects.filter(competitor=rival)
        ).items():
            if not r["count"]:
                continue
            m = market.setdefault(front, {"ratings": 0, "competitors": []})
            m["ratings"] += r["count"]
            m["competitors"].append((r["count"], rival.name))
    total = sum(m["ratings"] for m in market.values()) or 1

    langs: dict[str, dict] = {}

    def lang(code: str) -> dict:
        return langs.setdefault(
            code,
            {
                "code": code,
                "name": cp.language_name(code) if code != "other" else "Other",
                "competitors_supporting": [],
                "market_ratings": 0.0,
                "storefronts": [],
                "your_downloads": 0,
                "your_ratings": 0,
            },
        )

    for rival in rivals:
        for code in _languages(rival.languages):
            lang(code)["competitors_supporting"].append(rival.name)
    for front, m in market.items():
        spoken = STORE_LANGUAGES.get(front, ["other"])
        for code in spoken:
            entry = lang(code)
            entry["market_ratings"] += m["ratings"] / len(spoken)
            entry["storefronts"].append(front)
    for front, units in downloads.items():
        spoken = STORE_LANGUAGES.get(front, ["other"])
        for code in spoken:
            lang(code)["your_downloads"] += units / len(spoken)
    for front, r in your_ratings.items():
        spoken = STORE_LANGUAGES.get(front, ["other"])
        for code in spoken:
            lang(code)["your_ratings"] += (r["count"] or 0) / len(spoken)

    rows = []
    for entry in langs.values():
        supporters = entry["competitors_supporting"]
        entry |= {
            "you_support": None if yours is None else entry["code"] in yours,
            "competitors_supporting_count": len(supporters),
            "competitors_supporting_share": round(len(supporters) / len(rivals), 2),
            "competitors_supporting": sorted(supporters),
            "market_ratings": round(entry["market_ratings"]),
            "market_share": round(entry["market_ratings"] / total, 3),
            "your_downloads": round(entry["your_downloads"]),
            "your_ratings": round(entry["your_ratings"]),
            "storefronts": sorted(entry["storefronts"], key=lambda f: -market[f]["ratings"]),
        }
        rows.append(entry)
    rows.sort(key=lambda e: (-e["market_ratings"], -e["competitors_supporting_count"], e["code"]))

    return {
        "app": {"apple_id": found.apple_id, "name": found.name, "idea": found.is_idea},
        "your_languages": _named(yours) if yours is not None else None,
        "your_languages_missing_because": why,
        "competitors": len(rivals),
        "languages": rows,
        "storefronts": [
            {
                "storefront": front,
                "country": country_name(front),
                "languages": STORE_LANGUAGES.get(front, ["other"]),
                "competitor_ratings": m["ratings"],
                "competitors_with_ratings": len(m["competitors"]),
                "top_competitors": [n for _, n in sorted(m["competitors"], reverse=True)[:3]],
                "your_ratings": (your_ratings.get(front) or {}).get("count"),
                "your_downloads": downloads.get(front, 0),
            }
            for front, m in sorted(market.items(), key=lambda fm: -fm[1]["ratings"])
        ],
        "your_downloads_window": (
            {"days": DOWNLOAD_DAYS, "through": sales_end} if sales_end else None
        ),
        "method": (
            "market_ratings: the competitors' latest public rating counts in each storefront, "
            "summed, and credited to the storefront's language(s) (split evenly where a "
            "storefront has several; 'other' where none is mapped, which the App Store serves "
            "in English). It is a proxy for how many people use this kind of app in that "
            "language. competitors_supporting: competitors whose app (languageCodesISO2A) "
            "supports the language. Storefronts are read weekly, and only those your apps "
            "sell in; a storefront missing here may still matter. Ratings count people who "
            "rated, not downloads."
        ),
    }


def list_keywords(app: str, storefront: str | None = None) -> dict:
    found = _app(app)
    front = (storefront or "").strip().lower() or None
    board = kw.board(found, front)
    return {
        "app": {"apple_id": found.apple_id, "name": found.name},
        "as_of": board["date"],
        "note": f"rank is your app's position in the top {board['limit']} results "
        "(null: not in them). popularity, difficulty and opportunity are 0-100 estimates.",
        "keywords": [
            {
                "term": r.keyword.term,
                "storefront": r.keyword.storefront,
                "rank": r.rank,
                "change_7d": r.change,
                "popularity": r.scores.popularity,
                "difficulty": r.scores.difficulty,
                "opportunity": r.scores.opportunity,
                "competitors_ranked": [
                    {"rank": rank, "name": name} for rank, name, _ in r.rivals[:5]
                ],
            }
            for r in board["rows"]
        ],
    }


def list_iap_sales(app: str, days: int | None = None) -> dict:
    found = _app(app)
    if days is not None:
        try:
            days = int(days)
        except TypeError, ValueError:
            raise ToolError("days must be a whole number of days, e.g. 30.") from None
        if days < 1:
            raise ToolError("days must be 1 or more; leave it out for all time.")
    sales = iap.iap_sales(found, days)
    return {
        "app": {"apple_id": found.apple_id, "name": found.name},
        "period": {"days": days, "start": sales["start"], "through": sales["end"]}
        if sales["end"]
        else None,
        "note": "From the Sales & Trends sales report (Pacific days), one row per product SKU, "
        "best-selling first. units is net of refunds; refunds counts refunded units. "
        "proceeds_usd is developer proceeds (after Apple's commission, before tax) net of "
        "refunds, converted from each storefront's currency at that day's ECB rate; "
        "proceeds_unconverted holds currencies without a rate. names lists every title the "
        "report used for the product."
        if sales["end"]
        else f"{found.name} has no in-app purchase sales.",
        "total_units": sales["units"],
        "total_proceeds_usd": sales["usd"],
        "total_proceeds_unconverted": sales["unconverted"],
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "names": p.titles,
                "units": p.units,
                "refunds": p.refunds,
                "proceeds_usd": p.usd,
                "proceeds_unconverted": p.unconverted,
                "first_sale": p.first_sale,
                "last_sale": p.last_sale,
            }
            for p in sales["products"]
        ],
    }


TOOLS = [
    Tool(
        "list_apps",
        "List your apps and app ideas tracked in ASCStats, with how many competitors and "
        "keywords each follows. Start here to find an app's Apple ID.",
        {},
        [],
        list_apps,
    ),
    Tool(
        "list_competitors",
        "The competitors an app follows, busiest first: US ratings, new ratings in the last 30 "
        "days, price, subtitle, current version, the languages each supports, its declared "
        "accessibility features, and how many storefronts it has ratings in.",
        APP_ARG,
        ["app"],
        list_competitors,
    ),
    Tool(
        "get_competitor",
        "One competitor in depth: its listing and description, languages, accessibility, "
        "ratings in every storefront (next to yours), recent releases with notes, listing "
        "changes, and the App Store searches it ranks in.",
        APP_ARG | COMPETITOR_ARG,
        ["app", "competitor"],
        get_competitor,
    ),
    Tool(
        "localization_report",
        "Which languages to localize an app into, judged from its competitors: for each "
        "language, how many competitors support it, the size of the market in the storefronts "
        "that read it (competitors' ratings there), and your own downloads and ratings there; "
        "plus each storefront's market size. Your app's own languages come from its live "
        "App Store listing.",
        APP_ARG,
        ["app"],
        localization_report,
    ),
    Tool(
        "list_keywords",
        "The App Store search terms an app tracks: its rank, the change over 7 days, "
        "popularity, difficulty and opportunity, and which competitors rank.",
        APP_ARG
        | {
            "storefront": {
                "type": "string",
                "description": 'Two-letter storefront, e.g. "us"; all if left out.',
            }
        },
        ["app"],
        list_keywords,
    ),
    Tool(
        "list_iap_sales",
        "An app's in-app purchases by product (SKU), best-selling first: units sold net of "
        "refunds, refunds, developer proceeds in USD, and first and last sale dates. Answers "
        '"which packs sell best". All time unless days is given.',
        APP_ARG
        | {
            "days": {
                "type": "integer",
                "minimum": 1,
                "description": "Only the last this many days of sales data, e.g. 30 or 90; "
                "all time if left out.",
            }
        },
        ["app"],
        list_iap_sales,
    ),
]
BY_NAME = {t.name: t for t in TOOLS}
