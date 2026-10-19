"""Daily Sales & Trends import.

One data interval = one report day. The vendor-wide file is downloaded and validated once,
then loaded and checked per app (dynamic task mapping), each load an idempotent
delete+insert of that (app, day). Backfill with catchup or ``airflow backfill create``.
"""

from airflow.sdk import CronDataIntervalTimetable, dag, task

from ascstats_lib.pods import (
    ASC_CONN_ID,
    DEFAULT_ARGS,
    SALES_ASSET,
    START_DATE,
    manage_py,
    partial_manage_py,
)
from ascstats_lib.sensors import ReportAvailableSensor

DAY = "{{ ds }}"


@dag(
    dag_id="ascstats_daily_sales",
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=START_DATE,
    catchup=True,
    max_active_runs=3,
    default_args=DEFAULT_ARGS,
    tags=["ascstats", "sales"],
    doc_md=__doc__,
)
def ascstats_daily_sales():
    wait_for_report = ReportAvailableSensor(
        task_id="wait_for_report", report="sales", conn_id=ASC_CONN_ID
    )
    download_raw = manage_py(
        "download_raw",
        ["import_asc", "--report", "sales", "--date", DAY, "--step", "download"],
        talks_to_asc=True,
    )
    validate = manage_py(
        "validate", ["import_asc", "--report", "sales", "--date", DAY, "--step", "validate"]
    )
    apps = manage_py("list_apps", ["list_apps", "--xcom"], do_xcom_push=True)

    @task
    def per_app_arguments(apple_ids: list[int], step: str, ds=None) -> list[list[str]]:
        """Arguments for one pod per app. Built in a task because values mapped from XCom
        are not Jinja-rendered, so ``{{ ds }}`` must be resolved here."""
        return [
            ["import_asc", "--report", "sales", "--date", ds, "--step", step, "--app", str(a)]
            for a in apple_ids
        ]

    load_day = partial_manage_py("load_day").expand(
        arguments=per_app_arguments.override(task_id="load_arguments")(apps.output, "load")
    )
    quality_checks = partial_manage_py("quality_checks").expand(
        arguments=per_app_arguments.override(task_id="quality_arguments")(apps.output, "quality")
    )

    @task(outlets=[SALES_ASSET])
    def publish(ds=None):
        """Emit the asset event downstream DAGs (conversion metrics) schedule on."""
        return ds

    wait_for_report >> download_raw >> validate >> apps >> load_day >> quality_checks >> publish()


ascstats_daily_sales()
