"""Path converters: ``appid``, an app's Apple ID or an idea's negative ID (``App.is_idea``),
and ``isodate``, the day a dated Overview ends on."""

from datetime import date

from django.urls import register_converter


class AppId:
    regex = "-?[0-9]+"

    def to_python(self, value: str) -> int:
        return int(value)

    def to_url(self, value: int) -> str:
        return str(value)


register_converter(AppId, "appid")


class Day:
    """``isodate``: a calendar day as 2026-11-04. A day that doesn't exist (2026-02-30) is a 404."""

    regex = "[0-9]{4}-[0-9]{2}-[0-9]{2}"

    def to_python(self, value: str) -> date:
        return date.fromisoformat(value)  # ValueError makes the URL not match

    def to_url(self, value: date) -> str:
        return value.isoformat()


register_converter(Day, "isodate")
