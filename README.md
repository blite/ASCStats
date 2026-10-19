# ASCStats

A small, production-shaped data pipeline that pulls App Store Connect reports for my own
iOS apps into Postgres and serves them through Django.

- **Ingest**: a minimal, read-only App Store Connect client (ES256 JWT, JSON:API paging,
  retries on GET only) fetches Sales & Trends daily summaries and Analytics Reports.
- **Land**: raw report files go unchanged into an S3-compatible bucket (MinIO locally).
- **Load**: validated rows are loaded into Postgres with an idempotent delete+insert per
  `(app, report, date)` in one transaction; every attempt is written to a run log.
- **Serve**: Django admin, a JSON API (Django Ninja, OpenAPI at `/api/docs`) and one
  server-rendered htmx page with a chart.
- **Run**: Docker Compose for development, Kustomize manifests for Kubernetes (a CronJob per
  report), and Airflow 3 DAGs that orchestrate the same container as Kubernetes pods.

> The repository contains **no real sales numbers**. `seed_synthetic` generates
> deterministic, realistic files in Apple's exact format, and every demo below uses it.

## Architecture

```mermaid
flowchart LR
    ASC[App Store Connect API] -- GET only --> CMD
    subgraph image[ascstats image]
        CMD["manage.py import_asc<br/>check → download → validate → load → quality"]
        WEB[Django: admin · Ninja API · htmx page]
    end
    CMD -- raw .tsv.gz, unchanged --> S3[(MinIO / S3 / GCS)]
    S3 -- replayable --> CMD
    CMD -- delete+insert per app/report/day --> PG[(Postgres)]
    PG --> WEB
    CRON[k8s CronJob] -. runs .-> CMD
    AF[Airflow 3 DAGs] -. KubernetesPodOperator .-> CMD
```

One container image does everything; only the command differs. The unit of work is a single
management command:

```bash
python manage.py import_asc --report sales --date 2026-10-18
python manage.py import_asc --report analytics --date yesterday --app 1000000002
python manage.py import_asc --report sales --date 2026-10-18 --step validate   # one step only
```

| Step       | What it does                                                                                     |
|------------|--------------------------------------------------------------------------------------------------|
| `check`    | Is the report published? (Exit code 3 when not, so schedulers can retry later.)                   |
| `download` | Fetch and write the raw file(s) to the bucket under `sales/date=…/` or `analytics/date=…/app=…/`. |
| `validate` | Parse from the bucket: required columns, types, dates, in-app purchases attributable to an app.  |
| `load`     | One transaction: delete the `(app, report, day)` rows, bulk insert the new ones.                  |
| `quality`  | Postgres must match the raw file exactly: row counts and unit/count totals per app.              |

Steps hand state to each other only through the bucket, so each one can be retried on its
own, and any day can be reloaded from raw without asking Apple again.

### Tables (all owned by Django models and migrations)

| Model              | Notes                                                                                     |
|--------------------|-------------------------------------------------------------------------------------------|
| `App`              | Apple ID, name, SKU, bundle ID, `is_active` (included in imports).                         |
| `SalesRow`         | One line of the SALES/SUMMARY/DAILY report. IAPs attributed via `Parent Identifier`.       |
| `AnalyticsRow`     | One line of an Analytics Reports segment; common columns typed, the rest in `dimensions`. |
| `ConversionMetric` | Derived daily funnel: impressions, page views, first-time downloads, units.               |
| `IngestionRun`     | The run log: report, day, app, step, source, status, row counts, error.                   |

## Quick start (Docker Compose)

```bash
docker compose up --build -d
docker compose exec web python manage.py seed_synthetic --days 90
docker compose exec web python manage.py createsuperuser
open http://localhost:8000          # chart
open http://localhost:8000/admin/   # every table, including the run log
open http://localhost:8000/api/docs # OpenAPI
curl localhost:8000/api/apps/1000000002/sales/daily
```

