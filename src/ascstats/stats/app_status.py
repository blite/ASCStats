"""Should an app be imported at all? Asked of App Store Connect when the app is first set up
(and by sync_app_status), not on every run: deleted apps keep showing up in the sales report
for redownloads, but nothing else about them can be read."""

from __future__ import annotations

import logging

from ascstats.asc.client import ASCClient, ASCError, ReportNotReady

from .models import App

log = logging.getLogger(__name__)


def inactive_reason(client: ASCClient, apple_id: int) -> str | None:
    """Why the app should not be imported, or None when App Store Connect has a version of it
    READY_FOR_SALE. A deleted app has no versions at all: App Store Connect answers 404."""
    try:
        versions = client.app_store_versions(apple_id)
    except ReportNotReady:
        return "not in App Store Connect"
    except ASCError as exc:
        if exc.status != 403:
            raise
        return "not visible to this API key"
    states = sorted({v["attributes"].get("appStoreState") or "" for v in versions})
    if "READY_FOR_SALE" not in states:
        return f"not ready for sale ({', '.join(states) or 'no versions'})"
    return None


def apply_asc_status(client: ASCClient, app: App) -> str | None:
    """Deactivate the app unless it is on sale; returns the reason. Never
    reactivates: an app switched off by hand stays off."""
    reason = inactive_reason(client, app.apple_id)
    if reason and app.is_active:
        app.is_active = False
        app.save(update_fields=["is_active"])
        log.warning("app %s is %s; excluded from imports", app, reason)
    return reason
