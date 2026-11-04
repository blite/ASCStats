from django import template
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ascstats.stats.charts import ALL, DEFAULT_PERIOD
from ascstats.stats.revenue import money as _money
from ascstats.stats.territories import store_page

register = template.Library()

PERIOD_PAGES = {"overview": "dashboard", "revenue": "revenue"}


@register.simple_tag
def reviews_url(state="", app=None):
    """The review queue in one reply state, for every app (/reviews/, /reviews/replied/) or
    on one app's Reviews tab (/app/<id>/reviews/all/)."""
    if app is None:
        return reverse("reviews_state", args=[state]) if state else reverse("reviews")
    if state:
        return reverse("app_reviews_state", args=[app.apple_id, state])
    return reverse("app_reviews", args=[app.apple_id])


# App Store Connect's name for each platform in its page addresses
ASC_PLATFORM_PATHS = {"IOS": "ios", "MAC_OS": "macos", "VISION_OS": "visionos"}


@register.filter
def asc_reviews_href(review):
    """The Ratings and Reviews page in App Store Connect for the app and platform the review
    was written for, where replies are written. Apple has no link to a single review."""
    platform = ASC_PLATFORM_PATHS.get(review.platform, "ios")
    return (
        f"https://appstoreconnect.apple.com/apps/{review.app.apple_id}"
        f"/distribution/activity/{platform}/ratingsResponses"
    )


@register.filter
def store_href(review):
    """The app's public App Store page in the storefront the review was written in."""
    return store_page(review.app.apple_id, review.territory)


@register.simple_tag
def period_url(page, days):
    """A page for a period: /7d/, /revenue/1d/, /all/. The default period is the page's own
    URL, so each view has one address."""
    name = PERIOD_PAGES[page]
    if not days or days == DEFAULT_PERIOD:
        return reverse(name)
    if days == ALL:
        return reverse(f"{name}_all")
    return reverse(f"{name}_period", args=[days])


@register.simple_tag
def url_pattern(name, *fields):
    """A URL with {field} placeholders for a form to fill in from its fields, reversed like
    any other URL (so it carries the script prefix): url_pattern 'chart' 'app' 'days' gives
    /chart/{app}/{days}/."""
    sentinels = [str(10**9 + i) for i in range(len(fields))]
    url = reverse(name, args=sentinels)
    for sentinel, field in zip(sentinels, fields, strict=True):
        url = url.replace(sentinel, "{" + field + "}")
    return url


@register.filter
def money(value):
    return "\u2013" if value is None else _money(float(value))


@register.filter
def signed_pct(value):
    """+12% / -8%, or an en dash for None."""
    if value is None:
        return "\u2013"
    return "0%" if round(value) == 0 else f"{value:+.0f}%"


# Lucide icons (the set shadcn/ui uses), inner SVG only; all share one 24x24 stroked frame
ICONS = {
    "overview": '<rect width="7" height="9" x="3" y="3" rx="1"/>'
    '<rect width="7" height="5" x="14" y="3" rx="1"/>'
    '<rect width="7" height="9" x="14" y="12" rx="1"/>'
    '<rect width="7" height="5" x="3" y="16" rx="1"/>',
    "reviews": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "revenue": '<circle cx="12" cy="12" r="10"/>'
    '<path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 18V6"/>',
    "app": '<rect width="14" height="20" x="5" y="2" rx="2"/><path d="M12 18h.01"/>',
    "api": '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/>'
    '<path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>',
    "admin": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/>'
    '<path d="M3 12a9 3 0 0 0 18 0"/>',
    "jobs": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/>'
    '<path d="M13 12h8"/><path d="M13 18h8"/>',
    "source": '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    "panel": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/>'
    '<path d="M21 12H9"/>',
    "light": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-17.07 1.41 1.41m11.32 '
    '11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "dark": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "system": '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8m-4-4v4"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4m0 4h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/>',
    "stats": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
    "ratings": '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 '
    "1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 "
    "5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 "
    "1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 "
    '.294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>',
    "keywords": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "competitors": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "arrange": '<path d="m21 16-4 4-4-4"/><path d="M17 20V4"/><path d="m3 8 4-4 4 4"/>'
    '<path d="M7 4v16"/>',
    "grip": '<circle cx="9" cy="5" r="1"/><circle cx="9" cy="12" r="1"/>'
    '<circle cx="9" cy="19" r="1"/><circle cx="15" cy="5" r="1"/>'
    '<circle cx="15" cy="12" r="1"/><circle cx="15" cy="19" r="1"/>',
    "up": '<path d="m18 15-6-6-6 6"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
    "hide": '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 '
    '10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/>'
    '<path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 '
    '4.446-5.143"/><path d="m2 2 20 20"/>',
    "show": '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 '
    '.696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
}


@register.simple_tag
def icon(name):
    return format_html(
        '<svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>',
        mark_safe(ICONS[name]),  # constant markup from the table above, never from input
    )
