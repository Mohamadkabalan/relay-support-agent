"""Pure ASGI middleware (it must not buffer SSE responses):

- `RequestContext`: a request ID (kept from `X-Request-ID` if it looks safe, else new) for logs
  and the response. FastAPI's own telemetry makes the server span (see `relay.api.app`).
- `OriginCheck`: a browser's state-changing request must come from an allowed origin (§11).
  Requests without an `Origin` header (curl, server-to-server) pass; SameSite=Lax cookies already
  keep other sites' forms from carrying a session.
- `BodyLimit`: request bodies over the limit get 413 before any route sees them.
"""

import json
import re
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from relay.telemetry.logging import request_id

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


async def send_json(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"%d" % len(body)),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


class RequestContext:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = header(scope, b"x-request-id")
        rid = given if given and SAFE_REQUEST_ID.fullmatch(given) else secrets.token_hex(8)
        token = request_id.set(rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id.reset(token)


class OriginCheck:
    def __init__(self, app: ASGIApp, allowed: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = frozenset(origin.rstrip("/") for origin in allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = header(scope, b"origin")
            if origin is not None and origin.rstrip("/") not in self.allowed:
                await send_json(send, 403, "This origin may not make that request.")
                return
        await self.app(scope, receive, send)


class BodyLimit:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        declared = header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await send_json(send, 413, f"Request bodies are limited to {self.max_bytes} bytes.")
            return
        # Read the (small) body here, so a chunked upload can't stream past the limit.
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await send_json(send, 413, f"Request bodies are limited to {self.max_bytes} bytes.")
                return
            if not message.get("more_body", False):
                break
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
