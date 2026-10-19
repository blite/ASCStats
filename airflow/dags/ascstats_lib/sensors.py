"""Deferrable "is the report published yet?" sensor.

Apple publishes the previous day's reports at no fixed time. Rather than holding a worker
slot while poking, the sensor defers to the triggerer, which polls App Store Connect with a
few GETs every ``poke_interval`` until the report exists.

The ASC credentials live in the ``asc_default`` Airflow connection (login = key ID,
password = .p8 contents, extra = {"issuer_id", "vendor_number"}). With extra
``{"synthetic": true}`` the sensor only checks that the date is in the past, which is how the
pipeline runs end to end without ever calling Apple.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from airflow.sdk import BaseHook, BaseSensorOperator
from airflow.triggers.base import BaseTrigger, TriggerEvent

ASC = "https://api.appstoreconnect.apple.com"
ANALYTICS_REPORTS = ("App Store Discovery and Engagement Standard", "App Downloads Standard")


class ReportAvailableTrigger(BaseTrigger):
    def __init__(
        self,
        report: str,
        report_date: str,
        conn_id: str,
        apple_ids: list[int] | None = None,
        poke_interval: float = 1800,
    ):
        super().__init__()
        self.report = report
        self.report_date = report_date
        self.conn_id = conn_id
        self.apple_ids = apple_ids or []
        self.poke_interval = poke_interval

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            f"{type(self).__module__}.{type(self).__qualname__}",
            {
                "report": self.report,
                "report_date": self.report_date,
                "conn_id": self.conn_id,
                "apple_ids": self.apple_ids,
                "poke_interval": self.poke_interval,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        conn = await BaseHook.aget_connection(self.conn_id)
        extra = conn.extra_dejson
        while True:
            if extra.get("synthetic"):
                ready = date.fromisoformat(self.report_date) < datetime.now(UTC).date()
            else:
                ready = await self._asc_ready(conn.login, conn.password, extra)
            if ready:
                yield TriggerEvent({"status": "ready", "report_date": self.report_date})
                return
            self.log.info("%s %s not published yet", self.report, self.report_date)
            await asyncio.sleep(self.poke_interval)

    async def _asc_ready(self, key_id: str, private_key: str, extra: dict) -> bool:
        """Read-only probes; any 404 means "not yet"."""
        import httpx
        import jwt

        now = int(time.time())
        token = jwt.encode(
            {"iss": extra["issuer_id"], "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
            private_key,
            algorithm="ES256",
            headers={"kid": key_id, "typ": "JWT"},
        )
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=ASC, headers=headers, timeout=60) as http:
            if self.report == "sales":
                r = await http.get(
                    "/v1/salesReports",
                    params={
                        "filter[frequency]": "DAILY",
                        "filter[reportType]": "SALES",
                        "filter[reportSubType]": "SUMMARY",
                        "filter[vendorNumber]": extra["vendor_number"],
                        "filter[reportDate]": self.report_date,
                        "filter[version]": "1_1",
                    },
                )
                return r.status_code == 200
            for apple_id in self.apple_ids:
                requests = await http.get(
                    f"/v1/apps/{apple_id}/analyticsReportRequests",
                    params={"filter[accessType]": "ONGOING"},
                )
                if requests.status_code != 200 or not requests.json()["data"]:
                    return False
                request_id = requests.json()["data"][0]["id"]
                for name in ANALYTICS_REPORTS:
                    reports = await http.get(
                        f"/v1/analyticsReportRequests/{request_id}/reports",
                        params={"filter[name]": name},
                    )
                    if reports.status_code != 200 or not reports.json()["data"]:
                        return False
                    instances = await http.get(
                        f"/v1/analyticsReports/{reports.json()['data'][0]['id']}/instances",
                        params={
                            "filter[granularity]": "DAILY",
                            "filter[processingDate]": self.report_date,
                        },
                    )
                    if instances.status_code != 200 or not instances.json()["data"]:
                        return False
            return True


class ReportAvailableSensor(BaseSensorOperator):
    template_fields = ("report_date", "apple_ids")

    def __init__(
        self,
        *,
        report: str,
        report_date: str = "{{ ds }}",
        apple_ids: Any = None,
        conn_id: str = "asc_default",
        poke_interval: float = 1800,
        timeout: float = timedelta(hours=36).total_seconds(),
        **kwargs,
    ):
        super().__init__(poke_interval=poke_interval, timeout=timeout, **kwargs)
        self.report = report
        self.report_date = report_date
        self.apple_ids = apple_ids
        self.conn_id = conn_id

    def execute(self, context) -> None:
        apple_ids = self.apple_ids
        if apple_ids is not None and not isinstance(apple_ids, list):
            apple_ids = [apple_ids]
        self.defer(
            trigger=ReportAvailableTrigger(
                report=self.report,
                report_date=self.report_date,
                conn_id=self.conn_id,
                apple_ids=[int(a) for a in apple_ids or []],
                poke_interval=self.poke_interval,
            ),
            method_name="execute_complete",
            timeout=timedelta(seconds=self.timeout),
        )

    def execute_complete(self, context, event: dict[str, Any]) -> None:
        self.log.info("report available: %s", event)
