from django import template
from django.urls import reverse

from ascstats.stats.charts import ALL, DEFAULT_PERIOD
from ascstats.stats.revenue import money as _money
from ascstats.stats.territories import store_page

register = template.Library()

PERIOD_PAGES = {"overview": "dashboard", "revenue": "revenue"}


@register.simple_tag
def reviews_url(state="", app=None):
    """The review queue in one reply state, for every app or one: /reviews/,
    /reviews/replied/, /reviews/all/app/<id>/."""
    if app is None:
        return reverse("reviews_state", args=[state]) if state else reverse("reviews")
    if state:
        return reverse("reviews_state_app", args=[state, app.apple_id])
    return reverse("reviews_app", args=[app.apple_id])


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
