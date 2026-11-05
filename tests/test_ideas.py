from datetime import date

import pytest

from ascstats.aso import idea_apps, keywords, suggestions
from ascstats.aso.competitors import CompetitorError
from ascstats.aso.models import Competitor, KeywordSuggestion
from ascstats.stats.cache import data_version
from ascstats.stats.models import App

SNAKE = 1000000001
RIVAL = 1000000201


@pytest.mark.django_db
def test_an_idea_is_research_only(apps):
    before = data_version()
    idea = idea_apps.create("  Cribbage:   No Ads ")
    assert (idea.name, idea.apple_id, idea.is_idea) == ("Cribbage: No Ads", -1, True)
    assert idea_apps.create("Euchre Plus").apple_id == -2  # negative, never an Apple ID
    assert data_version() != before  # the sidebar lists it at once
    assert idea not in App.objects.active()  # no imports, rating snapshots or stats
    assert idea in App.objects.ideas()
    assert idea in App.objects.researched()
    with pytest.raises(CompetitorError, match="name"):
        idea_apps.create("   ")


@pytest.mark.django_db
def test_an_idea_s_keywords_are_searched_once(apps):
    snake = App.objects.get(apple_id=SNAKE)
    idea = idea_apps.create("Worm Garden")
    own, shared = keywords.add(idea, "worm garden, snake game")
    keywords.add(snake, "snake game")
    assert keywords.tracked() == [shared]  # the daily search: only what a real app tracks
    day = date(2026, 11, 4)
    # its one look at the suggestions, so its popularity has evidence; then never again
    assert {k.term for k in suggestions.due(day)} == {"worm garden", "snake game"}
    for k in (own, shared):
        KeywordSuggestion.objects.create(
            keyword=k, date=day, reach="absent", continuations=0, requests=2
        )
    assert suggestions.due(date(2026, 11, 20)) == [shared]


@pytest.mark.django_db
def test_an_idea_joins_the_app_it_became(apps):
    snake = App.objects.get(apple_id=SNAKE)
    idea = idea_apps.create("Worm Garden")
    keywords.add(idea, "worm garden, snake game")
    keywords.add(snake, "snake game")
    rival = Competitor.objects.create(apple_id=RIVAL, name="Worm Rush")
    rival.tracked_for.add(idea)
    real = idea_apps.make_real(idea, f"https://apps.apple.com/us/app/snake/id{SNAKE}")
    assert real == snake
    assert not App.objects.filter(pk=idea.pk).exists()
    assert sorted(snake.keywords.values_list("term", flat=True)) == ["snake game", "worm garden"]
    assert list(snake.competitors.all()) == [rival]
    assert keywords.tracked()[0].term in {"snake game", "worm garden"}  # searched daily now
    assert len(keywords.tracked()) == 2


@pytest.mark.django_db
def test_an_idea_becomes_an_app_the_site_does_not_have_yet(apps):
    idea = idea_apps.create("Worm Garden")
    keywords.add(idea, "worm garden")
    itself = Competitor.objects.create(apple_id=6700000001, name="Worm Garden")
    itself.tracked_for.add(idea)  # its research followed the app it became
    real = idea_apps.make_real(idea, "6700000001")
    assert real.pk == idea.pk
    assert (real.apple_id, real.is_idea) == (6700000001, False)
    assert real in App.objects.active()  # imported and snapshotted from now on
    assert not real.competitors.exists()  # an app never follows itself
    assert [k.term for k in keywords.tracked()] == ["worm garden"]
    with pytest.raises(CompetitorError, match="App Store link"):
        idea_apps.make_real(idea_apps.create("Another"), "not a link")


@pytest.mark.django_db
def test_idea_pages(client, apps):
    page = client.post("/ideas/new/", {"name": "Worm Garden"})
    idea = App.objects.ideas().get()
    assert page.url == f"/app/{idea.apple_id}/keywords/manage/"  # straight to its keywords
    assert client.get(f"/app/{idea.apple_id}/").url == f"/app/{idea.apple_id}/keywords/"
    tab = client.get(f"/app/{idea.apple_id}/keywords/").content.decode()
    assert "Worm Garden" in tab
    assert f'href="/app/{idea.apple_id}/real/"' in tab
    assert f'href="/app/{idea.apple_id}/ratings/"' not in tab  # no Stats, Ratings or Reviews
    assert client.get(f"/app/{idea.apple_id}/competitors/").status_code == 200
    assert client.get(f"/app/{idea.apple_id}/ratings/").status_code == 404
    overview = client.get("/").content.decode()
    assert f'href="/app/{idea.apple_id}/keywords/"' in overview  # the sidebar's Ideas
    real = client.post(f"/app/{idea.apple_id}/real/", {"app": "6700000001"})
    assert real.url == "/app/6700000001/keywords/"
    assert client.get(f"/app/{SNAKE}/real/").url == f"/app/{SNAKE}/keywords/"  # not an idea
