import plistlib

import httpx
import pytest
from django.test import override_settings

from ascstats.aso import finder, keywords
from ascstats.aso.models import Competitor, Keyword
from ascstats.stats.models import App

SPADES = 6776399198

# What the store suggests for each seed, much as it did for Spades on Nov 3, 2026
OFFERED = {
    "spades": ["spades", "spades plus", "spades royale", "spades with friends", "spades offline"],
    "spades offline": ["spades offline", "spades - offline card game", "offline music",
                       "offline maps"],
    "spades classic": ["spades: classic card game", "spades classic cards", "classic games"],
}  # fmt: skip


@pytest.fixture
def spades(db):
    app = App.objects.create(apple_id=SPADES, name="Spades: No Ads", sku="spades", bundle_id="s")
    keywords.add(app, "spades\nspades offline\ncard game\nspades no wifi offline card game")
    for i, name in enumerate(["Spades Plus - Card Game", "Spades - Classic Cards",
                              "Spades Classic Card Game", "Card Game Rules"]):  # fmt: skip
        Competitor.objects.create(apple_id=900 + i, name=name).tracked_for.add(app)
    return app


def store(asked: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        term = request.url.params["term"]
        asked.append(term)
        hints = [{"term": t} for t in OFFERED.get(term, [])]
        return httpx.Response(200, content=plistlib.dumps({"hints": hints}))

    return httpx.MockTransport(handler)


def test_seeds_are_short_keywords_and_shared_competitor_phrases(spades):
    assert finder.core_words(spades) == {"spades"}  # "no" and "ads" are filler
    assert finder.phrases(
        ["Spades - Classic Cards", "Spades Classic Card Game", "Card Game Rules"], {"spades"}
    ) == {"spades classic": 2}  # "card game" is in two names, but says nothing about spades
    seeds = finder.seeds(spades, "us", finder.SEEDS)
    assert seeds == [("spades", 0), ("spades classic", 2), ("spades offline", 0)]
    assert finder.seeds(spades, "us", 1) == [("spades", 0)]


def test_look_keeps_what_names_the_app(spades, settings):
    settings.ASC_SOURCE = "asc"
    asked = []
    found = finder.look(spades, "us", transport=store(asked), pause=0)
    assert asked == ["spades", "spades classic", "spades offline"]
    terms = [i.term for i in found["candidates"]]
    assert "offline music" not in terms
    assert "classic games" not in terms
    assert "spades" not in terms
    assert "spades offline" not in terms
    assert terms[:2] == ["spades classic card game", "spades classic cards"]  # best placed first
    by = {i.term: i for i in found["candidates"]}
    assert by["spades offline card game"].seeds == ["spades offline"]  # punctuation gone
    assert by["spades classic card game"].competitors == 2
    assert by["spades plus"].brand  # "Spades Plus - Card Game" starts with it
    assert not by["spades with friends"].brand
    assert found["room"] == keywords.MAX_PER_APP - 4
    assert not Keyword.objects.filter(term="spades plus").exists()  # nothing tracked on its own


def test_look_is_kept_for_the_day_less_what_gets_tracked(spades, settings):
    settings.ASC_SOURCE = "asc"
    finder.look(spades, "us", transport=store([]), pause=0)
    keywords.add(spades, "spades plus")
    kept = finder.cached(spades, "us", deep=False)
    assert "spades plus" not in [i.term for i in kept["candidates"]]
    assert finder.cached(spades, "us", deep=True) is None


def test_a_refusing_store_says_so(spades, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    refuse = httpx.MockTransport(lambda request: httpx.Response(503))
    found = finder.look(spades, "us", transport=refuse, pause=0)
    assert found["candidates"] == []
    assert "isn't answering" in found["error"]
    assert finder.cached(spades, "us", deep=False) is None  # a failure is not kept
    assert "no suggestions" in finder.look(spades, "zz")["error"]


def test_suggestions_card_looks_and_tracks(client, spades):
    page = client.get(f"/app/{SPADES}/keywords/").content.decode()
    assert f'hx-get="/app/{SPADES}/keywords/us/suggest/"' in page
    card = client.get(f"/app/{SPADES}/keywords/us/suggest/").content.decode()
    assert "Find suggestions" in card
    assert 'class="candidate"' not in card
    looked = client.post(f"/app/{SPADES}/keywords/us/suggest/look/").content.decode()  # synthetic
    assert 'class="candidate"' in looked
    assert "+ Track" in looked
    term = finder.cached(spades, "us", deep=False)["candidates"][0].term
    done = client.post(f"/app/{SPADES}/keywords/us/suggest/track/", {"term": term})
    assert "Tracking" in done.content.decode()
    assert spades.keywords.filter(term=term, storefront="us").exists()
    again = client.post(f"/app/{SPADES}/keywords/us/suggest/track/", {"term": term})
    assert "already tracks" in again.content.decode()
    card = client.get(f"/app/{SPADES}/keywords/us/suggest/").content.decode()
    assert f'value="{term}"' not in card
    assert client.get(f"/app/{SPADES}/keywords/us/suggest/look/").status_code == 405
    with override_settings(DEMO_SITE=True):
        assert client.get(f"/app/{SPADES}/keywords/us/suggest/").status_code == 404
