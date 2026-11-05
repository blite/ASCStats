"""The ``appid`` path converter: an app's Apple ID, or an idea's negative ID (``App.is_idea``)."""

from django.urls import register_converter


class AppId:
    regex = "-?[0-9]+"

    def to_python(self, value: str) -> int:
        return int(value)

    def to_url(self, value: int) -> str:
        return str(value)


register_converter(AppId, "appid")
