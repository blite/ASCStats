from datetime import date

import httpx
import jwt
import pytest

from ascstats.asc.client import ASCClient, ASCError, ReportNotReady
from tests.fake_asc import FIXTURES, FakeASC, make_credentials


def test_bearer_token_is_es256_with_apple_claims():
    credentials, public_key = make_credentials()
    fake = FakeASC()
    fake.client(credentials).sales_report("80000000", date(2026, 10, 10))

    token = fake.requests[0].headers["Authorization"].removeprefix("Bearer ")
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, public_key, algorithms=["ES256"], audience="appstoreconnect-v1")
    assert header == {"alg": "ES256", "kid": "TESTKEY123", "typ": "JWT"}
    assert claims["iss"] == "issuer-uuid"
    assert claims["exp"] - claims["iat"] <= 20 * 60


def test_token_is_reused_between_requests():
    fake = FakeASC()
    client = fake.client()
    client.sales_report("80000000", date(2026, 10, 10))
    client.sales_report("80000000", date(2026, 10, 10))
    first, second = (r.headers["Authorization"] for r in fake.requests)
    assert first == second


def test_sales_report_sends_filters_and_returns_raw_gzip():
    fake = FakeASC()
    data = fake.client().sales_report("80000000", date(2026, 10, 10))
    assert data == (FIXTURES / "sales_2026-10-10.tsv.gz").read_bytes()
    params = fake.requests[0].url.params
    assert params["filter[reportType]"] == "SALES"
    assert params["filter[frequency]"] == "DAILY"
    assert params["filter[vendorNumber]"] == "80000000"


def test_missing_sales_report_is_not_ready():
    with pytest.raises(ReportNotReady):
        FakeASC().client().sales_report("80000000", date(2026, 10, 11))


def test_retries_429_honouring_retry_after_then_succeeds():
    fake = FakeASC()
    fake.queue("/v1/salesReports", httpx.Response(429, headers={"Retry-After": "7"}),
               httpx.Response(503))  # fmt: skip
    slept = []
    client = ASCClient(make_credentials()[0], transport=httpx.MockTransport(fake.handler),
                       sleep=slept.append, backoff=0.5)  # fmt: skip
    assert client.sales_report("80000000", date(2026, 10, 10))
    assert slept == [7.0, 1.0]
    assert len(fake.requests) == 3


def test_retries_transport_errors():
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=b"ok")

    client = ASCClient(
        make_credentials()[0], transport=httpx.MockTransport(flaky), sleep=lambda _: None
    )
    assert client.sales_report("1", date(2026, 1, 1)) == b"ok"


def test_gives_up_after_max_retries_and_surfaces_apple_error_detail():
    fake = FakeASC()
    fake.queue("/v1/salesReports", *[httpx.Response(500)] * 3)
    client = ASCClient(make_credentials()[0], transport=httpx.MockTransport(fake.handler),
                       sleep=lambda _: None, max_retries=2)  # fmt: skip
    with pytest.raises(ASCError) as exc:
        client.sales_report("1", date(2026, 10, 10))
    assert exc.value.status == 500

    fake.queue(
        "/v1/salesReports", httpx.Response(401, content=(FIXTURES / "error_401.json").read_bytes())
    )
    with pytest.raises(ASCError, match="properly configured"):
        client.sales_report("1", date(2026, 10, 10))


def test_analytics_follows_pagination_and_downloads_segments_without_auth():
    fake = FakeASC()
    client = fake.client()
    segments = client.analytics_segments(1000000002, "App Downloads Standard", date(2026, 10, 10))
    assert [s["id"] for s in segments] == ["seg-1", "seg-2"]  # one per instance page

    data = client.download_segment(segments[0])
    assert data == (FIXTURES / "segment_seg-1.tsv.gz").read_bytes()
    download = fake.requests[-1]
    assert download.url.host == "segments.example-cdn.test"
    assert "Authorization" not in download.headers


def test_client_only_ever_sends_get():
    fake = FakeASC()
    client = fake.client()
    client.sales_report("80000000", date(2026, 10, 10))
    for s in client.analytics_segments(1000000002, "App Downloads Standard", date(2026, 10, 10)):
        client.download_segment(s)
    assert {r.method for r in fake.requests} == {"GET"}
    assert not any(hasattr(client, verb) for verb in ("post", "patch", "delete", "put"))


def test_report_requester_creates_only_where_none_runs():
    import json

    from ascstats.asc.report_requests import ReportRequester

    fake = FakeASC()
    stopped = {"data": [{"type": "analyticsReportRequests", "id": "old",
                         "attributes": {"accessType": "ONGOING", "stoppedDueToInactivity": True}}],
               "links": {}}  # fmt: skip
    fake.queue("/v1/apps/42/analyticsReportRequests", httpx.Response(200, json=stopped))
    fake.queue(
        "/v1/analyticsReportRequests",
        httpx.Response(201, json={"data": {"type": "analyticsReportRequests", "id": "new"}}),
    )
    requester = ReportRequester(fake.client())
    created = requester.ensure_ongoing(42)
    assert (created.created, created.request_id) == (True, "new")
    post = fake.requests[-1]
    assert (post.method, post.url.path) == ("POST", "/v1/analyticsReportRequests")
    body = json.loads(post.content)
    assert body["data"]["attributes"] == {"accessType": "ONGOING"}
    assert body["data"]["relationships"]["app"]["data"]["id"] == "42"
    # the recorded fixture has a running request for every other app: no POST at all
    before = len(fake.requests)
    assert requester.ensure_ongoing(1000000002).created is False
    assert {r.method for r in fake.requests[before:]} == {"GET"}
