"""Django settings. Everything environment-specific comes from environment variables."""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def database_from_url(url: str) -> dict:
    """postgres://user:pass@host:5432/name -> Django DATABASES entry; sqlite:///path for tests."""
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": parsed.path[1:] or ":memory:"}
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"unsupported DATABASE_URL scheme {parsed.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
    }


DEBUG = env_bool("DJANGO_DEBUG")
# Compose and k8s always set this; the fallback only exists so tests and one-off commands run.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-only-key")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ascstats.stats",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ascstats.urls"
WSGI_APPLICATION = "ascstats.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": database_from_url(os.environ.get("DATABASE_URL", "sqlite:///:memory:"))}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", "/tmp/ascstats-static"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# --- Pipeline settings -------------------------------------------------------------------

# Where raw report files land, unchanged: s3://bucket[/prefix] (MinIO/S3/GCS-interop) or file:///dir.
RAW_STORAGE_URL = os.environ.get("RAW_STORAGE_URL", "file:///tmp/ascstats-raw")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None  # MinIO; unset for real S3
# Report source: "asc" talks to App Store Connect, "synthetic" generates fake but realistic files.
ASC_SOURCE = os.environ.get("ASC_SOURCE", "synthetic")
ASC_VENDOR_NUMBER = os.environ.get("ASC_VENDOR_NUMBER", "")
