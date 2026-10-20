"""Daily ratings and reviews.

- ``snapshot_ratings``: today's public App Store rating totals (iTunes Lookup, no App Store
  Connect key). Apple publishes no rating history, so a missed day is a gap forever: no
  catchup, and retries matter more than anywhere else.
- ``build_versions`` then ``import_reviews``, and ``check_report_requests``: App Store
  Connect (GET only, ``asc_api`` pool).
  Versions first, so each written review can be filed under the version Apple says it is
  about.
"""

from datetime import timedelta

from airflow.sdk import dag

from ascstats_lib.pods import DEFAULT_ARGS, START_DATE, manage_py


@dag(
    dag_id="ascstats_daily_ratings",
    schedule="0 17 * * *",
    start_date=START_DATE,
    catchup=False,  # the public lookup only knows "now"; there is nothing to catch up on
    default_args=DEFAULT_ARGS | {"retries": 4, "retry_delay": timedelta(minutes=20)},
    tags=["ascstats", "ratings"],
    doc_md=__doc__,
)
def ascstats_daily_ratings():
    manage_py("snapshot_ratings", ["snapshot_ratings"])
    versions = manage_py("build_versions", ["build_versions"], talks_to_asc=True)
    versions >> manage_py("import_reviews", ["import_reviews"], talks_to_asc=True)
    # Read-only: fails, naming the apps, if an Analytics report request stopped (Apple stops
    # idle ones). Re-creating it needs an Admin key, which the cluster deliberately lacks.
    manage_py(
        "check_report_requests", ["ensure_report_requests", "--check-only"], talks_to_asc=True
    )


ascstats_daily_ratings()
