import json
from datetime import date, timedelta

import httpx
import pytest
from django.core.management import call_command

from ascstats import store
from ascstats.aso import competitors
from ascstats.aso.models import Competitor, ListingChange

RIVAL = 555000111
DAY = date(2026, 10, 10)
SHOT = "https://is1-ssl.mzstatic.com/image/thumb/Purple/v4/{}/shot.png/392x696bb.png"


def listing(**changes):
    out = {
        "trackId": RIVAL, "trackName": "Real Rival", "sellerName": "Rival Co",
        "primaryGenreName": "Games", "formattedPrice": "Free", "version": "4.0",
        "description": "Play spades.\n\nNo ads.", "screenshotUrls": [SHOT.format("a")],
        "languageCodesISO2A": ["EN"],
    }  # fmt: skip
    return out | changes


@pytest.fixture
def rival(apps):
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(apps[0])
    return rival


def fields(rival):
    return list(ListingChange.objects.filter(competitor=rival).values_list("field", flat=True))


@pytest.mark.django_db
def test_the_first_listing_is_the_baseline_and_changes_count_from_the_next_day(rival):
    competitors.store_listing(rival, listing(), DAY)
    assert fields(rival) == []
    assert (rival.description, rival.languages) == ("Play spades.\n\nNo ads.", ["EN"])
    competitors.store_listing(rival, listing(), DAY + timedelta(days=1))
    assert fields(rival) == []  # nothing changed

    later = DAY + timedelta(days=2)
    competitors.store_listing(rival, listing(
        trackName="Real Rival: Spades", formattedPrice="$0.99", version="4.1",
        description="Play spades with friends.\n\nNo ads.", languageCodesISO2A=["FR", "EN"],
    ), later)  # fmt: skip
    assert sorted(fields(rival)) == ["description", "languages", "name", "price"]
    name = ListingChange.objects.get(competitor=rival, field="name")
    assert (name.before, name.after, name.version, name.date) == (
        "Real Rival",
        "Real Rival: Spades",
        "4.1",
        later,
    )


@pytest.mark.django_db
def test_a_screenshot_at_another_size_is_the_same_screenshot(rival):
    competitors.store_listing(rival, listing(), DAY)
    resized = SHOT.format("a").replace("392x696bb", "300x0w")
    competitors.store_listing(rival, listing(screenshotUrls=[resized]), DAY + timedelta(days=1))
    assert fields(rival) == []
    new = [SHOT.format("b"), SHOT.format("a")]
    competitors.store_listing(rival, listing(screenshotUrls=new), DAY + timedelta(days=2))
    assert fields(rival) == ["screenshots"]


@pytest.mark.django_db
def test_only_full_listings_from_one_storefront_are_compared(rival):
    competitors.store_listing(rival, listing(), DAY, "us")
    # A search result carries a few fields: never a change, never a baseline
    competitors.store_listing(
        rival, {"trackId": RIVAL, "trackName": "Real Rival"}, DAY + timedelta(days=1)
    )
    # Another storefront's listing is in another language: it becomes the baseline instead
    competitors.store_listing(rival, listing(description="Jouez."), DAY + timedelta(days=2), "fr")
    assert fields(rival) == []
    assert rival.listing_storefront == "fr"


@pytest.mark.django_db
def test_changed_and_changed_back_within_a_day_is_no_change(rival):
    competitors.store_listing(rival, listing(), DAY)
    day = DAY + timedelta(days=1)
    competitors.store_listing(rival, listing(formattedPrice="$0.99"), day)
    assert fields(rival) == ["price"]
    competitors.store_listing(rival, listing(), day)
    assert fields(rival) == []


NOT_DECLARED = {"$kind": "Shelf", "contentType": "accessibilityParagraph", "items": [
    {"$kind": "AccessibilityParagraph", "text": {"styledText": {
        "rawText": "The developer has not yet indicated which accessibility features this app"
        " supports."}}},
]}  # fmt: skip


def declared(*titles):
    features = [{"$kind": "AccessibilityFeature", "title": t} for t in titles]
    card = {"$kind": "AccessibilityFeatures", "title": "Supported Features", "features": features}
    # The card again behind its "see all" action, as Apple's pages carry it
    return {"$kind": "Shelf", "items": [card | {"clickAction": {"pageData": {"shelves": [
        {"items": [card]}]}}}]}  # fmt: skip


def page(apple_id, subtitle, accessibility=None):
    lockup = {"$kind": "Lockup", "adamId": str(apple_id), "title": "Real Rival"}
    if subtitle is not None:
        lockup["subtitle"] = subtitle
    other = {"adamId": "999", "subtitle": "Someone else's"}
    shelves = {"lockup": lockup, "shelf": {"items": [other, other]}}
    if accessibility is not None:
        shelves["accessibilityHeader"] = NOT_DECLARED
        if accessibility:
            shelves["accessibilityFeatures"] = declared(*accessibility)
    data = {"data": [{"data": {"shelfMapping": shelves}}]}
    script = '<script type="application/json" id="serialized-server-data">'
    return f"<html>{script}{json.dumps(data)}</script></html>"


