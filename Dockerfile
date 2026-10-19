# syntax=docker/dockerfile:1.7
# Slim, multi-arch (arm64 + amd64) image: one image runs the web app, the CronJob and the
# Airflow KubernetesPodOperator tasks; only the command differs.

FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.14-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first so code edits do not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock manage.py ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable \
 && DJANGO_STATIC_ROOT=/app/staticfiles /app/.venv/bin/python manage.py collectstatic --noinput -v0

FROM python:3.14-slim-trixie
RUN useradd --uid 10001 --create-home app
COPY --from=build /app /app
COPY manage.py /app/manage.py
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DJANGO_STATIC_ROOT=/app/staticfiles \
    DJANGO_SETTINGS_MODULE=ascstats.settings
WORKDIR /app
USER 10001
EXPOSE 8000
CMD ["gunicorn", "ascstats.wsgi", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
