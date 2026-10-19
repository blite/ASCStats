"""Weekly 7-day restatement.

Apple can restate recent days (refunds, late transactions). Every Monday, re-import the
previous 7 complete days. Because each load is a delete+insert per (app, report, day),
re-running a day is always safe and simply replaces it.
"""

import pendulum
from airflow.sdk import CronTriggerTimetable, dag

from ascstats_lib.pods import DEFAULT_ARGS, manage_py

FROM = "{{ macros.ds_add(ds, -8) }}"
TO = "{{ macros.ds_add(ds, -2) }}"


@dag(
    dag_id="ascstats_weekly_restatement",
    schedule=CronTriggerTimetable("0 18 * * 1", timezone="UTC"),
    start_date=pendulum.datetime(2026, 9, 1, tz="UTC"),
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["ascstats", "restatement"],
    doc_md=__doc__,
)
def ascstats_weekly_restatement():
    imports = [
        manage_py(
            f"restate_{report}",
            ["import_asc", "--report", report, "--from", FROM, "--to", TO],
            talks_to_asc=True,
        )
        for report in ("sales", "analytics")
    ]
    rebuild = manage_py(
        "build_conversion_metrics", ["build_conversion_metrics", "--from", FROM, "--to", TO]
    )
    imports >> rebuild


ascstats_weekly_restatement()
