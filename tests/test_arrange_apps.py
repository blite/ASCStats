import re

import pytest
from django.core.management import call_command

from ascstats.stats.models import App


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "5")


def _sidebar(client, path="/"):
    page = client.get(path).content.decode()
    nav = page.split('aria-label="Sidebar"')[1].split("</aside>")[0]
    return re.findall(r'title="(Synthetic [^"]+)"', nav)


def _names():
    return [a.name for a in App.objects.listed()]


def test_new_apps_join_the_end(apps):
    assert [a.sort_order for a in apps] == [0, 1, 2]
    late = App.objects.create(apple_id=1, name="Aardvark")
    assert late.sort_order == 3
    assert App.objects.last() == late  # not first, though it sorts first by name


def test_arrange_puts_the_given_apps_first(apps):
    snake, sudoku, stargazer = apps
    App.objects.arrange([stargazer.pk, snake.pk])
    assert list(App.objects.all()) == [stargazer, snake, sudoku]


def test_arrows_move_an_app_in_the_sidebar(client, seeded):
    before = _sidebar(client)
    assert before == _names()
    assert len(before) == 3
    last = App.objects.get(name=before[-1])
    assert client.post("/apps/", {"up": last.pk}).status_code == 302
    assert _sidebar(client) == [before[0], before[2], before[1]]  # the cached page moved too
    client.post("/apps/", {"down": last.pk})
    assert _sidebar(client) == before
    client.post("/apps/", {"up": App.objects.get(name=before[0]).pk})  # already first
    assert _sidebar(client) == before


def test_drag_saves_the_whole_order(client, seeded):
    a, b, c = App.objects.all()
    assert client.post("/apps/", {"order": [c.pk, a.pk, b.pk]}).status_code == 204
    assert _sidebar(client) == [c.name, a.name, b.name]


def test_hidden_apps_leave_the_sidebar_only(client, seeded):
    snake = App.objects.get(name="Synthetic Snake")
    client.post("/apps/", {"hide": snake.pk})
    assert "Synthetic Snake" not in _sidebar(client)
    assert client.get(f"/app/{snake.apple_id}/").status_code == 200  # still reachable
    assert App.objects.active().filter(pk=snake.pk).exists()  # still imported and counted
    page = client.get("/apps/").content.decode()
    assert "Show hidden apps (1)" in page
    assert f'name="show" value="{snake.pk}"' in page

    client.post("/apps/", {"show": snake.pk})
    assert _sidebar(client)[-1] == "Synthetic Snake"  # back, at the end
    assert "Show hidden apps" not in client.get("/apps/").content.decode()


def test_the_sidebar_links_to_arrange(client, seeded):
    assert 'href="/apps/"' in client.get("/").content.decode()
