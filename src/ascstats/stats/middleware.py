"""Every page, fragment and API call needs a signed-in user."""

from django.conf import settings
from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.http import HttpResponse, JsonResponse
from django.shortcuts import resolve_url

# Probes run without a session: the kubelet's readiness check and compose's healthcheck
OPEN_PATHS = {"/api/health"}


class LoginRequired(LoginRequiredMiddleware):
    """Django's LoginRequiredMiddleware, with the answer shaped for who is asking: the API
    gets a 401, an htmx fragment request reloads the whole page onto the login form (rather
    than swapping it into a chart), and everything else is redirected there.

    The static demo is rendered signed out (export_site sets DEMO_SITE), so it is let through.
    """

    def process_view(self, request, view_func, view_args, view_kwargs):
        if settings.DEMO_SITE or request.path_info in OPEN_PATHS:
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)

    def handle_no_permission(self, request, view_func):
        if request.path_info.startswith("/api/") and request.path_info != "/api/docs":
            return JsonResponse({"detail": "Not signed in"}, status=401)
        if request.headers.get("HX-Request"):
            return HttpResponse(headers={"HX-Redirect": resolve_url(settings.LOGIN_URL)})
        return super().handle_no_permission(request, view_func)
