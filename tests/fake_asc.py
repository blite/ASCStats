"""Replays recorded App Store Connect responses from tests/fixtures/asc. No network."""

from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ascstats.asc.client import ASCClient, Credentials

FIXTURES = Path(__file__).parent / "fixtures" / "asc"


def make_credentials() -> tuple[Credentials, ec.EllipticCurvePublicKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return Credentials(
        key_id="TESTKEY123", issuer_id="issuer-uuid", private_key=pem
    ), key.public_key()


class FakeASC:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.queued: dict[str, list[httpx.Response]] = {}  # path -> responses served first

    def queue(self, path: str, *responses: httpx.Response) -> None:
        self.queued.setdefault(path, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/v1/salesReports":
            fixture = FIXTURES / f"sales_{params['filter[reportDate]']}.tsv.gz"
            if fixture.exists():
                return httpx.Response(200, content=fixture.read_bytes())
            return _json(404, "error_404_sales.json")
        if path.endswith("/analyticsReportRequests"):
            return _json(200, "analyticsReportRequests.json")
        if path.endswith("/reports"):
            return _json(200, "reports.json")
        if path.endswith("/instances"):
            return _json(
                200, "instances_page2.json" if "cursor" in params else "instances_page1.json"
            )
        if path.startswith("/v1/analyticsReportInstances/"):
            return _json(200, f"segments_{path.split('/')[3]}.json")
        if request.url.host == "segments.example-cdn.test":
            return httpx.Response(
                200,
                content=(FIXTURES / f"segment_{path.strip('/').split('.')[0]}.tsv.gz").read_bytes(),
            )
        return httpx.Response(404, json={"errors": [{"detail": f"no fixture for {path}"}]})

    def client(self, credentials: Credentials | None = None) -> ASCClient:
        return ASCClient(
            credentials or make_credentials()[0],
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _: None,
        )


def _json(status: int, name: str) -> httpx.Response:
    return httpx.Response(
        status, content=(FIXTURES / name).read_bytes(), headers={"Content-Type": "application/json"}
    )
