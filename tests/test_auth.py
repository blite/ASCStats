import pytest
from django.contrib.auth.models import User
from django.test import Client


@pytest.fixture
def anon(db):
    return Client()


def test_pages_redirect_to_login(anon):
    response = anon.get("/revenue/")
    assert response.status_code == 302
    assert response.url == "/login/?next=/revenue/"


def test_htmx_fragment_reloads_onto_login(anon):
    response = anon.get("/chart/1000000001/30/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login/"
    assert response.content == b""  # nothing for htmx to swap into the chart


def test_api_says_401_but_health_is_open(anon):
    assert anon.get("/api/apps").status_code == 401
    assert anon.get("/api/health").json() == {"ok": True}  # probes have no session
    assert anon.get("/api/docs").status_code == 302


def test_login_page_and_sign_in(anon):
    User.objects.create_user("ben", password="pw")
    page = anon.get("/login/?next=/revenue/")
    assert page.status_code == 200
    assert b'name="next" value="/revenue/"' in page.content
    bad = anon.post("/login/", {"username": "ben", "password": "nope"})
    assert bad.status_code == 200
    assert b"didn't match" in bad.content
    good = anon.post("/login/", {"username": "ben", "password": "pw", "next": "/revenue/"})
    assert good.status_code == 302
    assert good.url == "/revenue/"
    assert anon.get("/login/").url == "/"  # already signed in


def test_sidebar_names_the_user_and_signs_out(client):
    home = client.get("/").content.decode()
    assert 'aria-label="Sidebar"' in home
    assert 'href="/logout/"' in home
    assert ">tester<" in home
    assert client.get("/logout/").status_code == 200  # asks first; GET never signs out
    assert client.get("/").status_code == 200
    out = client.post("/logout/")
    assert out.url == "/login/"
    assert client.get("/").status_code == 302


def test_cached_pages_are_per_user(client, db):
    client.get("/revenue/")
    other = Client()
    other.force_login(User.objects.create_user("someone", password="pw"))
    page = other.get("/revenue/").content.decode()
    assert ">someone<" in page
    assert ">tester<" not in page
