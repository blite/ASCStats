"""Rebuild the derived conversion table and the version dates once both daily imports landed.

Asset-scheduled: runs after a sales AND an analytics asset event. The rebuild covers the last
7 days, so late or restated days are picked up without tracking which day triggered it.
Version dating reads App Store Connect's version list when ASC_SOURCE=asc, so it runs in the
asc_api pool.
"""

from airflow.sdk import dag

from ascstats_lib.pods import (
    ANALYTICS_ASSET,
    CONVERSION_ASSET,
    DEFAULT_ARGS,
    SALES_ASSET,
    START_DATE,
    manage_py,
)


@dag(
    dag_id="ascstats_conversion_metrics",
    schedule=(SALES_ASSET & ANALYTICS_ASSET),
    start_date=START_DATE,
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["ascstats", "metrics"],
    doc_md=__doc__,
)
def ascstats_conversion_metrics():
    conversion = manage_py(
        "build_conversion_metrics",
        ["build_conversion_metrics", "--days", "7"],
        outlets=[CONVERSION_ASSET],
    )
    conversion >> manage_py("build_versions", ["build_versions"], talks_to_asc=True)


ascstats_conversion_metrics()