def test_subtitle_is_read_from_the_page_data():
    def handler(request):
        if request.url.path.endswith(f"id{RIVAL}"):  # Apple redirects to the named page
            return httpx.Response(
                301, headers={"Location": f"https://apps.apple.com/us/app/real-rival/id{RIVAL}x"}
            )
        return httpx.Response(200, text=page(RIVAL, "Spades with friends"))

    with store.client(httpx.MockTransport(handler)) as http:
        assert store.subtitle(RIVAL, "us", http) == "Spades with friends"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (page(RIVAL, None), ""),  # the app is there with no subtitle
        (
            '<h1 class="x"><span>Real Rival</span></h1> '
            '<p class="subtitle svelte">Cards &amp; more</p>',
            "Cards & more",
        ),  # no data: the header
        ("<html>nothing here</html>", None),
    ],
)
def test_subtitle_without_one_or_without_the_data(text, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    with store.client(transport) as http:
        assert store.subtitle(RIVAL, "us", http) == expected


@pytest.mark.django_db
def test_subtitles_are_read_again_when_a_version_ships(rival, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    competitors.store_listing(rival, listing(), DAY)
    answers = iter(["Spades", "Spades", "Spades with friends"])
    asked = []
    monkeypatch.setattr(
        store, "page", lambda i, front, http: asked.append(i) or store.StorePage(next(answers), [])
    )

    assert competitors.read_subtitles(DAY, pause=0) == 1  # never read: read now
    assert competitors.read_subtitles(DAY, pause=0) == 0  # once a day at most
    assert competitors.read_subtitles(DAY + timedelta(days=1), pause=0) == 0  # same version
    rival.refresh_from_db()
    competitors.store_listing(rival, listing(version="4.1"), DAY + timedelta(days=2))
    assert competitors.read_subtitles(DAY + timedelta(days=2), pause=0) == 1
    assert fields(rival) == []  # same subtitle
    competitors.store_listing(rival, listing(version="4.2"), DAY + timedelta(days=3))
    competitors.read_subtitles(DAY + timedelta(days=3), pause=0)
    change = ListingChange.objects.get(competitor=rival, field="subtitle")
    assert (change.before, change.after) == ("Spades", "Spades with friends")
    later = DAY + timedelta(days=3 + competitors.SUBTITLE_EVERY)
    asked.clear()
    answers = iter(["Spades with friends"])
    competitors.read_subtitles(later, pause=0)
    assert asked == [RIVAL]  # a month on: read again in case


@pytest.mark.django_db
def test_subtitle_reads_stop_when_the_store_keeps_refusing(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    for i in range(4):
        Competitor.objects.create(apple_id=RIVAL + i, name=f"R{i}").tracked_for.add(apps[0])

    def refuse(apple_id, front, http):
        raise store.StoreError("HTTP 403")

    monkeypatch.setattr(store, "page", refuse)
    assert competitors.read_subtitles(DAY, pause=0) == 0
    assert not Competitor.objects.filter(subtitle_read_on__isnull=False).exists()


def test_word_diff_keeps_context_around_a_change():
    runs = competitors.word_diff("Retro snake", "Classic snake game")
    assert [(r["op"], r["text"]) for r in runs] == [
        ("del", "Retro"), ("ins", "Classic"), ("eq", "snake"), ("ins", "game"),
    ]  # fmt: skip
    long = " ".join(f"w{i}" for i in range(40))
    runs = competitors.word_diff(long, long.replace("w20", "new"), context=3)
    assert [r["op"] for r in runs] == ["gap", "eq", "del", "ins", "eq", "gap"]
    assert runs[1]["text"] == "w17 w18 w19"


@pytest.mark.django_db
def test_synthetic_listings_change_on_the_planted_days_and_show_on_the_tab(client):
    call_command("seed_synthetic", "--days", "30", "--end", "2026-06-20")
    found = set(ListingChange.objects.values_list("competitor__name", "date", "field"))
    assert found == {
        ("Synthetic Slither Classic", date(2026, 6, 1), "subtitle"),
        ("Synthetic Slither Classic", date(2026, 6, 1), "description"),
        ("Synthetic Slither Classic", date(2026, 6, 1), "screenshots"),
        ("Synthetic Slither Classic", date(2026, 6, 15), "price"),
    }
    html = client.get("/app/1000000001/competitors/").content.decode()
    assert "Listing changes" in html
    assert "<del>Retro</del>" in html
    assert "<ins>Classic</ins>" in html
    assert "Classic snake game, no ads" in html  # the subtitle under the name


@pytest.mark.django_db
def test_following_reads_the_whole_listing_at_once(apps, settings, monkeypatch):
    from django_tasks_db.models import DBTaskResult

    from ascstats.aso import jobs

    settings.ASC_SOURCE = "asc"

    def queued():
        found = DBTaskResult.objects.filter(task_path=jobs.read_competitor.module_path)
        return [r.args_kwargs["args"][0] for r in found]

    # From a keyword's results: only the name, seller and icon so far
    rival = competitors.add(
        apps[0], str(RIVAL), listing={"trackId": RIVAL, "trackName": "Real Rival"}
    )
    assert queued() == [rival.pk]

    monkeypatch.setattr(store, "lookup", lambda ids, front, http: [listing()])
    monkeypatch.setattr(
        store, "page", lambda i, front, http: store.StorePage("Spades with friends", ["VoiceOver"])
    )
    assert competitors.read_new(rival, DAY, pause=0) == "read"
    rival.refresh_from_db()
    assert rival.subtitle == "Spades with friends"
    assert rival.accessibility == ["VoiceOver"]
    assert rival.description == "Play spades.\n\nNo ads."
    assert (rival.listing_seen_on, rival.subtitle_read_on) == (DAY, DAY)

    DBTaskResult.objects.all().delete()  # a second app following it: nothing left to read
    competitors.add(apps[1], str(RIVAL))
    assert queued() == []


@pytest.mark.django_db
def test_first_subtitle_reads_are_not_capped(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    for i in range(5):
        Competitor.objects.create(apple_id=RIVAL + i, name=f"R{i}").tracked_for.add(apps[0])
    monkeypatch.setattr(store, "page", lambda i, front, http: store.StorePage("Sub", []))
    assert competitors.read_subtitles(DAY, pause=0, limit=2) == 5


@pytest.mark.parametrize(
    ("accessibility", "expected"),
    [
        (
            ["VoiceOver", "Larger Text", "Reduced Motion"],
            ["VoiceOver", "Larger Text", "Reduced Motion"],
        ),
        ([], []),  # the section says the developer has declared none
        (None, None),  # no Accessibility section at all: unknown, not "none"
    ],
)
def test_accessibility_is_read_from_the_page_data(accessibility, expected):
    text = page(RIVAL, "Spades with friends", accessibility)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    with store.client(transport) as http:
        found = store.page(RIVAL, "us", http)
    assert found == store.StorePage("Spades with friends", expected)


@pytest.mark.django_db
def test_accessibility_changes_are_noted_and_unread_pages_are_read(rival, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    competitors.store_listing(rival, listing(), DAY)
    # Its subtitle was read before accessibility was: read again without waiting a month
    Competitor.objects.filter(pk=rival.pk).update(subtitle="Spades", subtitle_read_on=DAY,
                                                  subtitle_version="4.0")  # fmt: skip
    answers = iter([[], ["VoiceOver", "Larger Text"]])
    monkeypatch.setattr(
        store, "page", lambda i, front, http: store.StorePage("Spades", next(answers))
    )
    next_day = DAY + timedelta(days=1)
    assert competitors.read_subtitles(next_day, pause=0) == 1
    rival.refresh_from_db()
    assert rival.accessibility == []
    assert fields(rival) == []  # the first read is the baseline
    assert competitors.read_subtitles(next_day + timedelta(days=1), pause=0) == 0  # read now

    competitors.store_listing(rival, listing(version="4.1"), next_day + timedelta(days=2))
    competitors.read_subtitles(next_day + timedelta(days=2), pause=0)
    change = ListingChange.objects.get(competitor=rival, field="accessibility")
    assert (change.before, change.after) == ([], ["VoiceOver", "Larger Text"])
    item = competitors.describe(change)
    assert (item["kind"], item["added"], item["removed"]) == (
        "list",
        ["Larger Text", "VoiceOver"],
        [],
    )


@pytest.mark.django_db
def test_a_page_without_the_section_keeps_what_was_read(rival, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    competitors.store_listing(rival, listing(), DAY)
    Competitor.objects.filter(pk=rival.pk).update(accessibility=["VoiceOver"])
    monkeypatch.setattr(store, "page", lambda i, front, http: store.StorePage("Spades", None))
    competitors.read_subtitles(DAY, pause=0)
    rival.refresh_from_db()
    assert (rival.accessibility, rival.subtitle) == (["VoiceOver"], "Spades")
    assert fields(rival) == []


@pytest.mark.django_db
def test_the_competitor_page_shows_declared_accessibility(client, apps, rival):
    from django.core.cache import cache

    url = f"/app/{apps[0].apple_id}/competitors/{RIVAL}/"
    assert "None declared" not in client.get(url).content.decode()  # never read
    Competitor.objects.filter(pk=rival.pk).update(accessibility=[])
    cache.clear()  # an edited row leaves the data version as it was
    assert "None declared" in client.get(url).content.decode()
    Competitor.objects.filter(pk=rival.pk).update(accessibility=["VoiceOver", "Larger Text"])
    cache.clear()
    html = client.get(url).content.decode()
    assert "Accessibility · 2" in html
    assert ">VoiceOver<" in html
