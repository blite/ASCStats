"""ASCStats as an MCP server: the Streamable HTTP transport, answered as plain JSON.

One endpoint, ``/mcp/``, takes JSON-RPC over POST: ``initialize``, ``ping``, ``tools/list``
and ``tools/call``. Every tool only reads (``tools.TOOLS``), so the server keeps no session
and never streams: each POST is answered with one JSON body, which the transport allows. A
GET (the optional server-to-client stream) is refused with 405, as the spec says a server
without one does.

It sits behind a bearer token, ``MCP_TOKEN`` (the ``ascstats-mcp`` Secret), not the site's
sign-in: an agent has no browser session. With no token set the endpoint is off.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import tools

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
SERVER_INFO = {"name": "ascstats", "title": "ASCStats", "version": "1"}
INSTRUCTIONS = (
    "ASCStats tracks the user's own App Store apps, the competitors each follows, the "
    "App Store searches they rank in, and which of their in-app purchases sell. Call "
    "list_apps first to find an app; every other tool takes the app's Apple ID or a part of "
    "its name. Everything is read-only."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = (
    -32700,
    -32600,
    -32601,
    -32602,
    -32603,
)


def _default(value):
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON: {type(value).__name__}")


def _plain(data):
    """``data`` with dates as ISO strings: what structuredContent and the text both carry."""
    return json.loads(json.dumps(data, default=_default))


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _initialize(params: dict) -> dict:
    asked = params.get("protocolVersion")
    return {
        "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": INSTRUCTIONS,
    }


def _call(params: dict) -> dict:
    tool = tools.BY_NAME.get(params.get("name"))
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {params.get('name')!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    missing = [a for a in tool.required if arguments.get(a) in (None, "")]
    unknown = [a for a in arguments if a not in tool.properties]
    if missing or unknown:
        problems = [f"missing {', '.join(missing)}"] if missing else []
        problems += [f"unknown {', '.join(unknown)}"] if unknown else []
        return _tool_error(f"{tool.name}: {'; '.join(problems)}.")
    try:
        data = _plain(tool.run(**arguments))
    except tools.ToolError as error:
        return _tool_error(str(error))
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data,
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


METHODS = {
    "initialize": _initialize,
    "ping": lambda params: {},
    "tools/list": lambda params: {"tools": [t.schema() for t in tools.TOOLS]},
    "tools/call": _call,
}


def _answer(message) -> dict | None:
    """The response to one JSON-RPC message; None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message")
    if "method" not in message:
        return None  # a response to something we asked: we ask nothing
    if "id" not in message:
        return None  # a notification (notifications/initialized and the like)
    rid, method = message["id"], message["method"]
    handler = METHODS.get(method)
    if handler is None:
        return _error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(rid, INVALID_PARAMS, "params must be an object")
    try:
        return {"jsonrpc": "2.0", "id": rid, "result": handler(params)}
    except RpcError as error:
        return _error(rid, error.code, error.message)
    except Exception:
        log.exception("mcp: %s failed", method)
        return _error(rid, INTERNAL_ERROR, f"{method} failed; the server log has why")


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _authorized(request: HttpRequest) -> bool:
    token = settings.MCP_TOKEN
    given = request.headers.get("Authorization", "")
    return bool(token) and hmac.compare_digest(given.encode(), f"Bearer {token}".encode())


@csrf_exempt
@login_not_required
def endpoint(request: HttpRequest) -> HttpResponse:
    if not settings.MCP_TOKEN:
        return JsonResponse({"detail": "The MCP server is off: MCP_TOKEN is not set"}, status=503)
    if not _authorized(request):
        return JsonResponse(
            {"detail": "A bearer token is required"},
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="ascstats-mcp"'},
        )
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    try:
        body = json.loads(request.body or b"null")
    except ValueError:
        return JsonResponse(_error(None, PARSE_ERROR, "Body is not JSON"), status=400)
    if isinstance(body, list):  # a batch (protocol 2025-03-26)
        answers = [a for a in map(_answer, body) if a is not None]
        if not body:
            return JsonResponse(_error(None, INVALID_REQUEST, "Empty batch"), status=400)
        return JsonResponse(answers, safe=False) if answers else HttpResponse(status=202)
    answer = _answer(body)
    if answer is None:
        return HttpResponse(status=202)
    return JsonResponse(answer)
