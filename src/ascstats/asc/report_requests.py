"""The pipeline's one write to App Store Connect, kept apart from the read-only client.

Analytics Reports only exist for an app after an ONGOING report request has been created for
it. Apple stops a request it considers inactive (``stoppedDueToInactivity``), so a daily job
checks every active app and creates a request only where none is running. Ongoing reports
only reach back a couple of weeks, so ``backfill_purchases`` also asks, once per app, for a
ONE_TIME_SNAPSHOT: history back to 2024. This class can do exactly that and nothing else: one
POST, to one path, with one body shape (the access type is the only thing that varies).
Everything it reads goes through the GET-only ``ASCClient``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import ASCClient, ASCError, _error_detail

PATH = "/v1/analyticsReportRequests"


@dataclass
class Ensured:
    app_apple_id: int
    created: bool
    request_id: str


class ReportRequester:
    def __init__(self, client: ASCClient):
        self._client = client

    def running_request(self, app_apple_id: int) -> str | None:
        for r in self._client.paginate(
            f"/v1/apps/{app_apple_id}/analyticsReportRequests",
            {"filter[accessType]": "ONGOING"},
        ):
            if not r["attributes"].get("stoppedDueToInactivity"):
                return r["id"]
        return None

    def snapshot_request(self, app_apple_id: int) -> str | None:
        for r in self._client.paginate(
            f"/v1/apps/{app_apple_id}/analyticsReportRequests",
            {"filter[accessType]": "ONE_TIME_SNAPSHOT"},
        ):
            return r["id"]
        return None

    def ensure_ongoing(self, app_apple_id: int) -> Ensured:
        """Create an ONGOING request unless one is already running. Idempotent."""
        if existing := self.running_request(app_apple_id):
            return Ensured(app_apple_id, False, existing)
        return self._create(app_apple_id, "ONGOING")

    def ensure_snapshot(self, app_apple_id: int) -> Ensured:
        """Create a ONE_TIME_SNAPSHOT request unless the app has one already. Idempotent."""
        if existing := self.snapshot_request(app_apple_id):
            return Ensured(app_apple_id, False, existing)
        return self._create(app_apple_id, "ONE_TIME_SNAPSHOT")

    def _create(self, app_apple_id: int, access: str) -> Ensured:
        body = {
            "data": {
                "type": "analyticsReportRequests",
                "attributes": {"accessType": access},
                "relationships": {"app": {"data": {"type": "apps", "id": str(app_apple_id)}}},
            }
        }
        # Not retried: a POST is not idempotent, and tomorrow's run tries again anyway.
        response = self._client._http.post(
            PATH, json=body, headers={"Authorization": f"Bearer {self._client._bearer()}"}
        )
        if response.is_error:
            raise ASCError(response.status_code, _error_detail(response))
        return Ensured(app_apple_id, True, response.json()["data"]["id"])
