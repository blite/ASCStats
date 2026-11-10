from datetime import date, timedelta

import httpx
import pytest
from django.core.management import call_command
from django.test import override_settings
from django_tasks_db.models import DBTaskResult

from ascstats.aso import jobs, keywords, rival_keywords
from ascstats.aso.models import Competitor, Keyword, KeywordScan

SNAKE, SUDOKU = 1000000001, 1000000002
RIVAL = 555000111
DAY = date(2026, 10, 10)


def test_grams_are_searches_people_type():
    # two words first, then one, then three; "card game classic" is all filler
    assert rival_keywords.grams("Spades: Card Game Classic") == [
        "spades card", "spades", "spades card game",
    ]  # fmt: skip
    assert "free" not in rival_keywords.grams("Free Solitaire")  # filler on its own
    assert "solitaire with" not in rival_keywords.grams("Solitaire with Friends")  # cut off
    assert rival_keywords.grams("2048") == []


def test_candidates_put_name_and_subtitle_first_and_keep_relevant_suggestions():
    hints = {
        "spades card": ["spades card", "spades card game", "music player", "spades card royale"],
        "spades": ["spades", "spades plus", "spades offline"],
    }
    found = rival_keywords.candidates(
        "Spades",
        "Trick taking card game",
        "Play spades online.\nSpades online with bots.\nSpades online, free.",
        lambda typed: hints.get(typed, []),
    )
    assert "spades" in found
    assert "trick taking" in found
    assert "card game" not in found  # all filler
    assert "spades trick" not in found  # name + subtitle are never read as one phrase
    assert "music player" not in found  # shares no word with the listing
    assert "spades plus" in found
    assert "spades offline" in found
    assert "spades online" in found  # the description repeats it
    assert found.index("trick taking") < found.index("spades plus") < found.index("spades online")


def test_candidates_leave_out_the_apps_own_full_name():
    found = rival_keywords.candidates("Spades Royale", "", "", lambda typed: [])
    assert "spades royale" not in found
    assert "spades" in found


def results(*ids):
    return [{"trackId": i, "trackName": f"App {i}", "sellerName": "S", "userRatingCount": 9}
            for i in ids]  # fmt: skip


@pytest.fixture
def rival(apps):
    rival = Competitor.objects.create(
        apple_id=RIVAL, name="Spades Royale", subtitle="Trick taking", listing_storefront="us",
        description="Spades online.\nSpades online with friends.",
    )  # fmt: skip
    rival.tracked_for.add(apps[0])
    return rival


@pytest.mark.django_db
def test_a_scan_searches_each_term_once_and_reuses_recent_searches(apps, rival, settings):
    settings.ASC_SOURCE = "asc"
    keywords._save(Keyword.objects.create(term="spades", storefront="us"), DAY, results(1))
    searched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            searched.append(request.url.params["term"])
            return httpx.Response(200, json={"results": results(7, RIVAL, SNAKE)})
        return httpx.Response(200, content=b"")  # hints: unreadable, so none

    scan = KeywordScan.objects.create(competitor=rival, storefront="us")
    rival_keywords.run(scan, DAY, httpx.MockTransport(handler), pause=0, hint_pause=0)
    scan.refresh_from_db()
    assert scan.status == "done"
    assert scan.finished_at
    assert "spades" in scan.terms
    assert "spades" not in searched
    assert scan.searched == len(searched) == len(scan.terms) - 1
    online = Keyword.objects.get(term="spades online", storefront="us")
    assert online.tracked_for.count() == 0  # nothing is tracked by a scan
    assert online.snapshots.get().rank(RIVAL) == 2
    assert keywords.tracked() == []  # and nothing is searched daily because of it


@pytest.mark.django_db
def test_a_scan_the_store_refuses_fails_with_the_reason(rival, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    refuse = httpx.MockTransport(lambda request: httpx.Response(503))
    scan = KeywordScan.objects.create(competitor=rival, storefront="us")
    rival_keywords.run(scan, DAY, refuse, pause=0, hint_pause=0)
    scan.refresh_from_db()
    assert scan.status == "failed"
    assert "503" in scan.error


@pytest.mark.django_db
def test_board_lists_terms_competitors_rank_for_that_you_dont_track(apps, rival):
    snake = apps[0]
    other = Competitor.objects.create(apple_id=RIVAL + 1, name="Not followed")
    mine = keywords.add(snake, "snake game")[0]
    for term, ids in [
        ("spades", [RIVAL, SNAKE]),
        ("card game", [*range(1, 12), RIVAL]),  # 12th
        ("hearts", [other.apple_id]),  # no competitor of Snake's
        ("deep", [*range(1, 60), RIVAL]),  # below the top 50
        ("snake game", [RIVAL]),  # tracked already
    ]:
        k = mine if term == "snake game" else Keyword.objects.create(term=term, storefront="us")
        keywords._save(k, DAY, results(*ids))
    old = Keyword.objects.create(term="old news", storefront="us")
    keywords._save(old, DAY - timedelta(days=rival_keywords.FRESH_DAYS + 1), results(RIVAL))
    board = rival_keywords.board(snake, "us")
    assert [(t.keyword.term, t.best, t.you) for t in board["terms"]] == [
        ("spades", 1, 2), ("card game", 12, None),
    ]  # fmt: skip
    assert rival_keywords.board(snake, "gb")["terms"] == []


@pytest.mark.django_db
def test_scanning_from_the_card_queues_one_scan_at_a_time(client, apps, rival):
    url = f"/app/{SNAKE}/keywords/us/scan/"
    page = client.get(url)
    assert "Spades Royale · never scanned" in page.content.decode()
    client.post(url, {"competitor": RIVAL})
    client.post(url, {"competitor": RIVAL})  # waiting already: not queued twice
    assert KeywordScan.objects.filter(status="queued").count() == 1
    assert DBTaskResult.objects.filter(task_path=jobs.scan_competitor.module_path).count() == 1
    page = client.post(url, {"competitor": 42})
    assert "Pick one of the apps it follows" in page.content.decode()
    with override_settings(DEMO_SITE=True):
        assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_the_scan_job_runs_a_queued_scan_once(apps, rival):
    scan = KeywordScan.objects.create(competitor=rival, storefront="us")
    assert jobs.scan_competitor.call(scan.pk) == "done"  # synthetic: no store asked
    assert jobs.scan_competitor.call(scan.pk) == "skipped"


@pytest.mark.django_db
def test_the_keywords_tab_shows_the_synthetic_competitors_terms(client):
    call_command("seed_synthetic", "--days", "8")  # a Monday's scans among them
    html = client.get(f"/app/{SUDOKU}/keywords/").content.decode()
    assert "Competitors&#x27; keywords" in html or "Competitors' keywords" in html
    assert "Synthetic Number Place" in html
    rival = Competitor.objects.get(name="Synthetic Number Place")
    assert 'class="spot' in html
    assert f'href="/app/{SUDOKU}/competitors/{rival.apple_id}/"' in html  # its page
    assert 'data-track="' in html
    assert KeywordScan.objects.filter(status="done").exists()
