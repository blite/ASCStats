import json

import pytest
from django.core.management import call_command
from django.test import Client

from ascstats import store
from ascstats.aso.models import Competitor

SNAKE = 1000000001
TOKEN = "test-token"


@pytest.fixture
def mcp(db, settings):
    settings.MCP_TOKEN = TOKEN
    http = Client()

    def post(body, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return http.post(
            "/mcp/", json.dumps(body), content_type="application/json", headers=headers
        )

    return post


def rpc(post, method, params=None, rid=1):
    response = post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["id"] == rid
    return body


def call(post, name, **arguments):
    result = rpc(post, "tools/call", {"name": name, "arguments": arguments})["result"]
    return result


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "40")


def test_off_without_a_token_and_closed_without_the_right_one(db, settings, mcp):
    settings.MCP_TOKEN = ""
    assert mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 503
    settings.MCP_TOKEN = TOKEN
    assert mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None).status_code == 401
    assert mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="wrong").status_code == 401
    assert Client().get("/mcp/", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405


def test_the_handshake_and_the_tool_list(mcp):
    init = rpc(mcp, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "0"}})  # fmt: skip
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    later = rpc(mcp, "initialize", {"protocolVersion": "2099-01-01"})
    assert later["result"]["protocolVersion"] == "2025-06-18"  # ours, when we do not know theirs

    note = mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    assert rpc(mcp, "ping")["result"] == {}

    listed = rpc(mcp, "tools/list")["result"]["tools"]
    assert {t["name"] for t in listed} == {
        "list_apps", "list_competitors", "get_competitor", "localization_report", "list_keywords",
        "list_iap_sales",
    }  # fmt: skip
    for t in listed:
        assert t["inputSchema"]["type"] == "object"
        assert t["annotations"]["readOnlyHint"] is True

    missing = rpc(mcp, "no/such/method")
    assert missing["error"]["code"] == -32601


def test_a_batch_answers_each_request(mcp):
    response = mcp([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])  # fmt: skip
    assert [a["id"] for a in response.json()] == [1, 2]


def test_tools_find_apps_by_name_and_say_what_they_cannot(mcp, seeded):
    apps = call(mcp, "list_apps")["structuredContent"]["apps"]
    assert SNAKE in [a["apple_id"] for a in apps]

    found = call(mcp, "list_competitors", app="snake")
    assert not found["isError"]
    data = found["structuredContent"]
    assert data["app"]["apple_id"] == SNAKE
    names = [c["name"] for c in data["competitors"]]
    assert "Synthetic Worm Rush" in names
    assert json.loads(found["content"][0]["text"]) == data  # the same answer, as text

    vague = call(mcp, "list_competitors", app="synthetic")
    assert vague["isError"]
    assert "several apps" in vague["content"][0]["text"]
    assert call(mcp, "list_competitors", app="nothing like it")["isError"]
    assert call(mcp, "list_competitors")["isError"]  # app is required
    assert call(mcp, "list_competitors", app="snake", extra=1)["isError"]
    unknown = rpc(mcp, "tools/call", {"name": "drop_tables", "arguments": {}})
    assert unknown["error"]["code"] == -32602


def test_one_competitor_in_depth(mcp, seeded):
    rival = Competitor.objects.get(name="Synthetic Worm Rush")
    data = call(mcp, "get_competitor", app=str(SNAKE), competitor="worm")["structuredContent"]
    assert data["competitor"]["apple_id"] == rival.apple_id
    assert data["ratings_by_storefront"][0]["storefront"] == "us"
    assert data["releases"]
    assert {"languages", "accessibility", "description"} <= set(data["competitor"])
    assert "search_ranks" in data


def test_the_localization_report_weighs_languages_by_market(mcp, seeded, monkeypatch):
    # The synthetic competitors' languages and markets, and your own app's live listing
    for c in Competitor.objects.filter(tracked_for__apple_id=SNAKE):
        c.languages = ["EN", "DE"] if c.name == "Synthetic Worm Rush" else ["EN"]
        c.save(update_fields=["languages"])
    monkeypatch.setattr(
        store, "lookup",
        lambda ids, front, http: [{"trackId": SNAKE, "languageCodesISO2A": ["EN"]}],
    )  # fmt: skip

    result = call(mcp, "localization_report", app="Synthetic Snake")
    assert not result["isError"], result
    data = result["structuredContent"]
    assert data["your_languages"] == [{"code": "EN", "name": "English"}]
    by_code = {row["code"]: row for row in data["languages"]}
    assert by_code["EN"]["you_support"] is True
    assert by_code["EN"]["competitors_supporting_share"] == 1.0
    assert by_code["DE"]["you_support"] is False
    assert by_code["DE"]["competitors_supporting"] == ["Synthetic Worm Rush"]
    assert by_code["EN"]["market_ratings"] > 0  # the US storefront reads English
    assert data["storefronts"][0]["storefront"] == "us"
    shares = sum(row["market_share"] for row in data["languages"])
    assert shares == pytest.approx(1, abs=0.01)


def test_keywords(mcp, seeded):
    data = call(mcp, "list_keywords", app=str(SNAKE), storefront="us")["structuredContent"]
    terms = {k["term"] for k in data["keywords"]}
    assert "snake game" in terms
    assert {k["storefront"] for k in data["keywords"]} == {"us"}
