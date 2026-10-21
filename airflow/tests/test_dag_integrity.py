"""DAG integrity: every DAG imports, parses fast, and follows the project's rules."""

import itertools
import time

import pytest
from airflow.dag_processing.dagbag import DagBag
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.sdk import CronDataIntervalTimetable

from ascstats_lib.pods import ASC_POOL
from ascstats_lib.sensors import ReportAvailableSensor, ReportAvailableTrigger
from tests.conftest import DAGS

EXPECTED = {
    "ascstats_daily_sales",
    "ascstats_daily_analytics",
    "ascstats_conversion_metrics",
    "ascstats_weekly_restatement",
    "ascstats_daily_ratings",
}


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(str(DAGS))


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}
    assert set(dagbag.dag_ids) == EXPECTED


def test_parses_quickly():
    start = time.monotonic()
    DagBag(str(DAGS))
    assert time.monotonic() - start < 5, "slow parse: avoid work at DAG top level"


def test_common_conventions(dagbag):
    for dag in dagbag.dags.values():
        assert "ascstats" in dag.tags
        assert dag.doc_md
        assert all(t.owner == "ascstats" for t in dag.tasks)
        assert all(t.retries >= 1 for t in dag.tasks), dag.dag_id


@pytest.mark.parametrize("dag_id", ["ascstats_daily_sales", "ascstats_daily_analytics"])
def test_daily_dags_catch_up_on_data_intervals(dagbag, dag_id):
    dag = dagbag.dags[dag_id]
    assert dag.catchup
    assert isinstance(dag.timetable, CronDataIntervalTimetable)
    order = ["wait_for_report", "download_raw", "validate", "load_day", "quality_checks"]
    ids = {t.task_id.rsplit(".", 1)[-1]: t for t in dag.tasks}
    for upstream, downstream in itertools.pairwise(order):
        assert ids[downstream].task_id in _all_downstream(dag, ids[upstream].task_id)
    assert isinstance(ids["wait_for_report"], ReportAvailableSensor)


def test_work_runs_in_pods_and_asc_calls_share_a_pool(dagbag):
    for dag in dagbag.dags.values():
        for t in dag.tasks:
            name = t.task_id.rsplit(".", 1)[-1]
            if name in {"download_raw", "restate_sales", "restate_analytics"}:
                assert t.pool == ASC_POOL, t.task_id
            if name in {"download_raw", "validate", "load_day", "quality_checks"}:
                assert t.operator_class is KubernetesPodOperator or isinstance(
                    t, KubernetesPodOperator
                )


def test_load_and_quality_are_mapped_over_apps(dagbag):
    sales = dagbag.dags["ascstats_daily_sales"]
    assert sales.get_task("load_day").is_mapped
    assert sales.get_task("quality_checks").is_mapped
    analytics = dagbag.dags["ascstats_daily_analytics"]
    assert type(analytics.task_group_dict["per_app"]).__name__ == "MappedTaskGroup"


def test_conversion_metrics_waits_for_both_assets(dagbag):
    dag = dagbag.dags["ascstats_conversion_metrics"]
    condition = dag.timetable.asset_condition
    assert type(condition).__name__ == "AssetAll"
    uris = {a.uri for a in condition.objects}
    assert uris == {"ascstats://postgres/stats_salesrow", "ascstats://postgres/analytics_rollups"}


def test_trigger_round_trips_through_serialization():
    trigger = ReportAvailableTrigger("sales", "2026-10-10", "asc_default", [1], 60)
    path, kwargs = trigger.serialize()
    assert path == "ascstats_lib.sensors.ReportAvailableTrigger"
    assert ReportAvailableTrigger(**kwargs).serialize() == (path, kwargs)


def _all_downstream(dag, task_id):
    return {t.task_id for t in dag.get_task(task_id).get_flat_relatives(upstream=False)}


def test_ratings_snapshot_never_catches_up_and_reviews_follow_versions(dagbag):
    dag = dagbag.dags["ascstats_daily_ratings"]
    assert not dag.catchup  # the public lookup has no history to backfill
    assert dag.get_task("import_reviews").pool == ASC_POOL
    assert "import_reviews" in _all_downstream(dag, "build_versions")
    assert dag.get_task("snapshot_ratings").pool != ASC_POOL  # public endpoint, not ASC
