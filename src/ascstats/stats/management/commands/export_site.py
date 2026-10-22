"""Render the synthetic demo as a static site for GitHub Pages: every page, period and htmx
fragment written to <path>/index.html, under a base path (/ASCStats/ for a project site).

It refuses to run on anything but a fresh synthetic database, so real sales can never end
up in the published files, then seeds the demo, renders it through Django's test client and
checks that every link it wrote points at a file it wrote."""

import re
import shutil
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import Client, override_settings
from django.urls import get_script_prefix, set_script_prefix

from ascstats.stats import fx
from ascstats.stats.charts import DEFAULT_PERIOD, PERIODS
from ascstats.stats.models import App
from ascstats.stats.views import WINDOWS

DEMO_END = date(2026, 6, 29)  # the iPhone went on sale on June 29 (2007); the demo ends there
LINK = re.compile(r'(?:href|hx-get)="([^"]*)"')


class Command(BaseCommand):
    help = "Seed the synthetic demo into an empty database and export it as a static site."

    def add_arguments(self, parser):
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--base-path", default="/", help="URL prefix, e.g. /ASCStats/")
        parser.add_argument("--end", type=date.fromisoformat, default=DEMO_END)
        parser.add_argument("--days", type=int, default=400, help="a year plus trend history")

    def handle(self, *, out, base_path, end, days, **_):
        if settings.ASC_SOURCE != "synthetic":
            raise CommandError("export_site only publishes synthetic data (ASC_SOURCE=synthetic)")
        call_command("migrate", verbosity=0)
        if App.objects.exists():
            raise CommandError("export_site needs an empty database; it seeds its own demo data")
        call_command("seed_synthetic", days=days, end=end, stdout=self.stdout)
        fx.load(end - timedelta(days=days + 7), end)  # real ECB rates for those dates

        prefix = "/" + base_path.strip("/")
        prefix = "" if prefix == "/" else prefix
        previous = get_script_prefix()
        set_script_prefix(prefix + "/")  # the test client, unlike a WSGI server, never sets it
        try:
            with override_settings(
                FORCE_SCRIPT_NAME=prefix, DEBUG=False, DEMO_SITE=True, ALLOWED_HOSTS=["testserver"]
            ):
                files = self._render(out, Client())
        finally:
            set_script_prefix(previous)
        self._check_links(out, prefix, files)
        self.stdout.write(f"exported {len(files)} pages to {out} under {prefix or '/'}")

    def _render(self, out: Path, client: Client) -> set[str]:
        """Write every page and fragment; returns their paths (relative, '' for the root)."""
        paths = ["funnel/"]
        for page in ("", "revenue/"):
            paths += [page] + [f"{page}{n}d/" for n in PERIODS if n != DEFAULT_PERIOD]
        for app in App.objects.active():
            paths += [f"chart/{app.apple_id}/{w}/" for w in WINDOWS]
            paths.append(f"funnel/app/{app.apple_id}/")
        if out.exists():
            shutil.rmtree(out)
        for path in paths:
            response = client.get("/" + path)
            if response.status_code != 200:
                raise CommandError(f"/{path} returned {response.status_code}")
            target = out / path / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
        missing = client.get("/no-such-page/")
        if missing.status_code != 404:
            raise CommandError(f"/no-such-page/ returned {missing.status_code}, not the 404 page")
        (out / "404.html").write_bytes(missing.content)
        (out / ".nojekyll").touch()  # serve the files as they are
        return set(paths)

    def _check_links(self, out: Path, prefix: str, files: set[str]) -> None:
        """Every internal link and fragment URL must be a file this export wrote, with no
        query string (static hosting ignores them). Placeholder URLs ({app}) are filled in
        by the browser from a form; their concrete paths are among the files."""
        broken = set()
        for page in [*out.rglob("index.html"), out / "404.html"]:
            for url in LINK.findall(page.read_text()):
                if url.startswith(("http://", "https://", "#")) or "{" in url:
                    continue
                if (
                    "?" in url
                    or not url.startswith(prefix + "/")
                    or url.removeprefix(prefix + "/") not in files
                ):
                    broken.add(f"{url} (in {page.relative_to(out)})")
        if broken:
            raise CommandError(
                "links to pages the export did not write:\n" + "\n".join(sorted(broken))
            )
