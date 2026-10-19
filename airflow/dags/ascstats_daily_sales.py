"""Daily Sales & Trends import.

One data interval = one report day. The vendor-wide file is downloaded and validated once,
then loaded and checked per app (dynamic task mapping), each load an idempotent
delete+insert of that (app, day). Backfill with catchup or ``airflow dags backfill``.
"""

import pendulum
from airflow.sdk import CronDataIntervalTimetable, dag, task

from ascstats_lib.pods import ASC_CONN_ID, DEFAULT_ARGS, SALES_ASSET, manage_py, partial_manage_py
from ascstats_lib.sensors import ReportAvailableSensor

DAY = "{{ ds }}"


@dag(
    dag_id="ascstats_daily_sales",
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=pendulum.datetime(2026, 9, 1, tz="UTC"),
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

    def step_args(step):
        return lambda apple_id: [
            "import_asc", "--report", "sales", "--date", DAY, "--step", step,
            "--app", str(apple_id),
        ]  # fmt: skip

    load_day = partial_manage_py("load_day").expand(arguments=apps.output.map(step_args("load")))
    quality_checks = partial_manage_py("quality_checks").expand(
        arguments=apps.output.map(step_args("quality"))
    )

    @task(outlets=[SALES_ASSET])
    def publish(ds=None):
        """Emit the asset event downstream DAGs (conversion metrics) schedule on."""
        return ds

    wait_for_report >> download_raw >> validate >> apps >> load_day >> quality_checks >> publish()


ascstats_daily_sales()