MinIO's console is on http://localhost:9001 (`ascstats` / `ascstats-dev-secret`), where the
raw files are visible under `ascstats-raw/`.

## Backfill and restatement

```bash
python manage.py import_asc --report sales --from 2026-01-01 --to 2026-03-31
python manage.py import_asc --report analytics --from today-7 --to yesterday
```

Backfill is the same command over a range: each day is independent, so a failed day is
reported and the rest continue (exit code 1 if any day failed, 3 if any were not yet
published). Because every load is a delete+insert of exactly one `(app, report, day)`,
running a day twice gives the same rows: restating the last week is just a re-run. In
Airflow, backfill is `airflow dags backfill` over data intervals (see below).

## Why no Celery (or Redis)

A daily import is a CronJob, not a queue. There is one job per report per day, it takes
seconds, and the only concurrency that matters is "never two imports of the same report at
once", which `concurrencyPolicy: Forbid` gives for free. A broker and a worker fleet would add
two stateful services to operate, a second retry mechanism next to Kubernetes' Job
`backoffLimit`, and a place for work to disappear (an acknowledged-but-lost message), all to
schedule something cron already schedules. When orchestration grows real dependencies
(wait for Apple, fan out over apps, run metrics when both reports land), that is a workflow
problem and Airflow handles it, still launching the same container as a pod.

## Kubernetes (Docker Desktop)

Enable Kubernetes in Docker Desktop (Settings → Kubernetes), then:

```bash
docker build -t ascstats:dev .
kubectl apply -k k8s/
kubectl -n ascstats rollout status deploy/web
kubectl -n ascstats exec deploy/web -- python manage.py seed_synthetic --days 30
open http://localhost:8000
kubectl -n ascstats create job --from=cronjob/import-sales import-sales-now   # run the CronJob once
```

`k8s/` contains: a Postgres StatefulSet, MinIO, the web Deployment (migrations run in an init
container), the `import-sales` and `import-analytics` CronJobs with `concurrencyPolicy: Forbid`,
and Secret templates in `k8s/secrets/`. For local convenience Kustomize generates dev-only
Secrets from `k8s/secrets/dev-*.env`; on a shared cluster delete that generator and create
the Secrets from the templates (or External Secrets).

### The App Store Connect key

The `.p8` key is a Kubernetes Secret mounted **as a file** at `/var/run/secrets/asc/AuthKey.p8`
(mode 0440, readable through `fsGroup` only) and read in exactly one place:
`src/ascstats/asc/credentials.py`. It is never an environment variable, so it does not show up
in `kubectl describe`, process listings or crash reports. See
`k8s/secrets/asc-api-key.example.yaml` for the `kubectl create secret` command. Everything
runs in synthetic mode until you set `ASC_SOURCE=asc` in the ConfigMap.

The client cannot write: it has no POST/PATCH/DELETE method at all, and a test asserts that
every recorded request is a GET. The one-time ONGOING Analytics report request is created by
hand in App Store Connect, outside the pipeline.

## Airflow (phase 2)

`airflow/` is a separate uv project and Compose stack (Airflow 3, LocalExecutor, its own
metadata Postgres). Airflow only orchestrates: every task is a `KubernetesPodOperator` running
the ascstats image's management command on the cluster above.

| DAG                           | Schedule                         | Shape                                                                                         |
|-------------------------------|----------------------------------|-----------------------------------------------------------------------------------------------|
| `ascstats_daily_sales`        | daily data intervals, catchup    | `wait_for_report` (deferrable) → `download_raw` → `validate` → `load_day`[apps] → `quality_checks`[apps] |
| `ascstats_daily_analytics`    | daily data intervals, catchup    | task group mapped over apps: `wait_for_report` → `download_raw` → `validate` → `load_day` → `quality_checks` |
| `ascstats_conversion_metrics` | assets: sales **and** analytics  | rebuild the last 7 days of `ConversionMetric`                                                 |
| `ascstats_weekly_restatement` | Mondays                          | re-import the previous 7 complete days of both reports, rebuild metrics                     |

