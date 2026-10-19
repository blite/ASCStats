"""Daily Analytics Reports import.

Analytics reports are per app, so the whole chain is a task group mapped over apps:
each app waits for its own report instance, downloads its segments, validates, loads and
checks independently. The ONGOING report request itself is created once, by hand, outside
the pipeline: the pipeline never writes to App Store Connect.
"""

from airflow.sdk import CronDataIntervalTimetable, dag, task, task_group

from ascstats_lib.pods import ANALYTICS_ASSET, ASC_CONN_ID, DEFAULT_ARGS, START_DATE, manage_py
from ascstats_lib.sensors import ReportAvailableSensor

DAY = "{{ ds }}"


@dag(
    dag_id="ascstats_daily_analytics",
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=START_DATE,
    catchup=True,
    max_active_runs=3,
    default_args=DEFAULT_ARGS,
    tags=["ascstats", "analytics"],
    doc_md=__doc__,
)
def ascstats_daily_analytics():
    apps = manage_py("list_apps", ["list_apps", "--xcom"], do_xcom_push=True)

    @task_group
    def per_app(apple_id):
        def step(name, **kwargs):
            return manage_py(
                name,
                ["import_asc", "--report", "analytics", "--date", DAY, "--app", apple_id,
                 "--step", kwargs.pop("step")],
                **kwargs,
            )  # fmt: skip

        wait_for_report = ReportAvailableSensor(
            task_id="wait_for_report", report="analytics", apple_ids=apple_id, conn_id=ASC_CONN_ID
        )
        (
            wait_for_report
            >> step("download_raw", step="download", talks_to_asc=True)
            >> step("validate", step="validate")
            >> step("load_day", step="load")
            >> step("quality_checks", step="quality")
        )

    @task(outlets=[ANALYTICS_ASSET])
    def publish(ds=None):
        return ds

    per_app.expand(apple_id=apps.output) >> publish()


ascstats_daily_analytics()
