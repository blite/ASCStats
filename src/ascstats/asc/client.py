"""Minimal, read-only App Store Connect API client.

Deliberately small: ES256 JWT auth, JSON:API pagination, retries for GETs. There is no
method for POST/PATCH/DELETE, so the pipeline cannot write to App Store Connect by accident.
The one-time ONGOING analytics report request is created by hand, outside the pipeline.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

BASE_URL = "https://api.appstoreconnect.apple.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}
TOKEN_TTL = 20 * 60  # Apple's maximum
TOKEN_REFRESH_MARGIN = 60


class ASCError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"App Store Connect returned {status}: {detail}")
        self.status = status


class ReportNotReady(Exception):
    """The report for that date does not exist (yet). Sensors poke again later."""


@dataclass(frozen=True)
class Credentials:
    key_id: str
    issuer_id: str
    private_key: str  # PEM contents of the .p8 file


class ASCClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 4,
        backoff: float = 1.0,
        sleep=time.sleep,
    ):
        self._credentials = credentials
        self._http = httpx.Client(base_url=BASE_URL, transport=transport, timeout=60)
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    # --- plumbing ------------------------------------------------------------------------

    def _bearer(self) -> str:
        now = time.time()
        if self._token is None or now > self._token_expires - TOKEN_REFRESH_MARGIN:
            self._token_expires = now + TOKEN_TTL
            self._token = jwt.encode(
                {
                    "iss": self._credentials.issuer_id,
                    "iat": int(now),
                    "exp": int(self._token_expires),
                    "aud": "appstoreconnect-v1",
                },
                self._credentials.private_key,
                algorithm="ES256",
                headers={"kid": self._credentials.key_id, "typ": "JWT"},
            )
        return self._token

    def _get(self, url: str, params: dict | None = None, *, auth: bool = True) -> httpx.Response:
        """GET with retries on 429/5xx and transport errors. GET is idempotent, so retrying
        is always safe; this is the only HTTP verb the client knows."""
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {self._bearer()}"} if auth else {}
            try:
                response = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise
                log.warning("GET %s failed (%s), retrying", url, exc)
                self._sleep(self._backoff * 2**attempt)
                continue
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else self._backoff * 2**attempt
                log.warning("GET %s -> %s, retrying in %.1fs", url, response.status_code, delay)
                self._sleep(delay)
                continue
            return response
        raise AssertionError("unreachable")

    def get_json(self, path: str, params: dict | None = None) -> dict[str, Any]:
        response = self._get(path, params)
        if response.status_code == 404:
            raise ReportNotReady(path)
        if response.is_error:
            raise ASCError(response.status_code, _error_detail(response))
        return response.json()

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict[str, Any]]:
        """Yield every resource in ``data`` across pages, following ``links.next``."""
        url: str | None = path
        while url:
            page = self.get_json(url, params)
            yield from page.get("data", [])
            url = page.get("links", {}).get("next")
            params = None  # the next link already carries the query string

    # --- Sales & Trends ------------------------------------------------------------------

    def sales_report(self, vendor_number: str, report_date: date) -> bytes:
        """Daily SALES/SUMMARY report as the gzipped TSV bytes Apple returns."""
        response = self._get(
            "/v1/salesReports",
            {
                "filter[frequency]": "DAILY",
                "filter[reportType]": "SALES",
                "filter[reportSubType]": "SUMMARY",
                "filter[vendorNumber]": vendor_number,
                "filter[reportDate]": report_date.isoformat(),
                "filter[version]": "1_1",
            },
        )
        if response.status_code == 404:
            # Apple uses 404 both for "not published yet" and "no sales that day".
            raise ReportNotReady(f"sales {report_date}")
        if response.is_error:
            raise ASCError(response.status_code, _error_detail(response))
        return response.content

    # --- Analytics Reports ---------------------------------------------------------------

    def ongoing_report_request_id(self, app_apple_id: int) -> str:
        requests = list(
            self.paginate(
                f"/v1/apps/{app_apple_id}/analyticsReportRequests",
                {"filter[accessType]": "ONGOING"},
            )
        )
        if not requests:
            raise ASCError(404, f"no ONGOING analytics report request for app {app_apple_id}")
        return requests[0]["id"]

    def analytics_segments(
        self, app_apple_id: int, report_name: str, processing_date: date
    ) -> list[dict[str, Any]]:
        """Segments (with presigned ``url`` + ``checksum``) for one daily report instance."""
        request_id = self.ongoing_report_request_id(app_apple_id)
        reports = list(
            self.paginate(
                f"/v1/analyticsReportRequests/{request_id}/reports", {"filter[name]": report_name}
            )
        )
        if not reports:
            raise ASCError(404, f"report {report_name!r} not offered for app {app_apple_id}")
        instances = list(
            self.paginate(
                f"/v1/analyticsReports/{reports[0]['id']}/instances",
                {
                    "filter[granularity]": "DAILY",
                    "filter[processingDate]": processing_date.isoformat(),
                },
            )
        )
        if not instances:
            raise ReportNotReady(f"{report_name} {app_apple_id} {processing_date}")
        segments = []
        for instance in instances:
            segments += self.paginate(f"/v1/analyticsReportInstances/{instance['id']}/segments")
        return segments

    def download_segment(self, segment: dict[str, Any]) -> bytes:
        """Segment URLs are presigned; sending our bearer token there would be rejected."""
        response = self._get(segment["attributes"]["url"], auth=False)
        if response.is_error:
            raise ASCError(response.status_code, "segment download failed")
        return response.content

    def close(self) -> None:
        self._http.close()


def _error_detail(response: httpx.Response) -> str:
    try:
        errors = response.json().get("errors", [])
        return "; ".join(e.get("detail") or e.get("title", "") for e in errors) or response.text
    except ValueError:
        return response.text[:200]
