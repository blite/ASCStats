"""The import pipeline: check -> download -> validate -> load -> quality.

Each step is callable on its own (Airflow runs them as separate tasks) and they hand state to
each other only through the raw store, so any step can be retried in isolation. Every call is
recorded in IngestionRun.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from ascstats.asc.client import ASCClient, ReportNotReady

from . import synthetic
from .models import AnalyticsRow, App, IngestionRun, Report, SalesRow
from .parsing import ANALYTICS_REPORTS, ValidationError, parse_analytics, parse_sales
from .storage import RawStore, raw_store

log = logging.getLogger(__name__)

STEPS = ["check", "download", "validate", "load", "quality"]
# Apple says analytics data is complete within five days. It also omits a report for a day
# when fewer than five users had the event, so for a small app "no instance" after that
# window means "nothing to report", not "not published yet".
COMPLETE_AFTER_DAYS = 5
NO_DATA = "_no-data"  # marker landed in place of a report Apple did not publish


class QualityError(Exception):
    pass


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def sales_prefix(day: date) -> str:
    return f"sales/date={day.isoformat()}/"


def analytics_prefix(day: date, apple_id: int, report_name: str) -> str:
    return f"analytics/date={day.isoformat()}/app={apple_id}/{_slug(report_name)}/"


@dataclass
class StepResult:
    keys: list[str] | None = None
    rows_parsed: int | None = None
    rows_deleted: int | None = None
    rows_loaded: int | None = None


class Pipeline:
    def __init__(
        self,
        source: str | None = None,
        store: RawStore | None = None,
        client_factory: Callable[[], ASCClient] | None = None,
    ):
        self.source = source or settings.ASC_SOURCE
        if self.source not in {"asc", "synthetic"}:
            raise ValueError(f"unknown source {self.source!r}")
        self.store = store or raw_store()
        self._client_factory = client_factory
        self._client: ASCClient | None = None

    @property
    def client(self) -> ASCClient:
        if self._client is None:
            if self._client_factory is None:
                from ascstats.asc.credentials import client_from_env

                self._client_factory = client_from_env
            self._client = self._client_factory()
        return self._client

    # --- orchestration -------------------------------------------------------------------

    def run(self, report: str, day: date, step: str = "all", app: App | None = None) -> StepResult:
        steps = STEPS if step == "all" else [step]
        result = StepResult()
        with self._logged(report, day, app, step) as run:
            for name in steps:
                if name == "check":
                    if not self.check(report, day, app):
                        raise ReportNotReady(f"{report} {day}")
                    continue
                partial = getattr(self, name)(report, day, app)
                for attr, value in vars(partial).items():
                    if value is not None:
                        setattr(result, attr, value)
            run.raw_key = ",".join(result.keys or [])[:500]
            run.rows_parsed = result.rows_parsed
            run.rows_deleted = result.rows_deleted
            run.rows_loaded = result.rows_loaded
        return result

    @contextmanager
    def _logged(self, report: str, day: date, app: App | None, step: str) -> Iterator[IngestionRun]:
        run = IngestionRun.objects.create(
            report=report, report_date=day, app=app, step=step, source=self.source
        )
        try:
            yield run
        except ReportNotReady as exc:
            run.status, run.error = IngestionRun.Status.NOT_READY, str(exc)
            raise
        except Exception as exc:
            run.status, run.error = IngestionRun.Status.FAILED, f"{type(exc).__name__}: {exc}"
            raise
        else:
            run.status = IngestionRun.Status.SUCCEEDED
        finally:
            run.finished_at = timezone.now()
            run.save()

    def _apps(self, app: App | None) -> list[App]:
        return [app] if app else list(App.objects.active())

    # --- check ---------------------------------------------------------------------------

    def check(self, report: str, day: date, app: App | None = None) -> bool:
        if self.source == "synthetic":
            return day < datetime.now(UTC).date()
        try:
            if report == Report.SALES:
                self.client.sales_report(settings.ASC_VENDOR_NUMBER, day)
            else:
                old_enough = day <= datetime.now(UTC).date() - timedelta(days=COMPLETE_AFTER_DAYS)
                return old_enough or all(self._any_report(a, day) for a in self._apps(app))
        except ReportNotReady:
            return False
        return True

    def _any_report(self, a: App, day: date) -> bool:
        """Has Apple published anything for this app and day yet?"""
        for name in ANALYTICS_REPORTS:
            try:
                self.client.analytics_segments(a.apple_id, name, day)
                return True
            except ReportNotReady:
                continue
        return False

    # --- download ------------------------------------------------------------------------

    def download(self, report: str, day: date, app: App | None = None) -> StepResult:
        """Fetch and land raw files unchanged. Sales is one vendor-wide file, so ``app`` only
        narrows analytics."""
        keys = []
        if report == Report.SALES:
            if self.source == "synthetic":
                data = synthetic.sales_report(day)
            else:
                data = self.client.sales_report(settings.ASC_VENDOR_NUMBER, day)
            key = sales_prefix(day) + "sales-summary-daily.tsv.gz"
            self.store.put(key, data)
            keys.append(key)
        else:
            for a in self._apps(app):
                for name in ANALYTICS_REPORTS:
                    prefix = analytics_prefix(day, a.apple_id, name)
                    if self.source == "synthetic":
                        segments = [synthetic.analytics_report(
                            synthetic.app_by_apple_id(a.apple_id), name, day
                        )]  # fmt: skip
                    else:
                        try:
                            found = self.client.analytics_segments(a.apple_id, name, day)
                        except ReportNotReady:
                            found = None  # below Apple's privacy threshold that day
                        segments = [self.client.download_segment(s) for s in found] if found else []
                    self.store.delete_prefix(prefix)  # a re-download replaces every segment
                    if not segments:
                        self.store.put(prefix + NO_DATA, b"")
                        keys.append(prefix + NO_DATA)
                    for n, data in enumerate(segments):
                        key = f"{prefix}segment-{n:03d}.tsv.gz"
                        self.store.put(key, data)
                        keys.append(key)
        log.info("landed %d raw file(s) for %s %s", len(keys), report, day)
        return StepResult(keys=keys)

    # --- validate ------------------------------------------------------------------------

    def _raw_keys(self, prefix: str) -> list[str]:
        keys = [k for k in self.store.list(prefix) if not k.endswith(".part")]
        if not keys:
            raise ValidationError([f"no raw files under {prefix}; run the download step first"])
        return keys

    def _sales_by_app(self, day: date) -> dict[int, list]:
        """Parse the day's sales file and attribute every line to an app's Apple ID.
        In-app purchases carry their parent app's SKU in Parent Identifier."""
        (key,) = self._raw_keys(sales_prefix(day))
        lines = parse_sales(self.store.get(key), day)
        sku_to_id = dict(App.objects.values_list("sku", "apple_id"))
        sku_to_id |= {ln.sku: ln.apple_identifier for ln in lines if not ln.parent_identifier}
        by_app, problems = defaultdict(list), []
        for ln in lines:
            if not ln.parent_identifier:
                by_app[ln.apple_identifier].append(ln)
            elif ln.parent_identifier in sku_to_id:
                by_app[sku_to_id[ln.parent_identifier]].append(ln)
            else:
                problems.append(f"{ln.sku}: unknown parent SKU {ln.parent_identifier!r}")
        if problems:
            raise ValidationError(problems)
        return by_app

    def _analytics_for(self, day: date, a: App) -> dict[str, list]:
        out = {}
        for name in ANALYTICS_REPORTS:
            lines = []
            for key in self._raw_keys(analytics_prefix(day, a.apple_id, name)):
                if not key.endswith(NO_DATA):
                    lines += parse_analytics(self.store.get(key))
            wrong = {ln.app_apple_id for ln in lines} - {a.apple_id}
            if wrong:
                raise ValidationError([f"{name}: rows for other apps {sorted(wrong)}"])
            out[name] = lines
        return out

    def validate(self, report: str, day: date, app: App | None = None) -> StepResult:
        if report == Report.SALES:
            by_app = self._sales_by_app(day)
            parsed = sum(len(v) for k, v in by_app.items() if app is None or k == app.apple_id)
        else:
            parsed = sum(
                len(lines)
                for a in self._apps(app)
                for lines in self._analytics_for(day, a).values()
            )
        return StepResult(rows_parsed=parsed)

    # --- load ----------------------------------------------------------------------------

    def load(self, report: str, day: date, app: App | None = None) -> StepResult:
        """Delete+insert per (app, report, date), all in one transaction: a re-run replaces
        the day exactly, and readers never see a half-loaded day."""
        deleted = loaded = parsed = 0
        with transaction.atomic():
            if report == Report.SALES:
                by_app = self._sales_by_app(day)
                if app is None:
                    targets = {aid: self._ensure_app(aid, lines) for aid, lines in by_app.items()}
                    # Apps that had sales before but are absent from the restated file.
                    stale = SalesRow.objects.filter(report_date=day).exclude(
                        app_id__in=[a.pk for a in targets.values()]
                    )
                    deleted += stale.delete()[0]
                else:
                    targets = {app.apple_id: app}
                for apple_id, target in targets.items():
                    lines = by_app.get(apple_id, [])
                    parsed += len(lines)
                    deleted += SalesRow.objects.filter(app=target, report_date=day).delete()[0]
                    loaded += len(SalesRow.objects.bulk_create(
                        [_sales_row(target, day, ln) for ln in lines], batch_size=2000
                    ))  # fmt: skip
            else:
                for a in self._apps(app):
                    for name, lines in self._analytics_for(day, a).items():
                        parsed += len(lines)
                        dates = {ln.date for ln in lines} | {day}
                        deleted += AnalyticsRow.objects.filter(
                            app=a, report_name=name, report_date__in=dates
                        ).delete()[0]
                        loaded += len(AnalyticsRow.objects.bulk_create(
                            [_analytics_row(a, name, ln) for ln in lines], batch_size=2000
                        ))  # fmt: skip
        log.info("%s %s: deleted %d, loaded %d", report, day, deleted, loaded)
        return StepResult(rows_parsed=parsed, rows_deleted=deleted, rows_loaded=loaded)

    def _ensure_app(self, apple_id: int, lines: list) -> App:
        top = next((ln for ln in lines if not ln.parent_identifier), lines[0])
        app, _ = App.objects.get_or_create(
            apple_id=apple_id, defaults={"name": top.title, "sku": top.sku}
        )
        return app

    # --- quality -------------------------------------------------------------------------

    def quality(self, report: str, day: date, app: App | None = None) -> StepResult:
        """Postgres must match the raw files exactly: row counts and unit/count totals."""
        problems = []
        if report == Report.SALES:
            by_app = self._sales_by_app(day)
            expected = {
                aid: (len(ls), sum(ln.units for ln in ls))
                for aid, ls in by_app.items()
                if app is None or aid == app.apple_id
            }
            actual = {
                r["app__apple_id"]: (r["n"], r["units"] or 0)
                for r in SalesRow.objects.filter(report_date=day, **({"app": app} if app else {}))
                .values("app__apple_id")
                .annotate(n=Count("id"), units=Sum("units"))
            }
            problems += [
                f"app {aid}: expected {expected.get(aid)} rows/units, found {actual.get(aid)}"
                for aid in expected.keys() | actual.keys()
                if expected.get(aid, (0, 0)) != actual.get(aid, (0, 0))
            ]
        else:
            for a in self._apps(app):
                for name, lines in self._analytics_for(day, a).items():
                    want = (len(lines), sum(ln.counts for ln in lines))
                    dates = {ln.date for ln in lines} | {day}
                    agg = AnalyticsRow.objects.filter(
                        app=a, report_name=name, report_date__in=dates
                    ).aggregate(n=Count("id"), counts=Sum("counts"))
                    got = (agg["n"], agg["counts"] or 0)
                    if want != got:
                        problems.append(f"{a.apple_id} {name}: expected {want}, found {got}")
        if problems:
            raise QualityError("; ".join(problems))
        return StepResult()


def _sales_row(app: App, day: date, ln) -> SalesRow:
    return SalesRow(
        app=app,
        report_date=day,
        sku=ln.sku,
        title=ln.title,
        version=ln.version,
        product_type=ln.product_type,
        units=ln.units,
        developer_proceeds=ln.developer_proceeds,
        currency_of_proceeds=ln.currency_of_proceeds,
        customer_price=ln.customer_price,
        customer_currency=ln.customer_currency,
        country_code=ln.country_code,
        device=ln.device,
        parent_identifier=ln.parent_identifier,
    )


def _analytics_row(app: App, report_name: str, ln) -> AnalyticsRow:
    return AnalyticsRow(
        app=app,
        report_date=ln.date,
        report_name=report_name,
        event=ln.event,
        source_type=ln.source_type,
        device=ln.device,
        territory=ln.territory,
        counts=ln.counts,
        unique_counts=ln.unique_counts,
        dimensions=ln.dimensions,
    )