- **Deferrable sensor**: `wait_for_report` hands off to the triggerer, which polls Apple with
  read-only GETs until the report exists, without holding a worker slot.
- **Pool**: every task that calls App Store Connect runs in the `asc_api` pool (2 slots),
  across all DAGs, including backfills.
- **Connection**: the key is the `asc_default` Airflow connection;
  `airflow/scripts/asc-conn-from-k8s.sh` builds it from the same Kubernetes Secret.
  `airflow/defaults.env` sets it to `{"synthetic": true}`, so nothing calls Apple by default.
- **Composer compatible**: no top-level Variable lookups, no custom Airflow config; the image,
  namespace and Kubernetes connection come from environment variables.

```bash
cd airflow
./scripts/kubeconfig.sh                                    # kubeconfig for the containers
docker compose up -d                                       # UI: http://localhost:8080
docker compose exec airflow-scheduler airflow dags unpause ascstats_daily_sales
docker compose exec airflow-scheduler \
  airflow backfill create --dag-id ascstats_daily_sales --from-date 2026-09-01 --to-date 2026-09-07
uv run pytest                                              # DAG-integrity tests
```

## Mapping to EKS / GKE

| Here (Docker Desktop)              | EKS                                                         | GKE                                                                    |
|------------------------------------|-------------------------------------------------------------|------------------------------------------------------------------------|
| Postgres StatefulSet               | RDS for PostgreSQL                                          | Cloud SQL for PostgreSQL (Cloud SQL Auth Proxy sidecar)               |
| MinIO                              | S3 bucket; pods get access via IRSA / EKS Pod Identity      | GCS bucket via its S3-interoperability endpoint, or Workload Identity |
| Kustomize-generated dev Secrets    | AWS Secrets Manager + External Secrets Operator             | Secret Manager + External Secrets / Secret Manager CSI driver         |
| `asc-api-key` Secret file mount    | same mount, sourced from Secrets Manager                    | same mount, sourced from Secret Manager                                |
| `web` `LoadBalancer` Service       | AWS Load Balancer Controller Ingress (ALB)                   | GKE Ingress / Gateway                                                  |
| local `ascstats:dev` image         | ECR                                                         | Artifact Registry                                                      |
| CronJobs                           | unchanged                                                   | unchanged                                                              |
| Airflow in Docker Compose          | Amazon MWAA (KubernetesPodOperator into EKS)                | Cloud Composer (DAGs as-is; pods into the Composer or another cluster) |

Only configuration changes: `DATABASE_URL`, `RAW_STORAGE_URL`/`S3_ENDPOINT_URL`, the image
name in `kustomization.yaml`, and an overlay that drops `minio.yaml`/`postgres.yaml`.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run pytest                 # SQLite by default; set DATABASE_URL for Postgres
(cd airflow && uv sync && uv run pytest)
```

Tests never touch the network: the ASC client is exercised against recorded JSON:API
responses and report files in `tests/fixtures/asc/` through an `httpx.MockTransport`, covering
JWT claims, pagination, retries with `Retry-After`, 404-as-not-ready and GET-only behaviour.

```
src/ascstats/
  asc/client.py         read-only App Store Connect client
  asc/credentials.py    the only place the .p8 key is read
  stats/pipeline.py     check / download / validate / load / quality
  stats/parsing.py      report parsing and validation (pure functions)
  stats/synthetic.py    synthetic reports in Apple's file format
  stats/storage.py      raw landing zone (S3 API or local files)
  stats/management/commands/  import_asc, seed_synthetic, build_conversion_metrics, list_apps
  api.py                Django Ninja API
k8s/                    Kustomize manifests
airflow/                Airflow 3 DAGs, Compose stack, DAG-integrity tests
```
