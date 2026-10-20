"""The pipeline's one write to App Store Connect, kept apart from the read-only client.

Analytics Reports only exist for an app after an ONGOING report request has been created for
it. Apple stops a request it considers inactive (``stoppedDueToInactivity``), so a daily job
checks every active app and creates a request only where none is running. This class can do
exactly that and nothing else: one POST, to one path, with one body shape. Everything it reads
goes through the GET-only ``ASCClient``.
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

    def ensure_ongoing(self, app_apple_id: int) -> Ensured:
        """Create an ONGOING request unless one is already running. Idempotent."""
        if existing := self.running_request(app_apple_id):
            return Ensured(app_apple_id, False, existing)
        body = {
            "data": {
                "type": "analyticsReportRequests",
                "attributes": {"accessType": "ONGOING"},
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
